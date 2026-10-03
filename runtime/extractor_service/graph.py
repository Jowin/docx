"""The extraction pipeline as a LangGraph state graph.

    START -> resolve_config -> ingest --(one Send per item)--> parse_item (parallel, sandboxed)
                                     \\-(no items)-----------\\
          -> assemble -> (readable?) extract -> verify -> route -> END
                                    \\------------^

* ``ingest`` opens the input, applies the ingestion filter, unpacks emails
  (recursively), zips and encrypted files, and spools item bytes (RT-34).
* ``parse_item`` runs once per item, concurrently (bounded by the config's
  ``concurrency.max_parallel``, RT-41), each in a sandboxed child process
  under a time budget (RT-39, RT-65). A failed item degrades the run (RT-10).
* ``assemble`` is the single join (RT-09): it numbers the documents and picks
  the skills whose fingerprint matches them (scope.py).
* ``extract`` asks the stub or the model; a model failure, the run ceiling
  (RT-60) or the cost ceiling (RT-62) become flags, never an exception.
* ``verify`` grounds every value; ``route`` turns reasons into flags.

Nodes record their duration and the path the run took (``metadata.graph``).
With a checkpointer, every node boundary is a checkpoint, so a run interrupted
by a lost worker resumes where it stopped (RT-31, RT-40).
"""
from __future__ import annotations

import hashlib
import operator
import time
from pathlib import Path
from typing import Annotated, Any, Callable, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from . import gateway as gateway_mod
from . import sandbox, scope
from .config_store import ConfigStore, ExtractionConfig
from .errors import ModelError, ServiceError
from .evidence import Doc, number_docs, render
from .gateway import Gateway
from .intake import IntakeContext, Item, Submission, open_submission
from .llm import DEFAULT_MODEL_ALIAS, LLMExtractor, get_extractor, user_prompt
from .spool import Spool
from .verify import dedupe, finalize

SUPPORTED_INPUTS = ("email", "msg", "zip", "csv", "excel", "pdf", "docx", "image")


def _merge(a: dict, b: dict) -> dict:
    return {**a, **b}


class RunState(TypedDict, total=False):
    # request
    file_location: str
    client: str | None
    usecase: str | None
    version: str | None
    spool_dir: str | None
    started: float
    # produced by nodes
    cfg: ExtractionConfig
    sub: Submission
    provider: str
    model_name: str
    audit_id: str
    parsed: Annotated[list[Doc], operator.add]
    docs: list[Doc]
    skills_applied: list[str]
    reasons: list[str]
    raw: dict[str, Any]
    model_call: dict[str, Any]
    model_error: dict[str, Any]
    cost: dict[str, Any]
    records: list[dict[str, Any]]        # [{"fields", "reasons", "confidence", "flagged"}]
    flagged: bool
    overall: float
    # bookkeeping, merged across nodes
    trace: Annotated[list[str], operator.add]
    timings_ms: Annotated[dict[str, float], _merge]


class ParseTask(TypedDict):
    item: Item
    evidence: dict[str, Any]
    spool_dir: str | None
    timeout_s: float


def _timed(name: str, fn: Callable[[Any], dict[str, Any]]) -> Callable[[Any], dict[str, Any]]:
    def node(state: Any) -> dict[str, Any]:
        t0 = time.perf_counter()
        update = fn(state)
        label = name if name != "parse_item" else f"parse_item:{state['item'].name}"
        update["trace"] = [label]
        update["timings_ms"] = {label: round((time.perf_counter() - t0) * 1000, 1)}
        return update
    node.__name__ = name
    return node


def _spool(spool_dir: str | None, settings: Any) -> Spool | None:
    return Spool(Path(spool_dir), getattr(settings, "state_key", None)) if spool_dir else None


def build_graph(store: ConfigStore, settings: Any, gateway: Gateway | None = None, checkpointer: Any = None):
    """Compile the pipeline. ``settings`` is pipeline.Settings (passed in to avoid a cycle)."""
    from .pipeline import resolve_location

    def resolve_config(state: RunState) -> dict[str, Any]:
        cfg = store.resolve(state.get("client"), state.get("usecase"), state.get("version"))
        provider = settings.model_provider or cfg.model.get("provider", "stub")
        name = "deterministic-stub" if provider == "stub" else (cfg.model.get("name") or DEFAULT_MODEL_ALIAS)
        return {"cfg": cfg, "provider": provider, "model_name": name}

    def ingest(state: RunState) -> dict[str, Any]:
        cfg = state["cfg"]
        path = resolve_location(settings, state["file_location"])
        ctx = IntakeContext(limits=cfg.intake, filter=cfg.ingestion_filter, client=cfg.client,
                            usecase=cfg.usecase, spool=_spool(state.get("spool_dir"), settings))
        sub = open_submission(state["file_location"], path.name, path.read_bytes(), ctx)
        unreadable = sub.kind not in SUPPORTED_INPUTS or (sub.kind == "image" and not sub.items)
        if sub.ignored is None and unreadable and "encrypted_no_key" not in sub.reasons:
            raise ServiceError(422, f"unsupported_input:{sub.kind}", f"cannot extract from a {sub.kind} file",
                               {"supported": ["eml", "msg", "zip", "csv", "xlsx", "xlsm", "xls", "pdf", "docx"]})
        audit_id = "run_" + hashlib.sha256(
            f"{sub.data_sha256}|{cfg.sha256}|{state['provider']}|{state['model_name']}".encode()).hexdigest()[:20]
        reasons = list(sub.reasons)
        if sub.ignored:
            reasons.append(f"input_ignored:{sub.ignored}")
        return {"sub": sub, "audit_id": audit_id, "reasons": reasons}

    def dispatch(state: RunState) -> Any:
        items = state["sub"].items
        if not items:
            return "assemble"
        ev = state["cfg"].evidence
        return [Send("parse_item", ParseTask(item=i, evidence=ev, spool_dir=state.get("spool_dir"),
                                             timeout_s=float(ev.get("parse_timeout_s", 120)))) for i in items]

    def parse_item(task: ParseTask) -> dict[str, Any]:
        doc = sandbox.parse(task["item"], task["evidence"], _spool(task.get("spool_dir"), settings),
                            mode=settings.parse_sandbox, timeout_s=task["timeout_s"],
                            memory_mb=settings.sandbox_memory_mb)
        return {"parsed": [doc]}

    def assemble(state: RunState) -> dict[str, Any]:
        docs = number_docs(list(state.get("parsed") or []))
        reasons = list(state["reasons"]) + [d.reason for d in docs if d.status == "failed" and d.reason]
        _, skills = state["cfg"].for_documents(scope.facts(state["sub"], [d for d in docs if d.status == "read"]))
        return {"docs": docs, "reasons": reasons, "skills_applied": [s.name for s in skills]}

    def has_readable(state: RunState) -> str:
        return "extract" if any(d.status == "read" for d in state["docs"]) else "verify"

    def extract(state: RunState) -> dict[str, Any]:
        cfg = state["cfg"]
        readable = [d for d in state["docs"] if d.status == "read"]
        chosen = [s for s in cfg.skills if s.name in set(state.get("skills_applied") or [])]
        dictionary, _ = cfg.for_documents(scope.facts(state["sub"], readable))
        extractor = get_extractor(cfg, settings.model_provider, gateway)
        system = cfg.system_prompt + "".join("\n\n" + s.body for s in chosen)
        evidence = render(readable, int(cfg.evidence["max_chars_per_document"]))
        trace = {"audit_id": state["audit_id"], "client": cfg.client, "usecase": cfg.usecase,
                 "config_version": cfg.version}
        empty = {"raw": {"records": [], "notes": []}}
        elapsed = time.time() - float(state.get("started") or time.time())
        if elapsed > float(cfg.limits["run_ceiling_s"]):
            return {**empty, "model_error": {"flag": "agent_timeout:run", "message": f"run ceiling reached "
                                             f"after {round(elapsed)}s"}}
        cost: dict[str, Any] = {}
        if isinstance(extractor, LLMExtractor):
            projected = gateway_mod.estimate_cost(extractor.name, len(system) + len(user_prompt(dictionary, evidence)),
                                                  extractor.max_tokens)
            cost["projected_usd"] = projected
            ceiling = cfg.limits.get("max_cost_usd")
            if ceiling is not None and projected is not None and projected > float(ceiling):
                return {**empty, "cost": cost, "model_error": {
                    "flag": "cost_ceiling_exceeded", "message": f"projected ${projected} > ${ceiling}"}}
        try:
            raw = extractor.extract(dictionary, readable, system=system, evidence=evidence, trace=trace)
        except ModelError as exc:
            return {**empty, "cost": cost, "model_error": {"flag": f"error:{exc.code}", "message": str(exc),
                                                           "transient": exc.transient}}
        call: dict[str, Any] = {}
        if isinstance(extractor, LLMExtractor) and extractor.last_response is not None:
            r = extractor.last_response
            call = {"model": r.model, "stop_reason": r.stop_reason, "usage": r.usage,
                    "gateway_request_id": r.gateway_request_id, "latency_ms": r.latency_ms}
            cost["model_usd"] = r.cost_usd
        return {"raw": raw, "model_call": call, "cost": cost}

    def verify(state: RunState) -> dict[str, Any]:
        cfg, docs = state["cfg"], {d.doc_id: d for d in state["docs"]}
        readable = any(d.status == "read" for d in state["docs"])
        raw = state.get("raw") or {}
        raw_records = raw.get("records") or []
        failed_model = state.get("model_error")
        if readable and not raw_records and not failed_model:
            raw_records = [{}]            # nothing found: one empty record carries the missing fields
        records = []
        for r in raw_records:
            fields, reasons = finalize(cfg, r, docs)
            records.append({"fields": fields, "reasons": dedupe(reasons)})
        run_reasons = list(state["reasons"])
        if not readable and not state["sub"].ignored:
            run_reasons.append("no_readable_content")
        if failed_model:
            run_reasons.append(failed_model["flag"])
        if any(n.startswith("unplaced_partials") for n in raw.get("notes", [])):
            run_reasons.append("unplaced_content")
        return {"records": records, "reasons": run_reasons}

    def route(state: RunState) -> dict[str, Any]:
        """Flag Router (RT-43): every applicable flag, in rule order; nothing is dropped."""
        cfg = state["cfg"]
        records = []
        for rec in state["records"]:
            fields, reasons = rec["fields"], list(rec["reasons"])
            required = [fields[f.name]["confidence"] if fields[f.name]["value"] is not None else 0.0
                        for f in cfg.dictionary.required]
            present = [v["confidence"] for v in fields.values() if v.get("value") is not None]
            conf = round(min(required), 4) if required else round(min(present or [0.0]), 4)
            if conf < cfg.accept_at and "low_confidence" not in reasons:
                reasons.append("low_confidence")
            records.append({**rec, "reasons": reasons, "confidence": conf, "flagged": bool(reasons)})
        run_reasons = dedupe(state["reasons"] + [r for rec in records for r in rec["reasons"]])
        overall = min((rec["confidence"] for rec in records), default=0.0)
        return {"records": records, "reasons": run_reasons, "overall": overall,
                "flagged": bool(run_reasons) or not records}

    g = StateGraph(RunState)
    for name, fn in (("resolve_config", resolve_config), ("ingest", ingest), ("parse_item", parse_item),
                     ("assemble", assemble), ("extract", extract), ("verify", verify), ("route", route)):
        g.add_node(name, _timed(name, fn))
    g.add_edge(START, "resolve_config")
    g.add_edge("resolve_config", "ingest")
    g.add_conditional_edges("ingest", dispatch, ["parse_item", "assemble"])
    g.add_edge("parse_item", "assemble")
    g.add_conditional_edges("assemble", has_readable, {"extract": "extract", "verify": "verify"})
    g.add_edge("extract", "verify")
    g.add_edge("verify", "route")
    g.add_edge("route", END)
    return g.compile(checkpointer=checkpointer)

"""The extraction pipeline as a LangGraph state graph.

    START -> resolve_config -> ingest --(one Send per item)--> parse_item (parallel, sandboxed)
                                     \\-(no items)-----------\\
          -> assemble -> classify -> (readable and in scope?) extract -> expand -> verify -> transform -> route -> END
                                    \\------------------------------------^

* ``ingest`` opens the input, applies the ingestion filter, unpacks emails
  (recursively), zips and encrypted files, and spools item bytes (RT-34).
* ``parse_item`` runs once per item, concurrently (bounded by the config's
  ``concurrency.max_parallel``, RT-41), each in a sandboxed child process
  under a time budget (RT-39, RT-65). A failed item degrades the run (RT-10).
* ``assemble`` is the single join (RT-09): it numbers the documents.
* ``classify`` runs the config's detection rules (classify.py) to pick the
  email type, narrows the config to that type's dictionary, skills and
  threshold, and picks the skills whose fingerprint matches (scope.py). An
  out-of-scope email skips extraction and is flagged ``out_of_scope``.
* ``extract`` asks the stub or the model; a model failure, the run ceiling
  (RT-60) or the cost ceiling (RT-62) become flags, never an exception.
* ``expand`` (expand.py) reads every remaining row of a large blotter when the
  dictionary's records are rows: the extractor's citations give the column
  mapping, code streams the file.
* ``verify`` grounds every value and flags content no step read in full
  (``content_truncated``); ``transform`` (transform.py) runs the ZEN rules
  for deterministic transformation and lookup over every record; ``route``
  turns reasons into flags.

Nodes record their duration and the path the run took (``metadata.graph``).
With a checkpointer, every node boundary is a checkpoint, so a run interrupted
by a lost worker resumes where it stopped (RT-31, RT-40).
"""
from __future__ import annotations

import hashlib
import operator
import time
from typing import Annotated, Any, Callable, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from . import classify as classify_mod
from . import expand as expand_mod
from . import transform as transform_mod
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
    spool_run: str | None              # the run id whose spool holds item bytes (jobs); None = in memory
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
    classification: dict[str, Any]
    expanded: list[dict[str, Any]]       # records read straight from the file by expand
    expand_report: list[dict[str, Any]]
    transform_report: dict[str, Any] | None   # what the transform rules changed
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
    spool_run: str | None
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


def build_graph(store: ConfigStore, settings: Any, gateway: Gateway | None = None, checkpointer: Any = None,
                pool: Any = None):
    """Compile the pipeline. ``settings`` is pipeline.Settings (passed in to avoid a cycle).

    ``pool`` (a Postgres connection pool) backs the spool; without it item bytes stay in memory.
    """
    from .pipeline import resolve_location

    def _spool(run_id: str | None) -> Spool | None:
        return Spool(pool, run_id, getattr(settings, "state_key", None)) if (run_id and pool is not None) else None

    def resolve_config(state: RunState) -> dict[str, Any]:
        cfg = store.resolve(state.get("client"), state.get("usecase"), state.get("version"))
        provider = settings.model_provider or cfg.model.get("provider", "stub")
        name = "deterministic-stub" if provider == "stub" else (cfg.model.get("name") or DEFAULT_MODEL_ALIAS)
        return {"cfg": cfg, "provider": provider, "model_name": name}

    def ingest(state: RunState) -> dict[str, Any]:
        cfg = state["cfg"]
        path = resolve_location(settings, state["file_location"])
        ctx = IntakeContext(limits=cfg.intake, filter=cfg.ingestion_filter, client=cfg.client,
                            usecase=cfg.usecase, spool=_spool(state.get("spool_run")))
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
        return [Send("parse_item", ParseTask(item=i, evidence=ev, spool_run=state.get("spool_run"),
                                             timeout_s=float(ev.get("parse_timeout_s", 120)))) for i in items]

    def parse_item(task: ParseTask) -> dict[str, Any]:
        doc = sandbox.parse(task["item"], task["evidence"], _spool(task.get("spool_run")),
                            mode=settings.parse_sandbox, timeout_s=task["timeout_s"],
                            memory_mb=settings.sandbox_memory_mb)
        return {"parsed": [doc]}

    def assemble(state: RunState) -> dict[str, Any]:
        docs = number_docs(list(state.get("parsed") or []))
        reasons = list(state["reasons"]) + [d.reason for d in docs if d.status == "failed" and d.reason]
        return {"docs": docs, "reasons": reasons}

    def classify(state: RunState) -> dict[str, Any]:
        cfg, sub = state["cfg"], state["sub"]
        readable = [d for d in state["docs"] if d.status == "read"]
        result = classify_mod.classify(cfg, classify_mod.submission_text(sub, readable)) if readable else \
            {"status": "unclassified", "type": cfg.default_type, "score": None, "scores": {}}
        reasons = list(state["reasons"])
        if result["status"] == "out_of_scope":
            reasons.append("out_of_scope" + (f":{result['type']}" if result.get("type") else ""))
        elif result["status"] == "ambiguous":
            reasons.append(f"classification_ambiguous:{result['type']}|{result['runner_up']}")
        typed = cfg.for_type(result.get("type") if cfg.extractable(result.get("type")) else None)
        _, skills = typed.for_documents(scope.facts(sub, readable))
        return {"cfg": typed, "classification": result, "reasons": reasons,
                "skills_applied": [s.name for s in skills]}

    def has_readable(state: RunState) -> str:
        if not any(d.status == "read" for d in state["docs"]):
            return "verify"
        c = state.get("classification") or {}
        if c.get("status") == "out_of_scope" and state["cfg"].classification.get("out_of_scope") != "extract":
            return "verify"
        return "extract"

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

    def expand(state: RunState) -> dict[str, Any]:
        cfg = state["cfg"]
        raw_records = (state.get("raw") or {}).get("records") or []
        if state.get("model_error") or not raw_records:
            return {}
        started = float(state.get("started") or time.time())
        out = expand_mod.expand(cfg, [d for d in state["docs"] if d.status == "read"], state["sub"].items,
                                raw_records, spool=_spool(state.get("spool_run")),
                                deadline=started + float(cfg.limits["run_ceiling_s"]),
                                sandbox_mode=settings.parse_sandbox, memory_mb=settings.sandbox_memory_mb)
        if not out["reports"]:
            return {}
        return {"expanded": out["records"], "expand_report": out["reports"],
                "reasons": list(state["reasons"]) + out["reasons"]}

    def verify(state: RunState) -> dict[str, Any]:
        cfg, docs = state["cfg"], {d.doc_id: d for d in state["docs"]}
        readable = any(d.status == "read" for d in state["docs"])
        raw = state.get("raw") or {}
        raw_records = raw.get("records") or []
        failed_model = state.get("model_error")
        skipped = (state.get("classification") or {}).get("status") == "out_of_scope" and "raw" not in state
        if readable and not raw_records and not failed_model and not skipped:
            raw_records = [{}]            # nothing found: one empty record carries the missing fields
        records = []
        for r in raw_records:
            fields, reasons = finalize(cfg, r, docs)
            records.append({"fields": fields, "reasons": dedupe(reasons)})
        records = _with_expanded(records, state.get("expanded") or [])
        run_reasons = list(state["reasons"])
        complete = {(r["document"]) for r in state.get("expand_report") or [] if r.get("complete")}
        for d in state["docs"]:
            cut = [n for n in d.notes if n.split(":")[0] in ("rows_truncated", "pages_truncated", "sheets_truncated")]
            if any(n.startswith(("ocr_fallback:", "ocr_unavailable:")) for r in state.get("expand_report") or []
                   if r.get("document") == d.source for n in r.get("notes", [])):
                run_reasons.append(f"ocr_fallback:{d.name}")
            if d.status == "read" and any(n.startswith(("ocr_fallback:", "ocr_unavailable:")) for n in d.notes):
                run_reasons.append(f"ocr_fallback:{d.name}")
            if d.status == "read" and cut and not (d.source in complete
                                                   and all(n.startswith(("rows_truncated", "pages_truncated"))
                                                           for n in cut)):
                run_reasons.append(f"content_truncated:{d.name}")
        if not readable and not state["sub"].ignored:
            run_reasons.append("no_readable_content")
        if failed_model:
            run_reasons.append(failed_model["flag"])
        if any(n.startswith("unplaced_partials") for n in raw.get("notes", [])):
            run_reasons.append("unplaced_content")
        return {"records": records, "reasons": run_reasons}

    def transform(state: RunState) -> dict[str, Any]:
        """Deterministic transformation and lookup (ZEN rules) over every record; see transform.py."""
        records, report = transform_mod.apply(state["cfg"], state["records"], state["sub"], state["docs"])
        return {"records": records, "transform_report": report} if report else {}

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
                     ("assemble", assemble), ("classify", classify), ("extract", extract), ("expand", expand),
                     ("verify", verify), ("transform", transform), ("route", route)):
        g.add_node(name, _timed(name, fn))
    g.add_edge(START, "resolve_config")
    g.add_edge("resolve_config", "ingest")
    g.add_conditional_edges("ingest", dispatch, ["parse_item", "assemble"])
    g.add_edge("parse_item", "assemble")
    g.add_edge("assemble", "classify")
    g.add_conditional_edges("classify", has_readable, {"extract": "extract", "verify": "verify"})
    g.add_edge("extract", "expand")
    g.add_edge("expand", "verify")
    g.add_edge("verify", "transform")
    g.add_edge("transform", "route")
    g.add_edge("route", END)
    return g.compile(checkpointer=checkpointer)


def _with_expanded(records: list[dict[str, Any]], expanded: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Put rows read by expand in file order (sheet, page, table, row) among the extracted ones of that document."""
    if not expanded:
        return records

    def pos(rec: dict[str, Any]) -> tuple | None:
        for v in rec["fields"].values():
            doc_id, _, loc = str((v or {}).get("source") or "").partition("#")
            m = expand_mod.parse_loc(loc)
            if doc_id and m:
                return (doc_id, *m[3])
        return None

    extra = [{"fields": e["fields"], "reasons": dedupe(e["reasons"]), "_pos": (e["_doc"], *e["_sort"])}
             for e in expanded]
    docs_expanded = {e["_pos"][0] for e in extra}
    out: list[dict[str, Any]] = []
    placed = False
    for rec in records:
        p = pos(rec)
        if p and p[0] in docs_expanded and not placed:
            block = [dict(r, _pos=pos(r)) for r in records if (pos(r) or ("",))[0] in docs_expanded] + extra
            block.sort(key=lambda r: r["_pos"])
            out.extend({k: v for k, v in r.items() if k != "_pos"} for r in block)
            placed = True
        elif not (p and p[0] in docs_expanded):
            out.append(rec)
    if not placed:
        out.extend({k: v for k, v in r.items() if k != "_pos"} for r in extra)
    return out

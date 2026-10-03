"""The extraction pipeline as a LangGraph state graph.

    START -> resolve_config -> read_input -> build_evidence --(readable)--> extract -> verify -> route -> END
                                                           \\-(nothing readable)--------^

Each node reads the state it needs and returns only what it adds. Nodes are
wrapped to record their duration and the path the run took, which lands in
the audit record (``metadata.graph``). Settings, the config store and the
model gateway are bound when the graph is built, so the state holds only the
run's own data.
"""
from __future__ import annotations

import hashlib
import operator
import time
from typing import Annotated, Any, Callable, TypedDict

from langgraph.graph import END, START, StateGraph

from .config_store import ConfigStore, ExtractionConfig
from .evidence import Doc, build_docs, render
from .gateway import Gateway
from .intake import Submission, open_submission
from .llm import DEFAULT_MODEL_ALIAS, LLMExtractor, get_extractor
from .verify import dedupe, finalize

SUPPORTED_INPUTS = ("email", "msg", "zip", "csv", "excel", "pdf")


def _merge(a: dict, b: dict) -> dict:
    return {**a, **b}


class RunState(TypedDict, total=False):
    # request
    file_location: str
    client: str | None
    usecase: str | None
    version: str | None
    # produced by nodes
    cfg: ExtractionConfig
    sub: Submission
    provider: str
    model_name: str
    audit_id: str
    docs: list[Doc]
    reasons: list[str]
    raw: dict[str, Any]
    model_call: dict[str, Any]
    records: list[dict[str, Any]]        # [{"fields", "reasons", "confidence", "status"}]
    status: str
    overall: float
    # bookkeeping, merged across nodes
    trace: Annotated[list[str], operator.add]
    timings_ms: Annotated[dict[str, float], _merge]


def _timed(name: str, fn: Callable[[RunState], dict[str, Any]]) -> Callable[[RunState], dict[str, Any]]:
    def node(state: RunState) -> dict[str, Any]:
        t0 = time.perf_counter()
        update = fn(state)
        update["trace"] = [name]
        update["timings_ms"] = {name: round((time.perf_counter() - t0) * 1000, 1)}
        return update
    node.__name__ = name
    return node


def build_graph(store: ConfigStore, settings: Any, gateway: Gateway | None = None):
    """Compile the pipeline. ``settings`` is pipeline.Settings (passed in to avoid a cycle)."""
    from .pipeline import ServiceError, resolve_location

    def resolve_config(state: RunState) -> dict[str, Any]:
        cfg = store.resolve(state.get("client"), state.get("usecase"), state.get("version"))
        provider = settings.model_provider or cfg.model.get("provider", "stub")
        name = "deterministic-stub" if provider == "stub" else (cfg.model.get("name") or DEFAULT_MODEL_ALIAS)
        return {"cfg": cfg, "provider": provider, "model_name": name}

    def read_input(state: RunState) -> dict[str, Any]:
        cfg = state["cfg"]
        path = resolve_location(settings, state["file_location"])
        sub = open_submission(state["file_location"], path.name, path.read_bytes(), cfg.intake)
        if sub.kind not in SUPPORTED_INPUTS:
            raise ServiceError(422, f"unsupported_input:{sub.kind}", f"cannot extract from a {sub.kind} file",
                               {"supported": ["eml", "msg", "zip", "csv", "xlsx", "xlsm", "xls", "pdf"]})
        audit_id = "run_" + hashlib.sha256(
            f"{sub.data_sha256}|{cfg.sha256}|{state['provider']}|{state['model_name']}".encode()).hexdigest()[:20]
        return {"sub": sub, "audit_id": audit_id, "reasons": list(sub.reasons)}

    def build_evidence(state: RunState) -> dict[str, Any]:
        docs, doc_reasons = build_docs(state["sub"], state["cfg"].evidence)
        return {"docs": docs, "reasons": state["reasons"] + doc_reasons}

    def has_readable(state: RunState) -> str:
        return "extract" if any(d.status == "read" for d in state["docs"]) else "verify"

    def extract(state: RunState) -> dict[str, Any]:
        cfg = state["cfg"]
        readable = [d for d in state["docs"] if d.status == "read"]
        extractor = get_extractor(cfg, settings.model_provider, gateway)
        system = cfg.system_prompt + "".join("\n\n" + s.body for s in cfg.skills)
        evidence = render(readable, int(cfg.evidence["max_chars_per_document"]))
        trace = {"audit_id": state["audit_id"], "client": cfg.client, "usecase": cfg.usecase,
                 "config_version": cfg.version}
        raw = extractor.extract(cfg.dictionary, readable, system=system, evidence=evidence, trace=trace)
        call: dict[str, Any] = {}
        if isinstance(extractor, LLMExtractor) and extractor.last_response is not None:
            r = extractor.last_response
            call = {"model": r.model, "stop_reason": r.stop_reason, "usage": r.usage,
                    "gateway_request_id": r.gateway_request_id, "latency_ms": r.latency_ms}
        return {"raw": raw, "model_call": call}

    def verify(state: RunState) -> dict[str, Any]:
        cfg, docs = state["cfg"], {d.doc_id: d for d in state["docs"]}
        readable = any(d.status == "read" for d in state["docs"])
        raw = state.get("raw") or {}
        raw_records = raw.get("records") or []
        if readable and not raw_records:
            raw_records = [{}]            # nothing found: one empty record carries the missing fields
        records = []
        for r in raw_records:
            fields, reasons = finalize(cfg, r, docs)
            records.append({"fields": fields, "reasons": dedupe(reasons)})
        run_reasons = list(state["reasons"])
        if not readable:
            run_reasons.append("no_readable_content")
        if any(n.startswith("unplaced_partials") for n in raw.get("notes", [])):
            run_reasons.append("unplaced_content")
        return {"records": records, "reasons": run_reasons}

    def route(state: RunState) -> dict[str, Any]:
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
            records.append({**rec, "reasons": reasons, "confidence": conf,
                            "status": "extracted" if not reasons else "review"})
        run_reasons = dedupe(state["reasons"] + [r for rec in records for r in rec["reasons"]])
        overall = min((rec["confidence"] for rec in records), default=0.0)
        status = "extracted" if records and not run_reasons else "review"
        return {"records": records, "reasons": run_reasons, "overall": overall, "status": status}

    g = StateGraph(RunState)
    for name, fn in (("resolve_config", resolve_config), ("read_input", read_input),
                     ("build_evidence", build_evidence), ("extract", extract),
                     ("verify", verify), ("route", route)):
        g.add_node(name, _timed(name, fn))
    g.add_edge(START, "resolve_config")
    g.add_edge("resolve_config", "read_input")
    g.add_edge("read_input", "build_evidence")
    g.add_conditional_edges("build_evidence", has_readable, {"extract": "extract", "verify": "verify"})
    g.add_edge("extract", "verify")
    g.add_edge("verify", "route")
    g.add_edge("route", END)
    return g.compile()

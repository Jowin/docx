"""Extractors: the deterministic stub, or a model reached through the model gateway.

Every extractor returns {"records": [record, ...], "notes": [...]}, where a
record has one entry per dictionary field:
  scalar   {"value": str | None, "source": "d2#Summary!B14", "confidence": 0.93}
  array    {"items": [{"values": {...}, "source": "d2#Summary!A10", "confidence": 0.9}]}

The LLM extractor never opens a connection itself: it builds a
``ModelRequest`` and hands it to the gateway function (gateway.py). The
model must answer through one tool whose input schema is generated from the
data dictionary, so the answer is always structured.
"""
from __future__ import annotations

import json
from typing import Any

from .config_store import ExtractionConfig
from .errors import ModelError
from .evidence import Doc
from .gateway import Gateway, ModelRequest, ModelResponse, ToolSpec, call_model
from .schema import DataDictionary
from .stub import StubModel

TOOL_NAME = "record_extraction"
DEFAULT_MODEL_ALIAS = "default"     # resolved to a real model id by the gateway


def tool_schema(dictionary: DataDictionary) -> ToolSpec:
    """One tool whose input is a list of records, each holding every dictionary field."""
    props: dict[str, Any] = {}
    for f in dictionary.fields:
        if f.type == "array":
            props[f.name] = {
                "type": "array",
                "description": f.description or f"Rows for {f.name}.",
                "items": {"type": "object", "properties": {
                    "values": {"type": "object", "properties": {
                        s.name: {"type": ["string", "null"], "description": s.description or s.type}
                        for s in f.items}},
                    "source": {"type": "string", "description": "Citation of the row's first cell, d<n>#<locator>."},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1}},
                    "required": ["values", "source", "confidence"]}}
        else:
            props[f.name] = {
                "type": "object",
                "description": f.description or f.type,
                "properties": {
                    "value": {"type": ["string", "null"]},
                    "source": {"type": "string", "description": "d<n>#<locator>, or empty when value is null."},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1}},
                "required": ["value", "source", "confidence"]}
    record = {"type": "object", "properties": props, "required": [f.name for f in dictionary.fields]}
    return ToolSpec(
        name=TOOL_NAME,
        description=("Record the extracted data. Call exactly once. Return one record per distinct "
                     f"{dictionary.name} in the evidence, told apart by {dictionary.record_key}; "
                     "a single one is a list with one record."),
        parameters={"type": "object", "properties": {"records": {"type": "array", "items": record}},
                    "required": ["records"]})


def user_prompt(dictionary: DataDictionary, evidence: str) -> str:
    return ("Data dictionary:\n```json\n" + json.dumps(dictionary.describe(), indent=1) +
            "\n```\n\nEvidence:\n" + evidence +
            f"\n\nCall {TOOL_NAME} once. Return one record per distinct {dictionary.name}, "
            f"identified by {dictionary.record_key}, each with every field in the data dictionary.")


class LLMExtractor:
    """Builds the request from the config and evidence; the gateway does the call."""

    provider = "gateway"

    def __init__(self, settings: dict[str, Any], gateway: Gateway) -> None:
        self.name = settings.get("name") or DEFAULT_MODEL_ALIAS
        self.max_tokens = int(settings.get("max_tokens", 4096))
        self.gateway = gateway
        self.last_response: ModelResponse | None = None

    def request(self, dictionary: DataDictionary, *, system: str, evidence: str,
                trace: dict[str, str]) -> ModelRequest:
        return ModelRequest(model=self.name, system=system,
                            messages=[{"role": "user", "content": user_prompt(dictionary, evidence)}],
                            tools=(tool_schema(dictionary),), required_tool=TOOL_NAME,
                            max_tokens=self.max_tokens, temperature=0.0, trace=trace)

    def extract(self, dictionary: DataDictionary, docs: list[Doc], *, system: str, evidence: str,
                trace: dict[str, str] | None = None) -> dict[str, Any]:
        resp = self.gateway(self.request(dictionary, system=system, evidence=evidence, trace=trace or {}))
        self.last_response = resp
        for call in resp.tool_calls:
            if call.name == TOOL_NAME:
                return _from_tool_input(dictionary, call.input)
        raise ModelError("model_failed", f"model did not call {TOOL_NAME} (stop_reason={resp.stop_reason})")


def _from_tool_input(dictionary: DataDictionary, data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or not isinstance(data.get("records"), list):
        raise ModelError("model_failed", "tool input has no 'records' list")
    return {"records": [_record(dictionary, r) for r in data["records"] if isinstance(r, dict)],
            "notes": []}


def _record(dictionary: DataDictionary, data: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for f in dictionary.fields:
        v = data.get(f.name)
        if f.type == "array":
            items = v if isinstance(v, list) else []
            out[f.name] = {"items": [i for i in items if isinstance(i, dict)]}
        elif isinstance(v, dict):
            out[f.name] = {"value": v.get("value"), "source": str(v.get("source") or ""),
                           "confidence": _conf(v.get("confidence"))}
        else:
            out[f.name] = {"value": None, "source": "", "confidence": 0.0}
    return out


def _conf(x: Any) -> float:
    try:
        return min(max(float(x), 0.0), 1.0)
    except (TypeError, ValueError):
        return 0.0


def get_extractor(cfg: ExtractionConfig, provider_override: str | None = None,
                  gateway: Gateway | None = None):
    settings = cfg.model
    provider = provider_override or settings.get("provider", "stub")
    if provider == "stub":
        return StubModel(date_order=cfg.date_order)
    return LLMExtractor(settings, gateway or call_model)

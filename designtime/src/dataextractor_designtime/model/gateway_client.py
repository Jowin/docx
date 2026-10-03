"""ModelClient that answers judgment tasks through the model gateway function.

The gateway function lives in the runtime (``extractor_service.gateway``):
one function every model call in the system goes through, and the only code
that knows which vendor and model ids sit behind it. This client imports it
from the runtime source that design-time already runs for isolated
extractions, so design-time carries no vendor logic of its own and asks only
for a model *alias*.

Tasks the client has a prompt for go to the gateway; every other task falls
back to the deterministic stub, so switching the gateway on changes only the
judgments it is meant to change.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any, Callable

from .base import ModelRequest, ModelResponse
from .stub import StubModelClient

SKILL_TOOL = "write_pattern_skill"

_SKILL_SYSTEM = """You write skills for a document extraction engine.

A skill teaches the engine where one document pattern keeps the fields of a data
dictionary. You are given the dictionary, the fields that failed on a sample of
the pattern, the hints rules already derived (labels the engine should treat as
the field's name, and anchors: phrases the value follows mid-sentence), lines of
the sample's evidence, and optional reference text from a person.

Answer by calling the tool with:
- body: markdown for the engine's model, starting "## Skill: <pattern> pattern".
  Say where each failing field is in this pattern and how to tell it apart from
  look-alikes. Do not restate the whole dictionary. Never invent values.
- hints: extra labels or anchors you are confident of, only for fields in the
  dictionary, copied exactly as they are written in the evidence. Leave it empty
  when the derived hints already cover the failures.
"""

_HINTS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "fields": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "properties": {
                    "labels": {"type": "array", "items": {"type": "string", "maxLength": 80}},
                    "anchors": {"type": "array", "items": {"type": "string", "maxLength": 80}},
                    "items": {"type": "object", "additionalProperties": {
                        "type": "object",
                        "properties": {"labels": {"type": "array", "items": {"type": "string"}}}}},
                },
                "additionalProperties": False,
            },
        }
    },
    "additionalProperties": False,
}

#: task -> (system prompt, tool name, tool description, tool input schema)
TASKS: dict[str, tuple[str, str, str, dict[str, Any]]] = {
    "pattern_skill.write": (
        _SKILL_SYSTEM, SKILL_TOOL, "Return the skill body and any extra hints.",
        {"type": "object", "required": ["body", "hints"], "additionalProperties": False,
         "properties": {"body": {"type": "string"}, "hints": _HINTS_SCHEMA}},
    ),
}


def load_gateway(runtime_dir: Path):
    """Import the runtime's gateway module from its source folder."""
    path = str(Path(runtime_dir).resolve())
    if path not in sys.path:
        sys.path.insert(0, path)
    return importlib.import_module("extractor_service.gateway")


class GatewayModelClient:
    """ModelClient over the runtime's ``call_model``."""

    name = "gateway"

    def __init__(self, runtime_dir: Path, *, alias: str = "default",
                 call: Callable[[Any], Any] | None = None, fallback: Any = None) -> None:
        self.runtime_dir = Path(runtime_dir)
        self.alias = alias
        self._call = call                    # tests pass a fake gateway function
        self.fallback = fallback or StubModelClient()

    def complete(self, request: ModelRequest) -> ModelResponse:
        task = TASKS.get(request.task)
        if task is None:
            return self.fallback.complete(request)
        system, tool, description, schema = task
        gw = load_gateway(self.runtime_dir)
        call = self._call or gw.call_model
        trace = {k: str(v) for k, v in (request.evidence.get("trace") or {}).items()}
        evidence = {k: v for k, v in request.evidence.items() if k != "trace"}
        try:
            response = call(gw.ModelRequest(
                model=self.alias, system=system,
                messages=[{"role": "user", "content": json.dumps(evidence, ensure_ascii=False, default=str)}],
                tools=(gw.ToolSpec(tool, description, schema),), required_tool=tool,
                max_tokens=request.token_budget, temperature=0.0, trace=trace))
        except Exception as exc:
            code = getattr(exc, "code", None)
            if not code:
                raise
            from ..agents.base import AgentError
            raise AgentError(str(exc), code=code, status=503 if code == "model_unavailable" else 502) from exc
        answer = next((c.input for c in response.tool_calls if c.name == tool), None)
        if not isinstance(answer, dict):
            from ..agents.base import AgentError
            raise AgentError(f"the model did not call {tool}", code="model_failed", status=502)
        return ModelResponse(task=request.task, output=answer,
                             produced_by=f"gateway:{self.alias}", deterministic=False)


def default_client(settings: Any) -> Any:
    """The gateway client when MODEL_GATEWAY_URL is set, otherwise the stub."""
    if getattr(settings, "model_gateway_url", None):
        return GatewayModelClient(settings.runtime_dir)
    return StubModelClient()

"""Model gateway: the one function every model call goes through.

    response = call_model(ModelRequest(...))

This is the only module that knows which model vendor sits behind the
gateway, which model ids exist, and what the wire protocol looks like. The
rest of the service speaks in neutral terms: a model *alias* from the config
(``"default"``), a ``ToolSpec`` whose parameters are a JSON Schema, and the
name of the tool the model must call. To route through a different gateway
or vendor, replace ``call_model`` (or pass your own function to
``create_app(model_gateway=...)``); nothing outside this file changes.

Default implementation: posts an Anthropic Messages API request to
``{MODEL_GATEWAY_URL}/v1/messages`` (most gateways accept this shape, and the
Anthropic API serves it directly). Model aliases resolve to Claude model ids.

  MODEL_GATEWAY_URL           https://gateway.internal  (or https://api.anthropic.com for local tests)
  MODEL_GATEWAY_TOKEN         the credential
  MODEL_GATEWAY_AUTH_HEADER   "Authorization" (sends "Bearer <token>", default) or "x-api-key"
  MODEL_GATEWAY_MODELS        JSON alias map overriding the built-in one, e.g. {"default": "claude-opus-5-5"}
  MODEL_GATEWAY_TIMEOUT_S     per attempt, default 120
  MODEL_GATEWAY_RETRIES       extra attempts on transient failures, default 2
  MODEL_GATEWAY_PRICES        JSON prices in USD per million tokens, by model id or alias, e.g.
                              {"default": {"input": 3.0, "output": 15.0}}; without it cost is unknown

Cost (RT-62): ``estimate_cost`` projects a call's cost before it is made and
``cost_of`` prices one that was; both return None when no price is configured.

Every request carries the run's audit id, client and use case as headers
(X-Request-Id, X-Client-Id, X-Usecase, X-Config-Version, X-Model-Alias) so
gateway logs join to audit records.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .errors import ModelError

# ------------------------------------------------------------------ vendor specifics (this file only)

ANTHROPIC_VERSION = "2023-06-01"
STOP_REASONS = {"tool_use": "tool_call", "end_turn": "end", "stop_sequence": "end",
                "max_tokens": "max_tokens", "refusal": "refusal", "pause_turn": "paused"}
MODEL_ALIASES = {
    "default": "claude-sonnet-5-5",
    "large": "claude-opus-5-5",
    "fast": "claude-haiku-4-5-20251001",
}


# ------------------------------------------------------------------ neutral contract

@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]           # JSON Schema of the tool's input


@dataclass(frozen=True)
class ToolCall:
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class ModelRequest:
    model: str                           # alias from the config ("default"), or an id the gateway knows
    system: str
    messages: list[dict[str, Any]]       # [{"role": "user" | "assistant", "content": str}]
    tools: tuple[ToolSpec, ...] = ()
    required_tool: str | None = None     # the model must answer by calling this tool
    max_tokens: int = 4096
    temperature: float = 0.0
    trace: dict[str, str] = field(default_factory=dict)   # audit_id, client, usecase, config_version


@dataclass(frozen=True)
class ModelResponse:
    model: str                           # the model id that actually answered
    stop_reason: str | None              # neutral: tool_call, end, max_tokens, refusal, paused
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    usage: dict[str, int] = field(default_factory=dict)
    gateway_request_id: str | None = None
    latency_ms: float = 0.0
    cost_usd: float | None = None


Gateway = Callable[[ModelRequest], ModelResponse]


def resolve_model(alias: str) -> str:
    """Config alias -> model id. Unknown names pass through as ids."""
    aliases = dict(MODEL_ALIASES)
    override = os.environ.get("MODEL_GATEWAY_MODELS")
    if override:
        try:
            aliases.update({str(k): str(v) for k, v in json.loads(override).items()})
        except (ValueError, AttributeError) as exc:
            raise ModelError("model_unavailable", f"MODEL_GATEWAY_MODELS is not a JSON object: {exc}") from exc
    return aliases.get(alias or "default", alias)


def _prices() -> dict[str, dict[str, float]]:
    raw = os.environ.get("MODEL_GATEWAY_PRICES")
    if not raw:
        return {}
    try:
        return {str(k): {"input": float(v["input"]), "output": float(v["output"])}
                for k, v in json.loads(raw).items()}
    except (ValueError, KeyError, TypeError, AttributeError):
        return {}


def _price(alias_or_id: str) -> dict[str, float] | None:
    prices = _prices()
    return prices.get(alias_or_id) or prices.get(resolve_model(alias_or_id))


def estimate_cost(alias: str, input_chars: int, max_output_tokens: int) -> float | None:
    """A ceiling on one call's cost: ~4 characters a token in, every allowed token out."""
    p = _price(alias or "default")
    if p is None:
        return None
    return round((input_chars / 4) * p["input"] / 1e6 + max_output_tokens * p["output"] / 1e6, 6)


def cost_of(alias: str, usage: dict[str, int]) -> float | None:
    p = _price(alias or "default")
    if p is None:
        return None
    tokens_in = usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0) + \
        usage.get("cache_read_input_tokens", 0)
    return round(tokens_in * p["input"] / 1e6 + usage.get("output_tokens", 0) * p["output"] / 1e6, 6)


def _wire_body(request: ModelRequest, model_id: str) -> dict[str, Any]:
    body: dict[str, Any] = {"model": model_id, "max_tokens": request.max_tokens,
                            "temperature": request.temperature, "system": request.system,
                            "messages": request.messages}
    if request.tools:
        body["tools"] = [{"name": t.name, "description": t.description, "input_schema": t.parameters}
                         for t in request.tools]
    if request.required_tool:
        body["tool_choice"] = {"type": "tool", "name": request.required_tool}
    return body


# ------------------------------------------------------------------ default: HTTP gateway

def call_model(request: ModelRequest, *, transport: Any = None) -> ModelResponse:
    """Send one request through the model gateway. Raises ``ModelError``.

    ``transport`` is for tests (an ``httpx`` transport); production leaves it unset.
    """
    import httpx

    url = os.environ.get("MODEL_GATEWAY_URL", "").rstrip("/")
    token = os.environ.get("MODEL_GATEWAY_TOKEN", "")
    if not url or not token:
        raise ModelError("model_unavailable", "MODEL_GATEWAY_URL and MODEL_GATEWAY_TOKEN must be set")
    auth_header = os.environ.get("MODEL_GATEWAY_AUTH_HEADER", "Authorization")
    timeout = float(os.environ.get("MODEL_GATEWAY_TIMEOUT_S", "120"))
    retries = int(os.environ.get("MODEL_GATEWAY_RETRIES", "2"))
    model_id = resolve_model(request.model)

    headers = {"content-type": "application/json", "anthropic-version": ANTHROPIC_VERSION,
               auth_header: f"Bearer {token}" if auth_header.lower() == "authorization" else token,
               "X-Model-Alias": request.model or "default"}
    for key, header in (("audit_id", "X-Request-Id"), ("client", "X-Client-Id"),
                        ("usecase", "X-Usecase"), ("config_version", "X-Config-Version")):
        if request.trace.get(key):
            headers[header] = request.trace[key]
    body = _wire_body(request, model_id)

    try:
        client = httpx.Client(timeout=timeout, transport=transport)
    except Exception as exc:                     # bad TLS bundle, proxy settings, ...
        raise ModelError("model_unavailable", f"cannot create gateway client: {exc}") from exc
    last: ModelError | None = None
    with client:
        for attempt in range(retries + 1):
            if attempt:
                time.sleep(min(2 ** (attempt - 1), 8))
            t0 = time.perf_counter()
            try:
                resp = client.post(f"{url}/v1/messages", json=body, headers=headers)
            except httpx.TimeoutException:
                last = ModelError("model_timeout", f"gateway timed out after {timeout}s", transient=True)
                continue
            except httpx.HTTPError as exc:
                last = ModelError("model_failed", f"gateway unreachable: {exc}", transient=True)
                continue
            latency = round((time.perf_counter() - t0) * 1000, 1)
            if resp.status_code == 200:
                parsed = _parse(resp.json(), resp.headers, latency)
                from dataclasses import replace
                return replace(parsed, cost_usd=cost_of(request.model, parsed.usage))
            detail = resp.text[:300]
            if resp.status_code in (401, 403):
                raise ModelError("model_unavailable", f"gateway refused credentials ({resp.status_code}): {detail}")
            if resp.status_code in (400, 404, 413, 422):
                raise ModelError("model_failed", f"gateway rejected the request ({resp.status_code}): {detail}")
            last = ModelError("model_failed", f"gateway error {resp.status_code}: {detail}",
                              transient=resp.status_code == 429 or resp.status_code >= 500)
            if not last.transient:
                raise last
    assert last is not None
    raise last


def _parse(data: dict[str, Any], headers: Any, latency: float) -> ModelResponse:
    calls, texts = [], []
    for block in data.get("content") or []:
        if block.get("type") == "tool_use":
            calls.append(ToolCall(name=block.get("name", ""), input=block.get("input") or {}))
        elif block.get("type") == "text":
            texts.append(block.get("text", ""))
    usage = {k: int(v) for k, v in (data.get("usage") or {}).items() if isinstance(v, int)}
    stop = data.get("stop_reason")
    return ModelResponse(model=data.get("model", ""), stop_reason=STOP_REASONS.get(stop, stop),
                         text="".join(texts), tool_calls=tuple(calls), usage=usage,
                         gateway_request_id=headers.get("x-request-id") or data.get("id"),
                         latency_ms=latency)

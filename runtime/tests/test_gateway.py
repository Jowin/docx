"""The default gateway function against a mock HTTP transport."""
import json

import httpx
import pytest

from extractor_service.errors import ModelError
from extractor_service.gateway import ModelRequest, ToolSpec, call_model, resolve_model

REQ = ModelRequest(model="default", system="sys",
                   messages=[{"role": "user", "content": "hi"}],
                   tools=(ToolSpec("record_extraction", "Record fields.", {"type": "object"}),),
                   required_tool="record_extraction",
                   trace={"audit_id": "run_abc", "client": "acme", "usecase": "ap-invoices",
                          "config_version": "1.1.0"})
OK = {"id": "msg_1", "model": "claude-sonnet-5-5", "stop_reason": "tool_use",
      "content": [{"type": "tool_use", "id": "t1", "name": "record_extraction", "input": {"a": 1}}],
      "usage": {"input_tokens": 10, "output_tokens": 5}}


@pytest.fixture(autouse=True)
def gateway_env(monkeypatch):
    monkeypatch.setenv("MODEL_GATEWAY_URL", "https://gateway.test/")
    monkeypatch.setenv("MODEL_GATEWAY_TOKEN", "secret")
    monkeypatch.setenv("MODEL_GATEWAY_RETRIES", "2")
    monkeypatch.setattr("time.sleep", lambda s: None)


def _transport(*responses):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        r = responses[min(len(seen) - 1, len(responses) - 1)]
        if isinstance(r, Exception):
            raise r
        status, body = r
        return httpx.Response(status, json=body, headers={"x-request-id": "gw-9"})
    return httpx.MockTransport(handler), seen


def test_request_shape_and_tracing_headers():
    t, seen = _transport((200, OK))
    resp = call_model(REQ, transport=t)
    req = seen[0]
    assert str(req.url) == "https://gateway.test/v1/messages"
    assert req.headers["authorization"] == "Bearer secret"
    assert req.headers["x-request-id"] == "run_abc" and req.headers["x-client-id"] == "acme"
    assert req.headers["x-usecase"] == "ap-invoices" and req.headers["x-model-alias"] == "default"
    body = json.loads(req.content)
    assert body["model"] == "claude-sonnet-5-5" and body["temperature"] == 0.0     # alias resolved here
    assert body["tool_choice"] == {"type": "tool", "name": "record_extraction"}
    assert body["tools"] == [{"name": "record_extraction", "description": "Record fields.",
                              "input_schema": {"type": "object"}}]
    assert resp.tool_calls[0].input == {"a": 1} and resp.usage == {"input_tokens": 10, "output_tokens": 5}
    assert resp.gateway_request_id == "gw-9"


def test_api_key_header_mode(monkeypatch):
    monkeypatch.setenv("MODEL_GATEWAY_AUTH_HEADER", "x-api-key")
    t, seen = _transport((200, OK))
    call_model(REQ, transport=t)
    assert seen[0].headers["x-api-key"] == "secret" and "authorization" not in seen[0].headers


def test_transient_errors_are_retried():
    t, seen = _transport((503, {"error": "busy"}), (429, {"error": "slow down"}), (200, OK))
    assert call_model(REQ, transport=t).stop_reason == "tool_call"     # vendor term translated
    assert len(seen) == 3


def test_retries_are_bounded():
    t, seen = _transport((503, {"error": "busy"}))
    with pytest.raises(ModelError) as e:
        call_model(REQ, transport=t)
    assert e.value.code == "model_failed" and e.value.transient and len(seen) == 3


@pytest.mark.parametrize("status,code", [(401, "model_unavailable"), (403, "model_unavailable"),
                                         (400, "model_failed")])
def test_permanent_errors_are_not_retried(status, code):
    t, seen = _transport((status, {"error": "no"}))
    with pytest.raises(ModelError) as e:
        call_model(REQ, transport=t)
    assert e.value.code == code and not e.value.transient and len(seen) == 1


def test_timeout_is_typed():
    t, _ = _transport(httpx.ReadTimeout("slow"))
    with pytest.raises(ModelError) as e:
        call_model(REQ, transport=t)
    assert e.value.code == "model_timeout" and e.value.transient


def test_missing_configuration(monkeypatch):
    monkeypatch.delenv("MODEL_GATEWAY_URL")
    with pytest.raises(ModelError) as e:
        call_model(REQ)
    assert e.value.code == "model_unavailable"


def test_client_setup_failure_is_typed(monkeypatch):
    import httpx as _httpx

    def broken(*a, **k):
        raise PermissionError("cannot read CA bundle")
    monkeypatch.setattr(_httpx, "Client", broken)
    with pytest.raises(ModelError) as e:
        call_model(REQ)
    assert e.value.code == "model_unavailable"


def test_model_aliases_and_override(monkeypatch):
    assert resolve_model("default") == "claude-sonnet-5-5"
    assert resolve_model("") == "claude-sonnet-5-5"
    assert resolve_model("my-gateway-route") == "my-gateway-route"        # unknown names pass through
    monkeypatch.setenv("MODEL_GATEWAY_MODELS", '{"default": "claude-opus-5-5"}')
    assert resolve_model("default") == "claude-opus-5-5"
    monkeypatch.setenv("MODEL_GATEWAY_MODELS", "not json")
    with pytest.raises(ModelError) as e:
        resolve_model("default")
    assert e.value.code == "model_unavailable"

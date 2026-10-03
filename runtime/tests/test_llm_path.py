"""The model path, with a fake gateway function standing in for the model gateway."""
from fastapi.testclient import TestClient

from extractor_service.api import create_app
from extractor_service.config_store import ConfigStore
from extractor_service.gateway import ModelResponse, ToolCall
from extractor_service.llm import TOOL_NAME, tool_schema


class FakeGateway:
    """Records every ModelRequest and answers with a fixed tool call."""

    def __init__(self, answer, tool=TOOL_NAME):
        self.answer, self.tool, self.requests = answer, tool, []

    def __call__(self, request):
        self.requests.append(request)
        calls = (ToolCall(self.tool, self.answer),) if self.tool else ()
        return ModelResponse(model=request.model, stop_reason="tool_call" if calls else "end",
                             tool_calls=calls, usage={"input_tokens": 1200, "output_tokens": 300},
                             gateway_request_id="gw-123", latency_ms=12.5)


def _record(**over):
    base = {
        "invoice_number": {"value": "INV-20194", "source": "d2#Invoice!A3", "confidence": 0.97},
        "invoice_date": {"value": "2026-08-15", "source": "d2#Invoice!B4", "confidence": 0.95},
        "due_date": {"value": "2026-09-14", "source": "d2#Invoice!A3", "confidence": 0.9},  # wrong cite
        "vendor": {"value": "Acme Corp", "source": "d2#Invoice!A6", "confidence": 0.95},
        "currency": {"value": "USD", "source": "d2#Invoice!D13", "confidence": 0.9},
        "total_amount": {"value": "12400.00", "source": "d2#Invoice!D13", "confidence": 0.98},
        "tax_amount": {"value": "999.99", "source": "d2#Invoice!D12", "confidence": 0.9},  # invented
        "po_number": {"value": "PO-5531", "source": "d2#Invoice!A7", "confidence": 0.96},
        "payment_reference": {"value": None, "source": "", "confidence": 0.0},
        "line_items": [
            {"values": {"description": "Consulting", "quantity": "10", "unit_price": "1000", "amount": "10000"},
             "source": "d2#Invoice!A10", "confidence": 0.95},
            {"values": {"description": "Ghost line", "quantity": "3", "unit_price": "7", "amount": "21"},
             "source": "d2#Invoice!A11", "confidence": 0.9}],
    }
    base.update(over)
    return base


def _answer(*records):
    return {"records": list(records) or [_record()]}


def _client(settings, gateway):
    return TestClient(create_app(settings, model_gateway=gateway))


def test_model_answer_is_verified_against_the_evidence(settings):
    gw = FakeGateway(_answer())
    e = _client(settings, gw).post("/extract", json={"file_location": "invoice-email.eml", "client": "acme",
                                                     "extended": True}).json()
    assert len(e["records"]) == 1
    f = e["records"][0]["fields"]
    assert f["total_amount"]["grounding"] == "verified" and f["total_amount"]["value"] == "12400"
    assert f["due_date"]["grounding"] == "relocated"
    assert f["due_date"]["source"] == "attachment:INV-20194.xlsx#Invoice!B5"
    assert f["tax_amount"]["value"] is None and f["tax_amount"]["rejected"]["value"] == "999.99"
    assert "unverified_value:tax_amount" in e["flags"]
    assert [i["description"] for i in e["data"][0]["line_items"]] == ["Consulting"]
    model = e["metadata"]["model"]
    assert model["provider"] == "gateway" and model["name"] == "default"
    assert model["call"]["usage"] == {"input_tokens": 1200, "output_tokens": 300}
    assert model["call"]["gateway_request_id"] == "gw-123"


def test_every_model_call_goes_through_the_gateway_function(settings):
    gw = FakeGateway(_answer())
    r = _client(settings, gw).post("/extract", json={"file_location": "invoice.xlsx", "client": "acme"})
    assert len(gw.requests) == 1
    req = gw.requests[0]
    assert req.model == "default" and req.temperature == 0
    assert req.required_tool == TOOL_NAME and req.tools[0].name == TOOL_NAME
    assert "Skill: field extraction" in req.system and "Skill: table extraction" in req.system
    assert "[d1#Invoice!A3] Invoice No: INV-20194" in req.messages[0]["content"]
    assert req.trace == {"audit_id": r.headers["x-audit-id"], "client": "acme",
                         "usecase": "ap-invoices", "config_version": "1.1.0"}


def test_stub_configs_never_call_the_gateway(settings):
    gw = FakeGateway(_answer())
    _client(settings, gw).post("/extract", json={"file_location": "invoice.xlsx"})
    assert gw.requests == []


def test_tool_schema_follows_the_dictionary(config_root):
    cfg = ConfigStore(config_root).resolve("acme", "ap-invoices", "1.1.0")
    schema = tool_schema(cfg.dictionary).parameters
    assert schema["required"] == ["records"]
    record = schema["properties"]["records"]["items"]
    assert set(record["required"]) == {f.name for f in cfg.dictionary.fields}
    items = record["properties"]["line_items"]["items"]["properties"]["values"]["properties"]
    assert set(items) == {"description", "quantity", "unit_price", "amount"}


def test_model_that_skips_the_tool_is_a_flagged_result(settings):
    r = _client(settings, FakeGateway({}, tool=None)).post(
        "/extract", json={"file_location": "invoice.xlsx", "client": "acme"})
    e = r.json()
    assert r.status_code == 200 and e["flagged"] is True and "error:model_failed" in e["flags"]
    assert e["metadata"]["model_error"]["message"].startswith("model did not call")

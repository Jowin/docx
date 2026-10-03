"""The judge and the pattern skill writer on their own, and the gateway model client."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from dataextractor_designtime.agents import (
    ExtractionJudge,
    ExtractionJudgeInput,
    PatternSkillWriter,
    PatternSkillWriterInput,
)
from dataextractor_designtime.model.gateway_client import SKILL_TOOL, GatewayModelClient

RUNTIME = Path(__file__).resolve().parents[2] / "runtime"
DICTIONARY = {
    "name": "invoice", "record_key": "invoice_number",
    "fields": [
        {"name": "invoice_number", "type": "string", "required": True, "aliases": ["invoice no"]},
        {"name": "due_date", "type": "date", "description": "Date payment is due.", "aliases": ["due date"]},
        {"name": "vendor", "type": "string", "required": True, "aliases": ["supplier"]},
        {"name": "total_amount", "type": "decimal", "required": True, "aliases": ["total due"]},
        {"name": "line_items", "type": "array", "items": [
            {"name": "description", "type": "string", "aliases": ["description"]},
            {"name": "amount", "type": "decimal", "aliases": ["amount"]}]},
    ],
}


def _output(records, status="extracted", reasons=()):
    return {"status": status, "data": records, "review_reasons": list(reasons),
            "records": [{"review_reasons": list(reasons), "fields": {}} for _ in records]}


def _doc(blocks, tables=()):
    return [{"doc_id": "d1", "source": "file:x.csv", "kind": "csv", "status": "read",
             "blocks": [{"locator": loc, "text": text, "row": row, "col": col, "group": "csv", "vtype": "string"}
                        for loc, text, row, col in blocks],
             "tables": list(tables)}]


# ------------------------------------------------------------------ judge


def test_judge_compares_values_by_type_and_pairs_records_by_key():
    out = _output([{"invoice_number": "B", "total_amount": 20, "due_date": "2026-09-02"},
                   {"invoice_number": "A", "total_amount": "10.50", "due_date": "2026-09-01"}])
    truth = [{"invoice_number": "A", "total_amount": "$10.5", "due_date": "2026-09-01"},
             {"invoice_number": "b", "total_amount": 21}]
    v = ExtractionJudge().run(ExtractionJudgeInput(output=out, dictionary=DICTIONARY, ground_truth=truth))
    assert not v.passed and v.mode == "ground_truth"
    [bad] = v.failures
    assert (bad.kind, bad.field, bad.expected, bad.actual, bad.record) == ("mismatch", "total_amount", 21, 20, 0)
    assert v.score.fields_checked == 5 and v.score.fields_correct == 4
    assert v.failing_fields == ["total_amount"]


def test_judge_line_items_and_missing_records():
    out = _output([{"invoice_number": "A", "line_items": [{"description": "x", "amount": 1}]}])
    truth = [{"invoice_number": "A", "line_items": [{"description": "x", "amount": 2}]},
             {"invoice_number": "Z"}]
    v = ExtractionJudge().run(ExtractionJudgeInput(output=out, dictionary=DICTIONARY, ground_truth=truth))
    kinds = [(f.kind, f.field) for f in v.failures]
    assert ("item_mismatch", "line_items") in kinds and ("missing_record", "invoice_number") in kinds


def test_without_ground_truth_any_review_reason_fails():
    out = _output([{"invoice_number": "A"}], status="review",
                  reasons=["missing_field:vendor", "unverified_value:line_items[0].amount", "low_confidence"])
    v = ExtractionJudge().run(ExtractionJudgeInput(output=out, dictionary=DICTIONARY))
    assert v.mode == "review_status" and not v.passed
    assert v.failing_fields == ["vendor", "line_items"]
    assert v.score.review_failures == 3 and v.score.value_failures == 0


def test_strict_off_ignores_review_when_values_are_right():
    out = _output([{"invoice_number": "A"}], status="review", reasons=["low_confidence"])
    truth = [{"invoice_number": "A"}]
    assert not ExtractionJudge().run(ExtractionJudgeInput(output=out, dictionary=DICTIONARY,
                                                          ground_truth=truth)).passed
    assert ExtractionJudge().run(ExtractionJudgeInput(output=out, dictionary=DICTIONARY,
                                                      ground_truth=truth, strict=False)).passed


# ------------------------------------------------------------------ writer


def _write(**kw):
    base = {"pattern_name": "p", "dictionary": DICTIONARY}
    return PatternSkillWriter().run(PatternSkillWriterInput(**{**base, **kw}))


def test_writer_reads_labels_columns_and_anchors_from_ground_truth():
    evidence = _doc([("A1", "Ref", 1, 1), ("B1", "Payee", 1, 2), ("A2", "RA-1", 2, 1), ("B2", "Hooli", 2, 2),
                     ("A4", "Settle by: 02 Sep 2026", 4, 1),
                     ("A5", "Thank you, kindly remit USD 310.50 promptly", 5, 1)],
                    [{"group": "csv", "header": ["A1", "B1"], "rows": [["A2", "B2"]]}])
    out = _write(ground_truth=[{"invoice_number": "RA-1", "vendor": "Hooli", "due_date": "2026-09-02",
                                "total_amount": "310.50"}], evidence=evidence,
                 failing_fields=["invoice_number", "vendor", "due_date", "total_amount"])
    assert out.hints == {"fields": {"invoice_number": {"labels": ["Ref"]}, "due_date": {"labels": ["Settle by"]},
                                    "vendor": {"labels": ["Payee"]},
                                    "total_amount": {"anchors": ["kindly remit"]}}}
    assert {(d.field, d.basis, d.at) for d in out.new_hints} >= {("invoice_number", "ground_truth", "d1#A2")}
    front = yaml.safe_load(out.markdown.split("---\n")[1])
    assert front["kind"] == "learned-pattern" and front["object"] == "invoice"
    assert front["hints"] == out.hints and front["written_by"] == "stub@0.1.0"
    assert '`vendor` is labelled "Payee".' in out.body


def test_writer_labels_line_item_columns_and_keeps_existing_hints():
    evidence = _doc([("A1", "Service", 1, 1), ("B1", "Fee", 1, 2), ("A2", "Hosting", 2, 1), ("B2", "12.00", 2, 2)],
                    [{"group": "csv", "header": ["A1", "B1"], "rows": [["A2", "B2"]]}])
    out = _write(ground_truth=[{"line_items": [{"description": "Hosting", "amount": 12}]}], evidence=evidence,
                 failing_fields=["line_items"],
                 existing_hints={"fields": {"vendor": {"labels": ["Payee"]}}})
    assert out.hints["fields"]["line_items"] == {"items": {"description": {"labels": ["Service"]},
                                                           "amount": {"labels": ["Fee"]}}}
    assert out.hints["fields"]["vendor"] == {"labels": ["Payee"]}
    assert {d.field for d in out.new_hints} == {"line_items.description", "line_items.amount"}


def test_writer_without_ground_truth_uses_unclaimed_labels_that_fit():
    evidence = _doc([("A1", "Payment deadline: 2026-09-30", None, None), ("A2", "Colour: blue", None, None)])
    out = _write(failing_fields=["due_date", "vendor"], evidence=evidence)
    assert out.hints == {"fields": {"due_date": {"labels": ["Payment deadline"]}}}
    assert out.notes == ["no_candidate:vendor"]


def test_writer_never_steals_another_fields_label():
    evidence = _doc([("A1", "Supplier: Hooli", None, None)])
    out = _write(ground_truth=[{"invoice_number": "Hooli"}], evidence=evidence, failing_fields=["invoice_number"])
    assert out.hints == {}


def test_writer_refuses_a_bad_pattern_name():
    from dataextractor_designtime.agents.base import AgentError
    with pytest.raises(AgentError) as exc:
        _write(pattern_name="../escape")
    assert exc.value.code == "invalid_pattern_name"


# ------------------------------------------------------------------ gateway client


@dataclass
class _Call:
    name: str
    input: dict


@dataclass
class _Response:
    tool_calls: tuple


@pytest.mark.skipif(not (RUNTIME / "extractor_service" / "gateway.py").is_file(), reason="runtime source missing")
def test_gateway_client_writes_the_body_and_its_hints_are_validated():
    seen = []

    def fake_gateway(request):
        seen.append(request)
        return _Response((_Call(SKILL_TOOL, {
            "body": "## Skill: p pattern\n\nThe payee is the vendor.\n",
            "hints": {"fields": {"vendor": {"labels": ["Payee"]}, "nope": {"labels": ["x"]},
                                 "invoice_number": {"labels": ["Supplier"]}}}}),))

    client = GatewayModelClient(RUNTIME, call=fake_gateway)
    out = PatternSkillWriter(model=client).run(PatternSkillWriterInput(
        pattern_name="p", dictionary=DICTIONARY, failing_fields=["vendor"],
        trace={"client": "acme", "usecase": "ap"}))
    assert out.body.startswith("## Skill: p pattern") and out.written_by == "gateway:default"
    # unknown field and a label another field owns are both dropped
    assert out.hints == {"fields": {"vendor": {"labels": ["Payee"]}}}
    [req] = seen
    assert req.model == "default" and req.required_tool == SKILL_TOOL and req.temperature == 0
    assert req.trace == {"client": "acme", "usecase": "ap"}
    assert '"failing_fields": ["vendor"]' in req.messages[0]["content"]
    # tasks it has no prompt for fall back to the stub
    from dataextractor_designtime.model.base import ModelRequest
    assert client.complete(ModelRequest(task="detection_rules.classification_threshold",
                                        evidence={})).produced_by.startswith("stub@")


def test_endpoints_validate_and_answer(client):
    r = client.post("/agents/extraction-judge/run", json={"output": {}, "dictionary": DICTIONARY, "strict": "yes"})
    assert r.status_code == 422
    assert {"loc": "strict", "msg": "expected a boolean, got str"} in r.json()["detail"]["errors"]
    ok = client.post("/agents/extraction-judge/run",
                     json={"output": _output([{"invoice_number": "A"}]), "dictionary": DICTIONARY})
    assert ok.status_code == 200 and ok.json()["passed"] is True
    w = client.post("/agents/pattern-skill-writer/run", json={"pattern_name": "p", "dictionary": DICTIONARY,
                                                              "reference_text": 'vendor: "Payee"'})
    assert w.status_code == 200 and w.json()["hints"] == {"fields": {"vendor": {"labels": ["Payee"]}}}
    spec = client.get("/openapi.json").json()["paths"]
    body = spec["/agents/pattern-skill-writer/run"]["post"]["requestBody"]["content"]["application/json"]
    assert body["schema"]["required"] == ["pattern_name", "dictionary"]
    assert "/learning/runs" in spec

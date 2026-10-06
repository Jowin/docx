"""Deterministic transformation and lookup with ZEN rules: the tool, and the transform step."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from extractor_service.api import create_app
from extractor_tools.common import ToolError
from extractor_tools.rules import RuleSet, check_decision, evaluate
from tests.conftest import ROOT

EXPR = {"passThrough": True, "inputField": None, "outputPath": None, "executionMode": "single"}


def _node(id, type, name, content=None):
    n = {"id": id, "type": type, "name": name, "position": {"x": 0, "y": 0}}
    if content is not None:
        n["content"] = content
    return n


def _chain(*ids):
    return [{"id": f"{a}-{b}", "sourceId": a, "targetId": b, "type": "edge"} for a, b in zip(ids, ids[1:])]


def expressions(**exprs):
    """A decision of one expression node: {key: expression}."""
    return {"nodes": [_node("i", "inputNode", "in"),
                      _node("x", "expressionNode", "x", {"expressions": [{"id": k, "key": k.replace("__", "."),
                                                                         "value": v} for k, v in exprs.items()],
                                                         **EXPR}),
                      _node("o", "outputNode", "out")], "edges": _chain("i", "x", "o")}


def lookup_table(field, rows, out_field=None):
    """A first-hit decision table: value of ``field`` in a row's list -> that row's output."""
    content = {"hitPolicy": "first", "inputs": [{"id": "a", "name": "in", "field": field}],
               "outputs": [{"id": "b", "name": "out", "field": out_field or field}],
               "rules": [{"_id": f"r{i}", "a": ", ".join(json.dumps(k) for k in keys), "b": json.dumps(v)}
                         for i, (keys, v) in enumerate(rows)], **EXPR}
    return {"nodes": [_node("i", "inputNode", "in"), _node("t", "decisionTableNode", "t", content),
                      _node("o", "outputNode", "out")], "edges": _chain("i", "t", "o")}


def calls(key):
    return {"nodes": [_node("i", "inputNode", "in"),
                      _node("d", "decisionNode", "lookup", {"key": key, **EXPR}),
                      _node("o", "outputNode", "out")], "edges": _chain("i", "d", "o")}


# ------------------------------------------------------------------ the tool


def test_a_decision_calls_its_lookup_tables():
    out = evaluate(calls("tables/ccy.json"), {"record": {"currency": "US$"}},
                   tables={"tables/ccy.json": lookup_table("record.currency", [(["US$", "$"], "USD")])})
    assert out["result"]["record"]["currency"] == "USD"


def test_many_inputs_in_one_call_and_errors_stay_per_input():
    out = evaluate(expressions(record__double="record.amount * 2"),
                   contexts=[{"record": {"amount": 2.5}}, {"record": {"amount": "x"}}])
    assert out["results"][0]["result"]["record"]["double"] == 5
    assert "error" in out["results"][1]


def test_amounts_keep_their_digits():
    from decimal import Decimal
    out = evaluate(expressions(record__total="record.amount + 0.01"), {"record": {"amount": Decimal("12345678901234.57")}})
    assert Decimal(repr(out["result"]["record"]["total"])) == Decimal("12345678901234.58")


@pytest.mark.parametrize("decision, why", [
    ({"nodes": [_node("i", "inputNode", "in"), _node("f", "functionNode", "js", {"source": "x"}),
                _node("o", "outputNode", "out")], "edges": []}, "functionNode"),
    (expressions(record__d="date('now')"), "clock"),
    ({"nodes": [], "edges": []}, "input node"),
    ("{not json", "not valid JSON"),
])
def test_only_deterministic_decisions_are_accepted(decision, why):
    with pytest.raises(ToolError) as exc:
        check_decision(decision)
    assert exc.value.code == "rules_invalid" and why in str(exc.value)


def test_a_call_to_a_missing_table_is_refused_at_load():
    with pytest.raises(ToolError, match="not in the rule set"):
        RuleSet({"m.json": calls("tables/nope.json")}, "m.json")


def test_the_tool_endpoint():
    from extractor_tools.api import app as tools_app
    client = TestClient(tools_app)
    r = client.post("/tools/rules", json={"decision": expressions(record__x="upper(record.x)"),
                                          "context": {"record": {"x": "abc"}}, "trace": True})
    assert r.status_code == 200 and r.json()["result"]["record"]["x"] == "ABC" and r.json()["trace"]
    bad = client.post("/tools/rules", json={"decision": {"nodes": [], "edges": []}})
    assert bad.status_code == 422 and bad.json()["error"] == "rules_invalid"


# ------------------------------------------------------------------ the transform step


@pytest.fixture
def app(settings, config_root):
    (config_root / "defaults.json").write_text((ROOT / "configs" / "defaults.json").read_text())
    return lambda: TestClient(create_app(settings, start_workers=False))


BLOTTER = ("Trade Date,Settlement Date,Portfolio,Transaction Type,Security ID,CCY,Net Amount,Purpose Code,Comments\n"
           "2026-10-01,2026-10-03,100234,purchase,US0378331005,USD,\"1,000.00\",,first\n"
           "2026-10-01,2026-09-30,GLB-FI-02,Cash Payment,,EUR,-50.00,,second\n"
           "2026-10-01,2026-10-03,PF-9,SELL,US5949181045,GBP,20.00,INTC,third\n")


def _extract(app, input_root, name="b.csv", body=BLOTTER):
    (input_root / name).write_text(body)
    return app().post("/extract", json={"file_location": name, "extended": True}).json()


def test_the_shipped_settlement_rules_normalise_and_look_up(app, input_root):
    e = _extract(app, input_root)
    first, second, third = e["data"]
    assert (first["transaction_type"], first["currency"], first["portfolio"], first["cash_purpose_code"]) == \
        ("BUY", "USD", "GLB-EQ-01", "SECU")                    # synonym, upper-case, account lookup, derived code
    assert (second["transaction_type"], second["cash_purpose_code"]) == ("CASH OUT", "CASH")
    assert third["cash_purpose_code"] == "INTC"                # a stated code is kept
    rec = e["records"][0]["fields"]
    assert rec["portfolio"]["transform"] == {"rule": "usecase", "from": "100234"}
    assert rec["portfolio"]["source"].endswith("#C2")          # still cites where the account number was
    assert rec["cash_purpose_code"]["grounding"] == "derived"
    assert e["records"][1]["flags"] == ["rule:settles_before_trade"]
    assert e["records"][0]["flags"] == [] and e["records"][2]["flags"] == []
    t = e["metadata"]["transform"]
    assert [l["level"] for l in t["layers"]] == ["usecase"] and t["records_changed"] == 2
    assert t["changes"] == {"transaction_type": 2, "portfolio": 1, "cash_purpose_code": 2}


def test_levels_run_version_global_client_usecase(app, config_root, input_root):
    version = config_root / "default" / "settlements" / "1.0.0" / "rules"
    (version / "transform.decision.json").write_text(json.dumps(expressions(record__comments="'version'")))
    (config_root / "lookups").mkdir()
    (config_root / "lookups" / "transform.decision.json").write_text(
        json.dumps(expressions(record__comments="record.comments + '>global'")))
    (config_root / "default" / "lookups").mkdir()
    (config_root / "default" / "lookups" / "transform.decision.json").write_text(
        json.dumps(expressions(record__comments="record.comments + '>client'")))
    e = _extract(app, input_root)
    assert e["data"][0]["comments"] == "version>global>client"
    assert [l["level"] for l in e["metadata"]["transform"]["layers"]] == ["version", "global", "client", "usecase"]
    assert e["records"][0]["fields"]["comments"]["transform"] == {"rule": "client", "from": "first"}


def test_a_rule_that_fails_on_a_record_keeps_it_and_flags_it(app, config_root, input_root):
    (config_root / "default" / "lookups").mkdir()
    (config_root / "default" / "lookups" / "transform.decision.json").write_text(
        json.dumps(expressions(record__amount="record.amount + record.currency")))
    e = _extract(app, input_root)
    assert len(e["data"]) == 3 and "transform_rule_error:client" in e["flags"]
    assert e["data"][2]["amount"] == 20                         # untouched by the failed level
    assert e["data"][0]["transaction_type"] == "BUY"            # the use case level still ran


def test_a_rule_that_repairs_a_value_clears_its_flag(config_root):
    from types import SimpleNamespace
    from extractor_service import transform
    from extractor_service.config_store import ConfigStore
    (config_root / "default" / "lookups").mkdir()
    (config_root / "default" / "lookups" / "transform.decision.json").write_text(
        json.dumps(lookup_table("record.currency", [(["US DOLLAR", "usd"], "USD")])))
    cfg = ConfigStore(config_root).resolve("default", "settlements")
    rec = {"fields": {"currency": {"value": "usd", "confidence": 0.4, "source": "d1#B2", "error": "pattern"},
                      "amount": {"value": 1, "confidence": 0.9, "source": "d1#C2"}},
           "reasons": ["schema_validation_failed:currency", "missing_field:portfolio"]}
    [out], report = transform.apply(cfg, [rec], SimpleNamespace(sender="a@b.com", subject="s", name="x.csv"), [])
    assert out["fields"]["currency"] == {"value": "USD", "confidence": 0.4, "source": "d1#B2",
                                         "transform": {"rule": "client", "from": "usd"}}
    assert out["reasons"] == ["missing_field:portfolio", "missing_field:settlement_date"]   # re-checked
    assert rec["reasons"] == ["schema_validation_failed:currency", "missing_field:portfolio"]   # input untouched


def test_a_rule_can_fill_a_required_field_and_break_one(app, config_root, input_root):
    (config_root / "default" / "lookups").mkdir()
    (config_root / "default" / "lookups" / "transform.decision.json").write_text(json.dumps(expressions(
        record__portfolio="record.portfolio ?? 'DEFAULT-PF'", record__currency="'dollars'", record__nope="1")))
    body = BLOTTER.splitlines()[0] + "\n2026-10-01,2026-10-03,,BUY,US0378331005,USD,1.00,SECU,x\n"
    e = _extract(app, input_root, body=body)
    [rec] = e["records"]
    assert rec["fields"]["portfolio"]["value"] == "DEFAULT-PF" and "missing_field:portfolio" not in rec["flags"]
    # the client level sets it, the use case level upper-cases it: still not a currency code
    assert rec["fields"]["currency"]["value"] == "DOLLARS" and "schema_validation_failed:currency" in rec["flags"]
    assert e["metadata"]["transform"]["unknown_fields"] == {"nope": 1}


def test_bad_rules_are_a_config_problem(app, config_root, input_root):
    (config_root / "default" / "lookups").mkdir()
    (config_root / "default" / "lookups" / "transform.decision.json").write_text(json.dumps(expressions(
        record__d="now()")))
    e = _extract(app, input_root)
    assert e["data"] == [] and e["flags"] == ["error:config_invalid"]


def test_a_whole_blotter_goes_through_the_rules(app, input_root):
    rows = "".join(f"2026-10-01,2026-10-03,100234,bought,US{i:09d}0,USD,{i}.25,,row {i}\n" for i in range(3000))
    e = _extract(app, input_root, body=BLOTTER.splitlines()[0] + "\n" + rows)
    assert len(e["data"]) == 3000 and e["flags"] == []
    assert {(r["transaction_type"], r["currency"], r["portfolio"], r["cash_purpose_code"]) for r in e["data"]} == \
        {("BUY", "USD", "GLB-EQ-01", "SECU")}
    assert e["metadata"]["timings_ms"]["transform"] < 3000

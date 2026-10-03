"""Pattern learning end to end: the real runtime in a child process, Postgres for the record.

Each test gets its own copy of the runtime's configs and an input folder, and
forces the isolated runtime onto the deterministic stub so results are exact.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

RUNTIME = Path(__file__).resolve().parents[2] / "runtime"

REMIT = ("Our Ref,Bill Date,Payable By,Biller,Amount Payable\n"
         "RA-501,2026-08-03,2026-09-02,Hooli Inc,\"$2,000.00\"\n")
REMIT_TRUTH = {"invoice_number": "RA-501", "invoice_date": "2026-08-03", "due_date": "2026-09-02",
               "vendor": "Hooli Inc", "total_amount": "2000.00"}
STATEMENT = ("Invoice No,Invoice Date,Due Date,Supplier,Total Due\n"
             "INV-1001,2026-08-01,2026-08-31,Acme Corp,\"$1,250.00\"\n")
#: passes on the base; a learned "kindly remit" anchor would read 30 as its total
TERMS = ("Acme Corp payment terms,\n,\nInvoice No: INV-9,\nInvoice Date: 2026-08-01,\n"
         "Supplier: Acme Corp,\n\"Kindly remit: 30 days from invoice date.\",\nTotal Due: $99.00,\n")
#: fails on the base (no total); its total sits after "Kindly remit" in a note
NOTE = ("Invoice No,Invoice Date,Supplier,Notes\n"
        "INV-77,2026-08-03,Globex Ltd,\n"
        ",,,\"Kindly remit $310.50 by month end.\"\n")


@pytest.fixture()
def learn_env(tmp_path, monkeypatch, client):
    if not (RUNTIME / "extractor_service" / "cli.py").is_file():
        pytest.skip("runtime source not next to designtime")
    configs = tmp_path / "configs"
    shutil.copytree(RUNTIME / "configs", configs)
    data = tmp_path / "data"
    data.mkdir()
    for name, text in (("remit.csv", REMIT), ("statement.csv", STATEMENT), ("terms.csv", TERMS),
                       ("note.csv", NOTE)):
        (data / name).write_text(text)
    monkeypatch.setenv("RUNTIME_DIR", str(RUNTIME))
    monkeypatch.setenv("RUNTIME_PYTHON", sys.executable)
    monkeypatch.setenv("RUNTIME_CONFIG_ROOT", str(configs))
    monkeypatch.setenv("LEARNING_INPUT_ROOT", str(data))
    monkeypatch.setenv("LEARNING_MODEL_PROVIDER", "stub")
    monkeypatch.delenv("MODEL_GATEWAY_URL", raising=False)
    return {"configs": configs, "data": data, "client": client}


def _versions(configs: Path, client="default", usecase="invoice") -> list[str]:
    return sorted(p.name for p in (configs / client / usecase).iterdir())


def _extract(env, name: str, version: str) -> dict:
    from dataextractor_designtime.learning import IsolatedRuntime
    rt = IsolatedRuntime(RUNTIME, python=sys.executable, input_root=env["data"], model_provider="stub")
    [res] = rt.run(env["configs"], [{"file_location": name, "client": "default", "usecase": "invoice",
                                     "version": version}])
    return res["output"]


def test_a_sample_that_already_passes_changes_nothing(learn_env):
    r = learn_env["client"].post("/learning/runs", json={"source": "statement.csv",
                                                         "pattern_name": "acme-statement"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["outcome"] == "passed" and body["passed_before"] is True
    assert body["attempts"] == [] and body["result_version"] is None
    assert body["path"] == ["extract_base", "judge_base", "finish"]
    assert body["client"] == "default" and body["object"] == "invoice" and body["base_version"] == "1.0.0"
    assert _versions(learn_env["configs"]) == ["1.0.0"]


def test_ground_truth_failure_learns_a_new_version(learn_env):
    configs = learn_env["configs"]
    base_before = (configs / "default/invoice/1.0.0/manifest.json").read_text()
    r = learn_env["client"].post("/learning/runs", json={
        "source": "remit.csv", "pattern_name": "hooli-remittance", "object": "invoice",
        "client": "default", "usecase": "invoice", "ground_truth": REMIT_TRUTH})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["outcome"] == "learned", body["attempts"]
    assert body["passed_before"] is False and body["passed_after"] is True
    assert body["verdict_before"]["mode"] == "ground_truth"
    assert {f["field"] for f in body["verdict_before"]["failures"] if f["kind"] == "missing_value"} >= \
        {"invoice_number", "vendor"}
    assert body["result_version"] == "1.0.1" and body["published"] is True
    assert body["path"][:6] == ["extract_base", "judge_base", "load_regressions", "write_skill",
                                "build_candidate", "test_candidate"]
    assert body["path"][-2:] == ["publish", "finish"]
    hints = {d["field"]: d["hint"] for d in body["attempts"][0]["new_hints"]}
    assert hints["invoice_number"] == "Our Ref" and hints["vendor"] == "Biller"
    assert body["data"][0]["invoice_number"] == "RA-501" and body["data"][0]["total_amount"] == 2000

    # the new version is on disk, lists the skill, and the runtime extracts with it
    assert _versions(configs) == ["1.0.0", "1.0.1"]
    new = configs / "default/invoice/1.0.1"
    manifest = json.loads((new / "manifest.json").read_text())
    assert manifest["skills"][-1] == "hooli-remittance" and manifest["learned_patterns"] == ["hooli-remittance"]
    skill = (new / "skills/hooli-remittance.md").read_text()
    assert skill.startswith("---\nname: hooli-remittance\nkind: learned-pattern\n")
    assert "## Skill: hooli-remittance pattern" in skill
    assert _extract(learn_env, "remit.csv", "1.0.1")["status"] == "extracted"
    # the base version is untouched
    assert (configs / "default/invoice/1.0.0/manifest.json").read_text() == base_before
    assert not (configs / "default/invoice/1.0.0/skills/hooli-remittance.md").exists()

    # recorded in the registry
    runs = learn_env["client"].get("/learning/runs", params={"pattern_name": "hooli-remittance"}).json()
    assert [(x["outcome"], x["result_version"]) for x in runs] == [("learned", "1.0.1")]
    one = learn_env["client"].get(f"/learning/runs/{body['id']}").json()
    assert one["skill"] == skill and one["ground_truth"] == [REMIT_TRUTH]


def test_without_ground_truth_review_status_is_the_test(learn_env):
    r = learn_env["client"].post("/learning/runs", json={
        "source": "remit.csv", "pattern_name": "hooli-remittance",
        "reference_text": ('invoice_number: "Our Ref"\n- invoice_date = "Bill Date"\n'
                           'due_date: "Payable By"\nThe vendor is the "Biller" column.\n'
                           'total_amount -> "Amount Payable"')})
    body = r.json()
    assert r.status_code == 200, r.text
    assert body["verdict_before"]["mode"] == "review_status"
    assert any(f["reason"] == "missing_field:invoice_number" for f in body["verdict_before"]["failures"])
    assert body["outcome"] == "learned" and body["result_version"] == "1.0.1"
    bases = {d["field"]: d["basis"] for d in body["attempts"][0]["new_hints"]}
    assert bases["vendor"] == "reference_text" and bases["invoice_number"] == "reference_text"
    assert "### Reference" in body["skill"]


def test_each_learning_call_adds_a_patch_version(learn_env):
    c = learn_env["client"]
    first = c.post("/learning/runs", json={"source": "remit.csv", "pattern_name": "hooli-remittance",
                                           "ground_truth": REMIT_TRUTH}).json()
    assert first["result_version"] == "1.0.1"
    again = c.post("/learning/runs", json={"source": "remit.csv", "pattern_name": "hooli-remittance",
                                           "ground_truth": REMIT_TRUTH}).json()
    assert again["outcome"] == "passed" and again["base_version"] == "1.0.1"
    note = c.post("/learning/runs", json={"source": "note.csv", "pattern_name": "globex-note",
                                          "ground_truth": {"invoice_number": "INV-77",
                                                           "total_amount": 310.50}}).json()
    assert note["outcome"] == "learned", note["attempts"]
    assert note["base_version"] == "1.0.1" and note["result_version"] == "1.0.2"
    hint = note["attempts"][0]["new_hints"][0]
    assert (hint["field"], hint["kind"], hint["hint"]) == ("total_amount", "anchor", "kindly remit")
    manifest = json.loads((learn_env["configs"] / "default/invoice/1.0.2/manifest.json").read_text())
    assert manifest["skills"][-2:] == ["hooli-remittance", "globex-note"]


def test_a_candidate_that_breaks_an_earlier_sample_is_rejected(learn_env):
    c = learn_env["client"]
    terms_truth = {"invoice_number": "INV-9", "total_amount": 99}
    ok = c.post("/learning/runs", json={"source": "terms.csv", "pattern_name": "acme-terms",
                                        "ground_truth": terms_truth}).json()
    assert ok["outcome"] == "passed"            # now part of the regression set
    r = c.post("/learning/runs", json={"source": "note.csv", "pattern_name": "globex-note",
                                       "ground_truth": {"invoice_number": "INV-77", "total_amount": 310.50}})
    body = r.json()
    assert body["regression_samples"] == 1
    first = body["attempts"][0]
    assert first["regressions"] == ["terms.csv"] and first["accepted"] is False
    assert first["score"]["value_failures"] == 0          # it did fix its own sample
    assert body["outcome"] == "failed" and body["result_version"] is None
    assert _versions(learn_env["configs"]) == ["1.0.0"]


def test_dry_run_reports_the_version_without_writing_it(learn_env):
    body = learn_env["client"].post("/learning/runs", json={
        "source": "remit.csv", "pattern_name": "hooli-remittance", "ground_truth": REMIT_TRUTH,
        "publish": False}).json()
    assert body["outcome"] == "learned" and body["result_version"] == "1.0.1"
    assert body["published"] is False and body["skill"].startswith("---\n")
    assert _versions(learn_env["configs"]) == ["1.0.0"]


@pytest.mark.parametrize("payload,status,code", [
    ({"source": "remit.csv", "pattern_name": "x", "object": "purchase_order"}, 422, "object_mismatch"),
    ({"source": "remit.csv", "pattern_name": "field-extraction", "ground_truth": REMIT_TRUTH}, 409,
     "skill_conflict"),
    ({"source": "missing.csv", "pattern_name": "x"}, 404, "file_not_found"),
    ({"source": "remit.csv", "pattern_name": "x", "client": "nobody"}, 404, "config_not_found"),
    ({"source": "remit.csv", "pattern_name": "x", "ground_truth": {"colour": "red"}}, 422,
     "ground_truth_invalid"),
    ({"source": "remit.csv", "pattern_name": "bad name"}, 422, "invalid_input"),
    ({"source": "remit.csv"}, 422, "invalid_input"),
    ({"source": "remit.csv", "pattern_name": "x", "max_iterations": 0}, 422, "invalid_input"),
])
def test_refusals_are_typed(learn_env, payload, status, code):
    r = learn_env["client"].post("/learning/runs", json=payload)
    assert r.status_code == status, r.text
    assert r.json()["detail"]["code"] == code
    assert _versions(learn_env["configs"]) == ["1.0.0"]


def test_learning_is_a_langgraph_graph():
    from dataextractor_designtime.learning import build_learning_graph
    graph = build_learning_graph(runtime=None, store=None, model=None, config_root=Path("."))
    nodes = set(graph.get_graph().nodes)
    assert {"extract_base", "judge_base", "load_regressions", "write_skill", "build_candidate",
            "test_candidate", "publish", "finish"} <= nodes

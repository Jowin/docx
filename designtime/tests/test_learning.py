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
    return sorted(p.name for p in (configs / client / usecase).iterdir() if p.is_dir() and p.name[0].isdigit())


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
    # a candidate: what was served is pinned, and the new version waits for a release
    releases = json.loads((configs / "default/invoice/releases.json").read_text())
    assert releases["active"] == "1.0.0" and releases["history"][0]["action"] == "adopt"
    record = learn_env["client"].get("/configs/default/invoice/1.0.1").json()
    assert record["status"] == "candidate" and record["provenance"]["origin"] == "learning"
    assert record["record"]["origin"] == "learning" and record["record"]["gates_passed"] is True
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


def test_without_ground_truth_the_flags_are_the_test(learn_env):
    r = learn_env["client"].post("/learning/runs", json={
        "source": "remit.csv", "pattern_name": "hooli-remittance",
        "reference_text": ('invoice_number: "Our Ref"\n- invoice_date = "Bill Date"\n'
                           'due_date: "Payable By"\nThe vendor is the "Biller" column.\n'
                           'total_amount -> "Amount Payable"')})
    body = r.json()
    assert r.status_code == 200, r.text
    assert body["verdict_before"]["mode"] == "flags"
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


def test_a_global_candidate_that_breaks_an_earlier_sample_is_rejected_and_remembered(learn_env):
    c = learn_env["client"]
    terms_truth = {"invoice_number": "INV-9", "total_amount": 99}
    ok = c.post("/learning/runs", json={"source": "terms.csv", "pattern_name": "acme-terms",
                                        "ground_truth": terms_truth}).json()
    assert ok["outcome"] == "passed"            # now part of the regression set
    note = {"source": "note.csv", "pattern_name": "globex-note", "scope": "global",
            "ground_truth": {"invoice_number": "INV-77", "total_amount": 310.50}}
    body = c.post("/learning/runs", json=note).json()
    assert body["regression_samples"] == 1
    first = body["attempts"][0]
    assert first["regressions"] == ["terms.csv"] and first["accepted"] is False
    assert first["score"]["value_failures"] == 0          # it did fix its own sample
    assert body["outcome"] == "failed" and body["result_version"] is None
    assert body["rejected_hints"] == ["total_amount:anchor:kindly remit"]
    assert _versions(learn_env["configs"]) == ["1.0.0"]
    # memory: the harmful anchor is never proposed again, in this call or the next
    mem = c.get("/learning/memory", params={"client": "default", "usecase": "invoice"}).json()
    assert [(h["field"], h["kind"], h["hint"]) for h in mem["rejected_hints"]] == \
        [("total_amount", "anchors", "kindly remit")]
    assert mem["rejected_hints"][0]["reason"] == "regression:terms.csv"
    again = c.post("/learning/runs", json=note).json()
    assert again["outcome"] == "failed" and again["attempts"][0]["new_hints"] == []
    assert "avoided:total_amount:anchor:kindly remit" in again["attempts"][0]["notes"]


def test_pattern_scope_keeps_hints_away_from_other_layouts(learn_env):
    c = learn_env["client"]
    assert c.post("/learning/runs", json={"source": "terms.csv", "pattern_name": "acme-terms",
                                          "ground_truth": {"invoice_number": "INV-9", "total_amount": 99}}
                  ).json()["outcome"] == "passed"
    body = c.post("/learning/runs", json={"source": "note.csv", "pattern_name": "globex-note",
                                          "ground_truth": {"invoice_number": "INV-77", "total_amount": 310.50}}
                  ).json()
    assert body["outcome"] == "learned", body["attempts"]
    assert body["attempts"][0]["regressions"] == []
    assert body["applies_to"]["headers"] == ["Invoice No", "Invoice Date", "Supplier", "Notes"]
    assert "applies_to:" in body["skill"]
    # the published version serves both: the note gets its total, the terms keep theirs
    assert _extract(learn_env, "note.csv", "1.0.1")["data"][0]["total_amount"] == 310.5
    terms = _extract(learn_env, "terms.csv", "1.0.1")
    assert terms["data"][0]["total_amount"] == 99 and "globex-note" not in terms["metadata"]["skills_applied"]
    mem = c.get("/learning/memory", params={"client": "default", "usecase": "invoice"}).json()
    [pattern] = [p for p in mem["patterns"] if p["name"] == "globex-note"]
    assert pattern["versions"] == ["1.0.1"] and pattern["applies_to"]["headers"][0] == "Invoice No"


class FlakyRuntime:
    """Delegates to the real isolated runtime, but dies on one chosen call (a lost process)."""

    def __init__(self, real, die_on: int) -> None:
        self.real, self.die_on, self.calls = real, die_on, 0

    def run(self, *a, **k):
        self.calls += 1
        if self.calls == self.die_on:
            raise RuntimeError("worker lost")
        return self.real.run(*a, **k)


def test_an_interrupted_learning_call_resumes_from_its_checkpoint(learn_env):
    from dataextractor_designtime.learning import IsolatedRuntime
    from dataextractor_designtime.main import app
    real = IsolatedRuntime(RUNTIME, python=sys.executable, input_root=learn_env["data"], model_provider="stub")
    flaky = FlakyRuntime(real, die_on=3)            # base run, regression set, then dies testing the candidate
    app.state.isolated_runtime = flaky
    try:
        with pytest.raises(RuntimeError):
            learn_env["client"].post("/learning/runs", json={"source": "remit.csv", "pattern_name": "hooli-remittance",
                                                             "ground_truth": REMIT_TRUTH})
        [run] = learn_env["client"].get("/learning/runs").json()
        assert run["outcome"] == "interrupted"
        assert learn_env["client"].get(f"/learning/runs/{run['id']}").json()["error"]["message"] == "worker lost"
        r = learn_env["client"].post(f"/learning/runs/{run['id']}/resume")
    finally:
        app.state.isolated_runtime = None
    body = r.json()
    assert r.status_code == 200, body
    assert body["outcome"] == "learned" and body["result_version"] == "1.0.1"
    assert flaky.calls == 4                         # resumed at test_candidate: nothing before it ran again
    assert body["path"].count("extract_base") == 1 and body["path"].count("write_skill") == 1
    assert learn_env["client"].post(f"/learning/runs/{run['id']}/resume").json()["detail"]["code"] == "run_finished"


def test_reviewer_corrections_become_ground_truth(learn_env):
    item = {"job_id": "job_abc", "audit_id": "run_1", "client": "default", "usecase": "invoice",
            "file_location": "remit.csv", "ground_truth": [REMIT_TRUTH], "sender": "billing@hooli.example",
            "reviewer": "maya", "corrected_fields": [{"record": 0, "field": "invoice_number"}]}
    c = learn_env["client"]
    first = c.post("/learning/corrections", json={"items": [item]}).json()["results"]
    assert first == [{"job_id": "job_abc", "status": "learned_from", "pattern_name": "corrected-hooli-example",
                      "learning_run": first[0]["learning_run"], "outcome": "learned", "result_version": "1.0.1",
                      "error": None}]
    run = c.get(f"/learning/runs/{first[0]['learning_run']}").json()
    assert run["requested_by"] == "review:maya" and run["ground_truth"] == [REMIT_TRUTH]
    again = c.post("/learning/corrections", json={"items": [item]}).json()["results"]
    assert again[0]["status"] == "already_imported" and _versions(learn_env["configs"]) == ["1.0.0", "1.0.1"]


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

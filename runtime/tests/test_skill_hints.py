"""Learned skills: front-matter hints fold into the dictionary and steer the stub."""
import json
import shutil

import pytest

from extractor_service.cli import run_batch
from extractor_service.config_store import ConfigStore, parse_skill
from extractor_service.errors import ConfigError

REMITTANCE = ("Our Ref,Bill Date,Payable By,Biller,Amount Payable\n"
              "RA-501,2026-08-03,2026-09-02,Hooli Inc,\"$2,000.00\"\n")

SKILL = """---
name: hooli-remittance
kind: learned-pattern
hints:
  fields:
    invoice_number: {labels: ["Our Ref"]}
    invoice_date: {labels: ["Bill Date"]}
    due_date: {labels: ["Payable By"]}
    vendor: {labels: ["Biller"]}
    total_amount: {labels: ["Amount Payable"], anchors: ["kindly remit"]}
---
## Skill: Hooli remittance advice

"Our Ref" is the invoice number.
"""


def _learned_version(config_root, skill=SKILL, name="hooli-remittance"):
    base = config_root / "default" / "invoice" / "1.0.0"
    new = base.parent / "1.0.1"
    shutil.copytree(base, new)
    (new / "skills" / f"{name}.md").write_text(skill)
    manifest = json.loads((new / "manifest.json").read_text())
    manifest["version"] = "1.0.1"
    manifest["skills"].append(name)
    (new / "manifest.json").write_text(json.dumps(manifest))
    return new


def test_front_matter_is_split_from_the_body():
    s = parse_skill("x", SKILL)
    assert s.body.startswith("## Skill: Hooli")
    assert s.hints["fields"]["vendor"] == {"labels": ["Biller"]}
    assert parse_skill("y", "## plain\n").meta == {}


def test_hints_become_aliases_and_anchors(config_root):
    _learned_version(config_root)
    cfg = ConfigStore(config_root).resolve("default", "invoice", "1.0.1")
    assert "Our Ref" in cfg.dictionary.get("invoice_number").aliases
    assert cfg.dictionary.get("total_amount").anchors == ("kindly remit",)
    assert all("---" not in s.body for s in cfg.skills)
    old = ConfigStore(config_root).resolve("default", "invoice", "1.0.0")
    assert "Our Ref" not in old.dictionary.get("invoice_number").aliases


def test_learned_labels_turn_review_into_extracted(stub_client, config_root, input_root):
    (input_root / "remit.csv").write_text(REMITTANCE)
    before = stub_client.post("/extract", json={"file_location": "remit.csv", "extended": True,
                                                "version": "1.0.0"}).json()
    assert before["flagged"] is True
    _learned_version(config_root)
    after = stub_client.post("/extract", json={"file_location": "remit.csv", "extended": True,
                                               "version": "1.0.1"}).json()
    assert after["flagged"] is False, after["flags"]
    rec = after["data"][0]
    assert rec["invoice_number"] == "RA-501" and rec["vendor"] == "Hooli Inc"
    assert rec["total_amount"] == 2000 and rec["due_date"] == "2026-09-02"


def test_anchor_reads_a_value_mid_sentence(stub_client, config_root, input_root):
    (input_root / "note.csv").write_text(
        "Our Ref,Bill Date,Biller\nRA-777,2026-08-03,Hooli Inc\n,,\n"
        "\"Kindly remit $310.50 by month end.\",,\n")
    _learned_version(config_root)
    e = stub_client.post("/extract", json={"file_location": "note.csv", "extended": True,
                                           "version": "1.0.1"}).json()
    total = e["records"][0]["fields"]["total_amount"]
    assert total["value"] == 310.5 and total["grounding"] == "verified"


@pytest.mark.parametrize("hints,msg", [
    ("hints: {fields: {nope: {labels: [x]}}}", "unknown field"),
    ("hints: {fields: {vendor: {colour: [x]}}}", "unknown hint keys"),
    ("hints: {fields: {vendor: {labels: x}}}", "list of strings"),
    ("hints: [1, 2", "not valid YAML"),
])
def test_bad_hints_are_config_errors(config_root, hints, msg):
    _learned_version(config_root, f"---\n{hints}\n---\nbody\n")
    with pytest.raises(ConfigError) as exc:
        ConfigStore(config_root).resolve("default", "invoice", "1.0.1")
    assert msg in str(exc.value)


def test_cli_batch_returns_outputs_errors_and_evidence(settings, input_root):
    out = run_batch({"runs": [{"file_location": "invoice.pdf"}, {"file_location": "missing.csv"}],
                     "include_evidence": True}, settings)
    ok, bad = out["results"]
    assert ok["ok"] and ok["output"]["data"][0]["invoice_number"] == "INI-0042"
    assert ok["dictionary"]["record_key"] == "invoice_number"
    assert ok["config"]["version"] == "1.0.0" and ok["skills"] == ["field-extraction", "table-extraction"]
    doc = ok["evidence"][0]
    assert doc["source"] == "file:invoice.pdf" and any("INI-0042" in b["text"] for b in doc["blocks"])
    assert bad["ok"] is False and bad["error"] == {"error": "file_not_found", "message": "no file at missing.csv",
                                                   "detail": {"file_location": "missing.csv"}, "status": 404}
    assert bad["output"]["flags"] == ["error:file_not_found"]          # still a result


def test_cli_process(settings):
    import os
    import subprocess
    import sys
    from pathlib import Path
    here = str(Path(__file__).parents[1])
    env = {**os.environ, "CONFIG_ROOT": str(settings.config_root), "INPUT_ROOT": str(settings.input_root),
           "MODEL_PROVIDER": "stub"}
    p = subprocess.run([sys.executable, "-m", "extractor_service.cli"], input=json.dumps(
        {"runs": [{"file_location": "statement.csv", "client": "acme", "version": "1.0.0"}]}),
        capture_output=True, text=True, env=env, cwd=here)
    assert p.returncode == 0, p.stderr
    [res] = json.loads(p.stdout)["results"]
    assert res["output"]["metadata"]["model"]["provider"] == "stub"
    assert res["output"]["data"][0]["po_number"] == "PO-7700"
    bad = subprocess.run([sys.executable, "-m", "extractor_service.cli"], input="[]", capture_output=True,
                         text=True, env=env, cwd=here)
    assert bad.returncode == 2 and json.loads(bad.stdout)["error"] == "bad_request"

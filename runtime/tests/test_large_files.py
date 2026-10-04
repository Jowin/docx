"""Large inputs: the readers' per-attachment guard follows the config, not a fixed 10 MB."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from extractor_service.api import create_app

HEADER = "Invoice No,Invoice Date,Due Date,Supplier,Total Due\n"


def _big_csv(path, mb: float) -> None:
    with open(path, "w") as f:
        f.write(HEADER)
        i = 0
        while f.tell() < mb * 1024 * 1024:
            f.write(f"INV-{i},2026-08-01,2026-08-31,Acme Corp,{i}.00\n")
            i += 1


def _run(settings, name):
    return TestClient(create_app(settings, start_workers=False)).post(
        "/extract", json={"file_location": name, "extended": True}).json()


def test_an_attachment_over_10_mb_is_read(settings, input_root):
    _big_csv(input_root / "big.csv", 11)
    e = _run(settings, "big.csv")
    [doc] = e["metadata"]["documents"]
    assert doc["status"] == "read" and any(n.startswith("rows_truncated:") for n in doc["notes"])
    assert e["data"] and e["data"][0]["invoice_number"] == "INV-0"


def test_the_config_sets_the_attachment_guard(settings, config_root, input_root):
    m = config_root / "default/invoice/1.0.0/manifest.json"
    manifest = json.loads(m.read_text())
    manifest["evidence"]["max_attachment_mb"] = 1
    m.write_text(json.dumps(manifest))
    _big_csv(input_root / "big.csv", 2)
    e = _run(settings, "big.csv")
    [doc] = e["metadata"]["documents"]
    assert doc["status"] == "failed" and doc["notes"][0].startswith("input_too_large")
    assert "attachment_parse_failed:big.csv" in e["flags"]

"""Every finished job is written as files under OUTPUT_ROOT: CSV by default."""
from __future__ import annotations

import csv
import io
import json
from dataclasses import replace

from fastapi.testclient import TestClient

from extractor_service.api import create_app


def _app(settings):
    return create_app(settings, start_workers=False)


def test_a_csv_is_written_by_default(settings, tmp_path):
    s = replace(settings, output_root=tmp_path / "out")
    r = TestClient(_app(s)).post("/extract", json={"file_location": "statement.csv", "extended": True})
    outs = r.json()["metadata"]["outputs"]
    assert [o["format"] for o in outs] == ["csv"]
    path = tmp_path / "out" / outs[0]["path"]
    assert path.parts[-4:-1][:2] == ("default", "invoice") and path.suffix == ".csv"
    rows = list(csv.reader(io.StringIO(path.read_text(encoding="utf-8-sig"))))
    assert rows[0][0] == "_record" and "invoice_number" in rows[0] and len(rows) > 1
    assert r.headers.get("X-Output-Files") is None or r.headers["X-Output-Files"].endswith(".csv")


def test_the_request_picks_the_formats(settings, tmp_path):
    s = replace(settings, output_root=tmp_path / "out")
    app = TestClient(_app(s))
    r = app.post("/extract", json={"file_location": "invoice.pdf", "extended": True,
                                   "output_formats": ["csv", "excel", "docx", "pdf"]})
    outs = r.json()["metadata"]["outputs"]
    assert [o["format"] for o in outs] == ["csv", "xlsx", "docx", "pdf"]
    assert all((tmp_path / "out" / o["path"]).stat().st_size == o["bytes"] for o in outs)
    none = app.post("/extract", json={"file_location": "invoices.csv", "extended": True, "output_formats": []})
    assert none.json()["metadata"]["outputs"] == []
    bad = app.post("/extract", json={"file_location": "invoice.pdf", "output_formats": ["rtf"]})
    assert bad.status_code == 422


def test_the_config_can_set_the_formats(settings, config_root, tmp_path):
    m = config_root / "default/invoice/1.0.0/manifest.json"
    manifest = json.loads(m.read_text())
    manifest["output"]["formats"] = ["xlsx"]
    m.write_text(json.dumps(manifest))
    s = replace(settings, output_root=tmp_path / "out")
    r = TestClient(_app(s)).post("/extract", json={"file_location": "statement.csv", "extended": True})
    assert [o["format"] for o in r.json()["metadata"]["outputs"]] == ["xlsx"]


def test_a_failed_run_still_writes_and_any_format_downloads(settings, tmp_path):
    s = replace(settings, output_root=tmp_path / "out")
    app = TestClient(_app(s))
    r = app.post("/extract", json={"file_location": "missing.eml", "extended": True})
    assert r.json()["flags"] == ["error:file_not_found"]
    assert r.json()["metadata"]["outputs"][0]["path"].startswith("_unresolved/_unresolved/")
    job = r.headers["X-Job-Id"]
    for fmt, magic in (("csv", b"\xef\xbb\xbf"), ("xlsx", b"PK"), ("docx", b"PK"), ("pdf", b"%PDF")):
        d = app.get(f"/extractions/{job}/output/{fmt}")
        assert d.status_code == 200 and d.content.startswith(magic), fmt
    assert app.get(f"/extractions/{job}/output/rtf").status_code == 400


def test_no_output_root_writes_nothing(settings):
    r = TestClient(_app(settings)).post("/extract", json={"file_location": "statement.csv", "extended": True})
    assert "outputs" not in r.json()["metadata"]

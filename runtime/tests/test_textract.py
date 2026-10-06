"""OCR through Textract (a stand-in client: the real service is not reachable from tests), and long PDFs."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from extractor_service import ocr
from extractor_service.api import create_app
from tests.conftest import ROOT
from tests.pdf_fixture import HEADER, FakeTextract, settlement_report, textract_blocks


@pytest.fixture(scope="module")
def report():
    return settlement_report(60, scan_every=25)          # 2 scanned pages, a continuation every 7th


@pytest.fixture
def app(settings, config_root, input_root, report):
    (config_root / "defaults.json").write_text((ROOT / "configs" / "defaults.json").read_text())
    (input_root / "report.pdf").write_bytes(report["pdf"])
    return TestClient(create_app(settings, start_workers=False))


def _textract(monkeypatch, fake, s3=None):
    monkeypatch.setenv("OCR_ENGINE", "textract")
    ocr.set_textract_client_factory(lambda: fake, (lambda: s3) if s3 else None)


def _run(app):
    return app.post("/extract", json={"file_location": "report.pdf", "extended": True}).json()


# ------------------------------------------------------------------ parsing Textract's answer


def test_textract_blocks_become_lines_and_a_table():
    rows = [["2026-10-03", "2026-10-01", "US0378331005", "BUY", "USD", "1,000.00", "PF-01", "SECU", "a b"]]
    page = ocr.parse_blocks(textract_blocks(rows, True, conf=lambda r, c: 90.0 if c == 6 else 99.0))[1]
    [table] = page.tables
    assert [t for t, _ in table.rows[0]] == HEADER
    assert table.rows[1][5] == ("1,000.00", 0.9) and table.rows[1][8] == ("a b", 0.99)
    assert page.lines[0].text.startswith("Settlement instructions")


def test_textract_unavailable_without_credentials_falls_back(monkeypatch):
    monkeypatch.setenv("OCR_ENGINE", "textract")
    monkeypatch.delenv("TEXTRACT_REGION", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    assert ocr.TextractEngine().available()                  # no region: says why
    assert ocr.available() == ocr.tesseract_available()      # the fallback still reads scans


# ------------------------------------------------------------------ a long report, end to end


def test_every_row_of_a_long_report_is_read_including_scanned_pages(app, report, monkeypatch):
    fake = FakeTextract(report)
    _textract(monkeypatch, fake)
    e = _run(app)
    assert e["flags"] == [] and len(e["data"]) == len(report["expected"]) == 56 * 25
    assert e["data"] == report["expected"]                   # every value, in page order
    [rep] = e["metadata"]["expanded"]
    assert rep["complete"] and "table_not_rows:p2:T1" in rep["notes"]     # the summary is not instructions
    assert "ocr_engine:textract:2" in rep["notes"]
    scanned = sorted(report["scanned"])[0]
    i = next(i for i, r in enumerate(e["records"]) if f"#p{scanned}:OT1:" in r["fields"]["amount"]["source"])
    assert 0.8 < e["records"][i]["fields"]["amount"]["confidence"] < 1.0


def test_misread_cells_are_flagged_not_passed(app, report, monkeypatch):
    s1, s2 = sorted(report["scanned"])
    fake = FakeTextract(report, errors={(s1, 3, "Net Amount"): "1,2O4.5O", (s2, 0, "Security ID"): "US12345678"})
    _textract(monkeypatch, fake)
    e = _run(app)
    flagged = [(i, r["flags"]) for i, r in enumerate(e["records"]) if r["flags"]]
    assert len(flagged) == 2
    assert "schema_validation_failed:amount" in flagged[0][1]
    assert flagged[1][1] == ["low_ocr_confidence:security_id"]   # a plausible misread, read at 41%
    assert len(e["data"]) == len(report["expected"])


def test_a_textract_failure_on_a_page_falls_back_and_says_so(app, report, monkeypatch):
    fake = FakeTextract(report, fail_pages={sorted(report["scanned"])[0]})
    _textract(monkeypatch, fake)
    e = _run(app)
    assert "ocr_fallback:report.pdf" in e["flags"]
    [rep] = e["metadata"]["expanded"]
    assert any(n.startswith("ocr_fallback:textract->tesseract:") for n in rep["notes"])
    # tesseract reads lines, not tables: the scanned page's rows are not read, and the run says so
    assert "content_truncated:report.pdf" in e["flags"] and len(e["data"]) == len(report["expected"]) - 25


def test_without_ocr_scanned_rows_are_flagged_missing(app, report, monkeypatch):
    monkeypatch.setenv("OCR_ENGINE", "textract")
    monkeypatch.setenv("OCR_FALLBACK", "none")
    monkeypatch.setattr(ocr.TextractEngine, "available", lambda self: "no AWS credentials")
    e = _run(app)
    assert "content_truncated:report.pdf" in e["flags"]
    [rep] = e["metadata"]["expanded"]
    assert any(n.startswith("pages_unread:no_ocr:") for n in rep["notes"]) and not rep["complete"]


class FakeS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, *, Bucket, Key, Body):
        self.objects[(Bucket, Key)] = Body

    def delete_object(self, *, Bucket, Key):
        self.objects.pop((Bucket, Key), None)


class FakeAsyncTextract:
    """start/get_document_analysis over the pages uploaded to FakeS3, two pages of results per call."""

    def __init__(self, report, s3):
        self.report, self.s3, self.jobs, self.uploaded = report, s3, {}, []

    def start_document_analysis(self, *, DocumentLocation, FeatureTypes):
        import pypdfium2 as pdfium
        obj = DocumentLocation["S3Object"]
        body = self.s3.objects[(obj["Bucket"], obj["Name"])]
        self.uploaded.append(len(pdfium.PdfDocument(body)))
        scans = sorted(self.report["scanned"])
        pages = [textract_blocks(self.report["scanned"][p]["rows"], self.report["scanned"][p]["header"], page=i)
                 for i, p in enumerate(scans, start=1)]
        self.jobs["j1"] = {"polls": 0, "pages": pages}
        return {"JobId": "j1"}

    def get_document_analysis(self, *, JobId, NextToken=None):
        job = self.jobs[JobId]
        job["polls"] += 1
        if job["polls"] == 1:
            return {"JobStatus": "IN_PROGRESS"}
        i = int(NextToken or 0)
        return {"JobStatus": "SUCCEEDED", "Blocks": job["pages"][i],
                **({"NextToken": str(i + 1)} if i + 1 < len(job["pages"]) else {})}


def test_async_textract_sends_only_the_scanned_pages(app, settings, report, monkeypatch):
    from dataclasses import replace
    app = TestClient(create_app(replace(settings, parse_sandbox="off"), start_workers=False))  # see the fake's calls
    s3 = FakeS3()
    fake = FakeAsyncTextract(report, s3)
    _textract(monkeypatch, fake, s3)
    monkeypatch.setenv("TEXTRACT_S3_BUCKET", "inbox")
    monkeypatch.setenv("TEXTRACT_ASYNC_MIN_PAGES", "2")
    monkeypatch.setattr(ocr.time, "sleep", lambda s: None)
    e = _run(app)
    assert fake.uploaded == [2] and s3.objects == {}          # 2 of 60 pages billed; the upload is removed
    assert e["flags"] == [] and e["data"] == report["expected"]

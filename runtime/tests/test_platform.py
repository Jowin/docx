"""The platform around the pipeline: ingestion filter, jobs, webhooks, polling, idempotency,
dead letters, checkpoint resume, review and corrections, audit chain, metrics, encryption,
threads, DOCX, OCR, sandbox limits, skill fingerprints, cost and run ceilings."""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import shutil
import threading
import zipfile
from dataclasses import replace
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest
from fastapi.testclient import TestClient

from extractor_service import ocr
from extractor_service.api import create_app
from extractor_service.jobs import sign
from tests.conftest import data_of
from tests.samples import invoice_pdf, invoice_xlsx, statement_csv

INVOICE_ROWS = "Invoice No,Invoice Date,Due Date,Supplier,Total Due\nINV-1,2026-08-01,2026-08-31,Acme Corp,$10.00\n"


# ------------------------------------------------------------------ helpers


class Hooks:
    """A fake webhook HTTP client: records every POST, answers with the next status in line."""

    def __init__(self, *statuses: int) -> None:
        self.calls: list[dict] = []
        self.statuses = list(statuses) or [200]

    def post(self, url, content, headers):
        self.calls.append({"url": url, "body": json.loads(content), "raw": content, "headers": headers})
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return httpx.Response(status, text="ok" if status < 300 else "nope")


def _app(settings, **kw):
    return create_app(settings, start_workers=False, **kw)


def _email(body: str, *attachments: tuple[str, bytes], subject: str = "Invoice", sender="ap@hooli.example") -> bytes:
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, sender, "ap@buyer.example"
    msg.set_content(body)
    for name, data in attachments:
        msg.add_attachment(data, maintype="application", subtype="octet-stream", filename=name)
    return bytes(msg)


def _lookups(config_root, data: dict | None = None, decision: dict | None = None, where: str = ""):
    folder = (config_root / where / "lookups") if where else config_root / "lookups"
    folder.mkdir(parents=True, exist_ok=True)
    if data is not None:
        (folder / "ingestion.json").write_text(json.dumps(data))
    if decision is not None:
        (folder / "ingestion.decision.json").write_text(json.dumps(decision))


def _decision(rules: list[tuple[str, str, str]]) -> dict:
    """A ZEN decision table over (name, size): rows of (name expr, size expr, rule)."""
    return {"nodes": [
        {"id": "in", "type": "inputNode", "name": "item", "position": {"x": 0, "y": 0}},
        {"id": "t", "type": "decisionTableNode", "name": "ingestion", "position": {"x": 1, "y": 0},
         "content": {"hitPolicy": "first",
                     "inputs": [{"id": "i1", "name": "Name", "field": "name"},
                                {"id": "i2", "name": "Size", "field": "size"}],
                     "outputs": [{"id": "o1", "name": "Action", "field": "action"},
                                 {"id": "o2", "name": "Rule", "field": "rule"}],
                     "rules": [{"_id": f"r{n}", "i1": a, "i2": b, "o1": '"ignore"', "o2": f'"{rule}"'}
                               for n, (a, b, rule) in enumerate(rules)]
                     + [{"_id": "keep", "i1": "", "i2": "", "o1": '"keep"', "o2": '""'}]}},
        {"id": "out", "type": "outputNode", "name": "decision", "position": {"x": 2, "y": 0}}],
        "edges": [{"id": "e1", "sourceId": "in", "targetId": "t", "type": "edge"},
                  {"id": "e2", "sourceId": "t", "targetId": "out", "type": "edge"}]}


# ------------------------------------------------------------------ ingestion filter


def test_filter_ignores_attachments_by_name_and_hash(settings, config_root, input_root):
    docusign = b"%PDF-1.4 not really a pdf, a DocuSign certificate"
    noise = b"Ref,Note\nx,boilerplate every email carries\n"
    _lookups(config_root, {"ignore_names": ["docusign"],
                           "ignore_hashes": [hashlib.sha256(noise).hexdigest()]})
    (input_root / "m.eml").write_bytes(_email("Invoice attached.", ("Invoice.xlsx", invoice_xlsx()),
                                              ("Summary_DocuSign.pdf", docusign), ("footer.csv", noise)))
    e = TestClient(_app(settings)).post("/extract", json={"file_location": "m.eml", "extended": True}).json()
    skipped = {s["item"]: s for s in e["metadata"]["skipped"] if s["reason"] == "ignored_by_filter"}
    assert skipped["Summary_DocuSign.pdf"]["rule"] == "global:name:docusign"
    assert skipped["footer.csv"]["rule"].startswith("global:sha256:")
    assert [d["source"] for d in e["metadata"]["documents"]] == ["body", "attachment:Invoice.xlsx"]
    assert not any(f.startswith("attachment_parse_failed") for f in e["flags"])     # ignoring is not a fault
    assert e["data"][0]["invoice_number"] == "INV-20194"


def test_filter_rules_as_a_zen_decision_per_config(settings, config_root, input_root):
    _lookups(config_root, decision=_decision([('startsWith(lower($), "signed_")', "", "signed-copies"),
                                             ("", "> 500000", "too-big")]), where="default/invoice/1.0.0")
    (input_root / "m.eml").write_bytes(_email("x", ("Signed_contract.xlsx", invoice_xlsx()),
                                              ("invoice.xlsx", invoice_xlsx())))
    e = TestClient(_app(settings)).post("/extract", json={"file_location": "m.eml", "extended": True}).json()
    assert {"item": "Signed_contract.xlsx", "reason": "ignored_by_filter",
            "rule": "config:decision:signed-copies"} in e["metadata"]["skipped"]
    assert [d["source"] for d in e["metadata"]["documents"]] == ["body", "attachment:invoice.xlsx"]


def test_an_ignored_input_file_is_a_flagged_empty_result(settings, config_root, input_root):
    _lookups(config_root, {"ignore_names": ["*.csv"]})
    e = TestClient(_app(settings)).post("/extract", json={"file_location": "statement.csv"}).json()
    assert e["flagged"] is True and e["data"] == [] and e["flags"] == ["input_ignored:global:name:*.csv"]


def test_zip_members_and_regex_names(settings, config_root, input_root):
    _lookups(config_root, {"ignore_names": ["re:^copy of "]})
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Copy of statement.csv", statement_csv())
        z.writestr("statement.csv", statement_csv())
    (input_root / "b.zip").write_bytes(buf.getvalue())
    e = TestClient(_app(settings)).post("/extract", json={"file_location": "b.zip", "extended": True}).json()
    assert [d["source"] for d in e["metadata"]["documents"]] == ["file:b.zip/statement.csv"]


def test_bad_lookup_data_is_a_config_problem(settings, config_root):
    _lookups(config_root, {"ignore_hashes": ["not-a-hash"]})
    health = TestClient(_app(settings)).get("/health").json()
    assert health["status"] == "degraded"
    assert "not a sha256" in health["config_problems"][0]["message"]


# ------------------------------------------------------------------ async, webhook, polling


def test_async_job_is_inprogress_then_extracted_when_polled(settings):
    app = _app(settings)
    c = TestClient(app)
    r = c.post("/extract", json={"file_location": "statement.csv", "async": True})
    assert r.status_code == 202 and r.headers["x-extraction-status"] == "inprogress"
    job = r.json()["job_id"]
    poll = c.get(f"/extractions/{job}")
    assert poll.json()["status"] == "inprogress" and "result" not in poll.json()
    app.state.runner.drain()
    done = c.get(f"/extractions/{job}")
    assert done.headers["x-extraction-status"] == "extracted"
    body = done.json()
    assert body["status"] == "extracted" and body["flagged"] is False
    assert body["result"][0]["invoice_number"] == "INV-30001"          # clean: plain data


def test_webhook_gets_the_result_signed(settings):
    hooks = Hooks(200)
    app = _app(replace(settings, webhook_secret="topsecret"), http=hooks)
    c = TestClient(app)
    r = c.post("/extract", json={"file_location": "statement.csv", "callback_url": "https://erp.example/hook",
                                 "idempotency_key": "msg-1"})
    assert r.status_code == 202 and r.json()["status"] == "inprogress"
    app.state.runner.drain()
    [call] = hooks.calls
    assert call["url"] == "https://erp.example/hook"
    assert call["headers"]["X-Extraction-Status"] == "extracted"
    assert call["headers"]["X-Signature"] == sign("topsecret", call["headers"]["X-Timestamp"], call["raw"])
    body = call["body"]
    assert body["event"] == "extraction.completed" and body["status"] == "extracted"
    assert body["flagged"] is False and body["result"][0]["invoice_number"] == "INV-30001"
    assert body["request"] == {"file_location": "statement.csv", "idempotency_key": "msg-1"}
    view = c.get(f"/extractions/{r.json()['job_id']}").json()
    assert view["delivery"][0]["status"] == "delivered"


def test_flagged_result_on_a_webhook_carries_the_flags(settings, input_root):
    (input_root / "partial.csv").write_text("Supplier,Total Due\nGlobex Ltd,\"$10.00\"\n")
    hooks = Hooks(200)
    app = _app(settings, http=hooks)
    TestClient(app).post("/extract", json={"file_location": "partial.csv", "callback_url": "https://erp.example/h"})
    app.state.runner.drain()
    result = hooks.calls[0]["body"]["result"]
    assert hooks.calls[0]["body"]["flagged"] is True
    assert "missing_field:invoice_number" in result["flags"] and result["records"][0]["fields"]


def test_webhook_retries_then_gives_up_and_can_be_redelivered(settings):
    hooks = Hooks(500, 503, 500)
    app = _app(replace(settings, webhook_max_attempts=3, webhook_backoff_s=0), http=hooks)
    c = TestClient(app)
    job = c.post("/extract", json={"file_location": "statement.csv", "callback_url": "https://erp.example/h"}
                 ).json()["job_id"]
    app.state.runner.drain()
    assert len(hooks.calls) == 3
    [d] = c.get(f"/extractions/{job}").json()["delivery"]
    assert d["status"] == "failed" and d["attempts"] == 3 and d["last_status"] == 500
    hooks.statuses = [200]
    assert c.post(f"/extractions/{job}/redeliver").status_code == 202
    app.state.runner.drain()
    assert c.get(f"/extractions/{job}").json()["delivery"][-1]["status"] == "delivered"


def test_real_http_webhook(settings):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers["content-length"]))))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        app = _app(settings, http=httpx.Client(trust_env=False))
        TestClient(app).post("/extract", json={"file_location": "statement.csv",
                                               "callback_url": f"http://127.0.0.1:{server.server_port}/hook"})
        app.state.runner.drain()
    finally:
        server.shutdown()
    assert received[0]["result"][0]["invoice_number"] == "INV-30001"


@pytest.mark.parametrize("url", ["ftp://x.example/h", "http://169.254.169.254/latest", "not a url"])
def test_unsafe_callback_urls_are_refused(settings, url):
    r = TestClient(_app(settings)).post("/extract", json={"file_location": "statement.csv", "callback_url": url})
    assert r.status_code == 422 and r.json()["error"] == "invalid_request"


def test_allowlist_restricts_callback_hosts(settings):
    c = TestClient(_app(replace(settings, webhook_allowed_hosts=("erp.example",))))
    assert c.post("/extract", json={"file_location": "statement.csv", "callback_url": "https://evil.example/h"}
                  ).status_code == 422
    assert c.post("/extract", json={"file_location": "statement.csv", "callback_url": "https://erp.example/h"}
                  ).status_code == 202


# ------------------------------------------------------------------ idempotency, dead letters, resume


def test_same_idempotency_key_is_the_same_job(settings):
    app = _app(settings)
    c = TestClient(app)
    a = c.post("/extract", json={"file_location": "statement.csv", "async": True, "idempotency_key": "k1"}).json()
    b = c.post("/extract", json={"file_location": "invoice.pdf", "async": True, "idempotency_key": "k1"}).json()
    assert a["job_id"] == b["job_id"]
    sync = c.post("/extract", json={"file_location": "statement.csv"})
    again = c.post("/extract", json={"file_location": "statement.csv"})
    assert sync.headers["x-job-id"] == again.headers["x-job-id"]
    assert sync.headers["x-audit-id"] == again.headers["x-audit-id"]


def test_engine_fault_retries_then_dead_letters_with_a_result(settings, monkeypatch):
    hooks = Hooks(200)
    app = _app(replace(settings, job_max_attempts=2, job_retry_backoff_s=0), http=hooks)
    import extractor_service.jobs as jobs_mod

    def boom(*a, **k):
        raise RuntimeError("engine bug")
    monkeypatch.setattr(jobs_mod.pipeline, "run", boom)
    job = TestClient(app).post("/extract", json={"file_location": "statement.csv",
                                                 "callback_url": "https://erp.example/h"}).json()["job_id"]
    app.state.runner.drain()
    row = app.state.jobs.get(job)
    assert row["attempts"] == 2 and row["dead"] is True and row["status"] == "done"
    body = hooks.calls[0]["body"]
    assert body["flagged"] is True and body["result"]["flags"] == ["error:internal_error", "dead_lettered"]


def test_a_run_interrupted_mid_graph_resumes_from_its_checkpoint(settings, monkeypatch):
    import extractor_service.graph as graph_mod
    import extractor_service.sandbox as sandbox_mod
    parsed = []
    real_parse = sandbox_mod.parse
    monkeypatch.setattr(sandbox_mod, "parse", lambda item, *a, **k: parsed.append(item.name) or real_parse(item, *a, **k))
    calls = {"n": 0}
    real = graph_mod.get_extractor

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("worker lost")       # dies after parsing, before extraction
        return real(*a, **k)
    monkeypatch.setattr(graph_mod, "get_extractor", flaky)
    app = _app(settings)
    job = TestClient(app).post("/extract", json={"file_location": "invoice-email.eml", "async": True}).json()["job_id"]
    first = app.state.jobs.claim("w1", 60)
    app.state.runner.execute(first)                 # fails mid-graph: back in the queue
    assert app.state.jobs.get(job)["status"] == "queued" and sorted(parsed) == ["INV-20194.xlsx", "email body"]
    app.state.jobs._exec("UPDATE jobs SET available_at=0 WHERE id=%s", (job,))
    app.state.runner.drain()
    row = app.state.jobs.get(job)
    assert row["status"] == "done" and json.loads(row["result"])["data"][0]["invoice_number"] == "INV-20194"
    assert sorted(parsed) == ["INV-20194.xlsx", "email body"]          # parsing was not repeated


# ------------------------------------------------------------------ review, corrections, audit, metrics


def test_review_correction_is_delivered_as_human_corrected_and_exported(settings, input_root):
    (input_root / "partial.csv").write_text("Supplier,Total Due\nGlobex Ltd,\"$10.00\"\n")
    hooks = Hooks(200)
    app = _app(settings, http=hooks)
    c = TestClient(app)
    job = c.post("/extract", json={"file_location": "partial.csv", "callback_url": "https://erp.example/h"}
                 ).json()["job_id"]
    app.state.runner.drain()
    [entry] = c.get("/review").json()
    assert entry["job_id"] == job and "missing_field:invoice_number" in entry["flags"]
    bad = c.post(f"/review/{job}/resolve", json={"reviewer": "maya", "corrections": [{"record": 0, "field": "nope",
                                                                                      "value": 1}]})
    assert bad.status_code == 422
    r = c.post(f"/review/{job}/resolve", json={"reviewer": "maya", "corrections": [
        {"record": 0, "field": "invoice_number", "value": "GLX-9"},
        {"record": 0, "field": "invoice_date", "value": "2026-08-01"}]})
    assert r.json() == {"job_id": job, "status": "corrected", "changes": 2}
    app.state.runner.drain()
    corrected = hooks.calls[-1]["body"]
    assert corrected["event"] == "extraction.corrected" and corrected["human_corrected"] is True
    assert corrected["flagged"] is False and corrected["result"][0]["invoice_number"] == "GLX-9"
    assert corrected["audit_id"] == hooks.calls[0]["body"]["audit_id"]           # RT-47: same audit id
    assert c.post(f"/review/{job}/resolve", json={"reviewer": "x", "action": "reject"}).status_code == 409
    [item] = c.get("/review/corrections/export").json()["items"]
    assert item["ground_truth"][0]["invoice_number"] == "GLX-9" and item["reviewer"] == "maya"
    assert {"record": 0, "field": "invoice_number"} in item["corrected_fields"]
    assert item["file_location"] == "partial.csv" and item["source_sha256"]
    assert c.get(f"/review/{job}").json()["original_result"]["flagged"] is True


def test_audit_chain_detects_tampering(settings):
    app = _app(settings)
    c = TestClient(app)
    c.post("/extract", json={"file_location": "statement.csv"})
    c.post("/extract", json={"file_location": "invoice.pdf"})
    assert c.get("/audit/verify").json()["ok"] is True
    app.state.jobs._exec("UPDATE jobs SET result=replace(result, 'INV-30001', 'INV-HACKED')")
    v = c.get("/audit/verify").json()
    assert v["ok"] is False and v["reason"] == "record_changed"


def test_metrics_per_client(settings, input_root):
    (input_root / "partial.csv").write_text("Supplier,Total Due\nGlobex Ltd,\"$10.00\"\n")
    c = TestClient(_app(settings))
    for f in ("statement.csv", "invoice.pdf", "partial.csv"):
        c.post("/extract", json={"file_location": f})
    [g] = c.get("/metrics").json()["groups"]
    assert (g["client"], g["usecase"], g["jobs"], g["extracted"], g["flagged"]) == ("default", "invoice", 3, 3, 1)
    assert g["flags"]["missing_field"] >= 1 and g["latency_ms"]["p50"] is not None
    text = c.get("/metrics/prometheus").text
    assert 'extractor_jobs_total{client="default",usecase="invoice"} 3' in text


def test_retention_sweep_removes_spools_and_checkpoints(settings):
    app = _app(settings)
    c = TestClient(app)
    job = c.post("/extract", json={"file_location": "invoice-email.eml", "async": True}).json()["job_id"]
    app.state.runner.drain()
    spooled = app.state.jobs._one("SELECT count(*) AS n FROM spool WHERE run_id=%s", (job,))["n"]
    assert spooled == 2                                  # the body and the workbook, by reference
    removed = app.state.jobs.sweep(90, app.state.runner.checkpointer)
    assert removed["spools"] == 1 and removed["checkpoints"] == 1
    assert app.state.jobs._one("SELECT count(*) AS n FROM spool WHERE run_id=%s", (job,))["n"] == 0


# ------------------------------------------------------------------ documents


def _pdf_with_password(password: str) -> bytes:
    from pypdf import PdfReader, PdfWriter
    w = PdfWriter(clone_from=PdfReader(io.BytesIO(invoice_pdf())))
    w.encrypt(user_password=password, owner_password=password + "-owner")
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def test_encrypted_attachments_open_with_a_key_from_the_email(settings, input_root):
    from msoffcrypto.format.ooxml import OOXMLFile
    xbuf = io.BytesIO()
    OOXMLFile(io.BytesIO(invoice_xlsx())).encrypt("Xl$2026", xbuf)
    body = "Hi,\nThe PDF password is Pdf-77 and the workbook password: Xl$2026\nThanks"
    (input_root / "enc.eml").write_bytes(_email(body, ("inv.pdf", _pdf_with_password("Pdf-77")),
                                                ("inv.xlsx", xbuf.getvalue())))
    e = TestClient(_app(settings)).post("/extract", json={"file_location": "enc.eml", "extended": True}).json()
    assert e["metadata"]["keys_used"] == {"inv.pdf": "body#L2", "inv.xlsx": "body#L2"}
    assert [d["source"] for d in e["metadata"]["documents"]] == ["body", "attachment:inv.pdf", "attachment:inv.xlsx"]
    assert "Pdf-77" not in json.dumps(e) and "Xl$2026" not in json.dumps(e["metadata"])   # keys never recorded
    assert {d["invoice_number"] for d in e["data"]} == {"INI-0042", "INV-20194"}


def test_encrypted_attachment_without_a_key_is_flagged_and_the_run_continues(settings, input_root):
    (input_root / "enc.eml").write_bytes(_email("Invoice No: INV-9\nTotal Due: $12.00\nInvoice Date: 2026-08-01\n"
                                                "Supplier: Acme Corp", ("inv.pdf", _pdf_with_password("nobody"))))
    e = TestClient(_app(settings)).post("/extract", json={"file_location": "enc.eml"}).json()
    assert "encrypted_no_key" in e["flags"] and e["data"][0]["invoice_number"] == "INV-9"


def test_newest_message_in_a_thread_wins(settings, input_root):
    body = ("Hi team, corrected figures below.\nInvoice No: INV-500\nTotal Due: $5,000.00\n"
            "Invoice Date: 2026-09-01\nSupplier: Hooli Inc\n\n-----Original Message-----\nFrom: Hooli\n"
            "Sent: Monday\nTo: AP\nSubject: Invoice\nInvoice No: INV-500\nTotal Due: $4,000.00\n")
    (input_root / "t.eml").write_bytes(_email(body))
    e = TestClient(_app(settings)).post("/extract", json={"file_location": "t.eml", "extended": True}).json()
    assert e["data"][0]["total_amount"] == 5000
    assert e["records"][0]["fields"]["total_amount"]["source"] == "body#L3"
    assert "thread_segments:2" in e["metadata"]["documents"][0]["notes"]
    from extractor_service.cli import run_batch
    [res] = run_batch({"runs": [{"file_location": "t.eml"}], "include_evidence": True},
                      replace(settings, parse_sandbox="off"))["results"]
    seg = {b["locator"]: b.get("segment") for b in res["evidence"][0]["blocks"]}
    assert (seg["L3"], seg["L13"]) == (0, 1)


def _docx(paragraphs: list[str], table: list[list[str]]) -> bytes:
    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    def p(t): return f"<w:p><w:r><w:t xml:space=\"preserve\">{t}</w:t></w:r></w:p>"
    rows = "".join("<w:tr>" + "".join(f"<w:tc>{p(c)}</w:tc>" for c in r) + "</w:tr>" for r in table)
    xml = f'<?xml version="1.0"?><w:document {w}><w:body>{"".join(p(x) for x in paragraphs)}' \
          f"<w:tbl>{rows}</w:tbl></w:body></w:document>"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", xml)
    return buf.getvalue()


def test_docx_paragraphs_and_tables(settings, input_root):
    (input_root / "inv.docx").write_bytes(_docx(
        ["Invoice No: DOC-12", "Invoice Date: 2026-08-02", "Supplier: Wordy Ltd", "Total Due: $300.00"],
        [["Description", "Qty", "Amount"], ["Editing", "3", "$300.00"]]))
    e = TestClient(_app(settings)).post("/extract", json={"file_location": "inv.docx", "extended": True}).json()
    rec = e["records"][0]
    assert rec["data"]["invoice_number"] == "DOC-12" and rec["data"]["total_amount"] == 300
    assert rec["fields"]["invoice_number"]["source"] == "file:inv.docx#P1"
    assert rec["data"]["line_items"][0]["description"] == "Editing"


@pytest.mark.skipif(not ocr.available(), reason="tesseract is not installed")
def test_scanned_pdf_is_read_by_ocr(settings, input_root):
    from PIL import Image, ImageDraw, ImageFont
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 40)
    img = Image.new("RGB", (1700, 900), "white")
    d = ImageDraw.Draw(img)
    for i, line in enumerate(["Invoice No: SCAN-881", "Invoice Date: 2026-08-15", "Supplier: Paper Co",
                              "Total Due: $812.40"]):
        d.text((120, 120 + i * 90), line, fill="black", font=font)
    buf = io.BytesIO()
    img.save(buf, "PDF", resolution=200)
    (input_root / "scan.pdf").write_bytes(buf.getvalue())
    e = TestClient(_app(settings)).post("/extract", json={"file_location": "scan.pdf", "extended": True}).json()
    doc = e["metadata"]["documents"][0]
    assert "page_source:p1=ocr" in doc["notes"] and doc["status"] == "read"
    rec = e["records"][0]
    assert rec["data"]["invoice_number"] == "SCAN-881" and rec["data"]["total_amount"] == 812.4
    assert rec["fields"]["invoice_number"]["source"].startswith("file:scan.pdf#p1:O")


def test_parse_timeout_degrades_the_run_instead_of_hanging(settings, config_root):
    m = config_root / "default/invoice/1.0.0/manifest.json"
    manifest = json.loads(m.read_text())
    manifest["evidence"]["parse_timeout_s"] = 0.001
    m.write_text(json.dumps(manifest))
    e = TestClient(_app(replace(settings, parse_sandbox="process"))).post(
        "/extract", json={"file_location": "invoice.pdf"}).json()
    assert e["flagged"] is True and "agent_timeout:parse:invoice.pdf" in e["flags"]


# ------------------------------------------------------------------ skills, cost, ceilings


def test_skill_applies_only_to_documents_matching_its_fingerprint(settings, config_root, input_root):
    base = config_root / "default/invoice/1.0.0"
    (base / "skills/hooli.md").write_text("---\nkind: learned-pattern\napplies_to:\n  headers: [Our Ref, Biller]\n"
                                          "hints:\n  fields:\n    invoice_number: {labels: [Our Ref]}\n"
                                          "    total_amount: {anchors: [kindly remit]}\n---\n## Skill: hooli\n")
    manifest = json.loads((base / "manifest.json").read_text())
    manifest["skills"].append("hooli")
    (base / "manifest.json").write_text(json.dumps(manifest))
    (input_root / "remit.csv").write_text("Our Ref,Biller,Total Due\nRA-1,Hooli Inc,$5.00\n")
    (input_root / "terms.csv").write_text("Acme Corp payment terms,\n,\nInvoice No: INV-9,\nInvoice Date: 2026-08-01,\n"
                                          "Supplier: Acme Corp,\n\"Kindly remit: 30 days\",\nTotal Due: $99.00,\n")
    c = TestClient(_app(settings))
    remit = c.post("/extract", json={"file_location": "remit.csv", "extended": True}).json()
    assert "hooli" in remit["metadata"]["skills_applied"] and remit["data"][0]["invoice_number"] == "RA-1"
    terms = c.post("/extract", json={"file_location": "terms.csv", "extended": True}).json()
    assert "hooli" not in terms["metadata"]["skills_applied"] and terms["data"][0]["total_amount"] == 99


def test_cost_ceiling_stops_the_model_call(settings, config_root, monkeypatch):
    from tests.test_llm_path import FakeGateway, _answer
    m = config_root / "acme/ap-invoices/1.1.0/manifest.json"
    manifest = json.loads(m.read_text())
    manifest["limits"] = {"max_cost_usd": 0.0001}
    m.write_text(json.dumps(manifest))
    monkeypatch.setenv("MODEL_GATEWAY_PRICES", json.dumps({"default": {"input": 3.0, "output": 15.0}}))
    gw = FakeGateway(_answer())
    e = TestClient(_app(settings, model_gateway=gw)).post(
        "/extract", json={"file_location": "invoice.xlsx", "client": "acme"}).json()
    assert gw.requests == [] and "cost_ceiling_exceeded" in e["flags"]
    assert e["metadata"]["cost"]["projected_usd"] > 0.0001


def test_run_ceiling_flags_instead_of_running_long(settings, config_root):
    m = config_root / "default/invoice/1.0.0/manifest.json"
    manifest = json.loads(m.read_text())
    manifest["limits"] = {"run_ceiling_s": -1}
    m.write_text(json.dumps(manifest))
    e = TestClient(_app(settings)).post("/extract", json={"file_location": "statement.csv"}).json()
    assert "agent_timeout:run" in e["flags"]


def test_worker_process_drains_the_shared_queue(settings, tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path
    app = _app(settings)
    job = TestClient(app).post("/extract", json={"file_location": "statement.csv", "async": True}).json()["job_id"]
    env = {**os.environ, "CONFIG_ROOT": str(settings.config_root), "INPUT_ROOT": str(settings.input_root),
           "DATABASE_URL": settings.database_url, "DB_SCHEMA": settings.db_schema}
    p = subprocess.run([sys.executable, "-m", "extractor_service.worker", "--drain"], env=env,
                       cwd=str(Path(__file__).parents[1]), capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stderr
    assert app.state.jobs.get(job)["status"] == "done"

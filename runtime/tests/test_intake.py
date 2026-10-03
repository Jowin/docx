import io
import zipfile
from email.message import EmailMessage

from extractor_service.config_store import DEFAULT_INTAKE
from extractor_service.intake import detect_kind, open_submission
from tests.samples import invoice_pdf, invoice_xlsx, statement_csv


def _zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


def _files(sub):
    return [i for i in sub.items if i.kind != "email_body"]


def test_kind_comes_from_bytes_not_extension():
    assert detect_kind(statement_csv()) == "csv"
    assert detect_kind(invoice_xlsx()) == "excel"
    assert detect_kind(invoice_pdf()) == "pdf"
    sub = open_submission("x.pdf", "x.pdf", statement_csv(), DEFAULT_INTAKE)
    assert sub.kind == "csv" and sub.items[0].kind == "csv"


def test_zip_bomb_ratio_refused():
    sub = open_submission("b.zip", "b.zip", _zip({"big.csv": b"a,b\n" + b"0,0\n" * 500_000}), DEFAULT_INTAKE)
    assert sub.items == [] and "archive_limit_exceeded:b.zip" in sub.reasons


def test_member_count_limit():
    limits = {**DEFAULT_INTAKE, "max_zip_members": 3}
    sub = open_submission("m.zip", "m.zip", _zip({f"{i}.csv": statement_csv() for i in range(5)}), limits)
    assert sub.items == [] and sub.skipped[0]["reason"] == "archive_limit_exceeded"


def test_traversal_names_skipped():
    sub = open_submission("t.zip", "t.zip", _zip({"../evil.csv": statement_csv(), "ok.csv": statement_csv()}),
                          DEFAULT_INTAKE)
    assert [i.name for i in sub.items] == ["t.zip/ok.csv"]
    assert sub.skipped[0]["reason"] == "unsafe_path"


def test_one_level_of_nested_zip_only():
    inner = _zip({"deep.csv": statement_csv()})
    middle = _zip({"inner.zip": inner, "mid.csv": statement_csv()})
    outer = _zip({"middle.zip": middle})
    sub = open_submission("o.zip", "o.zip", outer, DEFAULT_INTAKE)
    assert [i.name for i in sub.items] == ["o.zip/middle.zip/mid.csv"]
    assert {"item": "o.zip/middle.zip/inner.zip", "reason": "archive_depth_exceeded"} in sub.skipped


def test_zip_attached_to_email_is_unpacked_with_attachment_sources():
    msg = EmailMessage()
    msg["Subject"] = "Statements"
    msg.set_content("See attached.")
    msg.add_attachment(_zip({"s.csv": statement_csv()}), maintype="application", subtype="zip",
                       filename="s.zip")
    sub = open_submission("m.eml", "m.eml", bytes(msg), DEFAULT_INTAKE)
    assert [i.source_prefix for i in _files(sub)] == ["attachment:s.zip/s.csv"]
    assert sub.items[0].source_prefix == "body" and sub.items[0].kind == "email_body"


def test_attached_email_is_read_recursively():
    inner = EmailMessage()
    inner["Subject"] = "old"
    inner["From"] = "billing@hooli.example"
    inner.set_content("old thread\nTotal due: $7.00")
    inner.add_attachment(statement_csv(), maintype="text", subtype="csv", filename="s.csv")
    msg = EmailMessage()
    msg["Subject"] = "Fwd"
    msg.set_content("forwarding")
    msg.add_attachment(inner)
    sub = open_submission("f.eml", "f.eml", bytes(msg), DEFAULT_INTAKE)
    assert [(i.kind, i.source_prefix) for i in sub.items] == [
        ("email_body", "body"), ("email_body", "embedded:1:old.eml"), ("csv", "embedded:1:old.eml/s.csv")]
    assert sub.items[1].meta == {"subject": "old", "sender": "billing@hooli.example", "depth": 1}
    assert sub.reasons == []


def test_embedded_depth_is_checked_before_recursing():
    inner = EmailMessage()
    inner["Subject"] = "deep"
    inner.set_content("deepest")
    for level in range(3):
        outer = EmailMessage()
        outer["Subject"] = f"level {level}"
        outer.set_content("fwd")
        outer.add_attachment(inner)
        inner = outer
    sub = open_submission("f.eml", "f.eml", bytes(inner), {**DEFAULT_INTAKE, "max_email_depth": 2})
    assert [i.meta["depth"] for i in sub.items] == [0, 1, 2]
    assert sub.skipped[-1]["reason"] == "embedded_depth_exceeded" and "embedded_depth_exceeded" in sub.reasons


def test_duplicate_attachment_names_kept_apart():
    msg = EmailMessage()
    msg.set_content("two files")
    for _ in range(2):
        msg.add_attachment(statement_csv(), maintype="text", subtype="csv", filename="s.csv")
    sub = open_submission("d.eml", "d.eml", bytes(msg), DEFAULT_INTAKE)
    assert [i.name for i in _files(sub)] == ["s.csv", "s (2).csv"]


def test_html_only_email_body_becomes_text():
    msg = EmailMessage()
    msg["Subject"] = "Invoice"
    msg.set_content("<p>Invoice No: <b>INV-1</b></p><p>Total Due: $5.00</p>", subtype="html")
    sub = open_submission("h.eml", "h.eml", bytes(msg), DEFAULT_INTAKE)
    assert "Invoice No: INV-1" in sub.body_text and "Total Due: $5.00" in sub.body_text

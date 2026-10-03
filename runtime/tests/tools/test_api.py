from fastapi.testclient import TestClient

from extractor_tools.api import app

client = TestClient(app)


def test_csv_endpoint(invoice_csv):
    r = client.post("/tools/csv-reader", files={"file": ("s.csv", invoice_csv)})
    assert r.status_code == 200 and r.json()["tool"] == "csv_reader"
    assert r.json()["input"]["filename"] == "s.csv"


def test_sheet_endpoints(invoice_xlsx):
    f = {"file": ("inv.xlsx", invoice_xlsx)}
    assert client.post("/tools/spreadsheet-reader/sheets", files=f).json()["result"]["sheet_count"] == 3
    r = client.post("/tools/spreadsheet-reader/read", files=f, data={"sheet": "Summary", "cell_range": "B6"})
    assert r.json()["result"]["rows"][0]["cells"][0]["value"] == 12400


def test_pdf_endpoint(invoice_pdf):
    r = client.post("/tools/pdf-text", files={"file": ("i.pdf", invoice_pdf)}, data={"include_tables": "false"})
    assert r.status_code == 200 and "tables" not in r.json()["result"]["pages"][0]


def test_tool_error_is_422_with_code():
    r = client.post("/tools/spreadsheet-reader/sheets", files={"file": ("x.xlsx", b"not a workbook")})
    assert r.status_code == 422
    assert r.json()["error"] == "format_mismatch" and r.json()["permanent"] is True

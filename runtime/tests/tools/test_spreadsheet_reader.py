import io
import json
import zipfile

import pytest

from extractor_tools import ToolError, list_sheets, read_sheet


def _cells(result):
    return {c["ref"]: c for row in result["rows"] for c in row["cells"]}


def test_list_sheets_inventory(invoice_xlsx):
    r = list_sheets(invoice_xlsx, "inv.xlsx")["result"]
    assert r["format"] == "xlsx"
    by = {s["name"]: s for s in r["sheets"]}
    assert by["Hidden"]["state"] == "hidden"
    assert by["Summary"]["currency_formatted_cells"] == 2
    assert by["Summary"]["currency_density"] > by["Notes"]["currency_density"]


def test_read_xlsx_typed_cells(invoice_xlsx):
    r = read_sheet(invoice_xlsx, "inv.xlsx", sheet="Summary")["result"]
    c = _cells(r)
    assert r["ref_prefix"] == "Summary!"
    assert c["B6"]["type"] == "integer" and c["B6"]["value"] == 12400 and c["B6"]["currency_format"]
    assert c["B7"]["value"] == "49.5"
    assert c["C6"] == {"ref": "C6", "type": "percent", "value": "0.2", "number_format": "0%"}
    assert c["B15"]["type"] == "date" and c["B15"]["value"] == "2026-09-15"
    assert c["B16"]["value"] is False
    assert c["B14"]["type"] == "formula_uncached" and c["B14"]["formula"] == "=B6-B7"
    assert r["merged_ranges"] == ["A3:C3"]


def test_read_range(invoice_xlsx):
    r = read_sheet(invoice_xlsx, sheet="Summary", cell_range="A5:B7")["result"]
    assert set(_cells(r)) == {"A5", "B5", "A6", "B6", "A7", "B7"}


def test_read_xls(invoice_xls):
    r = read_sheet(invoice_xls, "inv.xls", sheet="Transactions")["result"]
    c = _cells(r)
    assert r["format"] == "xls"
    assert c["B2"]["value"] == "12400.5" and c["B2"]["currency_format"]
    assert c["C2"] == {"ref": "C2", "type": "date", "value": "2026-09-15", "number_format": "YYYY-MM-DD"}
    assert r["merged_ranges"] == ["A4:C4"]


def test_missing_sheet_names_the_alternatives(invoice_xlsx):
    with pytest.raises(ToolError) as e:
        read_sheet(invoice_xlsx, sheet="Invoice")
    assert e.value.code == "sheet_not_found"
    assert e.value.detail["sheets"] == ["Notes", "Summary", "Hidden"]


def test_zip_that_is_not_a_workbook():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a.csv", "x,y\n1,2\n")
    with pytest.raises(ToolError) as e:
        list_sheets(buf.getvalue(), "fake.xlsx")
    assert e.value.code == "format_mismatch"


def test_encrypted_ooxml_detected():
    fake = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64 + "EncryptedPackage".encode("utf-16-le")
    with pytest.raises(ToolError) as e:
        list_sheets(fake)
    assert e.value.code == "file_encrypted" and e.value.permanent


def test_csv_bytes_rejected():
    with pytest.raises(ToolError) as e:
        read_sheet(b"a,b\n1,2\n", sheet="x")
    assert e.value.code == "format_mismatch"


def test_deterministic(invoice_xlsx):
    a = json.dumps(read_sheet(invoice_xlsx, sheet="Summary"), sort_keys=True)
    assert a == json.dumps(read_sheet(invoice_xlsx, sheet="Summary"), sort_keys=True)

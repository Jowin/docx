import json

import pytest

from extractor_tools import ToolError, read_csv


def test_types_refs_and_column_evidence(invoice_csv):
    r = read_csv(invoice_csv, "statement.csv")["result"]
    assert r["delimiter"] == "," and r["encoding"] == "utf-8"
    cols = {c["name"]: c for c in r["columns"]}
    assert cols["Invoice Date"]["date_order"] == "DMY"          # 15/08 proves day-first
    assert cols["Invoice Date"]["date_order_evidence"] == "inferred"
    assert cols["Total Due"]["inferred_type"] == "decimal"
    row2 = {c["ref"]: c for c in r["rows"][0]["cells"]}
    assert row2["G2"] == {"ref": "G2", "raw": "$12,400.00", "type": "decimal",
                          "value": "12400.00", "currency": "$"}
    assert row2["B2"]["value"] == "2026-08-15"
    row3 = {c["ref"]: c for c in r["rows"][1]["cells"]}
    assert row3["B3"]["value"] == "2026-09-03"                   # resolved by column evidence
    assert row3["G3"]["value"] == "-450.50"                      # parentheses = negative


def test_leading_zero_ids_stay_text_and_blank_rows_counted(invoice_csv):
    r = read_csv(invoice_csv)["result"]
    assert r["blank_rows_skipped"] == 1
    last = {c["ref"]: c for c in r["rows"][-1]["cells"]}
    assert last["A5"]["type"] == "string" and last["A5"]["value"] == "00123"


def test_ambiguous_dates_are_reported_not_guessed():
    r = read_csv(b"Due\n03/04/2026\n05/06/2026\n")["result"]
    cell = r["rows"][0]["cells"][0]
    assert cell["type"] == "date_ambiguous" and cell["value"] is None
    assert cell["candidates"] == {"DMY": "2026-04-03", "MDY": "2026-03-04"}
    forced = read_csv(b"Due\n03/04/2026\n", date_order="MDY")["result"]
    assert forced["rows"][0]["cells"][0]["value"] == "2026-03-04"


def test_european_semicolon_file():
    data = "Rechnung;Betrag\nR-1;1.234,56 €\nR-2;99,5 €\n".encode("cp1252")
    r = read_csv(data)["result"]
    assert r["delimiter"] == ";" and r["encoding"] == "cp1252"
    assert r["columns"][1]["decimal_separator"] == ","
    vals = [row["cells"][1] for row in r["rows"]]
    assert vals[0]["value"] == "1234.56" and vals[0]["currency"] == "€"
    assert vals[1]["value"] == "99.5"


def test_preamble_above_header():
    r = read_csv(b"Acme statement\nPeriod: Aug\nRef,Amount\nA,10\n", header_row=3)["result"]
    assert [p["row"] for p in r["preamble"]] == [1, 2]
    assert r["columns"][1]["name"] == "Amount"
    assert r["rows"][0]["cells"][1]["ref"] == "B4"


def test_utf8_bom_and_percent():
    r = read_csv("\ufeffRate\n15%\n".encode("utf-8"))["result"]
    assert r["encoding"] == "utf-8-sig" and r["columns"][0]["name"] == "Rate"
    assert r["rows"][0]["cells"][0] == {"ref": "A2", "raw": "15%", "type": "percent", "value": "0.15"}


@pytest.mark.parametrize("data,code", [
    (b"PK\x03\x04rest", "format_mismatch"),
    (b"%PDF-1.4 ...", "format_mismatch"),
    (b"", "file_corrupt"),
])
def test_typed_errors(data, code):
    with pytest.raises(ToolError) as e:
        read_csv(data)
    assert e.value.code == code and e.value.permanent


def test_row_limit_is_an_error_not_a_truncation():
    with pytest.raises(ToolError) as e:
        read_csv(b"a\n" + b"1\n" * 20, max_rows=10)
    assert e.value.code == "input_too_large"


def test_deterministic(invoice_csv):
    a = json.dumps(read_csv(invoice_csv, "s.csv"), sort_keys=True)
    b = json.dumps(read_csv(invoice_csv, "s.csv"), sort_keys=True)
    assert a == b

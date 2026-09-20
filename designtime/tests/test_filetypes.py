"""Type detection reads the bytes, not the extension (DT-06, rehearsing RT-14)."""

from __future__ import annotations

import shutil

from dataextractor_designtime.agents import CorpusProfiler, ProfilerInput
from dataextractor_designtime.agents.filetypes import CSV, PDF, UNKNOWN, XLSX, detect
from dataextractor_designtime.agents.tabular import read
from dataextractor_designtime.contracts.corpus import Corpus


def test_detects_csv_and_xlsx(corpus_root):
    assert detect(corpus_root / "samples/inv_0000/attachments/inv_0000.csv") == CSV
    assert detect(corpus_root / "samples/inv_0001/attachments/inv_0001.xlsx") == XLSX


def test_an_xlsx_renamed_to_csv_is_still_an_xlsx(corpus_root, tmp_path):
    lying = tmp_path / "totally_a.csv"
    shutil.copy(corpus_root / "samples/inv_0001/attachments/inv_0001.xlsx", lying)
    assert detect(lying) == XLSX


def test_pdf_and_unknown(tmp_path):
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.7\n stuff")
    assert detect(pdf) == PDF
    blob = tmp_path / "blob.bin"
    blob.write_bytes(b"\x00\x01\x02\x03binary")
    assert detect(blob) == UNKNOWN


def test_missing_file_is_unknown_not_an_exception(tmp_path):
    assert detect(tmp_path / "nope.csv") == UNKNOWN


def test_locators_point_at_a_real_cell(corpus_root):
    xlsx = corpus_root / "samples/inv_0001/attachments/inv_0001.xlsx"
    sheet = read(xlsx)[0]
    assert sheet.headers == ["Vendor Name", "Amount Due", "Payment Due", "Invoice No"]
    assert sheet.locator("inv_0001.xlsx", 1, 0) == "attachment:inv_0001.xlsx!Summary!B2"

    csv_sheet = read(corpus_root / "samples/inv_0000/attachments/inv_0000.csv")[0]
    assert csv_sheet.locator("inv_0000.csv", 1, 0) == "attachment:inv_0000.csv#row2col2"


def test_unreadable_attachment_is_profiled_not_fatal(corpus_dict, corpus_root, tmp_path):
    broken = tmp_path / "broken.xlsx"
    broken.write_bytes(b"PK\x03\x04not-really-a-zip")
    data = {
        "meta": corpus_dict["meta"],
        "samples": [
            {
                "sample_id": "inv_0000",
                "subject": "x",
                "body": "y",
                "attachments": [{"filename": "broken.xlsx", "path": str(broken)}],
            }
        ],
        "labels": [corpus_dict["labels"][0]],
    }
    out = CorpusProfiler().run(
        ProfilerInput(corpus=Corpus(**data), corpus_root=str(corpus_root))
    )
    assert out.sample_count == 1
    assert out.unsupported or out.unreadable

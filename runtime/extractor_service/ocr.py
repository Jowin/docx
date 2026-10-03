"""OCR Tool: text for PDF pages with no text layer (and, when enabled, images).

Pages are rendered with pypdfium2 (pdfplumber's renderer) and read by the
tesseract command-line program, which must be on PATH (the image installs
it). Without tesseract, OCR is unavailable and such pages stay flagged as
before. OCR is invoked only on pages that lack a text layer (RT-16), and
each page records whether it was read as ``pdf_text`` or ``ocr``.

Output is one line per OCR text line, with tesseract's mean word confidence
(0-1), so extractors can trust OCR'd values less than text-layer ones.
"""
from __future__ import annotations

import csv
import io
import shutil
import subprocess
from dataclasses import dataclass

DPI = 300


@dataclass(frozen=True)
class OcrLine:
    line: int
    text: str
    confidence: float


def available() -> bool:
    return shutil.which("tesseract") is not None


def _tesseract(png: bytes, timeout_s: float, lang: str = "eng") -> list[OcrLine]:
    proc = subprocess.run(["tesseract", "stdin", "stdout", "-l", lang, "--psm", "6", "tsv"],
                          input=png, capture_output=True, timeout=timeout_s, check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode(errors="replace")[:300])
    rows = csv.DictReader(io.StringIO(proc.stdout.decode("utf-8", errors="replace")), delimiter="\t",
                          quoting=csv.QUOTE_NONE)
    lines: dict[tuple[int, int, int], list[tuple[str, float]]] = {}
    for r in rows:
        text = (r.get("text") or "").strip()
        if r.get("level") != "5" or not text:
            continue
        key = (int(r["block_num"]), int(r["par_num"]), int(r["line_num"]))
        lines.setdefault(key, []).append((text, max(0.0, float(r.get("conf") or 0)) / 100))
    out = []
    for n, key in enumerate(sorted(lines), start=1):
        words = lines[key]
        out.append(OcrLine(n, " ".join(w for w, _ in words), round(sum(c for _, c in words) / len(words), 3)))
    return out


def ocr_pdf_page(data: bytes, page_number: int, timeout_s: float = 60) -> list[OcrLine]:
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(data)
    try:
        page = pdf[page_number - 1]
        image = page.render(scale=DPI / 72).to_pil()
        page.close()
    finally:
        pdf.close()
    buf = io.BytesIO()
    image.save(buf, "PNG")
    return _tesseract(buf.getvalue(), timeout_s)


def ocr_image(data: bytes, timeout_s: float = 60) -> list[OcrLine]:
    from PIL import Image
    buf = io.BytesIO()
    Image.open(io.BytesIO(data)).convert("RGB").save(buf, "PNG")
    return _tesseract(buf.getvalue(), timeout_s)

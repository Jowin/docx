"""OCR: text (and tables) for PDF pages with no text layer, and for images.

Two engines, chosen by ``OCR_ENGINE``:

* ``textract`` (the default): Amazon Textract ``AnalyzeDocument`` with the
  ``TABLES`` feature, one call per rendered page (pages run in parallel,
  ``OCR_MAX_PARALLEL``, default 8). Textract returns lines *and* tables, so a
  scanned blotter keeps its rows and columns: table cells become evidence with
  ``p<n>:OT<t>:R<r>C<c>`` locators that the extractor, the expand step and the
  reviewers cite like any other table. With ``TEXTRACT_S3_BUCKET`` set, a PDF
  with more than ``TEXTRACT_ASYNC_MIN_PAGES`` (default 20) pages to OCR is
  sent as one asynchronous job instead (``StartDocumentAnalysis`` on the
  uploaded PDF, results read back page by page); the object is deleted after.
  Region: ``TEXTRACT_REGION`` or the usual AWS variables; credentials come
  from the standard AWS chain (instance role, environment, profile) and are
  never read or logged here.
* ``tesseract``: the local ``tesseract`` program, lines only (no tables).

When Textract cannot be used (no boto3, no credentials or region, or a call
fails), the engine falls back to ``OCR_FALLBACK`` (default ``tesseract``;
``none`` to fail instead) and says so on the document
(``ocr_fallback:<why>``), which flags the result: a scanned page read by the
fallback is read without table structure and at lower accuracy.

Pages are rendered with pypdfium2 at 300 DPI. Every line carries the engine's
confidence (0-1), so extractors trust OCR'd values less than text-layer ones.
"""
from __future__ import annotations

import csv
import io
import os
import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

DPI = 300
#: Textract's synchronous limit on one document's bytes.
SYNC_MAX_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True)
class OcrLine:
    line: int
    text: str
    confidence: float


@dataclass(frozen=True)
class OcrTable:
    #: Dense grid, row by row: (text, confidence); "" for an empty cell.
    rows: list[list[tuple[str, float]]]


@dataclass(frozen=True)
class OcrPage:
    lines: list[OcrLine]
    tables: list[OcrTable] = field(default_factory=list)
    engine: str = ""


class OcrUnavailable(RuntimeError):
    pass


# ---------------------------------------------------------------------- tesseract


def tesseract_available() -> bool:
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


class TesseractEngine:
    name = "tesseract"

    def __init__(self, timeout_s: float = 60) -> None:
        self.timeout_s = timeout_s

    def available(self) -> str | None:
        return None if tesseract_available() else "tesseract is not installed"

    def image(self, png: bytes) -> OcrPage:
        return OcrPage(_tesseract(png, self.timeout_s), [], self.name)


# ---------------------------------------------------------------------- textract


#: Tests (and other deployments) can supply their own client: () -> object with
#: analyze_document / start_document_analysis / get_document_analysis.
_client_factory: Callable[[], Any] | None = None
_s3_factory: Callable[[], Any] | None = None


def set_textract_client_factory(factory: Callable[[], Any] | None,
                                s3_factory: Callable[[], Any] | None = None) -> None:
    global _client_factory, _s3_factory
    _client_factory, _s3_factory = factory, s3_factory


def _region() -> str | None:
    return os.environ.get("TEXTRACT_REGION") or os.environ.get("AWS_REGION") or \
        os.environ.get("AWS_DEFAULT_REGION")


def parse_blocks(blocks: list[dict[str, Any]]) -> dict[int, OcrPage]:
    """Textract Blocks (one page or a whole async job) -> OcrPage per page number."""
    by_id = {b["Id"]: b for b in blocks}
    pages: dict[int, dict[str, list]] = {}

    def page_of(b: dict[str, Any]) -> int:
        return int(b.get("Page") or 1)

    def words_of(b: dict[str, Any]) -> str:
        ids = [i for rel in b.get("Relationships") or [] if rel["Type"] == "CHILD" for i in rel["Ids"]]
        return " ".join(by_id[i].get("Text", "") for i in ids
                        if i in by_id and by_id[i]["BlockType"] == "WORD").strip()

    for b in blocks:
        kind = b["BlockType"]
        p = pages.setdefault(page_of(b), {"lines": [], "tables": []})
        if kind == "LINE":
            box = (b.get("Geometry") or {}).get("BoundingBox") or {}
            p["lines"].append((round(box.get("Top", 0), 3), box.get("Left", 0), b.get("Text", ""),
                               float(b.get("Confidence", 0)) / 100))
        elif kind == "TABLE":
            cells = [by_id[i] for rel in b.get("Relationships") or [] if rel["Type"] == "CHILD"
                     for i in rel["Ids"] if i in by_id and by_id[i]["BlockType"] == "CELL"]
            if not cells:
                continue
            nrow = max(c["RowIndex"] for c in cells)
            ncol = max(c["ColumnIndex"] for c in cells)
            grid = [[("", 0.0) for _ in range(ncol)] for _ in range(nrow)]
            for c in cells:
                grid[c["RowIndex"] - 1][c["ColumnIndex"] - 1] = (words_of(c), float(c.get("Confidence", 0)) / 100)
            box = (b.get("Geometry") or {}).get("BoundingBox") or {}
            p["tables"].append((round(box.get("Top", 0), 3), OcrTable(grid)))
    out = {}
    for pno, p in pages.items():
        lines = [OcrLine(i, text, round(conf, 3))
                 for i, (_, _, text, conf) in enumerate(sorted(p["lines"], key=lambda x: (x[0], x[1])), start=1)]
        out[pno] = OcrPage(lines, [t for _, t in sorted(p["tables"], key=lambda x: x[0])], "textract")
    return out


class TextractEngine:
    name = "textract"

    def __init__(self, *, max_parallel: int | None = None) -> None:
        self.max_parallel = max_parallel or int(os.environ.get("OCR_MAX_PARALLEL", "8"))
        self.bucket = os.environ.get("TEXTRACT_S3_BUCKET") or None
        self.async_min_pages = int(os.environ.get("TEXTRACT_ASYNC_MIN_PAGES", "20"))
        self._client = None

    def available(self) -> str | None:
        if _client_factory is not None:
            return None
        try:
            import boto3
        except ImportError:
            return "boto3 is not installed"
        if not _region():
            return "no AWS region (set TEXTRACT_REGION or AWS_REGION)"
        if boto3.Session().get_credentials() is None:
            return "no AWS credentials"
        return None

    def client(self):
        if self._client is None:
            if _client_factory is not None:
                self._client = _client_factory()
            else:
                import boto3
                from botocore.config import Config
                self._client = boto3.client("textract", region_name=_region(), config=Config(
                    retries={"max_attempts": 8, "mode": "adaptive"}, connect_timeout=10, read_timeout=120))
        return self._client

    def image(self, png: bytes) -> OcrPage:
        if len(png) > SYNC_MAX_BYTES:
            png = _shrink(png)
        resp = self.client().analyze_document(Document={"Bytes": png}, FeatureTypes=["TABLES"])
        return parse_blocks(resp.get("Blocks") or []).get(1, OcrPage([], [], self.name))

    def pdf_async(self, data: bytes, pages: list[int], poll_s: float = 2.0,
                  timeout_s: float = 900) -> dict[int, OcrPage]:
        """One asynchronous Textract job (needs TEXTRACT_S3_BUCKET) for just these pages.

        Textract analyses (and bills) every page of a document it is given, so the pages that need
        OCR are copied into a PDF of their own first; its page i is ``pages[i - 1]`` of the original.
        """
        data = select_pages(data, pages)
        if _s3_factory is not None:
            s3 = _s3_factory()
        else:
            import boto3
            s3 = boto3.client("s3", region_name=_region())
        key = f"textract-inbox/{uuid.uuid4()}.pdf"
        s3.put_object(Bucket=self.bucket, Key=key, Body=data)
        try:
            job = self.client().start_document_analysis(
                DocumentLocation={"S3Object": {"Bucket": self.bucket, "Name": key}}, FeatureTypes=["TABLES"])
            job_id, deadline, blocks, token = job["JobId"], time.time() + timeout_s, [], None
            while True:
                kw = {"JobId": job_id, **({"NextToken": token} if token else {})}
                resp = self.client().get_document_analysis(**kw)
                status = resp.get("JobStatus")
                if status == "IN_PROGRESS":
                    if time.time() > deadline:
                        raise TimeoutError(f"textract job {job_id} still running after {timeout_s}s")
                    time.sleep(poll_s)
                    continue
                if status not in ("SUCCEEDED", "PARTIAL_SUCCESS"):
                    raise RuntimeError(f"textract job {job_id} {status}: {resp.get('StatusMessage', '')}")
                blocks.extend(resp.get("Blocks") or [])
                token = resp.get("NextToken")
                if not token:
                    break
        finally:
            try:
                s3.delete_object(Bucket=self.bucket, Key=key)
            except Exception:                                  # noqa: BLE001 - best effort clean-up
                pass
        parsed = parse_blocks(blocks)
        return {p: parsed.get(i, OcrPage([], [], self.name)) for i, p in enumerate(pages, start=1)}


def _shrink(png: bytes) -> bytes:
    """Re-encode an over-limit page as JPEG (Textract's sync limit is 10 MB)."""
    from PIL import Image
    img = Image.open(io.BytesIO(png)).convert("RGB")
    for quality in (85, 70, 55):
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality)
        if buf.tell() <= SYNC_MAX_BYTES:
            return buf.getvalue()
        img = img.resize((int(img.width * 0.8), int(img.height * 0.8)))
    return buf.getvalue()


# ---------------------------------------------------------------------- selection


@dataclass
class OcrResult:
    pages: dict[int, OcrPage]
    failed: dict[int, str]
    notes: list[str]


def _engine(name: str, timeout_s: float):
    return TextractEngine() if name == "textract" else TesseractEngine(timeout_s)


def primary_name() -> str:
    return (os.environ.get("OCR_ENGINE") or "textract").strip().lower()


def available() -> bool:
    """Whether any OCR is possible (the primary engine or its fallback)."""
    names = [primary_name(), (os.environ.get("OCR_FALLBACK") or "tesseract").strip().lower()]
    return any(n != "none" and _engine(n, 60).available() is None for n in names)


#: PDFium is not thread-safe: pages render one at a time, while the OCR calls run in parallel.
_PDFIUM = threading.Lock()


def select_pages(data: bytes, pages: list[int]) -> bytes:
    """A new PDF holding only these pages of ``data``, in this order."""
    import pypdfium2 as pdfium
    with _PDFIUM:
        src, dst = pdfium.PdfDocument(data), pdfium.PdfDocument.new()
        try:
            dst.import_pages(src, [p - 1 for p in pages])
            buf = io.BytesIO()
            dst.save(buf)
            return buf.getvalue()
        finally:
            dst.close()
            src.close()


def render_page(data: bytes, page_number: int) -> bytes:
    import pypdfium2 as pdfium
    with _PDFIUM:
        pdf = pdfium.PdfDocument(data)
        try:
            page = pdf[page_number - 1]
            image = page.render(scale=DPI / 72).to_pil()
            page.close()
        finally:
            pdf.close()
    buf = io.BytesIO()
    image.save(buf, "PNG")
    return buf.getvalue()


def ocr_pdf(data: bytes, pages: list[int], *, timeout_s: float = 60) -> OcrResult:
    """OCR these pages of a PDF with the configured engine, falling back as configured."""
    notes: list[str] = []
    primary = _engine(primary_name(), timeout_s)
    fallback_name = (os.environ.get("OCR_FALLBACK") or "tesseract").strip().lower()
    engine, why = primary, primary.available()
    if why:
        if fallback_name in ("none", primary.name) or _engine(fallback_name, timeout_s).available():
            raise OcrUnavailable(f"{primary.name}: {why}")
        notes.append(f"ocr_fallback:{primary.name}->{fallback_name}:{why}"[:200])
        engine = _engine(fallback_name, timeout_s)
    out: dict[int, OcrPage] = {}
    failed: dict[int, str] = {}
    if isinstance(engine, TextractEngine) and engine.bucket and len(pages) >= engine.async_min_pages:
        try:
            out = engine.pdf_async(data, pages)
            notes.append(f"ocr_engine:textract_async:{len(pages)}")
            return OcrResult(out, failed, notes)
        except Exception as exc:                               # noqa: BLE001 - fall back to page calls
            notes.append(f"ocr_async_failed:{type(exc).__name__}:{exc}"[:200])

    def one(p: int) -> tuple[int, OcrPage | None, str | None]:
        try:
            return p, engine.image(render_page(data, p)), None
        except Exception as exc:                               # noqa: BLE001 - per page, reported
            return p, None, f"{type(exc).__name__}: {exc}"[:200]

    workers = getattr(engine, "max_parallel", 1) if isinstance(engine, TextractEngine) else min(4, os.cpu_count() or 1)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results = list(pool.map(one, pages))
    retry = []
    for p, page, err in results:
        if page is not None:
            out[p] = page
        else:
            retry.append((p, err))
    if retry and engine is primary and fallback_name not in ("none", primary.name):
        fb = _engine(fallback_name, timeout_s)
        if fb.available() is None:
            notes.append(f"ocr_fallback:{primary.name}->{fallback_name}:{retry[0][1]}"[:200])
            for p, _ in retry:
                try:
                    out[p] = fb.image(render_page(data, p))
                except Exception as exc:                       # noqa: BLE001
                    failed[p] = f"{type(exc).__name__}: {exc}"[:200]
            retry = []
    for p, err in retry:
        failed[p] = err or "ocr failed"
    notes.append(f"ocr_engine:{engine.name}:{len(out)}")
    return OcrResult(out, failed, notes)


def ocr_image(data: bytes, timeout_s: float = 60) -> OcrPage:
    """OCR one image file (PNG/JPEG/TIFF...) with the configured engine and fallback."""
    from PIL import Image
    buf = io.BytesIO()
    Image.open(io.BytesIO(data)).convert("RGB").save(buf, "PNG")
    png = buf.getvalue()
    primary = _engine(primary_name(), timeout_s)
    fallback_name = (os.environ.get("OCR_FALLBACK") or "tesseract").strip().lower()
    for eng in (primary, _engine(fallback_name, timeout_s) if fallback_name not in ("none", primary.name) else None):
        if eng is None or eng.available():
            continue
        try:
            return eng.image(png)
        except Exception:                                       # noqa: BLE001 - try the fallback
            continue
    raise OcrUnavailable("no OCR engine could read the image")


# kept for callers of the earlier single-engine API
def ocr_pdf_page(data: bytes, page_number: int, timeout_s: float = 60) -> list[OcrLine]:
    res = ocr_pdf(data, [page_number], timeout_s=timeout_s)
    if page_number in res.failed:
        raise RuntimeError(res.failed[page_number])
    return res.pages[page_number].lines

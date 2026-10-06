"""FastAPI endpoints, one per tool operation, so each can be tested alone.

Mount the router in the existing app:   app.include_router(router)
Or run standalone:                       uvicorn extractor_tools.api:app --port 8081

Tool errors return 422 with {"error", "permanent", "message", "detail"}.
"""
from __future__ import annotations

import hashlib
from typing import Callable, Literal

from fastapi import APIRouter, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, Response

from .common import ToolError
from .csv_reader import read_csv
from .pdf_text import extract_pdf_text
from .rules import evaluate as evaluate_rules
from .spreadsheet_reader import list_sheets, read_sheet
from .writers import write

router = APIRouter(prefix="/tools", tags=["tools"])


MAX_UPLOAD = 10 * 1024 * 1024


async def _run(file: UploadFile, fn: Callable, **kwargs) -> JSONResponse:
    data = await file.read(MAX_UPLOAD + 1)     # never buffer more than the tool limit
    if len(data) > MAX_UPLOAD:
        err = ToolError("input_too_large", f"upload exceeds {MAX_UPLOAD} bytes",
                        detail={"limit": MAX_UPLOAD})
        return JSONResponse(err.to_dict(), status_code=422)
    try:
        return JSONResponse(fn(data, file.filename, **kwargs))
    except ToolError as exc:
        return JSONResponse(exc.to_dict(), status_code=422)


@router.post("/csv-reader")
async def csv_reader(file: UploadFile = File(...),
                     header_row: int = Form(1),
                     delimiter: str | None = Form(None),
                     decimal_separator: Literal["auto", ".", ","] = Form("auto"),
                     date_order: Literal["auto", "DMY", "MDY"] = Form("auto"),
                     row_offset: int = Form(0), row_limit: int = Form(50),
                     tail_rows: int = Form(5)):
    return await _run(file, read_csv, header_row=header_row, delimiter=delimiter or None,
                      decimal_separator=decimal_separator, date_order=date_order,
                      row_offset=row_offset, row_limit=row_limit, tail_rows=tail_rows)


@router.post("/spreadsheet-reader/sheets")
async def spreadsheet_sheets(file: UploadFile = File(...), preview_rows: int = Form(5),
                             sample_rows: int = Form(1000)):
    return await _run(file, list_sheets, preview_rows=preview_rows, sample_rows=sample_rows)


@router.post("/spreadsheet-reader/read")
async def spreadsheet_read(file: UploadFile = File(...), sheet: str = Form(...),
                           cell_range: str | None = Form(None), start_row: int = Form(1),
                           row_limit: int = Form(50), tail_rows: int = Form(5),
                           view: Literal["cells", "text", "both"] = Form("cells")):
    return await _run(file, read_sheet, sheet=sheet, cell_range=cell_range or None,
                      start_row=start_row, row_limit=row_limit, tail_rows=tail_rows, view=view)


@router.post("/pdf-text")
async def pdf_text(file: UploadFile = File(...), start_page: int = Form(1),
                   page_limit: int = Form(10), tail_pages: int = Form(1),
                   include_tables: bool = Form(True),
                   table_strategy: Literal["lines", "text"] = Form("lines"),
                   include_amounts: bool = Form(True)):
    return await _run(file, extract_pdf_text, start_page=start_page, page_limit=page_limit,
                      tail_pages=tail_pages, include_tables=include_tables,
                      table_strategy=table_strategy, include_amounts=include_amounts)


# ------------------------------------------------------------------ writers

_WRITER_PARAMS = {"csv": {"explode", "include_meta", "delimiter", "bom"}, "xlsx": set(),
                  "docx": {"title"}, "pdf": {"title"}}


async def _write(request: Request, fmt: str) -> Response:
    """Body: {"result": <a result, plain or extended>, "params": {...}} -> the file."""
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse(ToolError("param_invalid", "body must be JSON").to_dict(), status_code=422)
    if not isinstance(body, dict) or "result" not in body:
        return JSONResponse(ToolError("param_invalid", 'body must be {"result": ..., "params": {...}}').to_dict(),
                            status_code=422)
    params = body.get("params") or {}
    unknown = set(params) - _WRITER_PARAMS[fmt]
    if unknown:
        return JSONResponse(ToolError("param_invalid", f"unknown params: {', '.join(sorted(unknown))}").to_dict(),
                            status_code=422)
    try:
        data, media, ext = write(body["result"], fmt, **params)
    except ToolError as exc:
        return JSONResponse(exc.to_dict(), status_code=422)
    name = str(body.get("filename") or "extraction")
    return Response(data, media_type=media,
                    headers={"Content-Disposition": f'attachment; filename="{name}.{ext}"',
                             "X-Output-SHA256": hashlib.sha256(data).hexdigest()})


@router.post("/csv-writer")
async def csv_writer(request: Request) -> Response:
    """An extraction result as CSV (exploded by line items when there is one array field)."""
    return await _write(request, "csv")


@router.post("/excel-writer")
async def excel_writer(request: Request) -> Response:
    """An extraction result as an .xlsx workbook: Records, one sheet per array, Sources, Run."""
    return await _write(request, "xlsx")


@router.post("/docx-writer")
async def docx_writer(request: Request) -> Response:
    """An extraction result as a Word report."""
    return await _write(request, "docx")


@router.post("/pdf-writer")
async def pdf_writer(request: Request) -> Response:
    """An extraction result as a PDF report."""
    return await _write(request, "pdf")


# ------------------------------------------------------------------ rules (ZEN)

MAX_RULE_INPUTS = 10_000


@router.post("/rules")
async def rules(request: Request) -> JSONResponse:
    """Evaluate a ZEN decision (and the lookup tables it calls) over one input or many.

    Body: {"decision": <JDM>, "tables": {"tables/x.json": <JDM>, ...},
           "context": {...} | "contexts": [{...}, ...], "trace": false}
    """
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse(ToolError("param_invalid", "body must be JSON").to_dict(), status_code=422)
    if not isinstance(body, dict) or "decision" not in body:
        return JSONResponse(ToolError("param_invalid", 'body needs "decision"').to_dict(), status_code=422)
    contexts = body.get("contexts")
    if contexts is not None and (not isinstance(contexts, list) or len(contexts) > MAX_RULE_INPUTS):
        return JSONResponse(ToolError("param_invalid", f"contexts must be a list of at most {MAX_RULE_INPUTS}")
                            .to_dict(), status_code=422)
    try:
        return JSONResponse(evaluate_rules(body["decision"], body.get("context"), contexts=contexts,
                                           tables=body.get("tables"), trace=bool(body.get("trace"))))
    except ToolError as exc:
        return JSONResponse(exc.to_dict(), status_code=422)


app = FastAPI(title="DataExtractor tools")
app.include_router(router)

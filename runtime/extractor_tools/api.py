"""FastAPI endpoints, one per tool operation, so each can be tested alone.

Mount the router in the existing app:   app.include_router(router)
Or run standalone:                       uvicorn extractor_tools.api:app --port 8081

Tool errors return 422 with {"error", "permanent", "message", "detail"}.
"""
from __future__ import annotations

from typing import Callable, Literal

from fastapi import APIRouter, FastAPI, File, Form, UploadFile
from fastapi.responses import JSONResponse

from .common import ToolError
from .csv_reader import read_csv
from .pdf_text import extract_pdf_text
from .spreadsheet_reader import list_sheets, read_sheet

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


app = FastAPI(title="DataExtractor tools")
app.include_router(router)

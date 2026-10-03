"""Request bodies and response docs for routes, from our own records.

A route takes its body through ``json_body(Model)``: the raw JSON is parsed
and handed to ``Model.from_dict``, which validates it, so a bad payload is a
422 naming every problem. ``docs(Model, Output)`` publishes both contracts in
OpenAPI from the records' own JSON Schema, so ``/docs`` still shows each
component's exact input and output.

    @router.post("/x/run", **docs(XInput, XOutput))
    def x(payload: XInput = Depends(json_body(XInput))):
        return out(XAgent().run(payload))
"""

from __future__ import annotations

import json
from typing import Any, Callable

from fastapi import HTTPException, Request

from ..records import Record, ValidationError, to_jsonable


def invalid(exc: ValidationError) -> HTTPException:
    return HTTPException(status_code=422, detail={"code": "invalid_input", "message": str(exc),
                                                  "errors": exc.errors})


def json_body(model: type[Record]) -> Callable[..., Any]:
    """A FastAPI dependency that reads the request body as ``model``."""

    async def read(request: Request) -> Record:
        raw = await request.body()
        try:
            data = json.loads(raw or b"null")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail={"code": "invalid_json", "message": str(exc)}) from exc
        try:
            return model.from_dict(data)
        except ValidationError as exc:
            raise invalid(exc) from exc

    read.__name__ = f"read_{model.__name__}"
    return read


def docs(request: type[Record] | None = None, response: type[Record] | None = None, *,
         status: int = 200, many: bool = False) -> dict[str, Any]:
    """Route decorator arguments that publish the request and response schemas."""
    kwargs: dict[str, Any] = {"status_code": status}
    if request is not None:
        kwargs["openapi_extra"] = {"requestBody": {"required": True, "content": {
            "application/json": {"schema": request.json_schema()}}}}
    if response is not None:
        schema = response.json_schema()
        if many:
            schema = {"type": "array", "items": schema}
        kwargs["responses"] = {status: {"description": "OK", "content": {
            "application/json": {"schema": schema}}}}
    return kwargs


def out(value: Any) -> Any:
    """Records (and lists of them) -> JSON-ready data."""
    return to_jsonable(value)

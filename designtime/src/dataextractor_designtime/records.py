"""Typed records on stdlib dataclasses: validation, JSON, and JSON Schema.

Every contract, agent input and agent output is a ``Record``:

    @dataclass(kw_only=True)
    class Keyword(Record):
        term: str
        weight: Confidence
        support: int = 0

A record validates itself when it is built, however it is built, so a
``Corpus(**json_dict)`` and a ``Corpus.from_dict(json_dict)`` hold the same
typed objects or fail the same way:

* nested records, lists, dicts, sets and tuples are converted from plain JSON;
* ``str | None``, ``Literal[...]``, enums, ``datetime`` and ``date`` (ISO text
  accepted) are checked;
* ``Annotated[float, Range(0, 1)]`` bounds a number;
* unknown keys are refused unless the class sets ``__extra__ = "allow"``, in
  which case they are kept in its ``extra`` dict;
* a subclass's ``check()`` runs last for rules that span fields.

Failures raise ``ValidationError`` (a ``ValueError``) listing every problem
with its location, e.g. ``samples[3].attachments[0].filename: required``.

``to_dict()`` gives JSON-ready data (enums as values, dates as ISO text) and
``json_schema()`` the JSON Schema that the HTTP layer publishes in OpenAPI, so
the contract is written once.
"""

from __future__ import annotations

import dataclasses
import types
import typing
from dataclasses import MISSING, dataclass, field, fields
from datetime import date, datetime
from enum import Enum
from typing import Any, ClassVar, Literal, Union

__all__ = ["Confidence", "Range", "Record", "ValidationError", "dataclass", "field", "to_jsonable"]


class ValidationError(ValueError):
    """One or more fields failed; ``errors`` holds ``{"loc", "msg"}`` per problem."""

    def __init__(self, errors: list[dict[str, str]], model: str = "") -> None:
        self.errors = errors
        self.model = model
        lines = "; ".join(f"{e['loc'] or '(root)'}: {e['msg']}" for e in errors[:10])
        more = f" (+{len(errors) - 10} more)" if len(errors) > 10 else ""
        super().__init__(f"{model + ': ' if model else ''}{lines}{more}")


@dataclass(frozen=True)
class Range:
    """Numeric bounds for an ``Annotated`` field (inclusive)."""

    ge: float | None = None
    le: float | None = None


Confidence = typing.Annotated[float, Range(0.0, 1.0)]

_MISSING_VALUE = object()


def _join(loc: str, part: str | int) -> str:
    if isinstance(part, int):
        return f"{loc}[{part}]"
    return f"{loc}.{part}" if loc else part


def _name(tp: Any) -> str:
    return getattr(tp, "__name__", None) or str(tp).replace("typing.", "")


class _Fail(Exception):
    pass


def _coerce(value: Any, tp: Any, loc: str, errs: list[dict[str, str]]) -> Any:
    """Convert ``value`` to ``tp`` or record why not (and raise _Fail)."""

    def fail(msg: str) -> Any:
        errs.append({"loc": loc, "msg": msg})
        raise _Fail

    if tp is Any or tp is object:
        return value
    origin, args = typing.get_origin(tp), typing.get_args(tp)

    if origin is typing.Annotated:
        value = _coerce(value, args[0], loc, errs)
        for meta in args[1:]:
            if isinstance(meta, Range):
                if meta.ge is not None and value < meta.ge:
                    fail(f"must be >= {meta.ge}")
                if meta.le is not None and value > meta.le:
                    fail(f"must be <= {meta.le}")
        return value
    if origin in (Union, types.UnionType):
        if value is None:
            if type(None) in args:
                return None
            fail("must not be null")
        for option in (a for a in args if a is not type(None)):
            trial: list[dict[str, str]] = []
            try:
                return _coerce(value, option, loc, trial)
            except _Fail:
                continue
        fail(f"expected {' | '.join(_name(a) for a in args)}, got {type(value).__name__}")
    if tp is type(None):
        return value if value is None else fail("must be null")
    if origin is Literal:
        return value if value in args else fail(f"must be one of {list(args)}, got {value!r}")
    if value is None:
        fail("must not be null")

    if isinstance(tp, type):
        if issubclass(tp, Record):
            if isinstance(value, tp):
                return value
            if isinstance(value, dict):
                return tp._build(value, loc, errs)
            fail(f"expected an object ({tp.__name__}), got {type(value).__name__}")
        if issubclass(tp, Enum):
            if isinstance(value, tp):
                return value
            try:
                return tp(value)
            except ValueError:
                fail(f"must be one of {[m.value for m in tp]}, got {value!r}")
        if tp is bool:
            return value if isinstance(value, bool) else fail(f"expected a boolean, got {type(value).__name__}")
        if tp is int:
            if isinstance(value, int) and not isinstance(value, bool):
                return value
            if isinstance(value, float) and value.is_integer():
                return int(value)
            fail(f"expected an integer, got {type(value).__name__}")
        if tp is float:
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
            fail(f"expected a number, got {type(value).__name__}")
        if tp is str:
            return value if isinstance(value, str) else fail(f"expected a string, got {type(value).__name__}")
        if tp is datetime:
            if isinstance(value, datetime):
                return value
            if isinstance(value, str):
                try:
                    return datetime.fromisoformat(value.replace("Z", "+00:00"))
                except ValueError:
                    pass
            fail("expected an ISO 8601 date-time")
        if tp is date:
            if isinstance(value, date) and not isinstance(value, datetime):
                return value
            if isinstance(value, str):
                try:
                    return date.fromisoformat(value)
                except ValueError:
                    pass
            fail("expected an ISO 8601 date")
        if tp in (list, dict, set, tuple):
            origin, args = tp, ()

    if origin in (list, set, frozenset, tuple, typing.Sequence, typing.Iterable):
        if not isinstance(value, (list, tuple, set, frozenset)):
            fail(f"expected an array, got {type(value).__name__}")
        item = args[0] if args else Any
        out, bad = [], False
        for i, v in enumerate(value):
            try:
                out.append(_coerce(v, item, _join(loc, i), errs))
            except _Fail:
                bad = True
        if bad:
            raise _Fail
        return set(out) if origin is set else frozenset(out) if origin is frozenset else \
            tuple(out) if origin is tuple else out
    if origin in (dict, typing.Mapping):
        if not isinstance(value, dict):
            fail(f"expected an object, got {type(value).__name__}")
        kt, vt = (args + (Any, Any))[:2] if args else (Any, Any)
        out_d, bad = {}, False
        for k, v in value.items():
            try:
                out_d[_coerce(k, kt, _join(loc, str(k)), errs)] = _coerce(v, vt, _join(loc, str(k)), errs)
            except _Fail:
                bad = True
        if bad:
            raise _Fail
        return out_d
    if isinstance(tp, type) and isinstance(value, tp):
        return value
    return fail(f"expected {_name(tp)}, got {type(value).__name__}")


_HINTS: dict[type, dict[str, Any]] = {}


def _hints(cls: type) -> dict[str, Any]:
    if cls not in _HINTS:
        _HINTS[cls] = typing.get_type_hints(cls, include_extras=True)
    return _HINTS[cls]


class Record:
    """Base for typed records. Subclasses are ``@dataclass(kw_only=True)``."""

    #: "forbid" refuses unknown keys; "allow" keeps them in the ``extra`` field.
    __extra__: ClassVar[str] = "forbid"

    def __post_init__(self) -> None:
        errs: list[dict[str, str]] = []
        self._validate(errs, "")
        if errs:
            raise ValidationError(errs, type(self).__name__)

    def _validate(self, errs: list[dict[str, str]], loc: str) -> None:
        hints = _hints(type(self))
        failed = False
        for f in fields(self):
            if not f.init:
                continue
            try:
                setattr(self, f.name, _coerce(getattr(self, f.name), hints[f.name], _join(loc, f.name), errs))
            except _Fail:
                failed = True
        if failed:
            return
        try:
            self.check()
        except ValueError as exc:
            if isinstance(exc, ValidationError):
                errs.extend({"loc": _join(loc, e["loc"]) if e["loc"] else loc, "msg": e["msg"]}
                            for e in exc.errors)
            else:
                errs.append({"loc": loc, "msg": str(exc)})

    def check(self) -> None:
        """Override for rules beyond types. Raise ValueError (or ValidationError) to refuse."""

    # -------------------------------------------------------------- building

    @classmethod
    def _build(cls, data: dict[str, Any], loc: str, errs: list[dict[str, str]]):
        known = {f.name: f for f in fields(cls) if f.init}
        kwargs: dict[str, Any] = {}
        extra = {k: v for k, v in data.items() if k not in known}
        if extra:
            if cls.__extra__ == "allow" and "extra" in known:
                kwargs["extra"] = {**data.get("extra", {}), **extra} if isinstance(data.get("extra"), dict) else extra
            else:
                for k in extra:
                    errs.append({"loc": _join(loc, str(k)), "msg": "unknown field"})
        missing = False
        for name, f in known.items():
            if name in data:
                kwargs.setdefault(name, data[name])
            elif f.default is MISSING and f.default_factory is MISSING:
                errs.append({"loc": _join(loc, name), "msg": "required"})
                missing = True
        refused = bool(extra) and "extra" not in kwargs
        hints = _hints(cls)
        values: dict[str, Any] = {}
        before = len(errs)
        for name, f in known.items():            # check what is there, even if the record cannot be built
            if name in kwargs:
                try:
                    values[name] = _coerce(kwargs[name], hints[name], _join(loc, name), errs)
                except _Fail:
                    pass
        if missing or refused or len(errs) > before:
            raise _Fail
        obj = cls.__new__(cls)
        for name, f in known.items():
            if name in values:
                object.__setattr__(obj, name, values[name])
            elif f.default is not MISSING:
                object.__setattr__(obj, name, f.default)
            else:
                object.__setattr__(obj, name, f.default_factory())
        for f in fields(cls):
            if not f.init and f.default_factory is not MISSING:
                object.__setattr__(obj, f.name, f.default_factory())
        obj._validate(errs, loc)               # defaults, then check()
        if len(errs) > before:
            raise _Fail
        return obj

    @classmethod
    def from_dict(cls, data: Any):
        """Build from parsed JSON, refusing anything the type does not allow."""
        errs: list[dict[str, str]] = []
        if not isinstance(data, dict):
            raise ValidationError([{"loc": "", "msg": f"expected an object, got {type(data).__name__}"}],
                                  cls.__name__)
        try:
            return cls._build(data, "", errs)
        except _Fail:
            raise ValidationError(errs, cls.__name__) from None

    # -------------------------------------------------------------- output

    def to_dict(self, *, exclude_none: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in fields(self):
            v = getattr(self, f.name)
            if f.name == "extra" and type(self).__extra__ == "allow" and isinstance(v, dict):
                out.update({k: to_jsonable(x, exclude_none) for k, x in v.items()})
                continue
            if exclude_none and v is None:
                continue
            out[f.name] = to_jsonable(v, exclude_none)
        return out

    def replace(self, **changes: Any):
        """A copy with ``changes`` applied, validated like any new record."""
        return dataclasses.replace(self, **changes)

    # -------------------------------------------------------------- schema

    @classmethod
    def json_schema(cls) -> dict[str, Any]:
        hints = _hints(cls)
        props: dict[str, Any] = {}
        required: list[str] = []
        for f in fields(cls):
            if not f.init or (f.name == "extra" and cls.__extra__ == "allow"):
                continue
            schema = _schema(hints[f.name])
            if f.default is not MISSING and f.default is not None and \
                    isinstance(f.default, (str, int, float, bool)):
                schema = {**schema, "default": f.default.value if isinstance(f.default, Enum) else f.default}
            if f.metadata.get("description"):
                schema = {**schema, "description": f.metadata["description"]}
            props[f.name] = schema
            if f.default is MISSING and f.default_factory is MISSING:
                required.append(f.name)
        out: dict[str, Any] = {"title": cls.__name__, "type": "object", "properties": props,
                               "additionalProperties": cls.__extra__ == "allow"}
        if required:
            out["required"] = required
        doc = (cls.__doc__ or "").strip()
        if doc and not doc.startswith(cls.__name__ + "("):
            out["description"] = doc.split("\n\n")[0]
        return out


def _schema(tp: Any) -> dict[str, Any]:
    if tp is Any or tp is object:
        return {}
    origin, args = typing.get_origin(tp), typing.get_args(tp)
    if origin is typing.Annotated:
        s = dict(_schema(args[0]))
        for meta in args[1:]:
            if isinstance(meta, Range):
                if meta.ge is not None:
                    s["minimum"] = meta.ge
                if meta.le is not None:
                    s["maximum"] = meta.le
        return s
    if origin in (Union, types.UnionType):
        return {"anyOf": [_schema(a) for a in args]}
    if tp is type(None):
        return {"type": "null"}
    if origin is Literal:
        return {"enum": list(args)}
    if isinstance(tp, type):
        if issubclass(tp, Record):
            return tp.json_schema()
        if issubclass(tp, Enum):
            return {"enum": [m.value for m in tp]}
        simple = {bool: "boolean", int: "integer", float: "number", str: "string"}
        if tp in simple:
            return {"type": simple[tp]}
        if tp is datetime:
            return {"type": "string", "format": "date-time"}
        if tp is date:
            return {"type": "string", "format": "date"}
        if tp in (list, set, tuple):
            return {"type": "array"}
        if tp is dict:
            return {"type": "object"}
    if origin in (list, set, frozenset, tuple, typing.Sequence, typing.Iterable):
        return {"type": "array", "items": _schema(args[0]) if args else {}}
    if origin in (dict, typing.Mapping):
        return {"type": "object", "additionalProperties": _schema(args[1]) if len(args) > 1 else {}}
    return {}


def to_jsonable(value: Any, exclude_none: bool = False) -> Any:
    """Records, enums, dates, sets and tuples -> plain JSON data."""
    if isinstance(value, Record):
        return value.to_dict(exclude_none=exclude_none)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k.value if isinstance(k, Enum) else k: to_jsonable(v, exclude_none) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v, exclude_none) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted((to_jsonable(v, exclude_none) for v in value), key=repr)
    return value

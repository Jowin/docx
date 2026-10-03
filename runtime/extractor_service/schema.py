"""The data dictionary: which fields to extract, their types and their rules.

A data dictionary is ``schema.json`` in a config folder:

    {
      "name": "invoice",
      "version": "1.0.0",
      "fields": [
        {"name": "invoice_number", "type": "string", "required": true,
         "aliases": ["invoice no", "invoice number"], "pattern": "^[A-Z0-9-]+$"},
        {"name": "total_amount", "type": "decimal", "required": true, "critical": true},
        {"name": "line_items", "type": "array", "items": [
            {"name": "description", "type": "string"},
            {"name": "amount", "type": "decimal"}]}
      ]
    }

Field types: string, integer, decimal, date, boolean, enum (with ``values``),
and array (of objects whose ``items`` are scalar fields).

Output is always a list of records of this shape. ``record_key`` names the
field that tells records apart (an invoice number): two sources holding the
same key describe the same record. When omitted it defaults to the first
required string field, else the first scalar field.

``grounding`` is "required" (default: the value must be found in the
document it is cited from) or "optional" (the value may be inferred, such as
an ISO currency code read from a "$" sign).
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from extractor_tools.values import find_amounts, parse_date, parse_number

from .errors import ConfigError

SCALAR_TYPES = ("string", "integer", "decimal", "date", "boolean", "enum")
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_DATE_TOKEN = re.compile(
    r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?"
    r"|\d{1,2}[/.-]\d{1,2}[/.-](?:\d{4}|\d{2})"
    r"|\d{1,2}[\s-][A-Za-z]{3,9}\.?[\s-]\d{2,4}"
    r"|[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4}"
    r"|\d{4}[/.]\d{1,2}[/.]\d{1,2}")
_TRUE = {"true", "yes", "y", "1", "paid", "x"}
_FALSE = {"false", "no", "n", "0", "unpaid"}


@dataclass(frozen=True)
class Field:
    name: str
    type: str
    required: bool = False
    critical: bool = False
    description: str = ""
    aliases: tuple[str, ...] = ()
    pattern: str | None = None
    values: tuple[str, ...] = ()
    grounding: str = "required"
    items: tuple["Field", ...] = ()

    @property
    def labels(self) -> tuple[str, ...]:
        """Aliases plus the field name as words, lower case, longest first."""
        names = {a.strip().lower() for a in self.aliases if a.strip()}
        names.add(self.name.replace("_", " "))
        return tuple(sorted(names, key=lambda s: (-len(s), s)))

    def describe(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "type": self.type}
        for key in ("required", "critical"):
            if getattr(self, key):
                out[key] = True
        if self.description:
            out["description"] = self.description
        if self.aliases:
            out["aliases"] = list(self.aliases)
        if self.pattern:
            out["pattern"] = self.pattern
        if self.values:
            out["values"] = list(self.values)
        if self.grounding != "required":
            out["grounding"] = self.grounding
        if self.items:
            out["items"] = [f.describe() for f in self.items]
        return out


@dataclass(frozen=True)
class DataDictionary:
    name: str
    version: str
    fields: tuple[Field, ...]
    description: str = ""
    record_key: str = ""
    _index: dict[str, Field] = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        self._index.update({f.name: f for f in self.fields})

    def get(self, name: str) -> Field | None:
        return self._index.get(name)

    @property
    def required(self) -> tuple[Field, ...]:
        return tuple(f for f in self.fields if f.required)

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "version": self.version, "description": self.description,
                "record_key": self.record_key, "fields": [f.describe() for f in self.fields]}


# ------------------------------------------------------------------ loading

def _field(obj: Any, where: str, *, nested: bool) -> Field:
    if not isinstance(obj, dict):
        raise ConfigError("schema_invalid", f"{where}: a field must be an object")
    name = obj.get("name")
    if not isinstance(name, str) or not _NAME.match(name):
        raise ConfigError("schema_invalid", f"{where}: name must be snake_case, got {name!r}")
    ftype = obj.get("type")
    allowed = SCALAR_TYPES if nested else SCALAR_TYPES + ("array",)
    if ftype not in allowed:
        raise ConfigError("schema_invalid", f"{where}.{name}: type must be one of {list(allowed)}")
    pattern = obj.get("pattern")
    if pattern is not None:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ConfigError("schema_invalid", f"{where}.{name}: bad pattern: {exc}") from exc
    values = tuple(str(v) for v in obj.get("values", []))
    if ftype == "enum" and not values:
        raise ConfigError("schema_invalid", f"{where}.{name}: enum needs values")
    grounding = obj.get("grounding", "required")
    if grounding not in ("required", "optional"):
        raise ConfigError("schema_invalid", f"{where}.{name}: grounding must be required or optional")
    items: tuple[Field, ...] = ()
    if ftype == "array":
        raw_items = obj.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            raise ConfigError("schema_invalid", f"{where}.{name}: array needs items")
        items = tuple(_field(i, f"{where}.{name}", nested=True) for i in raw_items)
        _unique(items, f"{where}.{name}")
    aliases = obj.get("aliases", [])
    if not isinstance(aliases, list) or not all(isinstance(a, str) for a in aliases):
        raise ConfigError("schema_invalid", f"{where}.{name}: aliases must be a list of strings")
    return Field(name=name, type=ftype, required=bool(obj.get("required", False)),
                 critical=bool(obj.get("critical", False)),
                 description=str(obj.get("description", "")), aliases=tuple(aliases),
                 pattern=pattern, values=values, grounding=grounding, items=items)


def _unique(fields: tuple[Field, ...], where: str) -> None:
    seen = set()
    for f in fields:
        if f.name in seen:
            raise ConfigError("schema_invalid", f"{where}: duplicate field {f.name!r}")
        seen.add(f.name)


def load_dictionary(obj: Any) -> DataDictionary:
    if not isinstance(obj, dict) or not isinstance(obj.get("fields"), list) or not obj["fields"]:
        raise ConfigError("schema_invalid", "schema.json needs a non-empty 'fields' list")
    fields = tuple(_field(f, "fields", nested=False) for f in obj["fields"])
    _unique(fields, "fields")
    scalars = [f for f in fields if f.type != "array"]
    if not scalars:
        raise ConfigError("schema_invalid", "fields need at least one scalar field to key records")
    key = obj.get("record_key")
    if key is None:
        key = next((f.name for f in scalars if f.required and f.type == "string"), scalars[0].name)
    elif key not in {f.name for f in scalars}:
        raise ConfigError("schema_invalid", f"record_key {key!r} must name a scalar field")
    return DataDictionary(name=str(obj.get("name", "extraction")),
                          version=str(obj.get("version", "1")), fields=fields,
                          description=str(obj.get("description", "")), record_key=key)


# ------------------------------------------------------------------ values

def first_date_token(text: str) -> str | None:
    m = _DATE_TOKEN.search(text)
    return m.group(0) if m else None


def date_tokens(text: str) -> list[str]:
    return [m.group(0) for m in _DATE_TOKEN.finditer(text)]


def normalize(f: Field, raw: Any, *, date_order: str | None = None) -> tuple[Any, str | None]:
    """Raw extracted value -> (typed value, error). ``(None, None)`` = absent.

    Typed values: str, int, Decimal, ISO date str, bool. Errors are short
    codes: wrong_type, pattern, not_in_values, ambiguous_date.
    """
    if raw is None or (isinstance(raw, str) and raw.strip().lower() in ("", "null", "none", "n/a")):
        return None, None
    if f.type == "string":
        s = " ".join(str(raw).split())
        if f.pattern and not re.search(f.pattern, s):
            return s, "pattern"
        return s, None
    if f.type == "enum":
        s = str(raw).strip()
        for v in f.values:
            if v.lower() == s.lower():
                return v, None
        return s, "not_in_values"
    if f.type == "boolean":
        if isinstance(raw, bool):
            return raw, None
        s = str(raw).strip().lower()
        if s in _TRUE:
            return True, None
        if s in _FALSE:
            return False, None
        return str(raw), "wrong_type"
    if f.type in ("integer", "decimal"):
        d = _to_decimal(raw)
        if d is None:
            return str(raw), "wrong_type"
        if f.type == "integer":
            if d != d.to_integral_value():
                return str(raw), "wrong_type"
            return int(d), None
        return d, None
    if f.type == "date":
        if isinstance(raw, dt.datetime):
            return raw.date().isoformat(), None
        if isinstance(raw, dt.date):
            return raw.isoformat(), None
        s = str(raw).strip()
        token = first_date_token(s) or s
        parsed = parse_date(token, date_order)
        if parsed is None:
            return s, "wrong_type"
        if parsed["type"] == "date_ambiguous":
            return s, "ambiguous_date"
        return parsed["value"][:10], None
    return raw, "wrong_type"


def _to_decimal(raw: Any) -> Decimal | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return Decimal(raw)
    if isinstance(raw, float):
        return Decimal(format(raw, ".15g"))
    if isinstance(raw, Decimal):
        return raw
    s = str(raw).strip()
    for sep in (".", ","):
        p = parse_number(s, sep)
        if p and p["type"] in ("integer", "decimal"):
            try:
                return Decimal(str(p["value"]))
            except InvalidOperation:
                return None
        if p:
            break
    amounts = find_amounts(s)
    if amounts:
        return Decimal(amounts[0]["value"])
    return None


def to_json(value: Any, decimal_format: str = "number") -> Any:
    """Typed value -> JSON-native. Decimals become numbers unless configured as text."""
    if isinstance(value, Decimal):
        if decimal_format == "string":       # canonical: 12400.00 -> "12400", 0.50 -> "0.5"
            text = format(value.normalize(), "f")
            return "0" if text in ("-0", "") else text
        return int(value) if value == value.to_integral_value() and abs(value) < 2 ** 53 \
            else float(value)
    if isinstance(value, list):
        return [to_json(v, decimal_format) for v in value]
    if isinstance(value, dict):
        return {k: to_json(v, decimal_format) for k, v in value.items()}
    return value

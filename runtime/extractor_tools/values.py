"""Deterministic typing of text cells.

The rule for this module: when a value is genuinely ambiguous, report the
ambiguity instead of choosing. ``03/04/2026`` is ``date_ambiguous`` with both
candidates unless other cells in the same column settle the order. Picking a
reading is a judgment step and belongs to a skill, not a tool.

Value encoding in output (JSON-native, exact):
  integer         -> int
  decimal         -> str, canonical Decimal text ("12400.50")
  percent         -> str, as a fraction ("0.15" for "15%"), same as Excel
  date            -> "YYYY-MM-DD"
  datetime        -> "YYYY-MM-DDTHH:MM:SS"
  boolean         -> bool
  string          -> str (stripped of outer whitespace)
  date_ambiguous  -> None, with "candidates": {"DMY": ..., "MDY": ...}
"""
from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal, InvalidOperation
from typing import Any

ISO_CURRENCIES = tuple(sorted(
    "USD EUR GBP JPY CHF CAD AUD NZD INR CNY HKD SGD SEK NOK DKK ZAR MXN BRL "
    "AED SAR PLN CZK HUF ILS KRW TRY THB MYR IDR PHP".split()))
# Symbols are reported as seen. Mapping "$" to a currency code is a judgment
# (USD? CAD? AUD?) and is left to the extraction skill.
CURRENCY_SYMBOLS = ("R$", "US$", "C$", "A$", "$", "€", "£", "¥", "₹", "₩", "₽", "₺", "₪", "₫", "฿")

_WS = re.compile(r"[\s\u00a0\u2007\u202f]+")
_MONTHS = {m: i for i, m in enumerate(
    "jan feb mar apr may jun jul aug sep oct nov dec".split(), start=1)}
_MONTHS.update({m: i for i, m in enumerate(
    "january february march april may june july august september october "
    "november december".split(), start=1)})
_MONTHS["sept"] = 9

_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?)?$")
_YMD_SLASH = re.compile(r"^(\d{4})[/.](\d{1,2})[/.](\d{1,2})$")
_NUMERIC_DATE = re.compile(r"^(\d{1,2})([/.-])(\d{1,2})\2(\d{2}|\d{4})$")
_DAY_MON_YEAR = re.compile(r"^(\d{1,2})[\s-]([A-Za-z]{3,9})\.?[\s-](\d{2}|\d{4})$")
_MON_DAY_YEAR = re.compile(r"^([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})$")


def _year(y: str) -> int:
    # Excel's two-digit-year rule: 00-29 -> 20xx, 30-99 -> 19xx.
    n = int(y)
    if len(y) == 2:
        return 2000 + n if n < 30 else 1900 + n
    return n


def _mkdate(y: int, m: int, d: int) -> str | None:
    try:
        return dt.date(y, m, d).isoformat()
    except ValueError:
        return None


# ------------------------------------------------------------------ evidence
# Column-level evidence is gathered in a first pass over every cell, then
# applied in the second pass. Both passes are pure functions of the column.

def date_order_vote(text: str) -> str | None:
    """'DMY' / 'MDY' when a numeric date is unambiguous in one order only."""
    m = _NUMERIC_DATE.match(text.strip())
    if not m:
        return None
    a, b = int(m.group(1)), int(m.group(3))
    if a > 12 and b <= 12:
        return "DMY"
    if b > 12 and a <= 12:
        return "MDY"
    return None


def decimal_sep_vote(text: str) -> str | None:
    """'.' or ',' when the text proves which character is the decimal mark."""
    core = re.sub(r"[^0-9.,]", "", text)
    if not core or not re.search(r"\d", core):
        return None
    has_dot, has_comma = "." in core, "," in core
    if has_dot and has_comma:
        return "." if core.rfind(".") > core.rfind(",") else ","
    sep = "." if has_dot else "," if has_comma else None
    if sep is None:
        return None
    parts = core.split(sep)
    if len(parts) > 2:                       # 1.234.567 -> sep is grouping
        return "," if sep == "." else "."
    if len(parts[1]) != 3:                   # 12,5 or 12.50 -> sep is decimal
        return sep
    return None                              # 1,234 / 1.234 prove nothing


def resolve_votes(votes: list[str], default: str) -> tuple[str, str]:
    """-> (choice, evidence) where evidence is inferred | default | conflict."""
    kinds = set(votes)
    if len(kinds) == 1:
        return votes[0], "inferred"
    if not kinds:
        return default, "default"
    return default, "conflict"


# ------------------------------------------------------------------ parsing

_ISO_SET = frozenset(ISO_CURRENCIES)


def _strip_currency(s: str) -> tuple[str, str | None]:
    head, tail = s[:3], s[-3:]
    if head in _ISO_SET and (len(s) == 3 or not s[3].isalpha()):
        return s[3:].strip(), head
    if tail in _ISO_SET and (len(s) == 3 or not s[-4].isalpha()):
        return s[:-3].strip(), tail
    if s[:1].isdigit() and s[-1:].isdigit():
        return s, None                         # fast path: plain number
    for sym in CURRENCY_SYMBOLS:
        if s.startswith(sym):
            return s[len(sym):].strip(), sym
        if s.endswith(sym):
            return s[:-len(sym)].strip(), sym
    return s, None


def _shape(dec: str) -> tuple[re.Pattern, re.Pattern]:
    g = "[" + re.escape(",' " if dec == "." else ". '") + "]"
    d = re.escape(dec)
    return (re.compile(rf"^(?:\d{{1,3}}(?:{g}\d{{3}})+|\d+)?(?:{d}\d+)?$"), re.compile(g))


_NUM_SHAPE = {".": _shape("."), ",": _shape(",")}


def parse_number(text: str, decimal_sep: str = ".") -> dict[str, Any] | None:
    s = _WS.sub(" ", text).strip().replace("\u2212", "-")   # Unicode minus sign
    if not s:
        return None
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1].strip()
    if s.startswith("-"):
        negative, s = not negative, s[1:].strip()
    s, currency = _strip_currency(s)
    if s.startswith("-"):                      # "$-12.00"
        negative, s = not negative, s[1:].strip()
    if s.endswith("-"):                        # trailing minus, ledger style
        negative, s = not negative, s[:-1].strip()
    percent = s.endswith("%")
    if percent:
        s = s[:-1].strip()
    shape, group = _NUM_SHAPE[decimal_sep]
    if not shape.match(s) or not any(ch.isdigit() for ch in s):
        return None
    digits = group.sub("", s).replace(decimal_sep, ".")
    if re.match(r"^0\d", digits) and "." not in digits and not currency:
        return None                            # "00123" is an identifier, keep as text
    try:
        value = Decimal(digits)
    except InvalidOperation:
        return None
    if negative:
        value = -value
    if percent:
        value = value / 100
    out: dict[str, Any] = {}
    if percent:
        out.update(type="percent", value=_canon(value))
    elif "." in digits:
        out.update(type="decimal", value=_canon(value))
    else:
        out.update(type="integer", value=int(value))
    if currency:
        out["currency"] = currency
    return out


def _canon(d: Decimal) -> str:
    """Canonical text: no exponent, keeps meaningful trailing zeros from input."""
    text = format(d, "f")
    return "0" if text in ("-0", "") else text


def parse_date(text: str, order: str | None) -> dict[str, Any] | None:
    s = _WS.sub(" ", text).strip()
    m = _ISO_DATE.match(s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if m.group(4):
            try:
                v = dt.datetime(y, mo, d, int(m.group(4)), int(m.group(5)),
                                int(m.group(6) or 0))
            except ValueError:
                return None
            return {"type": "datetime", "value": v.isoformat()}
        iso = _mkdate(y, mo, d)
        return {"type": "date", "value": iso} if iso else None
    m = _YMD_SLASH.match(s)
    if m:
        iso = _mkdate(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return {"type": "date", "value": iso} if iso else None
    m = _DAY_MON_YEAR.match(s)
    if m and m.group(2).lower() in _MONTHS:
        iso = _mkdate(_year(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1)))
        return {"type": "date", "value": iso} if iso else None
    m = _MON_DAY_YEAR.match(s)
    if m and m.group(1).lower() in _MONTHS:
        iso = _mkdate(int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2)))
        return {"type": "date", "value": iso} if iso else None
    m = _NUMERIC_DATE.match(s)
    if m:
        a, b, y = int(m.group(1)), int(m.group(3)), _year(m.group(4))
        dmy, mdy = _mkdate(y, b, a), _mkdate(y, a, b)
        if order == "DMY" and dmy:
            return {"type": "date", "value": dmy}
        if order == "MDY" and mdy:
            return {"type": "date", "value": mdy}
        if dmy and mdy and dmy != mdy:
            return {"type": "date_ambiguous", "value": None,
                    "candidates": {"DMY": dmy, "MDY": mdy}}
        only = dmy or mdy
        return {"type": "date", "value": only} if only else None
    return None


def type_text(text: str, *, decimal_sep: str = ".", date_order: str | None = None) -> dict[str, Any]:
    """Type one text cell. Always returns {'type', 'value', ...}."""
    s = text.strip()
    if not s:
        return {"type": "empty", "value": None}
    low = s.lower()
    if low in ("true", "false"):
        return {"type": "boolean", "value": low == "true"}
    date = parse_date(s, date_order)
    if date:
        return date
    num = parse_number(s, decimal_sep)
    if num:
        return num
    return {"type": "string", "value": s}


def column_type(type_counts: dict[str, int]) -> str:
    """Dominant type of a column from its non-empty cell type counts."""
    kinds = {k for k, n in type_counts.items() if n and k != "empty"}
    if not kinds:
        return "empty"
    if kinds <= {"integer", "decimal"}:
        return "decimal" if "decimal" in kinds else "integer"
    if kinds <= {"date", "date_ambiguous"}:
        return "date"
    if kinds <= {"date", "datetime"}:
        return "datetime"
    if len(kinds) == 1:
        return next(iter(kinds))
    return "mixed"


# ------------------------------------------------------------------ amounts in free text
# Used where a value sits inside prose or a "Label: value" string: PDF lines,
# document-style Excel cells. Only numbers with a currency marker count, so
# dates, IDs, phone numbers and quantities are not reported as amounts.

_SYM_RE = "|".join(re.escape(s) for s in sorted(CURRENCY_SYMBOLS, key=len, reverse=True))
_ISO_RE = "|".join(ISO_CURRENCIES)
_CUR = rf"(?:{_SYM_RE}|(?<![A-Za-z])(?:{_ISO_RE})(?![A-Za-z]))"
_NUM = (r"\d{1,2}(?:,\d{2})+,\d{3}(?:\.\d{1,2})?"            # Indian lakh: 1,00,000.00
        r"|\d{1,3}(?:[,.'\u00a0 ]\d{3})+(?:[.,]\d{1,2})?"
        r"|\d+(?:[.,]\d{1,2})?")
_LAKH = re.compile(r"^\d{1,2}(?:,\d{2})+,\d{3}(?:\.\d{1,2})?$")
_AMOUNT = re.compile(
    rf"(?P<open>\()?\s?(?P<sign0>[-\u2212])?\s?"
    rf"(?:(?P<pre>{_CUR})\s?(?P<sign1>[-\u2212])?\s?(?<![\d.,])(?P<n1>{_NUM})(?![\d])"
    rf"|(?<![\w.,])(?P<n2>{_NUM})(?![\d])(?P<tsign>-)?\s?(?P<post>{_CUR}))"
    rf"\s?(?P<close>\))?(?P<trail>-(?![\w]))?")


def find_amounts(text: str) -> list[dict[str, Any]]:
    """Currency amounts inside free text, with character spans.

    Each: {"text", "start", "end", "value", "currency", "decimal_separator",
    "decimal_separator_evidence"}. A figure like "1.234 €" has no evidence for
    its decimal mark and is read with "." — the evidence field says "default"
    so the skill can challenge it. Only numbers next to a currency marker are
    reported, so dates, IDs, phone numbers and quantities are left out.
    """
    out = []
    for m in _AMOUNT.finditer(text):
        num = (m.group("n1") or m.group("n2")).replace("\u00a0", " ")
        currency = (m.group("pre") or m.group("post")).strip()
        paren = bool(m.group("open") and m.group("close"))
        negative = paren or bool(m.group("sign0") or m.group("sign1") or m.group("tsign")
                                 or m.group("trail"))
        if _LAKH.match(num):
            sep, evidence = ".", "inferred"
            parsed = parse_number(num.replace(",", ""), ".")
        elif "." not in num and "," not in num:
            sep, evidence = ".", "not_applicable"
            parsed = parse_number(num, ".")
        else:
            vote = decimal_sep_vote(num)
            sep, evidence = (vote, "inferred") if vote else (".", "default")
            parsed = parse_number(num, sep)
        if not parsed:
            continue
        value = str(parsed["value"])
        if negative and not value.startswith("-"):
            value = "-" + value
        start, end = m.span()
        raw = text[start:end]
        lead = len(raw) - len(raw.lstrip(" (" if not paren else " "))
        trail = len(raw) - len(raw.rstrip(" )" if not paren else " "))
        start, end = start + lead, end - trail
        out.append({"text": text[start:end], "start": start, "end": end,
                    "value": value, "currency": currency, "decimal_separator": sep,
                    "decimal_separator_evidence": evidence})
    return out

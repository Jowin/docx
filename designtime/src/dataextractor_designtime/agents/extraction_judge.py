"""Extraction Judge: did one isolated runtime extraction succeed?

Two modes, chosen by whether ground truth was supplied:

* ``ground_truth`` - every field the ground truth names is compared with the
  runtime's value, record by record (records pair up on the dictionary's
  ``record_key`` when the ground truth carries it, otherwise by position; records
  whose key was missed pair up by position with what is left).
  Missing and extra records are failures. With ``strict`` (default) any
  flag is a failure too, so a correct value the runtime was not confident
  about still asks for a skill.
* ``flags`` - no ground truth, so the runtime's own verdict is the test: any
  flag (missing required field, unverified value, low confidence, unplaced
  content, a skipped attachment, ...) is a failure.

The score orders outcomes for the learning loop: fewer value failures first,
then fewer flags. All of this is mechanical; nothing here needs a
model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from ..records import Record
from .base import AgentError, DesignAgent

FailureKind = Literal[
    "mismatch", "missing_value", "unexpected_value", "missing_record", "extra_record",
    "item_count", "item_mismatch", "flag",
]
_TRUE = {"true", "yes", "y", "1"}
_FALSE = {"false", "no", "n", "0"}


@dataclass(kw_only=True)
class Failure(Record):
    kind: FailureKind
    record: int | None = None
    field: str | None = None
    expected: Any = None
    actual: Any = None
    #: CTR-13 source the runtime cited for the actual value, when it cited one.
    source: str | None = None
    #: The runtime's flag, for ``flag`` failures.
    reason: str | None = None


@dataclass(kw_only=True)
class Score(Record):
    value_failures: int = 0
    flag_failures: int = 0
    fields_checked: int = 0
    fields_correct: int = 0

    def key(self) -> tuple[int, int]:
        """Lower is better."""
        return (self.value_failures, self.flag_failures)


@dataclass(kw_only=True)
class ExtractionJudgeInput(Record):
    #: The runtime's extended output (``"extended": true``).
    output: dict[str, Any]
    #: The data dictionary the runtime used (its ``describe()``).
    dictionary: dict[str, Any]
    #: Expected records for this source; omit to judge on the runtime's flags alone.
    ground_truth: list[dict[str, Any]] | None = None
    strict: bool = True


@dataclass(kw_only=True)
class ExtractionJudgeOutput(Record):
    passed: bool
    mode: Literal["ground_truth", "flags"]
    #: The runtime flagged the result.
    flagged: bool
    failures: list[Failure] = field(default_factory=list)
    #: Top-level dictionary fields a skill should address, in dictionary order.
    failing_fields: list[str] = field(default_factory=list)
    score: Score


# ------------------------------------------------------------------ comparison


def _num(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value))
    text = str(value).strip()
    neg = text.startswith("(") and text.endswith(")") or text.startswith("-")
    digits = re.sub(r"[^\d.]", "", text.replace(",", ""))
    if not digits or digits.count(".") > 1:
        return None
    try:
        d = Decimal(digits)
    except InvalidOperation:
        return None
    return -d if neg else d


def _text(value: Any) -> str:
    return " ".join(str(value).split()).casefold()


def same(ftype: str, expected: Any, actual: Any) -> bool:
    """Ground truth vs runtime value, for one field type."""
    if expected is None or expected == "":
        return actual is None or actual == ""
    if actual is None:
        return False
    if ftype in ("decimal", "integer"):
        a, b = _num(expected), _num(actual)
        if a is not None and b is not None:
            return a == b
    if ftype == "date":
        return str(expected).strip()[:10] == str(actual).strip()[:10]
    if ftype == "boolean":
        def as_bool(v: Any) -> Any:
            if isinstance(v, bool):
                return v
            s = str(v).strip().lower()
            return True if s in _TRUE else False if s in _FALSE else s
        return as_bool(expected) == as_bool(actual)
    return _text(expected) == _text(actual)


def field_of_reason(reason: str, names: set[str]) -> str | None:
    """``missing_field:vendor`` -> vendor; ``unverified_value:line_items[0].amount`` -> line_items."""
    _, _, rest = reason.partition(":")
    head = re.split(r"[.\[]", rest, maxsplit=1)[0]
    return head if head in names else None


class ExtractionJudge(DesignAgent[ExtractionJudgeInput, ExtractionJudgeOutput]):
    name = "extraction-judge"

    def run(self, payload: ExtractionJudgeInput) -> ExtractionJudgeOutput:
        fields = payload.dictionary.get("fields")
        if not isinstance(fields, list) or not fields:
            raise AgentError("dictionary has no fields", code="invalid_input")
        spec = {f["name"]: f for f in fields}
        names = set(spec)
        out = payload.output
        data = out.get("data") or []
        records = out.get("records") or [{} for _ in data]
        flagged = bool(out.get("flagged", out.get("status") not in (None, "extracted")))

        failures: list[Failure] = []
        checked = correct = 0
        if payload.ground_truth is not None:
            gt = [r for r in payload.ground_truth if isinstance(r, dict)]
            unknown = sorted({k for r in gt for k in r} - names)
            if unknown:
                raise AgentError(f"ground truth names fields the dictionary lacks: {unknown}",
                                 code="ground_truth_invalid", detail={"unknown_fields": unknown})
            pairs, missing, extra = self._pair(gt, data, payload.dictionary.get("record_key"), spec)
            for gi, di in pairs:
                c, k, fs = self._compare(gt[gi], data[di], records[di] if di < len(records) else {},
                                         spec, di)
                checked, correct = checked + c, correct + k
                failures.extend(fs)
            key = payload.dictionary.get("record_key")
            for gi in missing:
                failures.append(Failure(kind="missing_record", field=key,
                                        expected=gt[gi].get(key) if key else None))
                checked += len(gt[gi])
            for di in extra:
                failures.append(Failure(kind="extra_record", record=di, field=key,
                                        actual=data[di].get(key) if key else None))
        value_failures = len(failures)

        flags: list[Failure] = []
        if payload.ground_truth is None or payload.strict:
            per_record = [(i, r) for i, rec in enumerate(records)
                          for r in rec.get("flags", rec.get("review_reasons", []))]
            seen = {r for _, r in per_record}
            for i, r in per_record:
                flags.append(Failure(kind="flag", record=i, reason=r, field=field_of_reason(r, names)))
            for r in out.get("flags", out.get("review_reasons", [])):
                if r not in seen:
                    flags.append(Failure(kind="flag", reason=r, field=field_of_reason(r, names)))
            if flagged and not flags:
                flags.append(Failure(kind="flag", reason="flagged"))
        failures.extend(flags)

        failing = {f.field for f in failures if f.field}
        # a missing or extra record is about the key; low confidence is about whichever
        # required field scored lowest, which the writer works out from the evidence
        return ExtractionJudgeOutput(
            passed=not failures,
            mode="ground_truth" if payload.ground_truth is not None else "flags",
            flagged=flagged,
            failures=failures,
            failing_fields=[n for n in spec if n in failing],
            score=Score(value_failures=value_failures, flag_failures=len(flags),
                        fields_checked=checked, fields_correct=correct),
        )

    # -------------------------------------------------------------- helpers

    @staticmethod
    def _pair(gt: list[dict], data: list[dict], key: str | None, spec: dict):
        """Pair ground-truth records with output records."""
        if key and gt and all(r.get(key) not in (None, "") for r in gt):
            ftype = spec[key]["type"]
            used: set[int] = set()
            pairs, missing = [], []
            for gi, r in enumerate(gt):
                di = next((i for i, d in enumerate(data)
                           if i not in used and same(ftype, r[key], d.get(key))), None)
                if di is None:
                    missing.append(gi)
                else:
                    used.add(di)
                    pairs.append((gi, di))
            extra = [i for i in range(len(data)) if i not in used]
            # records the key could not pair (the key itself was missed or misread)
            # pair up in order, so each of their fields is still judged
            for gi, di in zip(list(missing), list(extra)):
                pairs.append((gi, di))
                missing.remove(gi)
                extra.remove(di)
            return sorted(pairs), missing, extra
        n = min(len(gt), len(data))
        return [(i, i) for i in range(n)], list(range(n, len(gt))), list(range(n, len(data)))

    @staticmethod
    def _compare(expected: dict, actual: dict, record: dict, spec: dict, di: int):
        fields_out = record.get("fields") or {}
        checked = correct = 0
        failures: list[Failure] = []
        for name, want in expected.items():
            f = spec[name]
            got = actual.get(name)
            src = (fields_out.get(name) or {}).get("source") or None
            if f["type"] == "array":
                want_items = want or []
                got_items = got or []
                checked += 1
                if len(want_items) != len(got_items):
                    failures.append(Failure(kind="item_count", record=di, field=name,
                                            expected=len(want_items), actual=len(got_items)))
                    continue
                subs = {s["name"]: s for s in f.get("items", [])}
                bad = [(i, k) for i, (w, g) in enumerate(zip(want_items, got_items))
                       for k, v in (w or {}).items()
                       if k in subs and not same(subs[k]["type"], v, (g or {}).get(k))]
                if bad:
                    i, k = bad[0]
                    failures.append(Failure(kind="item_mismatch", record=di, field=name,
                                            expected={"item": i, k: want_items[i].get(k)},
                                            actual={"item": i, k: (got_items[i] or {}).get(k)}))
                else:
                    correct += 1
                continue
            checked += 1
            if same(f["type"], want, got):
                correct += 1
            elif want in (None, ""):
                failures.append(Failure(kind="unexpected_value", record=di, field=name, actual=got, source=src))
            elif got is None:
                failures.append(Failure(kind="missing_value", record=di, field=name, expected=want))
            else:
                failures.append(Failure(kind="mismatch", record=di, field=name, expected=want,
                                        actual=got, source=src))
        return checked, correct, failures

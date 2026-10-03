"""The pydantic-free record layer: validation on construction, JSON in and out, JSON Schema."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal

import pytest

from dataextractor_designtime.contracts import AuditRecord, Corpus, FieldSchema, PackageManifest
from dataextractor_designtime.contracts.runtime import AgentTraceEntry
from dataextractor_designtime.records import Confidence, Record, ValidationError


@dataclass(kw_only=True)
class Item(Record):
    name: str
    weight: Confidence = 0.5
    seen: date | None = None


@dataclass(kw_only=True)
class Basket(Record):
    items: list[Item] = field(default_factory=list)
    by_name: dict[str, Item] = field(default_factory=dict)
    mode: Literal["a", "b"] = "a"


def test_nested_json_becomes_typed_records_however_it_is_built():
    raw = {"items": [{"name": "x", "seen": "2026-01-02"}], "by_name": {"y": {"name": "y", "weight": 1}}}
    for b in (Basket(**raw), Basket.from_dict(raw)):
        assert isinstance(b.items[0], Item) and b.items[0].seen == date(2026, 1, 2)
        assert b.by_name["y"].weight == 1.0
    assert Basket.from_dict(raw).to_dict() == {
        "items": [{"name": "x", "weight": 0.5, "seen": "2026-01-02"}],
        "by_name": {"y": {"name": "y", "weight": 1.0, "seen": None}}, "mode": "a"}


def test_every_problem_is_reported_with_its_location():
    with pytest.raises(ValidationError) as exc:
        Basket.from_dict({"items": [{"weight": 3}, {"name": 5}], "mode": "c", "colour": "red"})
    locs = {(e["loc"], e["msg"]) for e in exc.value.errors}
    assert ("items[0].name", "required") in locs
    assert ("items[1].name", "expected a string, got int") in locs
    assert ("colour", "unknown field") in locs
    assert isinstance(exc.value, ValueError)


def test_bounds_literals_and_construction_are_checked():
    with pytest.raises(ValidationError, match="must be <= 1.0"):
        Item(name="x", weight=1.5)
    with pytest.raises(ValidationError, match="must be one of"):
        Basket(mode="c")
    with pytest.raises(TypeError):
        Item(name="x", colour="red")          # unknown keyword at construction
    item = Item(name="x").replace(weight=0.9)
    assert item.weight == 0.9
    with pytest.raises(ValidationError):
        item.replace(weight=-1)


def test_json_schema_is_published_from_the_same_definition():
    s = Basket.json_schema()
    assert s["additionalProperties"] is False
    item = s["properties"]["items"]["items"]
    assert item["required"] == ["name"]
    assert item["properties"]["weight"] == {"type": "number", "minimum": 0.0, "maximum": 1.0, "default": 0.5}
    assert s["properties"]["mode"] == {"enum": ["a", "b"], "default": "a"}


def test_extra_allow_keeps_unknown_keys_flat():
    t = AgentTraceEntry.from_dict({"agent": "csv_parser", "rows": 12})
    assert t.extra == {"rows": 12} and t.to_dict()["rows"] == 12
    assert AuditRecord.from_dict({"audit_id": "a", "package_version": "1", "engine_version": "2",
                                  "outcome": "extracted", "agent_trace": [{"agent": "x", "k": 1}]}
                                 ).agent_trace[0].extra == {"k": 1}


def test_contracts_round_trip(corpus_dict):
    corpus = Corpus.from_dict(corpus_dict)
    again = Corpus.from_dict(corpus.to_dict())
    assert again == corpus
    schema = FieldSchema(email_type="invoice", generated_by="x")
    assert FieldSchema.from_dict(schema.to_dict()) == schema
    with pytest.raises(ValidationError, match="schema_unsupported"):
        FieldSchema.from_dict({**schema.to_dict(), "schema_version": "9.9"})
    m = PackageManifest.from_dict({"client_id": "a", "workflow_id": "w", "version": "1.0.0",
                                   "engine_range": ">=2", "created_by": "x", "source_corpus_id": "c",
                                   "created_at": "2026-09-01T10:00:00Z"})
    assert m.created_at.tzinfo is not None


def test_no_pydantic_in_designtime_code():
    root = Path(__file__).resolve().parents[1] / "src"
    hits = [str(p.relative_to(root)) for p in root.rglob("*.py") if "pydantic" in p.read_text(encoding="utf-8")]
    assert hits == []


def test_authoring_run_is_a_langgraph_graph():
    from dataextractor_designtime.orchestrator import build_authoring_graph
    nodes = list(build_authoring_graph().get_graph().nodes)
    assert nodes[1:-1] == ["intake", "profile", "discover_types", "confirm_types", "field_schemas",
                           "detection_rules", "author_skills", "evaluate_bootstrap", "tune_thresholds",
                           "evaluate_final", "package"]

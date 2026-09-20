"""Field Schema Agent (DT-08, DT-09).

Proposes a field only when it clears the support floor, shows the observed
frequency for every proposal, marks nothing critical without explicit
confirmation, and derives aliases from observed labels only — never values,
which is what keeps client data out of artifacts (CTR-28).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..config import get_settings
from ..contracts.artifacts import FieldDef, FieldSchema
from ..contracts.corpus import Corpus
from ..model.base import ModelRequest
from .base import DesignAgent


class FieldProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    type: str
    support: float
    occurrences: int
    of_samples: int
    required: bool
    proposed_critical: bool = False
    confirmed_critical: bool = False
    aliases: list[str] = Field(default_factory=list)
    #: DT-03: the sample ids this proposal came from.
    evidence_sample_ids: list[str] = Field(default_factory=list)


class FieldSchemaInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    corpus: Corpus
    email_type: str
    #: From the profiler; the pool alias proposal draws on (DT-09).
    observed_column_labels: list[str] = Field(default_factory=list)
    #: Fields the designer has confirmed as critical (DT-08).
    confirmed_critical: list[str] = Field(default_factory=list)
    support_min: float | None = None
    #: Exclude held-out samples from generation (DT-22).
    exclude_sample_ids: list[str] = Field(default_factory=list)


class FieldSchemaOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email_type: str
    proposals: list[FieldProposal] = Field(default_factory=list)
    artifact: FieldSchema
    #: Fields seen but below the support floor, so the designer can see them.
    below_support: list[FieldProposal] = Field(default_factory=list)


class FieldSchemaAgent(DesignAgent[FieldSchemaInput, FieldSchemaOutput]):
    name = "field-schema"

    def run(self, payload: FieldSchemaInput) -> FieldSchemaOutput:
        settings = get_settings()
        support_min = payload.support_min if payload.support_min is not None else settings.field_support_min
        excluded = set(payload.exclude_sample_ids)

        labels = [
            lbl
            for lbl in payload.corpus.labels
            if lbl.in_scope
            and lbl.email_type == payload.email_type
            and lbl.sample_id not in excluded
        ]
        total = len(labels)

        occurrences: Counter[str] = Counter()
        evidence: dict[str, list[str]] = defaultdict(list)
        observed_types: dict[str, Counter[str]] = defaultdict(Counter)

        for lbl in labels:
            for name, value in lbl.fields.items():
                if value is None or value == "":
                    continue
                occurrences[name] += 1
                evidence[name].append(lbl.sample_id)
                observed_types[name][self._infer_type(value)] += 1

        criticality = self.model.complete(
            ModelRequest(
                task="field_schema.classify_criticality",
                evidence={
                    "fields": [
                        {"name": n, "type": observed_types[n].most_common(1)[0][0]}
                        for n in sorted(occurrences)
                    ]
                },
            )
        ).output
        proposed_critical = set(criticality.get("proposed_critical", []))

        alias_out = self.model.complete(
            ModelRequest(
                task="field_schema.propose_aliases",
                evidence={
                    "fields": sorted(occurrences),
                    "observed_labels": sorted(set(payload.observed_column_labels)),
                },
            )
        ).output
        aliases: dict[str, list[str]] = alias_out.get("aliases", {})

        proposals: list[FieldProposal] = []
        below: list[FieldProposal] = []
        for name in sorted(occurrences):
            support = occurrences[name] / total if total else 0.0
            proposal = FieldProposal(
                name=name,
                type=observed_types[name].most_common(1)[0][0],
                support=round(support, 4),
                occurrences=occurrences[name],
                of_samples=total,
                required=support >= support_min,
                proposed_critical=name in proposed_critical,
                confirmed_critical=name in set(payload.confirmed_critical),
                aliases=aliases.get(name, []),
                evidence_sample_ids=sorted(evidence[name])[:5],
            )
            (proposals if support >= support_min else below).append(proposal)

        required = [
            FieldDef(
                name=p.name,
                type=p.type,  # type: ignore[arg-type]
                critical=p.confirmed_critical,
                validation=self._validation_for(p),
                support=p.support,
            )
            for p in proposals
        ]
        optional = [
            FieldDef(name=p.name, type=p.type, critical=False, support=p.support)  # type: ignore[arg-type]
            for p in below
        ]

        artifact = FieldSchema(
            email_type=payload.email_type,
            required_fields=required,
            optional_fields=optional,
            aliases={p.name: p.aliases for p in proposals if p.aliases},
            generated_by=self.identity,
        )

        return FieldSchemaOutput(
            email_type=payload.email_type,
            proposals=proposals,
            artifact=artifact,
            below_support=below,
        )

    @staticmethod
    def _infer_type(value: Any) -> str:
        if isinstance(value, bool):
            return "boolean"
        if isinstance(value, int):
            return "integer"
        if isinstance(value, float):
            return "decimal"
        text = str(value)
        if len(text) == 10 and text[4] == "-" and text[7] == "-":
            return "date"
        return "string"

    @staticmethod
    def _validation_for(p: FieldProposal) -> dict[str, Any]:
        if p.type == "decimal":
            return {"min": 0}
        if p.type == "date":
            return {"format": "ISO-8601"}
        return {}

"""Skill Author Agent (DT-12, DT-13).

Produces an override only where the engine default measurably underperforms on
the corpus, cites the error cases it is meant to fix, and generates the body
against the manifest contract: declared tools only, declared shapes only (CTR-09).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ..contracts.artifacts import FieldSchema, SkillManifest
from ..model.base import ModelRequest
from .base import DesignAgent

#: The engine's default skills, and what each is allowed to reach for (CTR-09).
DEFAULT_SKILLS: dict[str, dict[str, object]] = {
    "field-mapping": {
        "bound_agents": ["csv_parser", "spreadsheet_parser"],
        "tools": ["csv_reader", "spreadsheet_reader", "field_schema_lookup"],
        "inputs": {"columns": "string[]", "sample_rows": "row[]", "email_type": "string"},
        "outputs": {
            "mapping": "map<source_column, canonical_field>",
            "unmapped": "string[]",
            "per_field_confidence": "map<canonical_field, float>",
        },
    },
    "sheet-selection": {
        "bound_agents": ["spreadsheet_parser"],
        "tools": ["spreadsheet_reader", "field_schema_lookup"],
        "inputs": {"sheet_names": "string[]", "sheet_headers": "map<string, string[]>"},
        "outputs": {"sheet": "string", "reason": "string", "confidence": "float"},
    },
}


class OverrideDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill_id: str
    override: bool
    #: DT-12: the default's error cases this override is meant to fix.
    cited_failures: list[str] = Field(default_factory=list)
    baseline_metric: float | None = None
    reason: str = ""


class SkillAuthorInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email_type: str
    field_schema: FieldSchema
    #: Skills to consider; defaults to the Phase 1 pair.
    skill_ids: list[str] = Field(default_factory=lambda: ["field-mapping", "sheet-selection"])
    #: Per-skill baseline accuracy from a prior evaluation of the defaults.
    baseline_metrics: dict[str, float] = Field(default_factory=dict)
    #: Sample ids where the default skill got it wrong (DT-12 evidence).
    failure_cases: dict[str, list[str]] = Field(default_factory=dict)
    #: Below this, the default is judged to underperform.
    override_threshold: float = 0.9
    sheet_names: list[str] = Field(default_factory=list)


class AuthoredSkill(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manifest: SkillManifest
    body: str
    body_path: str
    manifest_path: str


class SkillAuthorOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decisions: list[OverrideDecision] = Field(default_factory=list)
    skills: list[AuthoredSkill] = Field(default_factory=list)


class SkillAuthorAgent(DesignAgent[SkillAuthorInput, SkillAuthorOutput]):
    name = "skill-author"

    def run(self, payload: SkillAuthorInput) -> SkillAuthorOutput:
        decisions: list[OverrideDecision] = []
        skills: list[AuthoredSkill] = []

        for skill_id in payload.skill_ids:
            spec = DEFAULT_SKILLS.get(skill_id)
            if spec is None:
                decisions.append(
                    OverrideDecision(
                        skill_id=skill_id,
                        override=False,
                        reason="no engine default by that id; nothing to compare against",
                    )
                )
                continue

            baseline = payload.baseline_metrics.get(skill_id)
            failures = payload.failure_cases.get(skill_id, [])
            underperforms = baseline is not None and baseline < payload.override_threshold

            decision = OverrideDecision(
                skill_id=skill_id,
                override=underperforms,
                cited_failures=failures[:5],
                baseline_metric=baseline,
                reason=(
                    f"default scored {baseline:.3f} against a {payload.override_threshold:.2f} "
                    f"threshold on {len(failures)} failing samples"
                    if underperforms
                    else "engine default performs within threshold; package inherits it (DT-12)"
                ),
            )
            decisions.append(decision)
            if not underperforms:
                continue

            body = self.model.complete(
                ModelRequest(
                    task="skill_author.write_body",
                    evidence={
                        "skill_id": skill_id,
                        "email_type": payload.email_type,
                        "fields": [f.name for f in payload.field_schema.required_fields],
                        "aliases": payload.field_schema.aliases,
                        "sheet_names": payload.sheet_names,
                    },
                )
            ).output["body"]

            manifest = SkillManifest(
                skill_id=skill_id,
                version="1.0.0",
                body=f"skills/{skill_id}.md",
                bound_agents=list(spec["bound_agents"]),  # type: ignore[arg-type]
                tools=list(spec["tools"]),  # type: ignore[arg-type]
                inputs=dict(spec["inputs"]),  # type: ignore[arg-type]
                outputs=dict(spec["outputs"]),  # type: ignore[arg-type]
                generated_by=self.identity,
            )
            self._validate_body(body, manifest)
            skills.append(
                AuthoredSkill(
                    manifest=manifest,
                    body=body,
                    body_path=f"skills/{skill_id}.md",
                    manifest_path=f"skills/{skill_id}.json",
                )
            )

        return SkillAuthorOutput(decisions=decisions, skills=skills)

    @staticmethod
    def _validate_body(body: str, manifest: SkillManifest) -> None:
        """DT-13: a body referencing an undeclared tool fails validation."""
        # Only tool names are checked. Skill ids are not tools, and a body
        # legitimately carries its own id as a heading.
        known = {t for s in DEFAULT_SKILLS.values() for t in s["tools"]}  # type: ignore[union-attr]
        declared = set(manifest.tools)
        for token in known - declared:
            if token in body:
                raise ValueError(
                    f"skill body references undeclared tool {token!r} (CTR-09, DT-13)"
                )

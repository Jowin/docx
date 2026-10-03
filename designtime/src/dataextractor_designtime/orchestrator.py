"""The authoring run (DT-01 .. DT-05), as a LangGraph state graph.

    START -> intake -> profile -> discover_types -> confirm_types -> field_schemas
          -> detection_rules -> author_skills -> evaluate_bootstrap
          -> tune_thresholds -> evaluate_final -> package -> END

Chains the agents into one addressable job: intake checks, profiling, type
confirmation, artifact generation, a bootstrap evaluation to get a confidence
distribution, threshold tuning, a final gated evaluation and packaging
(publication to the UAT channel is the route's choice).

Each node reads the state it needs and returns only what it adds; ``stages``
accumulates one record per stage. Every stage is also reachable on its own
through the API; this is the convenience path, not a different implementation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import operator
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from .records import Record
from .agents import (
    CorpusProfiler,
    DetectionRulesAgent,
    DetectionRulesInput,
    EvaluationAgent,
    EvaluationInput,
    FieldSchemaAgent,
    FieldSchemaInput,
    Packager,
    PackagerInput,
    ProfilerInput,
    SkillAuthorAgent,
    SkillAuthorInput,
    SkillBundle,
    ThresholdTuner,
    ThresholdTunerInput,
    TypeDiscovery,
    TypeDiscoveryInput,
)
from .agents.base import ConfirmationRequired, CorpusTooSmall
from .config import get_settings
from .contracts.artifacts import FieldSchema, Thresholds, TypeThresholds
from .contracts.corpus import Corpus
from .model.base import ModelClient


@dataclass(kw_only=True)
class AuthoringRunInput(Record):
    corpus: Corpus
    corpus_root: str | None = None
    client_id: str
    workflow_id: str
    #: DT-07: generation does not start until these are confirmed.
    confirmed_types: list[str] = field(default_factory=list)
    #: DT-08: nothing is critical without an explicit confirmation.
    confirmed_critical: dict[str, list[str]] = field(default_factory=dict)
    reviewed_by: str | None = None
    previous_version: str | None = None
    previous_artifacts: dict[str, Any] | None = None
    #: Skip the intake floor for a small demonstration corpus.
    enforce_corpus_floor: bool = True
    baseline_metrics: dict[str, float] = field(default_factory=dict)


@dataclass(kw_only=True)
class StageRecord(Record):
    stage: str
    agent: str
    summary: dict[str, Any] = field(default_factory=dict)


@dataclass(kw_only=True)
class AuthoringRunOutput(Record):
    client_id: str
    workflow_id: str
    version: str
    stages: list[StageRecord] = field(default_factory=list)
    manifest: dict[str, Any]
    artifacts: dict[str, Any]
    bodies: dict[str, str]
    eval_report: dict[str, Any]
    passed: bool
    gate_failed: bool
    heldout_ids: list[str] = field(default_factory=list)


def check_intake(corpus: Corpus) -> None:
    """DT-18: minimum viable size per type, with the shortfall named."""
    settings = get_settings()
    held = corpus.heldout_ids(settings.heldout_fraction)
    shortfalls: dict[str, dict[str, int]] = {}
    for email_type in sorted(corpus.types()):
        ids = corpus.samples_of_type(email_type)
        held_here = [i for i in ids if i in held]
        if len(ids) < settings.corpus_min_per_type or len(held_here) < settings.corpus_min_heldout:
            shortfalls[email_type] = {
                "have": len(ids),
                "need": settings.corpus_min_per_type,
                "heldout_have": len(held_here),
                "heldout_need": settings.corpus_min_heldout,
            }
    if shortfalls:
        raise CorpusTooSmall(
            "corpus below the intake floor (DT-18)", detail=shortfalls
        )


class AuthoringState(TypedDict, total=False):
    payload: AuthoringRunInput
    profile: Any
    proposed: list[str]
    held: list[str]
    schemas: dict[str, FieldSchema]
    rules: Any
    skills: list[SkillBundle]
    pass1: Any
    tuned: Any
    final: Any
    package: Any
    stages: Annotated[list[StageRecord], operator.add]


def build_authoring_graph(model: ModelClient | None = None):
    """Compile the authoring run with ``model`` bound for every judgment step."""
    settings = get_settings()

    def intake(state: AuthoringState) -> dict[str, Any]:
        if state["payload"].enforce_corpus_floor:
            check_intake(state["payload"].corpus)
        return {}

    def profile(state: AuthoringState) -> dict[str, Any]:
        payload = state["payload"]
        profiler = CorpusProfiler(model=model)
        out = profiler.run(ProfilerInput(corpus=payload.corpus, corpus_root=payload.corpus_root))
        return {"profile": out, "stages": [StageRecord(
            stage="profile",
            agent=profiler.identity,
            summary={
                "samples": out.sample_count,
                "attachment_kinds": out.attachment_kinds,
                "unreadable": len(out.unreadable),
                "unsupported": len(out.unsupported),
                "column_labels": len(out.observed_column_labels),
            },
        )]}

    def discover_types(state: AuthoringState) -> dict[str, Any]:
        discovery = TypeDiscovery(model=model)
        discovered = discovery.run(TypeDiscoveryInput(corpus=state["payload"].corpus))
        proposed = [p.email_type for p in discovered.proposals]
        return {"proposed": proposed, "stages": [StageRecord(
            stage="type_discovery",
            agent=discovery.identity,
            summary={"proposed": proposed, "requires_confirmation": True},
        )]}

    def confirm_types(state: AuthoringState) -> dict[str, Any]:
        payload, proposed = state["payload"], state["proposed"]
        if not payload.confirmed_types:
            raise ConfirmationRequired(
                "type list must be confirmed before generation (DT-07)", detail={"proposed": proposed}
            )
        unknown = sorted(set(payload.confirmed_types) - set(proposed))
        if unknown:
            raise ConfirmationRequired(
                "confirmed types not present in the corpus", detail={"unknown": unknown}
            )
        return {"held": sorted(payload.corpus.heldout_ids(settings.heldout_fraction))}

    def field_schemas(state: AuthoringState) -> dict[str, Any]:
        payload = state["payload"]
        schemas: dict[str, FieldSchema] = {}
        stages = []
        for email_type in payload.confirmed_types:
            agent = FieldSchemaAgent(model=model)
            out = agent.run(
                FieldSchemaInput(
                    corpus=payload.corpus,
                    email_type=email_type,
                    observed_column_labels=state["profile"].observed_column_labels,
                    confirmed_critical=payload.confirmed_critical.get(email_type, []),
                    exclude_sample_ids=state["held"],
                )
            )
            schemas[email_type] = out.artifact
            stages.append(StageRecord(
                stage=f"field_schema:{email_type}",
                agent=agent.identity,
                summary={
                    "required": [f.name for f in out.artifact.required_fields],
                    "optional": [f.name for f in out.artifact.optional_fields],
                    "aliases": {k: len(v) for k, v in out.artifact.aliases.items()},
                },
            ))
        return {"schemas": schemas, "stages": stages}

    def detection_rules(state: AuthoringState) -> dict[str, Any]:
        payload = state["payload"]
        agent = DetectionRulesAgent(model=model)
        rules = agent.run(
            DetectionRulesInput(
                corpus=payload.corpus, email_types=payload.confirmed_types,
                exclude_sample_ids=state["held"],
            )
        )
        return {"rules": rules, "stages": [StageRecord(
            stage="detection_rules",
            agent=agent.identity,
            summary={
                "kept": len(rules.evidence),
                "dropped": len(rules.dropped),
                "types": sorted(rules.artifact.types),
            },
        )]}

    def author_skills(state: AuthoringState) -> dict[str, Any]:
        payload = state["payload"]
        skills: list[SkillBundle] = []
        stages = []
        for email_type in payload.confirmed_types:
            author = SkillAuthorAgent(model=model)
            authored = author.run(
                SkillAuthorInput(
                    email_type=email_type,
                    field_schema=state["schemas"][email_type],
                    baseline_metrics=payload.baseline_metrics,
                    sheet_names=state["profile"].observed_sheet_names,
                )
            )
            skills.extend(SkillBundle(manifest=s.manifest, body=s.body) for s in authored.skills)
            stages.append(StageRecord(
                stage=f"skill_author:{email_type}",
                agent=author.identity,
                summary={
                    d.skill_id: ("override" if d.override else "inherit default")
                    for d in authored.decisions
                },
            ))
        return {"skills": skills, "stages": stages}

    def _evaluate(state: AuthoringState, thresholds: Thresholds):
        payload = state["payload"]
        evaluator = EvaluationAgent(model=model)
        return evaluator, evaluator.run(
            EvaluationInput(
                corpus=payload.corpus,
                corpus_root=payload.corpus_root,
                schemas=state["schemas"],
                detection=state["rules"].artifact,
                thresholds=thresholds,
                client_id=payload.client_id,
                workflow_id=payload.workflow_id,
            )
        )

    def evaluate_bootstrap(state: AuthoringState) -> dict[str, Any]:
        # Bootstrap thresholds accept everything, so pass 1 yields a confidence
        # distribution rather than a routing decision (DT-14).
        bootstrap = Thresholds(
            types={t: TypeThresholds(accept_at=0.0, review_below=0.0, reject_below=0.0, always_review_if=[])
                   for t in state["payload"].confirmed_types},
            generated_by="bootstrap",
        )
        evaluator, pass1 = _evaluate(state, bootstrap)
        return {"pass1": pass1, "stages": [StageRecord(
            stage="evaluation:bootstrap",
            agent=evaluator.identity,
            summary={"predictions": len(pass1.predictions), "metrics": pass1.metrics.to_dict()},
        )]}

    def tune_thresholds(state: AuthoringState) -> dict[str, Any]:
        schemas = state["schemas"]
        tuner = ThresholdTuner(model=model)
        # Only numeric and date criticals get a tolerance; a string critical field
        # has to match exactly, and an empty entry would read as "anything goes".
        tolerance = {
            t: {
                name: band
                for name, band in (
                    (n, _tolerance_for(schemas[t], n)) for n in schemas[t].critical_field_names()
                )
                if band is not None
            }
            for t in state["payload"].confirmed_types
        }
        tuned = tuner.run(
            ThresholdTunerInput(predictions=state["pass1"].predictions, critical_field_tolerance=tolerance)
        )
        return {"tuned": tuned, "stages": [StageRecord(
            stage="threshold_tuning",
            agent=tuner.identity,
            summary={t.email_type: t.chosen_band for t in tuned.tuning},
        )]}

    def evaluate_final(state: AuthoringState) -> dict[str, Any]:
        evaluator, final = _evaluate(state, state["tuned"].artifact)
        return {"final": final, "stages": [StageRecord(
            stage="evaluation:final",
            agent=evaluator.identity,
            summary={"metrics": final.metrics.to_dict(), "passed": final.passed},
        )]}

    def package(state: AuthoringState) -> dict[str, Any]:
        payload = state["payload"]
        packager = Packager(model=model)
        built = packager.run(
            PackagerInput(
                client_id=payload.client_id,
                workflow_id=payload.workflow_id,
                source_corpus_id=payload.corpus.meta.corpus_id,
                schemas=state["schemas"],
                detection=state["rules"].artifact,
                thresholds=state["tuned"].artifact,
                skills=state["skills"],
                eval_report=state["final"].report,
                previous_version=payload.previous_version,
                previous_artifacts=payload.previous_artifacts,
                reviewed_by=payload.reviewed_by,
                email_types=payload.confirmed_types,
            )
        )
        return {"package": built, "stages": [StageRecord(
            stage="package",
            agent=packager.identity,
            summary={
                "version": built.manifest.version,
                "required_bump": built.required_bump,
                "artifacts": len(built.manifest.artifacts),
            },
        )]}

    order = [("intake", intake), ("profile", profile), ("discover_types", discover_types),
             ("confirm_types", confirm_types), ("field_schemas", field_schemas),
             ("detection_rules", detection_rules), ("author_skills", author_skills),
             ("evaluate_bootstrap", evaluate_bootstrap), ("tune_thresholds", tune_thresholds),
             ("evaluate_final", evaluate_final), ("package", package)]
    g = StateGraph(AuthoringState)
    previous = START
    for name, fn in order:
        g.add_node(name, fn)
        g.add_edge(previous, name)
        previous = name
    g.add_edge(previous, END)
    return g.compile()


def run_authoring(payload: AuthoringRunInput, model: ModelClient | None = None) -> AuthoringRunOutput:
    state = build_authoring_graph(model).invoke({"payload": payload, "stages": []})
    package, final = state["package"], state["final"]
    return AuthoringRunOutput(
        client_id=payload.client_id,
        workflow_id=payload.workflow_id,
        version=package.manifest.version,
        stages=state["stages"],
        manifest=package.manifest.to_dict(),
        artifacts=package.artifacts,
        bodies=package.bodies,
        eval_report=final.report,
        passed=final.passed,
        gate_failed=package.gate_failed,
        heldout_ids=state["held"],
    )


def _tolerance_for(schema: FieldSchema, name: str) -> dict[str, float] | None:
    for f in schema.required_fields + schema.optional_fields:
        if f.name != name:
            continue
        if f.type in {"decimal", "integer"}:
            return {"absolute": 0.01}
        if f.type == "date":
            return {"days": 0}
        return None
    return None

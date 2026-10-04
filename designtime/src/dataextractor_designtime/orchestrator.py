"""The authoring run (DT-01 .. DT-05), as a LangGraph state graph.

    START -> intake -> profile -> discover_types -> confirm_types -> field_schemas
          -> detection_rules -> author_skills -> build_config -> evaluate_bootstrap
          -> tune_thresholds -> evaluate_final -> publish -> END

One path with pattern learning. Both turn evidence into a **runtime config
version folder** (the source of truth the runtime serves), both are scored by
the **real runtime** in an isolated process, and both publish through
``configroot.publish_version`` as a *candidate* that a person signs off and
releases. They differ only in scale:

* the authoring run designs a use case from a labelled corpus: field schemas
  per email type, detection rules, skills, thresholds; it publishes the next
  minor version (or 1.0.0 for a new use case), inheriting operational settings
  and every learned-pattern skill from the version before;
* pattern learning (learning/graph.py) refines the newest version from one
  sample at a time and publishes the next patch.

``build_config`` writes the candidate folder (configwriter.py) with bootstrap
thresholds; ``evaluate_bootstrap`` runs the held-out corpus through the runtime
to get a confidence distribution; ``tune_thresholds`` picks each type's band and
rewrites the folder; ``evaluate_final`` scores exactly the folder that
``publish`` then copies into the config root and records in the registry with
its evaluation. A failed gate does not stop publication (the candidate and its
report are what a reviewer looks at); it blocks release unless overridden.

Each node reads the state it needs and returns only what it adds; ``stages``
accumulates one record per stage. Every agent is also reachable on its own
through the API; this is the convenience path, not a different implementation.
"""

from __future__ import annotations

import operator
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from . import configroot, configwriter
from .records import Record
from .agents import (
    CorpusProfiler,
    DetectionRulesAgent,
    DetectionRulesInput,
    EvaluationAgent,
    EvaluationInput,
    FieldSchemaAgent,
    FieldSchemaInput,
    ProfilerInput,
    SkillAuthorAgent,
    SkillAuthorInput,
    SkillBundle,
    ThresholdTuner,
    ThresholdTunerInput,
    TypeDiscovery,
    TypeDiscoveryInput,
)
from .agents.base import AgentError, ConfirmationRequired, CorpusTooSmall
from .config import Settings, get_settings
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
    #: Who asked for the run; recorded as the version's creator (they cannot sign it off alone).
    requested_by: str = "designtime"
    #: Write the evaluated folder into the config root as a candidate version.
    publish: bool = True
    #: The email type served when detection is not decisive; default: the first confirmed type.
    default_type: str | None = None
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
    run_id: str
    client_id: str
    workflow_id: str
    #: The version written to the config root; None when publish was false.
    version: str | None = None
    base_version: str | None = None
    published: bool = False
    stages: list[StageRecord] = field(default_factory=list)
    #: The candidate's manifest.json, and every file in the folder (path -> bytes).
    manifest: dict[str, Any]
    files: dict[str, int] = field(default_factory=dict)
    artifacts: dict[str, Any] = field(default_factory=dict)
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


AUTHORING_GENERATED_BY = "designtime:authoring@0.2.0"
AUTHORING_NODES = ("intake", "profile", "discover_types", "confirm_types", "field_schemas", "detection_rules",
                   "author_skills", "build_config", "evaluate_bootstrap", "tune_thresholds", "evaluate_final",
                   "publish")


class AuthoringState(TypedDict, total=False):
    run_id: str
    payload: AuthoringRunInput
    workdir: str
    folder: str
    base_version: str | None
    published: dict[str, Any]
    profile: Any
    proposed: list[str]
    held: list[str]
    schemas: dict[str, FieldSchema]
    rules: Any
    skills: list[SkillBundle]
    pass1: Any
    tuned: Any
    final: Any
    stages: Annotated[list[StageRecord], operator.add]


def build_authoring_graph(model: ModelClient | None = None, settings: Settings | None = None,
                          session: Any = None, checkpointer: Any = None):
    """Compile the authoring run with ``model`` bound for every judgment step.

    ``session`` (a registry session) records the published version; without it
    the version is written but not recorded (the registry records it on first
    sign-off or release, from its provenance.json).
    """
    settings = settings or get_settings()
    root = Path(settings.runtime_config_root)

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
        held = payload.corpus.heldout_ids(settings.heldout_fraction) | \
            payload.corpus.heldout_out_of_scope(settings.heldout_fraction)
        return {"held": sorted(held)}

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

    def build_config(state: AuthoringState) -> dict[str, Any]:
        payload = state["payload"]
        configroot.check_segment("client", payload.client_id)
        configroot.check_segment("usecase", payload.workflow_id)
        base, base_version = configwriter.base_folder(root, payload.client_id, payload.workflow_id)
        bootstrap = Thresholds(
            types={t: TypeThresholds(accept_at=0.0, review_below=0.0, reject_below=0.0, always_review_if=[])
                   for t in payload.confirmed_types},
            generated_by="bootstrap",
        )
        folder = configwriter.write_version(
            Path(state["workdir"]) / "candidate", client=payload.client_id, usecase=payload.workflow_id,
            schemas=state["schemas"], detection=state["rules"].artifact, thresholds=bootstrap,
            skills=state["skills"], base=base, default_type=payload.default_type)
        files = sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file())
        return {"folder": str(folder), "base_version": base_version, "stages": [StageRecord(
            stage="build_config", agent="configwriter",
            summary={"base_version": base_version, "inherited_from": str(base.relative_to(root)) if base else None,
                     "files": files})]}

    def _evaluate(state: AuthoringState):
        payload = state["payload"]
        evaluator = EvaluationAgent(model=model)
        return evaluator, evaluator.run(
            EvaluationInput(
                corpus=payload.corpus,
                corpus_root=payload.corpus_root,
                schemas=state["schemas"],
                detection=state["rules"].artifact,
                thresholds=state.get("tuned").artifact if state.get("tuned") else Thresholds(generated_by="bootstrap"),
                client_id=payload.client_id,
                workflow_id=payload.workflow_id,
                engine="runtime",
                config_folder=state["folder"],
            )
        )

    def evaluate_bootstrap(state: AuthoringState) -> dict[str, Any]:
        # Bootstrap thresholds accept everything, so pass 1 yields a confidence
        # distribution rather than a routing decision (DT-14).
        evaluator, pass1 = _evaluate(state)
        return {"pass1": pass1, "stages": [StageRecord(
            stage="evaluation:bootstrap",
            agent=evaluator.identity,
            summary={"engine": "runtime", "predictions": len(pass1.predictions),
                     "metrics": pass1.metrics.to_dict(), "scope": pass1.report.get("scope")},
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
        configwriter.set_thresholds(Path(state["folder"]), tuned.artifact)
        return {"tuned": tuned, "stages": [StageRecord(
            stage="threshold_tuning",
            agent=tuner.identity,
            summary={t.email_type: t.chosen_band for t in tuned.tuning},
        )]}

    def evaluate_final(state: AuthoringState) -> dict[str, Any]:
        evaluator, final = _evaluate(state)
        return {"final": final, "stages": [StageRecord(
            stage="evaluation:final",
            agent=evaluator.identity,
            summary={"engine": "runtime", "metrics": final.metrics.to_dict(), "passed": final.passed,
                     "scope": final.report.get("scope")},
        )]}

    def publish(state: AuthoringState) -> dict[str, Any]:
        payload, final = state["payload"], state["final"]
        if not payload.publish:
            return {"stages": [StageRecord(stage="publish", agent="configroot", summary={"published": False})]}
        published = configroot.publish_version(
            Path(state["folder"]), root, payload.client_id, payload.workflow_id, base=state.get("base_version"),
            bump="minor", origin="authoring", created_by=payload.requested_by, run_id=state["run_id"],
            summary={"corpus_id": payload.corpus.meta.corpus_id, "types": payload.confirmed_types,
                     "passed": final.passed, "metrics": final.metrics.to_dict()})
        if session is not None:
            from .registry.configs import ConfigRegistry
            ConfigRegistry(session, root).record(
                published, evaluation={"kind": "corpus", "report_sha256": final.report_sha256,
                                       "metrics": final.metrics.to_dict(), "gates": final.gates,
                                       "gate_results": final.gate_results, "scope": final.report.get("scope")},
                gates_passed=final.passed)
        return {"published": published, "stages": [StageRecord(
            stage="publish", agent="configroot",
            summary={"version": published["version"], "status": "candidate", "sha256": published["sha256"]})]}

    order = [("intake", intake), ("profile", profile), ("discover_types", discover_types),
             ("confirm_types", confirm_types), ("field_schemas", field_schemas),
             ("detection_rules", detection_rules), ("author_skills", author_skills),
             ("build_config", build_config), ("evaluate_bootstrap", evaluate_bootstrap),
             ("tune_thresholds", tune_thresholds), ("evaluate_final", evaluate_final), ("publish", publish)]
    g = StateGraph(AuthoringState)
    previous = START
    for name, fn in order:
        g.add_node(name, fn)
        g.add_edge(previous, name)
        previous = name
    g.add_edge(previous, END)
    return g.compile(checkpointer=checkpointer)


def run_authoring(payload: AuthoringRunInput, model: ModelClient | None = None, *,
                  settings: Settings | None = None, session: Any = None) -> AuthoringRunOutput:
    """Run the authoring graph; with ``session``, log it with the learning runs and record the version."""
    settings = settings or get_settings()
    run_id = str(uuid.uuid4())
    store = None
    if session is not None:
        from .learning.store import LearningStore
        store = LearningStore(session)
        store.add(id=run_id, kind="authoring", client_id=payload.client_id, usecase=payload.workflow_id,
                  object_name=",".join(payload.confirmed_types) or None, pattern_name="authoring",
                  source=payload.corpus.meta.corpus_id, outcome="running", passed_before=False, attempts=[],
                  generated_by=AUTHORING_GENERATED_BY, requested_by=payload.requested_by)
        session.commit()
    workdir = tempfile.mkdtemp(prefix="dt-author-")
    try:
        state = build_authoring_graph(model, settings, session).invoke(
            {"run_id": run_id, "payload": payload, "workdir": workdir, "stages": []})
    except AgentError:
        shutil.rmtree(workdir, ignore_errors=True)
        if store is not None:                  # a refused request leaves no trace beyond its error
            session.rollback()
            store.delete(run_id)
            session.commit()
        raise
    except Exception as exc:
        shutil.rmtree(workdir, ignore_errors=True)
        if store is not None:
            session.rollback()
            store.update(run_id, outcome="error", error={"type": type(exc).__name__,
                                                          "code": getattr(exc, "code", None),
                                                          "message": str(exc)[:500]})
            session.commit()
        raise
    final, published = state["final"], state.get("published")
    folder = Path(published["path"]) if published else Path(state["folder"])
    import json as _json
    manifest = _json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    files = {p.relative_to(folder).as_posix(): p.stat().st_size for p in sorted(folder.rglob("*")) if p.is_file()}
    artifacts = {"rules/detection.json": state["rules"].artifact.to_dict(),
                 **{f"field_schema/{t}": sc.to_dict() for t, sc in state["schemas"].items()},
                 "thresholds": state["tuned"].artifact.to_dict()}
    shutil.rmtree(workdir, ignore_errors=True)
    out = AuthoringRunOutput(
        run_id=run_id,
        client_id=payload.client_id,
        workflow_id=payload.workflow_id,
        version=published["version"] if published else None,
        base_version=state.get("base_version"),
        published=bool(published),
        stages=state["stages"],
        manifest=manifest,
        files=files,
        artifacts=artifacts,
        eval_report=final.report,
        passed=final.passed,
        gate_failed=not final.passed,
        heldout_ids=state["held"],
    )
    if store is not None:
        outcome = ("published" if published else "evaluated") if final.passed else \
            ("gate_failed" if published else "eval_failed")
        store.update(run_id, outcome=outcome, base_version=state.get("base_version"),
                     result_version=out.version, passed_after=final.passed,
                     attempts=[st.to_dict() for st in state["stages"]], path=list(AUTHORING_NODES),
                     verdict_after={"metrics": final.metrics.to_dict(), "gate_results": final.gate_results,
                                    "scope": final.report.get("scope"), "report_sha256": final.report_sha256})
    return out


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

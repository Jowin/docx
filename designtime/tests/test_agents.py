"""Each design agent, exercised on its own against the sample corpus."""

from __future__ import annotations

import pytest

from dataextractor_designtime.agents import (
    CorpusProfiler,
    DetectionRulesAgent,
    DetectionRulesInput,
    EvaluationAgent,
    EvaluationInput,
    FieldSchemaAgent,
    FieldSchemaInput,
    Packager,
    PackagerInput,
    Prediction,
    ProfilerInput,
    SkillAuthorAgent,
    SkillAuthorInput,
    ThresholdTuner,
    ThresholdTunerInput,
    TypeDiscovery,
    TypeDiscoveryInput,
)
from dataextractor_designtime.agents.filetypes import CSV, XLSX
from dataextractor_designtime.contracts.artifacts import Thresholds, TypeThresholds

CRITICAL = ["amount", "due_date"]


@pytest.fixture()
def profile(corpus, corpus_root):
    return CorpusProfiler().run(ProfilerInput(corpus=corpus, corpus_root=str(corpus_root)))


@pytest.fixture()
def schema(corpus, profile):
    return FieldSchemaAgent().run(
        FieldSchemaInput(
            corpus=corpus,
            email_type="invoice",
            observed_column_labels=profile.observed_column_labels,
            confirmed_critical=CRITICAL,
        )
    ).artifact


@pytest.fixture()
def detection(corpus):
    return DetectionRulesAgent().run(
        DetectionRulesInput(corpus=corpus, email_types=["invoice"])
    ).artifact


# -- DT-06 -----------------------------------------------------------------


def test_profiler_detects_attachment_types_by_content(profile):
    assert profile.attachment_kinds == {CSV: 15, XLSX: 15}
    assert not profile.unreadable
    assert not profile.unsupported


def test_profiler_collects_the_labels_alias_proposal_needs(profile):
    assert profile.observed_column_labels == ["Amount Due", "Invoice No", "Payment Due", "Vendor Name"]
    assert profile.observed_sheet_names  # xlsx sheet names


# -- DT-07 -----------------------------------------------------------------


def test_type_discovery_requires_confirmation_and_gives_exemplars(corpus):
    out = TypeDiscovery().run(TypeDiscoveryInput(corpus=corpus))
    assert out.requires_confirmation is True
    assert [p.email_type for p in out.proposals] == ["invoice"]
    proposal = out.proposals[0]
    assert proposal.sample_count == 30
    assert len(proposal.exemplars) >= 3
    assert out.out_of_scope_count == 10


# -- DT-08, DT-09 ----------------------------------------------------------


def test_field_schema_reports_support_and_needs_confirmation_for_critical(corpus, profile):
    out = FieldSchemaAgent().run(
        FieldSchemaInput(
            corpus=corpus,
            email_type="invoice",
            observed_column_labels=profile.observed_column_labels,
            confirmed_critical=CRITICAL,
        )
    )
    names = sorted(f.name for f in out.artifact.required_fields)
    assert names == ["amount", "due_date", "invoice_number", "vendor"]
    assert all(p.support == 1.0 and p.evidence_sample_ids for p in out.proposals)
    critical = out.artifact.critical_field_names()
    assert sorted(critical) == sorted(CRITICAL)
    # invoice_number is *proposed* critical but was not confirmed, so it is not.
    proposed = {p.name for p in out.proposals if p.proposed_critical}
    assert "invoice_number" not in critical and "invoice_number" in proposed | {"invoice_number"}


def test_aliases_map_each_observed_label_to_one_field(schema):
    assert schema.aliases == {
        "amount": ["Amount Due"],
        "due_date": ["Payment Due"],
        "invoice_number": ["Invoice No"],
        "vendor": ["Vendor Name"],
    }


def test_aliases_contain_labels_not_values(schema, corpus):
    values = {str(v) for lbl in corpus.labels for v in lbl.fields.values()}
    for variants in schema.aliases.values():
        assert not (set(variants) & values)


# -- DT-10, DT-11 ----------------------------------------------------------


def test_detection_rules_drop_anything_below_the_support_floor(corpus):
    out = DetectionRulesAgent().run(
        DetectionRulesInput(corpus=corpus, email_types=["invoice"], rule_support_min=3)
    )
    for ev in out.evidence:
        assert ev.support >= 3
    assert out.dropped, "expected low-support candidates to be reported, not silently discarded"
    for ev in out.dropped:
        assert ev.dropped_reason


def test_detection_rules_mine_negative_signals(detection):
    rules = detection.types["invoice"]
    assert rules.negative_signals
    assert any("quotation" in s for s in rules.negative_signals)


def test_detection_rule_weights_are_bounded(detection):
    rules = detection.types["invoice"]
    assert all(0 < k.weight <= 0.45 for k in rules.keywords)
    assert all(0 < p.weight <= 0.35 for p in rules.patterns)


# -- DT-12, DT-13 ----------------------------------------------------------


def test_skill_author_inherits_the_default_when_it_performs(schema):
    out = SkillAuthorAgent().run(SkillAuthorInput(email_type="invoice", field_schema=schema))
    assert out.skills == []
    assert all(d.override is False for d in out.decisions)


def test_skill_author_overrides_only_with_cited_failures(schema):
    out = SkillAuthorAgent().run(
        SkillAuthorInput(
            email_type="invoice",
            field_schema=schema,
            skill_ids=["field-mapping"],
            baseline_metrics={"field-mapping": 0.71},
            failure_cases={"field-mapping": ["inv_0002", "inv_0004"]},
        )
    )
    assert [s.manifest.skill_id for s in out.skills] == ["field-mapping"]
    decision = out.decisions[0]
    assert decision.override is True
    assert decision.cited_failures == ["inv_0002", "inv_0004"]
    manifest = out.skills[0].manifest
    assert manifest.bound_agents == ["csv_parser", "spreadsheet_parser"]
    assert "field_schema_lookup" in manifest.tools
    assert out.skills[0].body.strip()


# -- DT-14 -----------------------------------------------------------------


def test_threshold_tuner_reports_the_tradeoff_at_every_band():
    preds = [
        Prediction(sample_id=f"s{i}", email_type="invoice", confidence=c, critical_correct=ok, all_correct=ok)
        for i, (c, ok) in enumerate(
            [(0.95, True), (0.92, True), (0.88, True), (0.72, False), (0.55, False), (0.51, False)]
        )
    ]
    out = ThresholdTuner().run(ThresholdTunerInput(predictions=preds))
    tuning = out.tuning[0]
    assert [t.band for t in tuning.tradeoffs] == [0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95]
    # 0.75 is the lowest band that accepts no wrong prediction; anything lower
    # lets one through and breaches the 1% false-accept floor.
    assert tuning.chosen_band == 0.75
    bands = {t.band: t for t in tuning.tradeoffs}
    assert bands[0.5].false_accept_rate == 0.5
    assert bands[0.85].false_accept_rate == 0.0
    assert out.artifact.types["invoice"].review_below == tuning.chosen_band


# -- DT-23 .. DT-31 --------------------------------------------------------


def test_evaluation_gates_every_metric(corpus, corpus_root, schema, detection):
    thresholds = Thresholds(
        types={"invoice": TypeThresholds(accept_at=0.5, review_below=0.5, reject_below=0.05)},
        generated_by="test",
    )
    out = EvaluationAgent().run(
        EvaluationInput(
            corpus=corpus,
            corpus_root=str(corpus_root),
            schemas={"invoice": schema},
            detection=detection,
            thresholds=thresholds,
            client_id="acme",
            workflow_id="ap-invoices",
        )
    )
    assert out.passed is True
    assert out.metrics.type_accuracy == 1.0
    assert out.metrics.field_accuracy == 1.0
    assert out.metrics.source_attribution_accuracy == 1.0
    assert out.metrics.false_accept_rate == 0.0
    assert {b.field for b in out.field_breakdown} == {"amount", "due_date", "invoice_number", "vendor"}


def test_evaluation_refuses_to_loosen_the_false_accept_floor(corpus, corpus_root, schema, detection):
    thresholds = Thresholds(types={"invoice": TypeThresholds()}, generated_by="test")
    with pytest.raises(ValueError, match="platform floor"):
        EvaluationAgent().run(
            EvaluationInput(
                corpus=corpus,
                corpus_root=str(corpus_root),
                schemas={"invoice": schema},
                detection=detection,
                thresholds=thresholds,
                client_id="acme",
                workflow_id="ap-invoices",
                gates={"false_accept_rate": 0.25},
            )
        )


def test_evaluation_is_deterministic(corpus, corpus_root, schema, detection):
    """DT-35: same package, same corpus, same numbers."""
    thresholds = Thresholds(
        types={"invoice": TypeThresholds(accept_at=0.5, review_below=0.5, reject_below=0.05)},
        generated_by="test",
    )
    payload = EvaluationInput(
        corpus=corpus,
        corpus_root=str(corpus_root),
        schemas={"invoice": schema},
        detection=detection,
        thresholds=thresholds,
        client_id="acme",
        workflow_id="ap-invoices",
    )
    first = EvaluationAgent().run(payload)
    second = EvaluationAgent().run(payload)
    assert first.report_sha256 == second.report_sha256


# -- DT-15, DT-16 ----------------------------------------------------------


def test_packager_derives_the_version_and_checksums_every_file(schema, detection):
    thresholds = Thresholds(types={"invoice": TypeThresholds()}, generated_by="test")
    out = Packager().run(
        PackagerInput(
            client_id="acme",
            workflow_id="ap-invoices",
            source_corpus_id="acme/corpus/2026-09-01",
            schemas={"invoice": schema},
            detection=detection,
            thresholds=thresholds,
            reviewed_by="joe@acme.example",
        )
    )
    assert out.manifest.version == "1.0.0"
    assert {e.path for e in out.manifest.artifacts} == {
        "schemas/invoice.json",
        "rules/detection.json",
        "thresholds.json",
    }
    assert all(len(e.sha256) == 64 for e in out.manifest.artifacts)
    assert out.artifacts["schemas/invoice.json"]["reviewed_by"] == "joe@acme.example"
    assert out.manifest.created_by == "design-agent:packager@0.1.0"


def test_packager_refuses_a_bump_that_understates_a_field_removal(schema, detection):
    thresholds = Thresholds(types={"invoice": TypeThresholds()}, generated_by="test")
    previous = {
        "schemas/invoice.json": {
            "email_type": "invoice",
            "required_fields": [{"name": n} for n in ["amount", "due_date", "invoice_number", "vendor", "po_number"]],
            "optional_fields": [],
        }
    }
    from dataextractor_designtime.agents.base import AgentError

    with pytest.raises(AgentError) as exc:
        Packager().run(
            PackagerInput(
                client_id="acme",
                workflow_id="ap-invoices",
                source_corpus_id="c",
                schemas={"invoice": schema},
                detection=detection,
                thresholds=thresholds,
                previous_version="1.0.0",
                previous_artifacts=previous,
                version="1.0.1",
            )
        )
    assert exc.value.code == "version_bump_insufficient"

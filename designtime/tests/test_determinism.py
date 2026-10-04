"""DT-02 and DT-35: the same inputs produce the same artifacts and numbers."""

from __future__ import annotations

from dataextractor_designtime.contracts.artifacts import sha256_of
from dataextractor_designtime.contracts.corpus import Corpus
from dataextractor_designtime.orchestrator import AuthoringRunInput, run_authoring


def _run(corpus_dict, corpus_root, publish=False):
    return run_authoring(
        AuthoringRunInput(
            publish=publish,
            corpus=Corpus(**corpus_dict),
            corpus_root=str(corpus_root),
            client_id="acme",
            workflow_id="ap-invoices",
            confirmed_types=["invoice"],
            confirmed_critical={"invoice": ["amount", "due_date"]},
            reviewed_by="joe@acme.example",
        )
    )


def test_two_runs_over_an_unchanged_corpus_agree(corpus_dict, corpus_root):
    first = _run(corpus_dict, corpus_root)
    second = _run(corpus_dict, corpus_root)
    assert sha256_of(first.artifacts) == sha256_of(second.artifacts)
    assert first.eval_report["metrics"] == second.eval_report["metrics"]
    assert first.eval_report == second.eval_report
    assert first.manifest == second.manifest and first.files == second.files


def test_publishing_twice_writes_two_versions_never_one_over_the_other(corpus_dict, corpus_root):
    first = _run(corpus_dict, corpus_root, publish=True)
    second = _run(corpus_dict, corpus_root, publish=True)
    assert (first.base_version, first.version) == ("1.1.0", "1.2.0")
    assert (second.base_version, second.version) == ("1.2.0", "1.3.0")


def test_heldout_split_is_stable_and_excluded_from_generation(corpus_dict, corpus_root):
    corpus = Corpus(**corpus_dict)
    assert corpus.heldout_ids(0.2) == corpus.heldout_ids(0.2)
    out = _run(corpus_dict, corpus_root)
    assert len(out.heldout_ids) == 8              # 6 invoices and 2 out-of-scope quotations
    assert set(out.heldout_ids) == corpus.heldout_ids(0.2) | corpus.heldout_out_of_scope(0.2)


def test_a_corpus_change_moves_exactly_one_artifact(corpus_dict, corpus_root):
    """DT-02: an alias appearing in a few samples changes the field schema only."""
    baseline = _run(corpus_dict, corpus_root)

    changed = {
        "meta": corpus_dict["meta"],
        "labels": corpus_dict["labels"],
        "samples": [
            {**s, "body": s["body"].replace("Amount:", "Invoice Total:")}
            if s["sample_id"] in {f"inv_{i:04d}" for i in range(0, 10, 2)}
            else s
            for s in corpus_dict["samples"]
        ],
    }
    after = _run(changed, corpus_root)

    differing = {
        path
        for path in baseline.artifacts
        if sha256_of(baseline.artifacts[path]) != sha256_of(after.artifacts[path])
    }
    assert differing == {"rules/detection.json"}, differing


def test_intake_floor_names_the_shortfall(corpus_dict, corpus_root):
    from dataextractor_designtime.agents.base import CorpusTooSmall
    from dataextractor_designtime.orchestrator import check_intake
    import pytest

    small = {
        "meta": corpus_dict["meta"],
        "samples": corpus_dict["samples"][:4],
        "labels": corpus_dict["labels"][:4],
    }
    with pytest.raises(CorpusTooSmall) as exc:
        check_intake(Corpus(**small))
    assert exc.value.detail["invoice"]["have"] == 4
    assert exc.value.detail["invoice"]["need"] == 25

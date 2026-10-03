"""The learning call's request and response records."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from ..agents.extraction_judge import ExtractionJudgeOutput, Failure, Score
from ..agents.pattern_skill_writer import Derivation
from ..records import Record

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MAX_REFERENCE_CHARS = 20_000
MAX_ITERATIONS = 10

Outcome = Literal["passed", "learned", "improved", "failed"]


@dataclass(kw_only=True)
class LearnRequest(Record):
    """One sample of a document pattern to learn from."""

    #: The input file, inside the learning input root (absolute, or relative to it).
    source: str
    #: Names the pattern; its learned skill is ``skills/<pattern_name>.md``.
    pattern_name: str
    client: str | None = None
    usecase: str | None = None
    #: Base config version to learn from; default is the latest.
    version: str | None = None
    #: The data object the pattern produces (the data dictionary's name, e.g. "invoice").
    object: str | None = None
    #: Expected records for this source; a single object is one record.
    ground_truth: list[dict[str, Any]] | dict[str, Any] | None = None
    #: Free text explaining the pattern, e.g. 'invoice_number: "Our Ref"'.
    reference_text: str | None = None
    #: Skill-writing attempts before giving up; default from LEARNING_MAX_ITERATIONS.
    max_iterations: int | None = None
    #: Write the learned version into the runtime configs (false = dry run).
    publish: bool = True
    #: With ground truth, also fail on any review status, not only wrong values.
    strict: bool = True
    requested_by: str = "designtime"

    def check(self) -> None:
        problems = []
        if not self.source.strip():
            problems.append("source: must not be empty")
        if not _NAME.match(self.pattern_name):
            problems.append("pattern_name: 1-64 letters, digits, '.', '_' or '-'")
        for name in ("client", "usecase", "version"):
            v = getattr(self, name)
            if v is not None and not _NAME.match(v):
                problems.append(f"{name}: not a valid config folder name")
        if self.reference_text is not None and len(self.reference_text) > MAX_REFERENCE_CHARS:
            problems.append(f"reference_text: at most {MAX_REFERENCE_CHARS} characters")
        if self.max_iterations is not None and not 1 <= self.max_iterations <= MAX_ITERATIONS:
            problems.append(f"max_iterations: between 1 and {MAX_ITERATIONS}")
        if problems:
            raise ValueError("; ".join(problems))
        if isinstance(self.ground_truth, dict):
            self.ground_truth = [self.ground_truth]


@dataclass(kw_only=True)
class Attempt(Record):
    """One candidate skill, as tested by the isolated runtime."""

    attempt: int
    version: str
    new_hints: list[Derivation] = field(default_factory=list)
    passed: bool
    score: Score
    failures: list[Failure] = field(default_factory=list)
    #: Earlier samples that passed on the base version and fail on this candidate.
    regressions: list[str] = field(default_factory=list)
    #: Better than everything before it, with no regressions.
    accepted: bool
    notes: list[str] = field(default_factory=list)


@dataclass(kw_only=True)
class LearnResponse(Record):
    id: str
    #: passed: nothing to learn. learned: a new version passes. improved: a new
    #: version is better but still fails. failed: no candidate beat the base.
    outcome: Outcome
    client: str
    usecase: str
    object: str
    pattern_name: str
    source: str
    base_version: str
    #: The version written (or that would be written, on a dry run).
    result_version: str | None = None
    published: bool = False
    passed_before: bool
    passed_after: bool | None = None
    verdict_before: ExtractionJudgeOutput
    verdict_after: ExtractionJudgeOutput | None = None
    attempts: list[Attempt] = field(default_factory=list)
    regression_samples: int = 0
    skill_path: str | None = None
    skill: str | None = None
    #: The data the runtime extracts with the resulting config (base if nothing was learned).
    data: list[dict[str, Any]] = field(default_factory=list)
    #: Nodes the learning graph ran, in order.
    path: list[str] = field(default_factory=list)


@dataclass(kw_only=True)
class LearnRunSummary(Record):
    id: str
    created_at: str
    client: str
    usecase: str
    object: str | None = None
    pattern_name: str
    source: str
    outcome: str
    base_version: str | None = None
    result_version: str | None = None
    passed_before: bool
    passed_after: bool | None = None
    requested_by: str

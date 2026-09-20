"""Corpus and ground-truth label contracts (DT-17 .. DT-22).

A corpus is the only client input to design-time. It is immutable and versioned
by id; a correction creates a new corpus version, never an edit in place (DT-17).
"""

from __future__ import annotations

import hashlib
from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SampleAttachment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str
    # Detected by content, not extension (mirrors RT-14 at design time).
    mime: str | None = None
    # Path relative to the corpus root, or absolute on disk.
    path: str | None = None
    unreadable: bool = False
    expected_unreadable: bool = False


class Label(BaseModel):
    """One ground-truth record per sample (DT-19)."""

    model_config = ConfigDict(extra="forbid")

    sample_id: str
    email_type: str
    in_scope: bool = True
    fields: dict[str, Any] = Field(default_factory=dict)
    # Where the value came from, when it came from an attachment (DT-19).
    field_sources: dict[str, str] = Field(default_factory=dict)
    labelled_by: str | None = None
    labelled_at: date | None = None


class Sample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sample_id: str
    subject: str = ""
    body: str = ""
    attachments: list[SampleAttachment] = Field(default_factory=list)


class CorpusMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    corpus_id: str
    client_id: str
    created_at: date | None = None
    provenance: str = ""
    consent_note: str = ""


class Corpus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    meta: CorpusMeta
    samples: list[Sample] = Field(default_factory=list)
    labels: list[Label] = Field(default_factory=list)

    def label_index(self) -> dict[str, Label]:
        return {label.sample_id: label for label in self.labels}

    def sample_index(self) -> dict[str, Sample]:
        return {sample.sample_id: sample for sample in self.samples}

    def types(self) -> set[str]:
        return {label.email_type for label in self.labels if label.in_scope}

    def samples_of_type(self, email_type: str) -> list[str]:
        return [lbl.sample_id for lbl in self.labels if lbl.in_scope and lbl.email_type == email_type]

    def out_of_scope_ids(self) -> list[str]:
        return [lbl.sample_id for lbl in self.labels if not lbl.in_scope]

    def heldout_ids(self, fraction: float = 0.2) -> set[str]:
        """Deterministic hold-out selection, seeded by the corpus id (DT-22).

        The same corpus always yields the same hold-out set, on any machine,
        so an evaluation is reproducible (DT-35) without storing the split.
        """
        held: set[str] = set()
        for email_type in sorted(self.types()):
            ids = sorted(self.samples_of_type(email_type))
            if not ids:
                continue
            scored = sorted(
                ids,
                key=lambda sid: hashlib.sha256(f"{self.meta.corpus_id}:{sid}".encode()).hexdigest(),
            )
            take = max(1, round(len(scored) * fraction))
            held.update(scored[:take])
        return held

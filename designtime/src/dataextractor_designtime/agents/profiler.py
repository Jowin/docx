"""Corpus Profiler (DT-06).

Enumerates every sample's attachments by magic bytes, records headers, sheet
names and column labels, and flags encrypted or unreadable items — all before
any generation runs, so an unreadable attachment shows up in the profile rather
than as a mid-run failure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..records import Record
from ..contracts.corpus import Corpus
from . import filetypes, tabular
from .base import DesignAgent


@dataclass(kw_only=True)
class AttachmentProfile(Record):
    filename: str
    detected_mime: str
    declared_mime: str | None = None
    #: True when the extension disagrees with the bytes (RT-14 rehearsal).
    extension_mismatch: bool = False
    supported_phase_1: bool = False
    readable: bool = True
    unreadable_reason: str | None = None
    sheet_names: list[str] = field(default_factory=list)
    column_labels: list[str] = field(default_factory=list)
    row_count: int = 0


@dataclass(kw_only=True)
class SampleProfile(Record):
    sample_id: str
    email_type: str | None = None
    in_scope: bool = True
    body_chars: int = 0
    subject_chars: int = 0
    attachments: list[AttachmentProfile] = field(default_factory=list)


@dataclass(kw_only=True)
class ProfilerInput(Record):
    corpus: Corpus
    #: Root to resolve relative attachment paths against.
    corpus_root: str | None = None


@dataclass(kw_only=True)
class ProfilerOutput(Record):
    corpus_id: str
    sample_count: int
    samples: list[SampleProfile] = field(default_factory=list)
    #: detected mime -> number of attachments.
    attachment_kinds: dict[str, int] = field(default_factory=dict)
    #: Every distinct column/sheet label seen, the input to alias proposal (DT-09).
    observed_column_labels: list[str] = field(default_factory=list)
    observed_sheet_names: list[str] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    generated_by: str


class CorpusProfiler(DesignAgent[ProfilerInput, ProfilerOutput]):
    name = "corpus-profiler"

    def run(self, payload: ProfilerInput) -> ProfilerOutput:
        corpus = payload.corpus
        root = Path(payload.corpus_root) if payload.corpus_root else None
        labels = corpus.label_index()

        samples: list[SampleProfile] = []
        kinds: dict[str, int] = {}
        column_labels: set[str] = set()
        sheet_names: set[str] = set()
        unreadable: list[str] = []
        unsupported: list[str] = []

        for sample in corpus.samples:
            label = labels.get(sample.sample_id)
            profiles: list[AttachmentProfile] = []

            for att in sample.attachments:
                path = self._resolve(att.path, root)
                detected = filetypes.detect(path) if path else filetypes.UNKNOWN
                kinds[detected] = kinds.get(detected, 0) + 1

                prof = AttachmentProfile(
                    filename=att.filename,
                    detected_mime=detected,
                    declared_mime=att.mime,
                    extension_mismatch=self._mismatch(att.filename, detected),
                    supported_phase_1=detected in filetypes.SUPPORTED_PHASE_1,
                    readable=not att.unreadable,
                )

                if att.unreadable:
                    prof.unreadable_reason = "marked unreadable in corpus"
                    unreadable.append(f"{sample.sample_id}:{att.filename}")
                elif detected not in filetypes.SUPPORTED_PHASE_1:
                    unsupported.append(f"{sample.sample_id}:{att.filename}:{detected}")
                elif path is not None:
                    try:
                        sheets = tabular.read(path)
                        prof.sheet_names = [s.name for s in sheets]
                        prof.column_labels = sorted({h for s in sheets for h in s.headers})
                        prof.row_count = sum(len(s.rows) for s in sheets)
                        sheet_names.update(prof.sheet_names)
                        column_labels.update(prof.column_labels)
                    except Exception as exc:  # unreadable is data, not a run failure
                        prof.readable = False
                        prof.unreadable_reason = str(exc)
                        unreadable.append(f"{sample.sample_id}:{att.filename}")

                profiles.append(prof)

            samples.append(
                SampleProfile(
                    sample_id=sample.sample_id,
                    email_type=label.email_type if label else None,
                    in_scope=label.in_scope if label else True,
                    body_chars=len(sample.body),
                    subject_chars=len(sample.subject),
                    attachments=profiles,
                )
            )

        return ProfilerOutput(
            corpus_id=corpus.meta.corpus_id,
            sample_count=len(corpus.samples),
            samples=samples,
            attachment_kinds=dict(sorted(kinds.items())),
            observed_column_labels=sorted(column_labels),
            observed_sheet_names=sorted(sheet_names),
            unreadable=sorted(unreadable),
            unsupported=sorted(unsupported),
            generated_by=self.identity,
        )

    @staticmethod
    def _resolve(path: str | None, root: Path | None) -> Path | None:
        if not path:
            return None
        p = Path(path)
        if not p.is_absolute() and root is not None:
            p = root / p
        return p if p.exists() else None

    @staticmethod
    def _mismatch(filename: str, detected: str) -> bool:
        suffix = Path(filename).suffix.lower()
        expected = {
            ".csv": filetypes.CSV,
            ".xlsx": filetypes.XLSX,
            ".xls": filetypes.XLS,
            ".pdf": filetypes.PDF,
            ".docx": filetypes.DOCX,
        }.get(suffix)
        return bool(expected) and expected != detected

"""Detection Rules Agent (DT-10, DT-11).

Generates keyword weights and regex patterns that generalise: a rule that does
not clear the support floor is dropped rather than shipped, because a pattern
encoding one sample's literal text is a defect, not a rule (CTR-28).
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from ..records import Record
from ..config import get_settings
from ..contracts.artifacts import DetectionRules, Keyword, Pattern, TypeDetectionRules
from ..contracts.corpus import Corpus
from ..model.base import ModelRequest
from .base import DesignAgent

_WORD_RE = re.compile(r"[a-z][a-z'-]{2,}")
_BIGRAM_STOP = {"the", "and", "for", "you", "your", "with", "this", "that", "from"}

#: Generic document patterns, kept client-agnostic on purpose.
_CANDIDATE_PATTERNS: list[tuple[str, str]] = [
    ("reference_no", r"(?i)\b(?:inv(?:oice)?|ref|po|doc)[ #:.-]{0,3}([A-Z0-9][A-Z0-9-]{3,19})\b"),
    ("money_amount", r"(?i)(?:[$£€]\s?|\b(?:usd|eur|gbp)\s)\d[\d,]*(?:\.\d{2})?\b"),
    ("iso_date", r"\b\d{4}-\d{2}-\d{2}\b"),
    ("percentage", r"\b\d{1,3}(?:\.\d+)?\s?%"),
]


@dataclass(kw_only=True)
class RuleEvidence(Record):
    rule: str
    kind: str
    support: int
    sample_ids: list[str] = field(default_factory=list)
    kept: bool = True
    dropped_reason: str | None = None


@dataclass(kw_only=True)
class DetectionRulesInput(Record):
    corpus: Corpus
    email_types: list[str] = field(default_factory=list)
    rule_support_min: int | None = None
    exclude_sample_ids: list[str] = field(default_factory=list)


@dataclass(kw_only=True)
class DetectionRulesOutput(Record):
    artifact: DetectionRules
    evidence: list[RuleEvidence] = field(default_factory=list)
    dropped: list[RuleEvidence] = field(default_factory=list)


class DetectionRulesAgent(DesignAgent[DetectionRulesInput, DetectionRulesOutput]):
    name = "detection-rules"

    def run(self, payload: DetectionRulesInput) -> DetectionRulesOutput:
        settings = get_settings()
        support_min = (
            payload.rule_support_min if payload.rule_support_min is not None else settings.rule_support_min
        )
        corpus = payload.corpus
        samples = corpus.sample_index()
        excluded = set(payload.exclude_sample_ids)
        types = payload.email_types or sorted(corpus.types())

        kept: list[RuleEvidence] = []
        dropped: list[RuleEvidence] = []
        per_type: dict[str, TypeDetectionRules] = {}

        # Terms that appear across every type carry no signal; count globally first.
        global_terms: Counter[str] = Counter()
        for lbl in corpus.labels:
            if lbl.sample_id in excluded or lbl.sample_id not in samples:
                continue
            s = samples[lbl.sample_id]
            global_terms.update(self._terms(f"{s.subject} {s.body}"))

        for email_type in types:
            ids = [i for i in corpus.samples_of_type(email_type) if i not in excluded]
            if not ids:
                continue

            term_support: Counter[str] = Counter()
            term_samples: dict[str, list[str]] = {}
            for sid in ids:
                s = samples.get(sid)
                if s is None:
                    continue
                for term in self._terms(f"{s.subject} {s.body}"):
                    term_support[term] += 1
                    term_samples.setdefault(term, []).append(sid)

            # Keep terms that are frequent here and not ubiquitous everywhere.
            discriminative = {
                term: count
                for term, count in term_support.items()
                if count >= support_min and count / len(ids) >= 0.5
                and global_terms[term] < len(corpus.labels)
            }
            for term, count in sorted(term_support.items()):
                ev = RuleEvidence(
                    rule=term,
                    kind="keyword",
                    support=count,
                    sample_ids=sorted(term_samples.get(term, []))[:5],
                    kept=term in discriminative,
                    dropped_reason=None if term in discriminative else f"support {count} < {support_min} or not discriminative",
                )
                (kept if ev.kept else dropped).append(ev)

            weights = self.model.complete(
                ModelRequest(
                    task="detection_rules.weight_terms",
                    evidence={"term_counts": dict(sorted(discriminative.items()))},
                )
            ).output["weights"]

            keywords = [
                Keyword(term=term, weight=weights[term], support=discriminative[term])
                for term in sorted(discriminative)
            ]

            patterns: list[Pattern] = []
            for name, regex in _CANDIDATE_PATTERNS:
                compiled = re.compile(regex)
                matched = [
                    sid
                    for sid in ids
                    if sid in samples
                    and compiled.search(f"{samples[sid].subject} {samples[sid].body}")
                ]
                ev = RuleEvidence(
                    rule=name,
                    kind="pattern",
                    support=len(matched),
                    sample_ids=sorted(matched)[:5],
                    kept=len(matched) >= support_min,
                    dropped_reason=None
                    if len(matched) >= support_min
                    else f"matched {len(matched)} distinct samples, floor is {support_min}",
                )
                (kept if ev.kept else dropped).append(ev)
                if ev.kept:
                    patterns.append(
                        Pattern(
                            name=name,
                            regex=regex,
                            weight=round(min(0.35, len(matched) / len(ids) * 0.35), 3),
                            support=len(matched),
                        )
                    )

            threshold = self.model.complete(
                ModelRequest(
                    task="detection_rules.classification_threshold",
                    evidence={"type_count": len(types)},
                )
            ).output["threshold"]

            per_type[email_type] = TypeDetectionRules(
                keywords=keywords,
                patterns=patterns,
                entity_weights={"MONEY": 0.25, "DATE": 0.10, "ORG": 0.10},
                classification_threshold=threshold,
                negative_signals=self._negative_signals(corpus, samples, email_type, support_min),
            )

        return DetectionRulesOutput(
            artifact=DetectionRules(types=per_type, generated_by=self.identity),
            evidence=kept,
            dropped=dropped,
        )

    @staticmethod
    def _terms(text: str) -> set[str]:
        words = [w for w in _WORD_RE.findall(text.lower())]
        unigrams = {w for w in words if w not in _BIGRAM_STOP}
        bigrams = {
            f"{a} {b}"
            for a, b in zip(words, words[1:])
            if a not in _BIGRAM_STOP and b not in _BIGRAM_STOP
        }
        return unigrams | bigrams

    def _negative_signals(
        self, corpus: Corpus, samples: dict, email_type: str, support_min: int
    ) -> list[str]:
        """DT-11: mined from samples labelled as other types or out of scope."""
        others = [
            lbl.sample_id
            for lbl in corpus.labels
            if (not lbl.in_scope or lbl.email_type != email_type) and lbl.sample_id in samples
        ]
        if not others:
            return []
        mine_terms: set[str] = set()
        for sid in corpus.samples_of_type(email_type):
            if sid in samples:
                mine_terms |= self._terms(f"{samples[sid].subject} {samples[sid].body}")
        counter: Counter[str] = Counter()
        for sid in others:
            s = samples[sid]
            counter.update(self._terms(f"{s.subject} {s.body}") - mine_terms)
        return sorted(t for t, c in counter.items() if c >= min(support_min, len(others)))

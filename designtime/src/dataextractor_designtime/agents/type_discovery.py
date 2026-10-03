"""Type Discovery (DT-07).

Proposes the email types in scope, each with a sample count and at least three
exemplars, and stops there: generation cannot start until a designer confirms
the list, and the confirmed list becomes ``email_types`` in the manifest (CTR-05).
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from ..records import Record
from ..contracts.corpus import Corpus
from ..model.base import ModelRequest
from .base import DesignAgent

_WORD_RE = re.compile(r"[a-z][a-z'-]{2,}")
_STOPWORDS = {
    "the", "and", "for", "you", "your", "with", "this", "that", "from", "have",
    "has", "are", "was", "will", "can", "our", "all", "any", "please", "dear",
    "hello", "thanks", "regards", "kind", "best", "team", "find", "attached",
    "attachment", "see", "below", "above", "not", "but", "per", "vendor",
}

MIN_EXEMPLARS = 3


@dataclass(kw_only=True)
class TypeProposal(Record):
    email_type: str
    sample_count: int
    exemplars: list[str] = field(default_factory=list)
    frequent_terms: list[str] = field(default_factory=list)
    #: DT-07 requires at least three exemplars; below that the proposal is weak.
    sufficient_exemplars: bool = True
    rationale: str = ""


@dataclass(kw_only=True)
class TypeDiscoveryInput(Record):
    corpus: Corpus


@dataclass(kw_only=True)
class TypeDiscoveryOutput(Record):
    corpus_id: str
    proposals: list[TypeProposal] = field(default_factory=list)
    out_of_scope_count: int = 0
    #: Always true. Generation is gated on an explicit confirmation (DT-07).
    requires_confirmation: bool = True
    generated_by: str


class TypeDiscovery(DesignAgent[TypeDiscoveryInput, TypeDiscoveryOutput]):
    name = "type-discovery"

    def run(self, payload: TypeDiscoveryInput) -> TypeDiscoveryOutput:
        corpus = payload.corpus
        samples = corpus.sample_index()
        proposals: list[TypeProposal] = []

        for email_type in sorted(corpus.types()):
            ids = corpus.samples_of_type(email_type)
            terms = self._frequent_terms(
                [
                    f"{samples[i].subject} {samples[i].body}"
                    for i in ids
                    if i in samples
                ]
            )
            named = self.model.complete(
                ModelRequest(
                    task="type_discovery.name_cluster",
                    evidence={"labelled_type": email_type, "frequent_terms": terms},
                )
            ).output
            proposals.append(
                TypeProposal(
                    email_type=named["name"],
                    sample_count=len(ids),
                    exemplars=sorted(ids)[:MIN_EXEMPLARS],
                    frequent_terms=terms,
                    sufficient_exemplars=len(ids) >= MIN_EXEMPLARS,
                    rationale=named.get("rationale", ""),
                )
            )

        return TypeDiscoveryOutput(
            corpus_id=corpus.meta.corpus_id,
            proposals=proposals,
            out_of_scope_count=len(corpus.out_of_scope_ids()),
            generated_by=self.identity,
        )

    @staticmethod
    def _frequent_terms(texts: list[str], limit: int = 8) -> list[str]:
        counter: Counter[str] = Counter()
        for text in texts:
            seen = {w for w in _WORD_RE.findall(text.lower()) if w not in _STOPWORDS}
            counter.update(seen)
        return [term for term, _ in counter.most_common(limit)]

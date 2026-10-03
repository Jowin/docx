"""Learning memory: what pattern learning keeps between calls, in LangGraph's store.

Long-term memory lives in a LangGraph ``BaseStore`` (``PostgresStore`` on the
registry's database; ``InMemoryStore`` in tests or without Postgres), under
namespaces scoped by client and use case:

    ("learning", client, usecase, "rejected_hints")   key: "<field>|<kind>|<hint>"
        a hint whose candidate broke an earlier sample; the writer never proposes it again
    ("learning", client, usecase, "patterns")         key: pattern name
        what each learned pattern looks like (its fingerprint) and its history
    ("learning", client, usecase, "corrections")      key: runtime job id
        reviewer corrections already taken in as ground truth, so an import is idempotent

The run state of one learning call is checkpointed separately (``PostgresSaver``),
so a call interrupted mid-way resumes from its last node.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any, Iterator

NS = "learning"


def _ns(client: str, usecase: str, kind: str) -> tuple[str, ...]:
    return (NS, client, usecase, kind)


def hint_key(field: str, kind: str, hint: str) -> str:
    return f"{field}|{kind}|{' '.join(hint.split()).casefold()}"


class LearningMemory:
    def __init__(self, store: Any) -> None:
        self.store = store

    # -------------------------------------------------------------- rejected hints

    def rejected(self, client: str, usecase: str) -> list[dict[str, Any]]:
        return [i.value for i in self.store.search(_ns(client, usecase, "rejected_hints"), limit=1000)]

    def reject(self, client: str, usecase: str, hints: list[dict[str, Any]], *, reason: str, pattern: str,
               run_id: str) -> None:
        for h in hints:
            kind = "labels" if h["kind"] in ("label", "labels") else "anchors"
            self.store.put(_ns(client, usecase, "rejected_hints"), hint_key(h["field"], kind, h["hint"]),
                           {"field": h["field"], "kind": kind, "hint": h["hint"], "reason": reason,
                            "pattern": pattern, "run_id": run_id, "at": time.time()})

    def forget_rejected(self, client: str, usecase: str, key: str) -> None:
        self.store.delete(_ns(client, usecase, "rejected_hints"), key)

    # -------------------------------------------------------------- patterns

    def pattern(self, client: str, usecase: str, name: str) -> dict[str, Any] | None:
        item = self.store.get(_ns(client, usecase, "patterns"), name)
        return item.value if item else None

    def remember_pattern(self, client: str, usecase: str, name: str, *, applies_to: dict[str, Any],
                         run_id: str, outcome: str, version: str | None, source: str) -> None:
        prev = self.pattern(client, usecase, name) or {"samples": [], "versions": []}
        samples = (prev.get("samples") or []) + [{"run_id": run_id, "source": source, "outcome": outcome,
                                                  "at": time.time()}]
        versions = (prev.get("versions") or []) + ([version] if version else [])
        self.store.put(_ns(client, usecase, "patterns"), name,
                       {"name": name, "applies_to": applies_to or prev.get("applies_to") or {},
                        "samples": samples[-50:], "versions": versions[-50:], "last_outcome": outcome,
                        "updated_at": time.time()})

    def patterns(self, client: str, usecase: str) -> list[dict[str, Any]]:
        return [i.value for i in self.store.search(_ns(client, usecase, "patterns"), limit=1000)]

    # -------------------------------------------------------------- corrections

    def correction_seen(self, client: str, usecase: str, job_id: str) -> dict[str, Any] | None:
        item = self.store.get(_ns(client, usecase, "corrections"), job_id)
        return item.value if item else None

    def remember_correction(self, client: str, usecase: str, job_id: str, value: dict[str, Any]) -> None:
        self.store.put(_ns(client, usecase, "corrections"), job_id, {**value, "at": time.time()})

    def corrections(self, client: str, usecase: str) -> list[dict[str, Any]]:
        return [i.value for i in self.store.search(_ns(client, usecase, "corrections"), limit=1000)]


# ------------------------------------------------------------------ backends


def psycopg_url(sqlalchemy_url: str) -> str:
    """postgresql+psycopg2://u:p@h/db -> postgresql://u:p@h/db (what psycopg 3 takes)."""
    scheme, _, rest = sqlalchemy_url.partition("://")
    return f"{scheme.split('+', 1)[0]}://{rest}"


#: Types the learning graph's checkpoints may hold (deserialization is limited to these).
CHECKPOINT_TYPES = [
    ("dataextractor_designtime.learning.models", "LearnRequest"),
    ("dataextractor_designtime.learning.models", "Attempt"),
    ("dataextractor_designtime.agents.extraction_judge", "ExtractionJudgeOutput"),
    ("dataextractor_designtime.agents.extraction_judge", "Failure"),
    ("dataextractor_designtime.agents.extraction_judge", "Score"),
    ("dataextractor_designtime.agents.pattern_skill_writer", "Derivation"),
    ("pathlib", "PosixPath"), ("pathlib", "WindowsPath"), ("pathlib", "Path"),
]

_SETUP_DONE: set[str] = set()


@contextmanager
def open_memory(database_url: str | None) -> Iterator[tuple[LearningMemory, Any]]:
    """(memory, checkpointer) for one request: Postgres when reachable, else in-process."""
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    serde = JsonPlusSerializer(allowed_msgpack_modules=CHECKPOINT_TYPES)
    if database_url and database_url.startswith("postgresql"):
        import psycopg
        from langgraph.checkpoint.postgres import PostgresSaver
        from langgraph.store.postgres import PostgresStore
        from psycopg.rows import dict_row
        url = psycopg_url(database_url)
        kw = {"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row}
        # one connection each: the checkpointer writes from a background thread in pipeline mode
        with psycopg.connect(url, **kw) as store_conn, psycopg.connect(url, **kw) as saver_conn:
            store, saver = PostgresStore(store_conn), PostgresSaver(saver_conn, serde=serde)
            if url not in _SETUP_DONE:
                store.setup()
                saver.setup()
                _SETUP_DONE.add(url)
            yield LearningMemory(store), saver
        return
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.store.memory import InMemoryStore
    yield LearningMemory(_LOCAL_STORE), InMemorySaver(serde=serde)


def _local_store():
    from langgraph.store.memory import InMemoryStore
    return InMemoryStore()


_LOCAL_STORE = _local_store()


def reset_setup_cache() -> None:
    """Tests drop LangGraph's tables between cases; the next open re-creates them."""
    _SETUP_DONE.clear()

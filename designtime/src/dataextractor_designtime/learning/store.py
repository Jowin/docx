"""Design runs in the Postgres registry (pattern learning and authoring): the record of each, and the
regression set (pattern runs only)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..agents.base import AgentError
from ..registry.models import LearningRunRow

#: Outcomes whose source passed on the version they left behind.
_PASSING = ("passed", "learned")


class LearningStore:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, **values: Any) -> LearningRunRow:
        row = LearningRunRow(**values)
        self.session.add(row)
        self.session.flush()
        return row

    def update(self, run_id: str, **values: Any) -> LearningRunRow:
        row = self.get(run_id)
        for k, v in values.items():
            setattr(row, k, v)
        self.session.flush()
        return row

    def delete(self, run_id: str) -> None:
        row = self.session.get(LearningRunRow, run_id)
        if row is not None:
            self.session.delete(row)
            self.session.flush()

    def get(self, run_id: str) -> LearningRunRow:
        row = self.session.get(LearningRunRow, run_id)
        if row is None:
            raise AgentError(f"no learning run {run_id}", code="learning_run_not_found", status=404)
        return row

    def list(self, client: str | None = None, usecase: str | None = None,
             pattern_name: str | None = None, limit: int = 50, kind: str | None = None) -> list[LearningRunRow]:
        stmt = select(LearningRunRow)
        for col, value in ((LearningRunRow.client_id, client), (LearningRunRow.usecase, usecase),
                           (LearningRunRow.pattern_name, pattern_name), (LearningRunRow.kind, kind)):
            if value:
                stmt = stmt.where(col == value)
        stmt = stmt.order_by(LearningRunRow.created_at.desc()).limit(max(1, min(limit, 500)))
        return list(self.session.execute(stmt).scalars())

    def regression_samples(self, client: str, usecase: str, *, exclude_sha256: str | None,
                           limit: int) -> list[LearningRunRow]:
        """The latest passing run per source for this client and use case, newest first.

        A source that passed when it was last learned must keep passing: these
        are re-run against every candidate before it is accepted.
        """
        stmt = (select(LearningRunRow)
                .where(LearningRunRow.client_id == client, LearningRunRow.usecase == usecase,
                       LearningRunRow.kind == "pattern")
                .order_by(LearningRunRow.created_at.desc()))
        seen: set[str] = set()
        out: list[LearningRunRow] = []
        for row in self.session.execute(stmt).scalars():
            key = row.source_sha256 or row.source
            if key in seen:
                continue
            seen.add(key)               # only the latest run of a source speaks for it
            if key == exclude_sha256 or row.outcome not in _PASSING:
                continue
            out.append(row)
            if len(out) >= limit:
                break
        return out

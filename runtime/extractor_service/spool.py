"""Run spool: item bytes stored once per run in Postgres and referenced from the run state.

Intake puts each item's bytes here and keeps only a reference (the content's
SHA-256) in the submission, so checkpoints stay small whatever the attachment
size (RT-34), and any worker on any host can read them back when it resumes a
run. With STATE_KEY set (a Fernet key) the bytes are encrypted at rest
(RT-35, RT-51). A finished run's rows are deleted by the retention sweep.

    spool(run_id, ref, data bytea, created_at)    primary key (run_id, ref)
"""
from __future__ import annotations

import hashlib
import time
from typing import Any

DDL = """
CREATE TABLE IF NOT EXISTS spool (
  run_id TEXT NOT NULL, ref TEXT NOT NULL, data BYTEA NOT NULL, created_at DOUBLE PRECISION NOT NULL,
  PRIMARY KEY (run_id, ref));
"""


class Spool:
    def __init__(self, pool: Any, run_id: str, key: str | None = None) -> None:
        self.pool, self.run_id = pool, run_id
        self._fernet = None
        if key:
            from cryptography.fernet import Fernet
            self._fernet = Fernet(key.encode() if isinstance(key, str) else key)

    def put(self, data: bytes) -> str:
        ref = hashlib.sha256(data).hexdigest()
        blob = self._fernet.encrypt(data) if self._fernet else data
        with self.pool.connection() as conn:
            conn.execute("INSERT INTO spool(run_id, ref, data, created_at) VALUES (%s, %s, %s, %s) "
                         "ON CONFLICT (run_id, ref) DO NOTHING", (self.run_id, ref, blob, time.time()))
        return ref

    def get(self, ref: str) -> bytes:
        with self.pool.connection() as conn:
            row = conn.execute("SELECT data FROM spool WHERE run_id=%s AND ref=%s", (self.run_id, ref)).fetchone()
        if row is None:
            raise KeyError(f"no spooled bytes {ref[:12]} for run {self.run_id}")
        blob = bytes(row["data"])
        return self._fernet.decrypt(blob) if self._fernet else blob

    def delete(self) -> int:
        with self.pool.connection() as conn:
            return conn.execute("DELETE FROM spool WHERE run_id=%s", (self.run_id,)).rowcount

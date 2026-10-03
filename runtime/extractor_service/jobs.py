"""Jobs: the queue, the workers, result delivery, the review queue and the audit chain.

Every extraction is a job, whether the caller waits for it or not:

    POST /extract                       -> runs now, answers with the result     (status extracted)
    POST /extract {"callback_url": ...}  -> queued; the result is POSTed there    (status inprogress, then extracted)
    POST /extract {"async": true}        -> queued; poll GET /extractions/{job_id}

Lifecycle: ``queued`` -> ``running`` -> ``done``. Callers only ever see
``inprogress`` (queued or running) or ``extracted`` (done). Every job ends in a
result: an internal failure is retried with backoff and, after
``JOB_MAX_ATTEMPTS``, dead-lettered with a flagged result (RT-56) so the caller
still hears back.

* **Durable queue (RT-49).** Jobs live in Postgres (db.py) before any work
  starts. Workers claim a job with ``FOR UPDATE SKIP LOCKED`` and a lease, so
  any number of worker processes on any number of hosts share one queue; a job
  whose worker vanished is claimed again when the lease runs out, and resumes
  from its last LangGraph checkpoint (also in Postgres) rather than starting
  over (RT-40).
* **Idempotency (RT-04).** A job is keyed by the caller's ``idempotency_key``,
  or by the input's SHA-256 with the resolved config. Within the dedupe window
  the same key returns the existing job (and its ``audit_id``).
* **Webhooks.** Results are POSTed as JSON, signed with HMAC-SHA256 when
  WEBHOOK_SECRET is set (``X-Signature: sha256=<hex>`` over
  ``<X-Timestamp>.<body>``), retried with exponential backoff, and can be
  redelivered on request.
* **Review queue (RT-44..48).** A flagged result also opens a review entry. A
  reviewer's correction is logged field by field (original, corrected, who,
  when), produces a corrected result with the same ``audit_id`` marked
  ``human_corrected``, and is delivered again. Corrections export as ground
  truth for design-time learning (DT-39).
* **Audit chain.** Every finished, corrected or rejected result is appended to
  a hash chain (each entry hashes the previous one), so any edit to the record
  is detectable (``GET /audit/verify``). Appends are serialised with a
  transaction-scoped advisory lock.

A job's completion (its result, review entry, chain entry and webhook
delivery) commits in one transaction, so none of them exists without the others.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import ipaddress
import json
import logging
import socket
import statistics
import threading
import time
import uuid
from typing import Any
from urllib.parse import urlparse

from . import pipeline
from .errors import ServiceError

log = logging.getLogger("extractor_service.jobs")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, idem_key TEXT NOT NULL, request JSONB NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'done')),
  attempts INTEGER NOT NULL DEFAULT 0, available_at DOUBLE PRECISION NOT NULL,
  lease_until DOUBLE PRECISION, worker TEXT, created_at DOUBLE PRECISION NOT NULL,
  started_at DOUBLE PRECISION, finished_at DOUBLE PRECISION,
  client TEXT, usecase TEXT, config_version TEXT, audit_id TEXT, flagged BOOLEAN, flags JSONB,
  result TEXT, original_result TEXT, callback_url TEXT, dead BOOLEAN NOT NULL DEFAULT FALSE,
  human_corrected BOOLEAN NOT NULL DEFAULT FALSE, cost_usd DOUBLE PRECISION, last_error TEXT,
  extended BOOLEAN NOT NULL DEFAULT FALSE);
CREATE INDEX IF NOT EXISTS ix_jobs_queue ON jobs(status, available_at);
CREATE INDEX IF NOT EXISTS ix_jobs_idem ON jobs(idem_key, created_at);
CREATE INDEX IF NOT EXISTS ix_jobs_client ON jobs(client, usecase);
CREATE TABLE IF NOT EXISTS deliveries (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  event TEXT NOT NULL, url TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0, next_at DOUBLE PRECISION NOT NULL, last_status INTEGER,
  last_error TEXT, created_at DOUBLE PRECISION NOT NULL, delivered_at DOUBLE PRECISION);
CREATE INDEX IF NOT EXISTS ix_deliveries_due ON deliveries(status, next_at);
CREATE TABLE IF NOT EXISTS review (
  job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE, audit_id TEXT, client TEXT, usecase TEXT,
  flags JSONB, status TEXT NOT NULL, opened_at DOUBLE PRECISION NOT NULL, resolved_at DOUBLE PRECISION,
  reviewer TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS corrections (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  audit_id TEXT, record INTEGER, field TEXT, original JSONB, corrected JSONB, reviewer TEXT NOT NULL,
  at DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS audit_chain (
  seq BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, job_id TEXT, audit_id TEXT, event TEXT,
  at DOUBLE PRECISION NOT NULL, record_sha256 TEXT NOT NULL, prev_hash TEXT NOT NULL, hash TEXT NOT NULL);
"""
GENESIS = "0" * 64
PUBLIC = {"queued": "inprogress", "running": "inprogress", "done": "extracted"}
#: pg_advisory_xact_lock key that serialises audit-chain appends
_CHAIN_LOCK = 0x45585452


def _now() -> float:
    return time.time()


def _sha(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _json(value: Any) -> Any:
    """JSONB comes back parsed; TEXT JSON is parsed here."""
    return json.loads(value) if isinstance(value, (str, bytes)) else value


def _jb(value: Any) -> Any:
    from psycopg.types.json import Jsonb
    return Jsonb(value)


# ------------------------------------------------------------------ webhook safety


def check_callback_url(url: str, allowed_hosts: tuple[str, ...]) -> str:
    """http(s) only; never link-local or cloud metadata addresses; an allowlist when one is set."""
    p = urlparse(url)
    host = (p.hostname or "").lower()
    if p.scheme not in ("http", "https") or not host:
        raise ServiceError(422, "invalid_request", "callback_url must be an http(s) URL",
                           {"errors": [{"loc": "callback_url", "msg": "must be an http(s) URL"}]})
    if allowed_hosts and host not in allowed_hosts:
        raise ServiceError(422, "invalid_request", f"callback host {host!r} is not allowed",
                           {"errors": [{"loc": "callback_url", "msg": "host not in WEBHOOK_ALLOWED_HOSTS"}]})
    if host in ("metadata.google.internal", "metadata") or _link_local(host):
        raise ServiceError(422, "invalid_request", "callback_url points at a link-local address",
                           {"errors": [{"loc": "callback_url", "msg": "link-local addresses are refused"}]})
    return url


def _link_local(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_link_local
    except ValueError:
        return False


def _resolves_link_local(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    return any(_link_local(i[4][0].split("%")[0]) for i in infos)


def sign(secret: str, timestamp: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


# ------------------------------------------------------------------ the store


class JobStore:
    """Postgres-backed jobs, deliveries, review queue, corrections, audit chain and spool."""

    def __init__(self, pool: Any) -> None:
        from .spool import DDL as SPOOL_DDL
        self.pool = pool
        with pool.connection() as conn, conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_CHAIN_LOCK + 1,))   # one schema creator at a time
            conn.execute(SCHEMA + SPOOL_DDL, prepare=False)

    # -------------------------------------------------------------- helpers

    def _one(self, sql: str, args: tuple = ()) -> dict[str, Any] | None:
        with self.pool.connection() as conn:
            return conn.execute(sql, args).fetchone()

    def _all(self, sql: str, args: tuple | list = ()) -> list[dict[str, Any]]:
        with self.pool.connection() as conn:
            return conn.execute(sql, args).fetchall()

    def _exec(self, sql: str, args: tuple = ()) -> int:
        with self.pool.connection() as conn:
            return conn.execute(sql, args).rowcount

    # -------------------------------------------------------------- jobs

    def find_recent(self, idem_key: str, window_days: float) -> dict[str, Any] | None:
        return self._one("SELECT * FROM jobs WHERE idem_key=%s AND created_at>=%s AND NOT dead "
                         "ORDER BY created_at DESC LIMIT 1", (idem_key, _now() - window_days * 86400))

    def create(self, idem_key: str, request: dict[str, Any], *, callback_url: str | None, extended: bool,
               running_by: str | None = None, lease_s: float = 0) -> str:
        job_id = "job_" + uuid.uuid4().hex[:20]
        now = _now()
        self._exec(
            "INSERT INTO jobs(id, idem_key, request, status, attempts, available_at, lease_until, worker, "
            "created_at, started_at, client, usecase, callback_url, extended) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (job_id, idem_key, _jb(request), "running" if running_by else "queued", 1 if running_by else 0,
             now, now + lease_s if running_by else None, running_by, now, now if running_by else None,
             request.get("client"), request.get("usecase"), callback_url, bool(extended)))
        return job_id

    def get(self, job_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM jobs WHERE id=%s", (job_id,))

    def claim(self, worker: str, lease_s: float) -> dict[str, Any] | None:
        """The oldest claimable job, locked against every other worker (SKIP LOCKED)."""
        now = _now()
        with self.pool.connection() as conn, conn.transaction():
            row = conn.execute(
                "SELECT id FROM jobs WHERE (status='queued' AND available_at<=%s) "
                "OR (status='running' AND lease_until<%s) ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED",
                (now, now)).fetchone()
            if row is None:
                return None
            return conn.execute(
                "UPDATE jobs SET status='running', worker=%s, lease_until=%s, attempts=attempts+1, "
                "started_at=COALESCE(started_at, %s) WHERE id=%s RETURNING *",
                (worker, now + lease_s, now, row["id"])).fetchone()

    def finish(self, job_id: str, outcome: pipeline.Outcome, *, dead: bool = False) -> None:
        ext = outcome.extended
        cfg = (ext.get("metadata") or {}).get("config") or {}
        cost = ((ext.get("metadata") or {}).get("cost") or {}).get("model_usd")
        with self.pool.connection() as conn, conn.transaction():
            job = conn.execute(
                "UPDATE jobs SET status='done', finished_at=%s, lease_until=NULL, audit_id=%s, flagged=%s, "
                "flags=%s, result=%s, dead=%s, cost_usd=%s, client=COALESCE(%s, client), "
                "usecase=COALESCE(%s, usecase), config_version=%s WHERE id=%s RETURNING *",
                (_now(), outcome.audit_id, bool(outcome.flagged), _jb(outcome.flags), json.dumps(ext, default=str),
                 bool(dead), cost, cfg.get("client"), cfg.get("usecase"), cfg.get("version"), job_id)).fetchone()
            self._chain(conn, job_id, outcome.audit_id, "extraction.completed", ext)
            if outcome.flagged:
                conn.execute(
                    "INSERT INTO review(job_id, audit_id, client, usecase, flags, status, opened_at) "
                    "VALUES (%s,%s,%s,%s,%s,'open',%s) ON CONFLICT (job_id) DO UPDATE SET "
                    "audit_id=EXCLUDED.audit_id, flags=EXCLUDED.flags, status='open', opened_at=EXCLUDED.opened_at",
                    (job_id, outcome.audit_id, job["client"], job["usecase"], _jb(outcome.flags), _now()))
            if job["callback_url"]:
                self._enqueue(conn, job, "extraction.completed")

    def retry_later(self, job_id: str, error: str, delay_s: float) -> None:
        self._exec("UPDATE jobs SET status='queued', available_at=%s, lease_until=NULL, last_error=%s WHERE id=%s",
                   (_now() + delay_s, error[:1000], job_id))

    def list(self, *, status: str | None = None, client: str | None = None, flagged: bool | None = None,
             limit: int = 50) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM jobs WHERE TRUE", []
        if status:
            internal = {"inprogress": ["queued", "running"], "extracted": ["done"]}.get(status, [status])
            q += " AND status = ANY(%s)"
            args.append(internal)
        if client:
            q += " AND client=%s"
            args.append(client)
        if flagged is not None:
            q += " AND flagged=%s"
            args.append(bool(flagged))
        q += " ORDER BY created_at DESC LIMIT %s"
        args.append(max(1, min(limit, 500)))
        return self._all(q, args)

    # -------------------------------------------------------------- delivery

    def payload(self, job: dict[str, Any], event: str) -> dict[str, Any]:
        ext = _json(job["result"])
        req = _json(job["request"])
        return {"event": event, "job_id": job["id"], "status": "extracted", "flagged": bool(job["flagged"]),
                "human_corrected": bool(job["human_corrected"]), "audit_id": job["audit_id"],
                "request": {k: req.get(k) for k in ("file_location", "client", "usecase", "version",
                                                    "idempotency_key") if req.get(k) is not None},
                "result": pipeline.deliverable(ext, bool(job["extended"])),
                **({"rejected": True} if event == "extraction.rejected" else {})}

    def _enqueue(self, conn: Any, job: dict[str, Any], event: str) -> None:
        conn.execute("INSERT INTO deliveries(job_id, event, url, payload, status, next_at, created_at) "
                     "VALUES (%s,%s,%s,%s,'pending',%s,%s)",
                     (job["id"], event, job["callback_url"], json.dumps(self.payload(job, event), default=str),
                      _now(), _now()))

    def enqueue_delivery(self, job_id: str, event: str) -> None:
        with self.pool.connection() as conn, conn.transaction():
            job = conn.execute("SELECT * FROM jobs WHERE id=%s", (job_id,)).fetchone()
            if job and job["callback_url"] and job["status"] == "done":
                self._enqueue(conn, job, event)

    def due_deliveries(self, limit: int = 20) -> list[dict[str, Any]]:
        """Due deliveries, each claimed briefly so two workers never send the same one at once."""
        now = _now()
        with self.pool.connection() as conn, conn.transaction():
            rows = conn.execute("SELECT * FROM deliveries WHERE status='pending' AND next_at<=%s "
                                "ORDER BY next_at LIMIT %s FOR UPDATE SKIP LOCKED", (now, limit)).fetchall()
            if rows:
                conn.execute("UPDATE deliveries SET next_at=%s WHERE id = ANY(%s)",
                             (now + 120, [r["id"] for r in rows]))       # in flight: hidden for 2 minutes
        return rows

    def mark_delivery(self, delivery_id: int, ok: bool, status: int | None, error: str | None,
                      max_attempts: int, backoff_s: float) -> None:
        with self.pool.connection() as conn, conn.transaction():
            d = conn.execute("SELECT * FROM deliveries WHERE id=%s FOR UPDATE", (delivery_id,)).fetchone()
            attempts = d["attempts"] + 1
            if ok:
                conn.execute("UPDATE deliveries SET status='delivered', attempts=%s, last_status=%s, "
                             "delivered_at=%s, last_error=NULL WHERE id=%s", (attempts, status, _now(), delivery_id))
            else:
                final = attempts >= max_attempts
                conn.execute("UPDATE deliveries SET status=%s, attempts=%s, last_status=%s, last_error=%s, "
                             "next_at=%s WHERE id=%s",
                             ("failed" if final else "pending", attempts, status, (error or "")[:500],
                              _now() + min(backoff_s * 2 ** (attempts - 1), 3600), delivery_id))

    def deliveries(self, job_id: str) -> list[dict[str, Any]]:
        return self._all("SELECT id, event, status, attempts, last_status, last_error, created_at, delivered_at "
                         "FROM deliveries WHERE job_id=%s ORDER BY id", (job_id,))

    # -------------------------------------------------------------- review

    def review_list(self, *, status: str | None = "open", client: str | None = None,
                    limit: int = 50) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM review WHERE TRUE", []
        if status:
            q += " AND status=%s"
            args.append(status)
        if client:
            q += " AND client=%s"
            args.append(client)
        q += " ORDER BY opened_at LIMIT %s"
        args.append(max(1, min(limit, 500)))
        return self._all(q, args)

    def review_get(self, job_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM review WHERE job_id=%s", (job_id,))

    def resolve(self, job_id: str, *, reviewer: str, action: str, corrections: list[dict[str, Any]],
                records: list[dict[str, Any]] | None, note: str | None) -> dict[str, Any]:
        with self.pool.connection() as conn, conn.transaction():
            entry = conn.execute("SELECT * FROM review WHERE job_id=%s FOR UPDATE", (job_id,)).fetchone()
            job = conn.execute("SELECT * FROM jobs WHERE id=%s FOR UPDATE", (job_id,)).fetchone()
            if entry is None or job is None:
                raise ServiceError(404, "review_not_found", f"no review entry for {job_id}")
            if entry["status"] != "open":
                raise ServiceError(409, "review_closed", f"review for {job_id} is already {entry['status']}")
            ext = _json(job["result"])
            if action == "reject":
                conn.execute("UPDATE review SET status='rejected', resolved_at=%s, reviewer=%s, note=%s "
                             "WHERE job_id=%s", (_now(), reviewer, note, job_id))
                self._chain(conn, job_id, job["audit_id"], "extraction.rejected", {"reviewer": reviewer, "note": note})
                if job["callback_url"]:
                    self._enqueue(conn, job, "extraction.rejected")
                return {"job_id": job_id, "status": "rejected"}
            corrected = copy.deepcopy(ext)
            data = corrected["data"]
            log_rows = []
            if records is not None:
                for i, rec in enumerate(records):
                    old = data[i] if i < len(data) else {}
                    for f in sorted(set(old) | set(rec)):
                        if old.get(f) != rec.get(f):
                            log_rows.append((i, f, old.get(f), rec.get(f)))
                data[:] = records
            for c in corrections:
                i, f = int(c["record"]), c["field"]
                if i < 0 or i > len(data):
                    raise ServiceError(422, "invalid_request", f"record {i} does not exist")
                if i == len(data):
                    data.append({k: None for k in (data[0] if data else {})})
                if data and f not in data[i] and data[i]:
                    raise ServiceError(422, "invalid_request", f"record {i} has no field {f!r}")
                log_rows.append((i, f, data[i].get(f), c.get("value")))
                data[i][f] = c.get("value")
            if not log_rows and records is None:
                raise ServiceError(422, "invalid_request", "a correction needs corrections or records")
            now = _now()
            for i, f, old, new in log_rows:
                conn.execute("INSERT INTO corrections(job_id, audit_id, record, field, original, corrected, "
                             "reviewer, at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                             (job_id, job["audit_id"], i, f, _jb(old), _jb(new), reviewer, now))
            corrected.update({"flagged": False, "flags": [], "human_corrected": True,
                              "corrected_by": reviewer, "original_flags": ext.get("flags", [])})
            for i, rec in enumerate(corrected.get("records") or []):
                if i < len(data):
                    rec["data"], rec["flagged"], rec["flags"] = data[i], False, []
            job = conn.execute("UPDATE jobs SET result=%s, original_result=COALESCE(original_result, result), "
                               "flagged=FALSE, flags='[]'::jsonb, human_corrected=TRUE WHERE id=%s RETURNING *",
                               (json.dumps(corrected, default=str), job_id)).fetchone()
            conn.execute("UPDATE review SET status='corrected', resolved_at=%s, reviewer=%s, note=%s WHERE job_id=%s",
                         (now, reviewer, note, job_id))
            self._chain(conn, job_id, job["audit_id"], "extraction.corrected", corrected)
            if job["callback_url"]:
                self._enqueue(conn, job, "extraction.corrected")
            return {"job_id": job_id, "status": "corrected", "changes": len(log_rows)}

    def corrections_export(self, client: str | None = None, usecase: str | None = None) -> list[dict[str, Any]]:
        q = ("SELECT j.* FROM jobs j JOIN review r ON r.job_id=j.id WHERE r.status='corrected'"
             + (" AND j.client=%s" if client else "") + (" AND j.usecase=%s" if usecase else "")
             + " ORDER BY r.resolved_at")
        out = []
        for j in self._all(q, [a for a in (client, usecase) if a]):
            ext, req = _json(j["result"]), _json(j["request"])
            inp = ((_json(j["original_result"] or j["result"]).get("metadata") or {}).get("input") or {})
            rows = self._all("SELECT record, field, reviewer, at FROM corrections WHERE job_id=%s ORDER BY id",
                             (j["id"],))
            out.append({"job_id": j["id"], "audit_id": j["audit_id"], "client": j["client"],
                        "usecase": j["usecase"], "config_version": j["config_version"],
                        "file_location": req["file_location"], "source_sha256": inp.get("sha256"),
                        "sender": inp.get("sender"), "subject": inp.get("subject"),
                        "ground_truth": ext["data"],
                        "corrected_fields": [{"record": r["record"], "field": r["field"]} for r in rows],
                        "reviewer": rows[-1]["reviewer"] if rows else ext.get("corrected_by"),
                        "corrected_at": rows[-1]["at"] if rows else None})
        return out

    # -------------------------------------------------------------- audit chain

    def _chain(self, conn: Any, job_id: str, audit_id: str | None, event: str, record: Any) -> None:
        """Append to the hash chain inside the caller's transaction, one appender at a time."""
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_CHAIN_LOCK,))
        last = conn.execute("SELECT hash FROM audit_chain ORDER BY seq DESC LIMIT 1").fetchone()
        prev = last["hash"] if last else GENESIS
        at = _now()
        rec_sha = _sha(record)
        h = hashlib.sha256(f"{prev}|{job_id}|{audit_id}|{event}|{at!r}|{rec_sha}".encode()).hexdigest()
        conn.execute("INSERT INTO audit_chain(job_id, audit_id, event, at, record_sha256, prev_hash, hash) "
                     "VALUES (%s,%s,%s,%s,%s,%s,%s)", (job_id, audit_id, event, at, rec_sha, prev, h))

    def verify_chain(self) -> dict[str, Any]:
        """Check every link, then that each job's stored result is the one last chained for it."""
        rows = self._all("SELECT * FROM audit_chain ORDER BY seq")
        prev = GENESIS
        latest: dict[str, dict[str, Any]] = {}
        for r in rows:
            expect = hashlib.sha256(f"{prev}|{r['job_id']}|{r['audit_id']}|{r['event']}|{r['at']!r}|"
                                    f"{r['record_sha256']}".encode()).hexdigest()
            if r["prev_hash"] != prev or r["hash"] != expect:
                return {"ok": False, "entries": len(rows), "broken_at": r["seq"], "reason": "link_broken"}
            if r["event"] in ("extraction.completed", "extraction.corrected"):
                latest[r["job_id"]] = r
            prev = r["hash"]
        for job_id, r in latest.items():
            job = self.get(job_id)
            if job is not None and job["result"] is not None and _sha(_json(job["result"])) != r["record_sha256"]:
                return {"ok": False, "entries": len(rows), "broken_at": r["seq"], "reason": "record_changed",
                        "job_id": job_id}
        return {"ok": True, "entries": len(rows), "head": prev}

    # -------------------------------------------------------------- metrics

    def metrics(self, client: str | None = None) -> dict[str, Any]:
        rows = self._all("SELECT * FROM jobs" + (" WHERE client=%s" if client else ""), (client,) if client else ())
        deliveries = self._all("SELECT status, COUNT(*) AS n FROM deliveries GROUP BY status")
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for r in rows:
            groups.setdefault((r["client"] or "", r["usecase"] or ""), []).append(r)
        out = []
        for (c, u), rs in sorted(groups.items()):
            done = [r for r in rs if r["status"] == "done"]
            lat = sorted((r["finished_at"] - r["created_at"]) * 1000 for r in done if r["finished_at"])
            flags: dict[str, int] = {}
            for r in done:
                for f in _json(r["flags"]) or []:
                    key = f.split(":", 1)[0]
                    flags[key] = flags.get(key, 0) + 1
            costs = [r["cost_usd"] for r in done if r["cost_usd"] is not None]
            flagged = sum(1 for r in done if r["flagged"] or r["human_corrected"])
            out.append({"client": c, "usecase": u, "jobs": len(rs), "extracted": len(done),
                        "inprogress": len(rs) - len(done), "flagged": flagged,
                        "flag_rate": round(flagged / len(done), 4) if done else 0.0,
                        "corrected": sum(1 for r in done if r["human_corrected"]),
                        "dead_lettered": sum(1 for r in done if r["dead"]),
                        "latency_ms": {"p50": _pct(lat, 0.5), "p95": _pct(lat, 0.95)},
                        "cost_usd": {"total": round(sum(costs), 6) if costs else None,
                                     "per_extraction": round(statistics.mean(costs), 6) if costs else None},
                        "flags": dict(sorted(flags.items(), key=lambda kv: -kv[1]))})
        return {"groups": out, "deliveries": {r["status"]: r["n"] for r in deliveries}}

    # -------------------------------------------------------------- retention

    def sweep(self, retention_days: float, checkpointer: Any = None) -> dict[str, int]:
        """Delete spools and checkpoints of finished jobs; purge everything past retention (RT-35, RT-51)."""
        removed = {"spools": 0, "checkpoints": 0, "jobs": 0}
        with self.pool.connection() as conn:
            spooled = conn.execute("SELECT DISTINCT s.run_id FROM spool s JOIN jobs j ON j.id=s.run_id "
                                   "WHERE j.status='done'").fetchall()
            for r in spooled:
                conn.execute("DELETE FROM spool WHERE run_id=%s", (r["run_id"],))
            removed["spools"] = len(spooled)
            done = [r["id"] for r in conn.execute("SELECT id FROM jobs WHERE status='done'").fetchall()]
        if checkpointer is not None:
            for job_id in done:
                try:
                    if checkpointer.get_tuple({"configurable": {"thread_id": job_id}}) is not None:
                        checkpointer.delete_thread(job_id)
                        removed["checkpoints"] += 1
                except Exception:                              # noqa: BLE001 - retention is best-effort
                    pass
        cutoff = _now() - retention_days * 86400
        with self.pool.connection() as conn, conn.transaction():
            removed["jobs"] = conn.execute(
                "DELETE FROM jobs j WHERE j.status='done' AND j.finished_at<%s AND NOT EXISTS "
                "(SELECT 1 FROM review r WHERE r.job_id=j.id AND r.status='open')", (cutoff,)).rowcount
        return removed


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    idx = min(len(values) - 1, max(0, int(round(q * (len(values) - 1)))))
    return round(values[idx], 1)


# ------------------------------------------------------------------ running jobs


class Runner:
    """Runs jobs through the graph and delivers results; also the worker loops."""

    def __init__(self, store: JobStore, graph: Any, settings: pipeline.Settings, checkpointer: Any = None,
                 http: Any = None) -> None:
        self.store, self.graph, self.settings, self.checkpointer = store, graph, settings, checkpointer
        self.http = http
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.worker_id = f"{socket.gethostname()}:{uuid.uuid4().hex[:6]}"

    # -------------------------------------------------------------- one job

    def execute(self, job: dict[str, Any]) -> None:
        req = _json(job["request"])
        try:
            outcome = pipeline.run(self.graph, self.settings, file_location=req["file_location"],
                                   client=req.get("client"), usecase=req.get("usecase"), version=req.get("version"),
                                   thread_id=job["id"], spool_run=job["id"])
        except Exception as exc:                               # noqa: BLE001 - an engine bug: retry, then dead-letter
            log.exception("job %s failed", job["id"])
            if job["attempts"] >= self.settings.job_max_attempts:
                outcome = pipeline._failed(self.settings, req, "internal_error", type(exc).__name__,
                                           {"attempts": job["attempts"]}, time.time())
                outcome.flags.append("dead_lettered")
                outcome.extended["flags"].append("dead_lettered")
                self.store.finish(job["id"], outcome, dead=True)
            else:
                self.store.retry_later(job["id"], f"{type(exc).__name__}: {exc}",
                                       delay_s=min(self.settings.job_retry_backoff_s * 2 ** (job["attempts"] - 1), 60))
            return
        self.store.finish(job["id"], outcome)

    # -------------------------------------------------------------- delivery

    def deliver_due(self) -> int:
        import httpx
        sent = 0
        for d in self.store.due_deliveries():
            body = d["payload"].encode()
            ts = str(int(time.time()))
            headers = {"content-type": "application/json", "X-Extraction-Status": "extracted",
                       "X-Job-Id": d["job_id"], "X-Event": d["event"], "X-Timestamp": ts,
                       "User-Agent": f"dataextractor-runtime/{pipeline.ENGINE_VERSION}"}
            if self.settings.webhook_secret:
                headers["X-Signature"] = sign(self.settings.webhook_secret, ts, body)
            host = urlparse(d["url"]).hostname or ""
            if _resolves_link_local(host):
                self.store.mark_delivery(d["id"], False, None, "link-local address refused", 1, 0)
                continue
            try:
                client = self.http or httpx.Client(timeout=self.settings.webhook_timeout_s, follow_redirects=False)
                resp = client.post(d["url"], content=body, headers=headers)
                ok = 200 <= resp.status_code < 300
                self.store.mark_delivery(d["id"], ok, resp.status_code, None if ok else resp.text[:300],
                                         self.settings.webhook_max_attempts, self.settings.webhook_backoff_s)
            except Exception as exc:                           # noqa: BLE001 - recorded, retried
                self.store.mark_delivery(d["id"], False, None, f"{type(exc).__name__}: {exc}",
                                         self.settings.webhook_max_attempts, self.settings.webhook_backoff_s)
            sent += 1
        return sent

    # -------------------------------------------------------------- loops

    def work_once(self) -> bool:
        job = self.store.claim(self.worker_id, self.settings.job_lease_s)
        if job is None:
            return False
        self.execute(job)
        return True

    def drain(self, timeout_s: float = 60) -> None:
        """Run queued jobs and due deliveries until nothing is left (tests, the CLI)."""
        end = time.time() + timeout_s
        while time.time() < end:
            busy = self.work_once()
            busy = self.deliver_due() > 0 or busy
            if not busy:
                return

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if not self.work_once():
                    self._stop.wait(self.settings.poll_interval_s)
            except Exception:                                  # noqa: BLE001 - the loop must survive
                log.exception("worker loop")
                self._stop.wait(1)

    def _delivery_loop(self) -> None:
        last_sweep = 0.0
        while not self._stop.is_set():
            try:
                self.deliver_due()
                if time.time() - last_sweep > 600:
                    self.store.sweep(self.settings.retention_days, self.checkpointer)
                    last_sweep = time.time()
            except Exception:                                  # noqa: BLE001
                log.exception("delivery loop")
            self._stop.wait(self.settings.poll_interval_s)

    def start(self, workers: int | None = None) -> None:
        n = self.settings.workers if workers is None else workers
        for i in range(n):
            t = threading.Thread(target=self._worker_loop, name=f"extract-worker-{i}", daemon=True)
            t.start()
            self._threads.append(t)
        t = threading.Thread(target=self._delivery_loop, name="extract-delivery", daemon=True)
        t.start()
        self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=10)
        self._threads.clear()
        self._stop = threading.Event()


# ------------------------------------------------------------------ checkpoints

#: The only types a checkpoint may be deserialized into: the run state's own.
CHECKPOINT_TYPES = [("extractor_service.config_store", "ExtractionConfig"), ("extractor_service.config_store", "Skill"),
                    ("extractor_service.schema", "DataDictionary"), ("extractor_service.schema", "Field"),
                    ("extractor_service.intake", "Submission"), ("extractor_service.intake", "Item"),
                    ("extractor_service.evidence", "Doc"), ("extractor_service.evidence", "Block"),
                    ("extractor_service.evidence", "Table"), ("pathlib", "PosixPath"), ("pathlib", "WindowsPath"),
                    ("pathlib", "Path"), ("decimal", "Decimal"), ("langgraph.types", "Send")]


def checkpointer(pool: Any):
    """A Postgres LangGraph checkpointer on the runtime's pool (tables in the runtime's schema)."""
    from langgraph.checkpoint.postgres import PostgresSaver
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    saver = PostgresSaver(pool, serde=JsonPlusSerializer(allowed_msgpack_modules=CHECKPOINT_TYPES))
    with pool.connection() as conn:              # one migrator at a time across processes
        conn.execute("SELECT pg_advisory_lock(%s)", (_CHAIN_LOCK + 2,))
        try:
            saver.setup()
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (_CHAIN_LOCK + 2,))
    return saver

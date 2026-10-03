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

* **Durable queue (RT-49).** Jobs live in SQLite under STATE_DIR before any
  work starts. Workers claim a job with a lease; a job whose worker vanished is
  claimed again when the lease runs out, and resumes from its last LangGraph
  checkpoint rather than starting over (RT-40).
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
  is detectable (``GET /audit/verify``).
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import ipaddress
import json
import logging
import shutil
import socket
import sqlite3
import statistics
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import pipeline
from .errors import ServiceError

log = logging.getLogger("extractor_service.jobs")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, idem_key TEXT NOT NULL, request TEXT NOT NULL,
  status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, available_at REAL NOT NULL,
  lease_until REAL, worker TEXT, created_at REAL NOT NULL, started_at REAL, finished_at REAL,
  client TEXT, usecase TEXT, config_version TEXT, audit_id TEXT, flagged INTEGER, flags TEXT,
  result TEXT, original_result TEXT, callback_url TEXT, dead INTEGER NOT NULL DEFAULT 0,
  human_corrected INTEGER NOT NULL DEFAULT 0, cost_usd REAL, last_error TEXT, extended INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_jobs_queue ON jobs(status, available_at);
CREATE INDEX IF NOT EXISTS ix_jobs_idem ON jobs(idem_key, created_at);
CREATE TABLE IF NOT EXISTS deliveries (
  id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, event TEXT NOT NULL, url TEXT NOT NULL,
  payload TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL,
  last_status INTEGER, last_error TEXT, created_at REAL NOT NULL, delivered_at REAL);
CREATE INDEX IF NOT EXISTS ix_deliveries_due ON deliveries(status, next_at);
CREATE TABLE IF NOT EXISTS review (
  job_id TEXT PRIMARY KEY, audit_id TEXT, client TEXT, usecase TEXT, flags TEXT, status TEXT NOT NULL,
  opened_at REAL NOT NULL, resolved_at REAL, reviewer TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS corrections (
  id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, audit_id TEXT, record INTEGER, field TEXT,
  original TEXT, corrected TEXT, reviewer TEXT NOT NULL, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS audit_chain (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, audit_id TEXT, event TEXT, at REAL NOT NULL,
  record_sha256 TEXT NOT NULL, prev_hash TEXT NOT NULL, hash TEXT NOT NULL);
"""
GENESIS = "0" * 64
PUBLIC = {"queued": "inprogress", "running": "inprogress", "done": "extracted"}


def _now() -> float:
    return time.time()


def _sha(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


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
    """SQLite-backed jobs, deliveries, review queue, corrections and audit chain."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.state_dir / "jobs.db", check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA busy_timeout=5000")
            self.db.executescript(SCHEMA)

    def spool_dir(self, job_id: str) -> Path:
        return self.state_dir / "spool" / job_id

    # -------------------------------------------------------------- jobs

    def find_recent(self, idem_key: str, window_days: float) -> sqlite3.Row | None:
        with self.lock:
            return self.db.execute(
                "SELECT * FROM jobs WHERE idem_key=? AND created_at>=? AND dead=0 ORDER BY created_at DESC LIMIT 1",
                (idem_key, _now() - window_days * 86400)).fetchone()

    def create(self, idem_key: str, request: dict[str, Any], *, callback_url: str | None, extended: bool,
               running_by: str | None = None, lease_s: float = 0) -> str:
        job_id = "job_" + uuid.uuid4().hex[:20]
        now = _now()
        with self.lock:
            self.db.execute(
                "INSERT INTO jobs(id, idem_key, request, status, attempts, available_at, lease_until, worker, "
                "created_at, started_at, client, usecase, callback_url, extended) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, idem_key, json.dumps(request), "running" if running_by else "queued",
                 1 if running_by else 0, now, now + lease_s if running_by else None, running_by, now,
                 now if running_by else None, request.get("client"), request.get("usecase"),
                 callback_url, int(extended)))
        return job_id

    def get(self, job_id: str) -> sqlite3.Row | None:
        with self.lock:
            return self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()

    def claim(self, worker: str, lease_s: float) -> sqlite3.Row | None:
        now = _now()
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                row = self.db.execute(
                    "SELECT id FROM jobs WHERE (status='queued' AND available_at<=?) "
                    "OR (status='running' AND lease_until<?) ORDER BY created_at LIMIT 1", (now, now)).fetchone()
                if row is None:
                    self.db.execute("COMMIT")
                    return None
                self.db.execute(
                    "UPDATE jobs SET status='running', worker=?, lease_until=?, attempts=attempts+1, "
                    "started_at=COALESCE(started_at, ?) WHERE id=?", (worker, now + lease_s, now, row["id"]))
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
            return self.get(row["id"])

    def finish(self, job_id: str, outcome: pipeline.Outcome, *, dead: bool = False) -> None:
        ext = outcome.extended
        cfg = (ext.get("metadata") or {}).get("config") or {}
        cost = ((ext.get("metadata") or {}).get("cost") or {}).get("model_usd")
        with self.lock:
            self.db.execute(
                "UPDATE jobs SET status='done', finished_at=?, lease_until=NULL, audit_id=?, flagged=?, flags=?, "
                "result=?, dead=?, cost_usd=?, client=COALESCE(?, client), usecase=COALESCE(?, usecase), "
                "config_version=? WHERE id=?",
                (_now(), outcome.audit_id, int(outcome.flagged), json.dumps(outcome.flags),
                 json.dumps(ext, default=str), int(dead), cost, cfg.get("client"), cfg.get("usecase"),
                 cfg.get("version"), job_id))
            job = self.get(job_id)
            self.chain(job_id, outcome.audit_id, "extraction.completed", ext)
            if outcome.flagged:
                self.db.execute(
                    "INSERT OR REPLACE INTO review(job_id, audit_id, client, usecase, flags, status, opened_at) "
                    "VALUES (?,?,?,?,?,?,?)", (job_id, outcome.audit_id, job["client"], job["usecase"],
                                               json.dumps(outcome.flags), "open", _now()))
            if job["callback_url"]:
                self.enqueue_delivery(job_id, "extraction.completed")

    def retry_later(self, job_id: str, error: str, delay_s: float) -> None:
        with self.lock:
            self.db.execute("UPDATE jobs SET status='queued', available_at=?, lease_until=NULL, last_error=? "
                            "WHERE id=?", (_now() + delay_s, error[:1000], job_id))

    def list(self, *, status: str | None = None, client: str | None = None, flagged: bool | None = None,
             limit: int = 50) -> list[sqlite3.Row]:
        q, args = "SELECT * FROM jobs WHERE 1=1", []
        if status:
            internal = {"inprogress": ("queued", "running"), "extracted": ("done",)}.get(status, (status,))
            q += f" AND status IN ({','.join('?' * len(internal))})"
            args += list(internal)
        if client:
            q += " AND client=?"
            args.append(client)
        if flagged is not None:
            q += " AND flagged=?"
            args.append(int(flagged))
        q += " ORDER BY created_at DESC LIMIT ?"
        args.append(max(1, min(limit, 500)))
        with self.lock:
            return self.db.execute(q, args).fetchall()

    # -------------------------------------------------------------- delivery

    def payload(self, job: sqlite3.Row, event: str) -> dict[str, Any]:
        ext = json.loads(job["result"])
        req = json.loads(job["request"])
        return {"event": event, "job_id": job["id"], "status": "extracted", "flagged": bool(job["flagged"]),
                "human_corrected": bool(job["human_corrected"]), "audit_id": job["audit_id"],
                "request": {k: req.get(k) for k in ("file_location", "client", "usecase", "version",
                                                    "idempotency_key") if req.get(k) is not None},
                "result": pipeline.deliverable(ext, bool(job["extended"])),
                **({"rejected": True} if event == "extraction.rejected" else {})}

    def enqueue_delivery(self, job_id: str, event: str) -> None:
        with self.lock:
            job = self.get(job_id)
            if not job or not job["callback_url"] or job["status"] != "done":
                return
            self.db.execute("INSERT INTO deliveries(job_id, event, url, payload, status, next_at, created_at) "
                            "VALUES (?,?,?,?,?,?,?)", (job_id, event, job["callback_url"],
                                                        json.dumps(self.payload(job, event), default=str),
                                                        "pending", _now(), _now()))

    def due_deliveries(self, limit: int = 20) -> list[sqlite3.Row]:
        with self.lock:
            return self.db.execute("SELECT * FROM deliveries WHERE status='pending' AND next_at<=? "
                                   "ORDER BY next_at LIMIT ?", (_now(), limit)).fetchall()

    def mark_delivery(self, delivery_id: int, ok: bool, status: int | None, error: str | None,
                      max_attempts: int, backoff_s: float) -> None:
        with self.lock:
            d = self.db.execute("SELECT * FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
            attempts = d["attempts"] + 1
            if ok:
                self.db.execute("UPDATE deliveries SET status='delivered', attempts=?, last_status=?, "
                                "delivered_at=?, last_error=NULL WHERE id=?", (attempts, status, _now(), delivery_id))
            else:
                final = attempts >= max_attempts
                self.db.execute("UPDATE deliveries SET status=?, attempts=?, last_status=?, last_error=?, "
                                "next_at=? WHERE id=?",
                                ("failed" if final else "pending", attempts, status, (error or "")[:500],
                                 _now() + min(backoff_s * 2 ** (attempts - 1), 3600), delivery_id))

    def deliveries(self, job_id: str) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.db.execute("SELECT id, event, status, attempts, last_status, last_error, created_at, "
                                   "delivered_at FROM deliveries WHERE job_id=? ORDER BY id", (job_id,)).fetchall()
        return [dict(r) for r in rows]

    # -------------------------------------------------------------- review

    def review_list(self, *, status: str | None = "open", client: str | None = None,
                    limit: int = 50) -> list[sqlite3.Row]:
        q, args = "SELECT * FROM review WHERE 1=1", []
        if status:
            q += " AND status=?"
            args.append(status)
        if client:
            q += " AND client=?"
            args.append(client)
        q += " ORDER BY opened_at LIMIT ?"
        args.append(max(1, min(limit, 500)))
        with self.lock:
            return self.db.execute(q, args).fetchall()

    def review_get(self, job_id: str) -> sqlite3.Row | None:
        with self.lock:
            return self.db.execute("SELECT * FROM review WHERE job_id=?", (job_id,)).fetchone()

    def resolve(self, job_id: str, *, reviewer: str, action: str, corrections: list[dict[str, Any]],
                records: list[dict[str, Any]] | None, note: str | None) -> dict[str, Any]:
        with self.lock:
            entry = self.review_get(job_id)
            job = self.get(job_id)
            if entry is None or job is None:
                raise ServiceError(404, "review_not_found", f"no review entry for {job_id}")
            if entry["status"] != "open":
                raise ServiceError(409, "review_closed", f"review for {job_id} is already {entry['status']}")
            ext = json.loads(job["result"])
            if action == "reject":
                self.db.execute("UPDATE review SET status='rejected', resolved_at=?, reviewer=?, note=? "
                                "WHERE job_id=?", (_now(), reviewer, note, job_id))
                self.chain(job_id, job["audit_id"], "extraction.rejected", {"reviewer": reviewer, "note": note})
                if job["callback_url"]:
                    self.enqueue_delivery(job_id, "extraction.rejected")
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
                self.db.execute("INSERT INTO corrections(job_id, audit_id, record, field, original, corrected, "
                                "reviewer, at) VALUES (?,?,?,?,?,?,?,?)",
                                (job_id, job["audit_id"], i, f, json.dumps(old, default=str),
                                 json.dumps(new, default=str), reviewer, now))
            corrected.update({"flagged": False, "flags": [], "human_corrected": True,
                              "corrected_by": reviewer, "original_flags": ext.get("flags", [])})
            for i, rec in enumerate(corrected.get("records") or []):
                if i < len(data):
                    rec["data"], rec["flagged"], rec["flags"] = data[i], False, []
            self.db.execute("UPDATE jobs SET result=?, original_result=COALESCE(original_result, result), "
                            "flagged=0, flags='[]', human_corrected=1 WHERE id=?",
                            (json.dumps(corrected, default=str), job_id))
            self.db.execute("UPDATE review SET status='corrected', resolved_at=?, reviewer=?, note=? WHERE job_id=?",
                            (now, reviewer, note, job_id))
            self.chain(job_id, job["audit_id"], "extraction.corrected", corrected)
            if job["callback_url"]:
                self.enqueue_delivery(job_id, "extraction.corrected")
            return {"job_id": job_id, "status": "corrected", "changes": len(log_rows)}

    def corrections_export(self, client: str | None = None, usecase: str | None = None) -> list[dict[str, Any]]:
        q = ("SELECT j.* FROM jobs j JOIN review r ON r.job_id=j.id WHERE r.status='corrected'"
             + (" AND j.client=?" if client else "") + (" AND j.usecase=?" if usecase else "")
             + " ORDER BY r.resolved_at")
        args = [a for a in (client, usecase) if a]
        with self.lock:
            jobs = self.db.execute(q, args).fetchall()
            out = []
            for j in jobs:
                ext, req = json.loads(j["result"]), json.loads(j["request"])
                inp = ((json.loads(j["original_result"] or j["result"]).get("metadata") or {}).get("input") or {})
                rows = self.db.execute("SELECT record, field, reviewer, at FROM corrections WHERE job_id=? "
                                       "ORDER BY id", (j["id"],)).fetchall()
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

    def chain(self, job_id: str, audit_id: str | None, event: str, record: Any) -> None:
        with self.lock:
            last = self.db.execute("SELECT hash FROM audit_chain ORDER BY seq DESC LIMIT 1").fetchone()
            prev = last["hash"] if last else GENESIS
            at = _now()
            rec_sha = _sha(record)
            h = hashlib.sha256(f"{prev}|{job_id}|{audit_id}|{event}|{at!r}|{rec_sha}".encode()).hexdigest()
            self.db.execute("INSERT INTO audit_chain(job_id, audit_id, event, at, record_sha256, prev_hash, hash) "
                            "VALUES (?,?,?,?,?,?,?)", (job_id, audit_id, event, at, rec_sha, prev, h))

    def verify_chain(self) -> dict[str, Any]:
        """Check every link, then that each job's stored result is the one last chained for it."""
        with self.lock:
            rows = self.db.execute("SELECT * FROM audit_chain ORDER BY seq").fetchall()
            prev = GENESIS
            latest: dict[str, sqlite3.Row] = {}
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
                if job is not None and job["result"] is not None and _sha(json.loads(job["result"])) != r["record_sha256"]:
                    return {"ok": False, "entries": len(rows), "broken_at": r["seq"], "reason": "record_changed",
                            "job_id": job_id}
            return {"ok": True, "entries": len(rows), "head": prev}

    # -------------------------------------------------------------- metrics

    def metrics(self, client: str | None = None) -> dict[str, Any]:
        q = "SELECT * FROM jobs" + (" WHERE client=?" if client else "")
        with self.lock:
            rows = self.db.execute(q, (client,) if client else ()).fetchall()
            deliveries = self.db.execute("SELECT status, COUNT(*) n FROM deliveries GROUP BY status").fetchall()
        groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
        for r in rows:
            groups.setdefault((r["client"] or "", r["usecase"] or ""), []).append(r)
        out = []
        for (c, u), rs in sorted(groups.items()):
            done = [r for r in rs if r["status"] == "done"]
            lat = sorted((r["finished_at"] - r["created_at"]) * 1000 for r in done if r["finished_at"])
            flags: dict[str, int] = {}
            for r in done:
                for f in json.loads(r["flags"] or "[]"):
                    key = f.split(":", 1)[0]
                    flags[key] = flags.get(key, 0) + 1
            costs = [r["cost_usd"] for r in done if r["cost_usd"] is not None]
            out.append({"client": c, "usecase": u, "jobs": len(rs), "extracted": len(done),
                        "inprogress": len(rs) - len(done),
                        "flagged": sum(1 for r in done if r["flagged"] or r["human_corrected"]),
                        "flag_rate": round(sum(1 for r in done if r["flagged"] or r["human_corrected"])
                                           / len(done), 4) if done else 0.0,
                        "corrected": sum(r["human_corrected"] for r in done),
                        "dead_lettered": sum(r["dead"] for r in done),
                        "latency_ms": {"p50": _pct(lat, 0.5), "p95": _pct(lat, 0.95)},
                        "cost_usd": {"total": round(sum(costs), 6) if costs else None,
                                     "per_extraction": round(statistics.mean(costs), 6) if costs else None},
                        "flags": dict(sorted(flags.items(), key=lambda kv: -kv[1]))})
        return {"groups": out, "deliveries": {r["status"]: r["n"] for r in deliveries}}

    # -------------------------------------------------------------- retention

    def sweep(self, retention_days: float, checkpointer: Any = None) -> dict[str, int]:
        """Delete spools and checkpoints of finished jobs; purge everything past retention (RT-35, RT-51)."""
        removed = {"spools": 0, "checkpoints": 0, "jobs": 0}
        with self.lock:
            done = [r["id"] for r in self.db.execute("SELECT id FROM jobs WHERE status='done'").fetchall()]
        for job_id in done:
            spool = self.spool_dir(job_id)
            if spool.exists():
                shutil.rmtree(spool, ignore_errors=True)
                removed["spools"] += 1
            if checkpointer is not None:
                try:
                    if checkpointer.get_tuple({"configurable": {"thread_id": job_id}}) is not None:
                        checkpointer.delete_thread(job_id)
                        removed["checkpoints"] += 1
                except Exception:                              # noqa: BLE001 - retention is best-effort
                    pass
        cutoff = _now() - retention_days * 86400
        with self.lock:
            old = [r["id"] for r in self.db.execute(
                "SELECT j.id FROM jobs j LEFT JOIN review r ON r.job_id=j.id WHERE j.status='done' AND "
                "j.finished_at<? AND (r.status IS NULL OR r.status!='open')", (cutoff,)).fetchall()]
            for job_id in old:
                for table in ("deliveries", "review", "corrections"):
                    self.db.execute(f"DELETE FROM {table} WHERE job_id=?", (job_id,))
                self.db.execute("DELETE FROM jobs WHERE id=?", (job_id,))
            removed["jobs"] = len(old)
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

    def execute(self, job: sqlite3.Row) -> None:
        req = json.loads(job["request"])
        try:
            outcome = pipeline.run(self.graph, self.settings, file_location=req["file_location"],
                                   client=req.get("client"), usecase=req.get("usecase"), version=req.get("version"),
                                   thread_id=job["id"], spool_dir=str(self.store.spool_dir(job["id"])))
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


def checkpointer(state_dir: Path):
    """A SQLite LangGraph checkpointer whose deserializer only accepts the run state's own types."""
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    from langgraph.checkpoint.sqlite import SqliteSaver
    allowed = [("extractor_service.config_store", "ExtractionConfig"), ("extractor_service.config_store", "Skill"),
               ("extractor_service.schema", "DataDictionary"), ("extractor_service.schema", "Field"),
               ("extractor_service.intake", "Submission"), ("extractor_service.intake", "Item"),
               ("extractor_service.evidence", "Doc"), ("extractor_service.evidence", "Block"),
               ("extractor_service.evidence", "Table"), ("pathlib", "PosixPath"), ("pathlib", "WindowsPath"),
               ("pathlib", "Path"), ("decimal", "Decimal"), ("langgraph.types", "Send"),
               ("extractor_service.ingest_filter", "IngestionFilter")]
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(Path(state_dir) / "checkpoints.db", check_same_thread=False)
    return SqliteSaver(conn, serde=JsonPlusSerializer(allowed_msgpack_modules=allowed))

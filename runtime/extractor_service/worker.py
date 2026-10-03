"""Queue workers without the HTTP API, for scaling out (RT-61).

    python -m extractor_service.worker              # WORKERS threads + webhook delivery + retention
    python -m extractor_service.worker --drain      # run what is queued, deliver, then exit

Every worker process points at the same Postgres (DATABASE_URL, DB_SCHEMA): jobs
are claimed with FOR UPDATE SKIP LOCKED and a lease, so any number of processes
on any number of hosts share the queue, and a job whose worker died is picked
up again and resumes from its checkpoint. Workers need no shared disk.
"""
from __future__ import annotations

import argparse
import logging
import signal
import threading

from . import pipeline
from .config_store import ConfigStore
from .graph import build_graph
from .db import open_pool
from .jobs import JobStore, Runner, checkpointer


def build_runner(settings: pipeline.Settings | None = None) -> Runner:
    settings = settings or pipeline.Settings.from_env()
    if not settings.database_url:
        raise SystemExit("DATABASE_URL must be set for workers")
    pool = open_pool(settings.database_url, settings.db_schema, max_size=max(4, settings.workers + 4))
    saver = checkpointer(pool)
    graph = build_graph(ConfigStore(settings.config_root), settings, checkpointer=saver, pool=pool)
    return Runner(JobStore(pool), graph, settings, checkpointer=saver)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--drain", action="store_true", help="process the queue and due deliveries, then exit")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    runner = build_runner()
    if args.drain:
        runner.drain(timeout_s=3600)
        return 0
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    runner.start()
    stop.wait()
    runner.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

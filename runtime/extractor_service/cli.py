"""Run extractions from the command line, one process per batch.

Design-time uses this to run the runtime in isolation: it points CONFIG_ROOT
at a scratch copy of the configs (with a candidate version in it), runs a
batch, and reads the answers back. Nothing is served and nothing outside the
scratch folders is written.

    echo '{"runs": [{"file_location": "inv.pdf", "client": "acme"}],
           "include_evidence": true}' | python -m extractor_service.cli

Reads one JSON object on stdin:
    runs               [{"file_location", "client"?, "usecase"?, "version"?}, ...]
    include_evidence   also return each run's evidence blocks (default false)

Writes one JSON object on stdout:
    {"engine_version": "...",
     "results": [{"ok": true, "output": <extended output>, "config": {...},
                  "dictionary": {...}, "skills": [...], "evidence": [...]?}
                 | {"ok": false, "error": {"error", "message", "detail", "status"}}]}

Settings come from the environment, as for the service (CONFIG_ROOT,
INPUT_ROOT, MODEL_PROVIDER, ...). The exit code is 0 whenever the batch was
read, even if single runs failed; 2 means the request itself was unusable.
"""
from __future__ import annotations

import json
import sys
from typing import Any

from . import pipeline
from .config_store import ConfigStore
from .errors import ModelError, ServiceError
from .graph import build_graph

MAX_BLOCKS_PER_DOC = 5000


def evidence_of(docs: list[Any]) -> list[dict[str, Any]]:
    out = []
    for d in docs:
        blocks = d.blocks[:MAX_BLOCKS_PER_DOC]
        out.append({
            "doc_id": d.doc_id, "source": d.source, "name": d.name, "kind": d.kind, "status": d.status,
            "truncated": len(d.blocks) > MAX_BLOCKS_PER_DOC,
            "blocks": [{"locator": b.locator, "text": b.text, "vtype": b.vtype, "group": b.group,
                        "row": b.row, "col": b.col} for b in blocks],
            "tables": [{"group": t.group, "header": [b.locator for b in t.header],
                        "rows": [[b.locator for b in row] for row in t.rows]} for t in d.tables],
        })
    return out


def run_batch(request: dict[str, Any], settings: pipeline.Settings | None = None,
              model_gateway: Any = None) -> dict[str, Any]:
    settings = settings or pipeline.Settings.from_env()
    graph = build_graph(ConfigStore(settings.config_root), settings, model_gateway)
    results = []
    for r in request.get("runs") or []:
        try:
            out = pipeline.run(graph, settings, file_location=r["file_location"], client=r.get("client"),
                               usecase=r.get("usecase"), version=r.get("version"))
        except ServiceError as exc:
            results.append({"ok": False, "error": {**exc.to_dict(), "status": exc.status}})
            continue
        except ModelError as exc:
            results.append({"ok": False, "error": {"error": exc.code, "message": str(exc),
                                                   "detail": {"transient": exc.transient}, "status": 502}})
            continue
        res = {"ok": True, "output": out.extended, "config": out.cfg.ref(),
               "dictionary": out.cfg.dictionary.describe(), "skills": [s.name for s in out.cfg.skills]}
        if request.get("include_evidence"):
            res["evidence"] = evidence_of(out.docs)
        results.append(res)
    return {"engine_version": pipeline.ENGINE_VERSION, "results": results}


def main() -> int:
    try:
        request = json.loads(sys.stdin.read() or "{}")
        if not isinstance(request, dict) or not isinstance(request.get("runs"), list) or \
                not all(isinstance(r, dict) and isinstance(r.get("file_location"), str) for r in request["runs"]):
            raise ValueError("expected {\"runs\": [{\"file_location\": ...}, ...]}")
    except ValueError as exc:
        json.dump({"error": "bad_request", "message": str(exc)}, sys.stdout)
        return 2
    json.dump(run_batch(request), sys.stdout, default=str)
    return 0


if __name__ == "__main__":
    sys.exit(main())

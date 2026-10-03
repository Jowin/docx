"""The isolated runtime: the real extraction service, run in a child process.

Every learning attempt runs the runtime exactly as it ships (its
``extractor_service.cli`` entry point) against a scratch copy of the config
folders, so a candidate skill is tested by the engine that will serve it and
nothing the attempt writes can reach the live configs or this process.

The child gets a minimal environment: the scratch CONFIG_ROOT, the learning
INPUT_ROOT, an optional MODEL_PROVIDER override, and the model gateway
variables when the configs call the gateway. No AUDIT_DIR, so isolated runs
leave no audit records.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from ..agents.base import AgentError

_PASS_THROUGH = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "HOME", "LANG", "LC_ALL",
                 "USERPROFILE", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY")
_GATEWAY_PREFIX = "MODEL_GATEWAY_"


class RuntimeFailed(AgentError):
    code = "runtime_failed"
    status = 502


class IsolatedRuntime:
    def __init__(self, runtime_dir: Path, *, python: str, input_root: Path,
                 timeout_s: float = 300, model_provider: str | None = None) -> None:
        self.runtime_dir = Path(runtime_dir)
        self.python = python
        self.input_root = Path(input_root)
        self.timeout_s = timeout_s
        self.model_provider = model_provider
        if not (self.runtime_dir / "extractor_service" / "cli.py").is_file():
            raise RuntimeFailed(f"no runtime at {self.runtime_dir} (set RUNTIME_DIR)",
                                code="runtime_not_found", status=500)

    def _env(self, config_root: Path) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k in _PASS_THROUGH or k.startswith(_GATEWAY_PREFIX)}
        env.update({"CONFIG_ROOT": str(config_root), "INPUT_ROOT": str(self.input_root),
                    "PYTHONPATH": str(self.runtime_dir), "PYTHONDONTWRITEBYTECODE": "1"})
        if self.model_provider:
            env["MODEL_PROVIDER"] = self.model_provider
        return env

    def run(self, config_root: Path, runs: list[dict[str, Any]], *,
            include_evidence: bool = False) -> list[dict[str, Any]]:
        """Run a batch; one result per run, in order (see extractor_service.cli)."""
        if not runs:
            return []
        request = json.dumps({"runs": runs, "include_evidence": include_evidence})
        try:
            proc = subprocess.run([self.python, "-m", "extractor_service.cli"], input=request,
                                  capture_output=True, text=True, encoding="utf-8",
                                  cwd=str(self.runtime_dir), env=self._env(config_root),
                                  timeout=self.timeout_s)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeFailed(f"isolated runtime timed out after {self.timeout_s}s",
                                code="runtime_timeout", status=504) from exc
        except OSError as exc:
            raise RuntimeFailed(f"cannot start the isolated runtime: {exc}") from exc
        if proc.returncode != 0:
            raise RuntimeFailed(f"isolated runtime exited with {proc.returncode}",
                                detail={"stderr": proc.stderr[-2000:], "stdout": proc.stdout[-500:]})
        try:
            results = json.loads(proc.stdout)["results"]
        except (ValueError, KeyError) as exc:
            raise RuntimeFailed("isolated runtime returned no results",
                                detail={"stderr": proc.stderr[-2000:]}) from exc
        if len(results) != len(runs):
            raise RuntimeFailed(f"asked for {len(runs)} runs, got {len(results)}")
        return results

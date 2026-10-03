"""Runtime configuration, read from the environment."""

from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DATABASE_URL = "postgresql+psycopg2://designtime:designtime@localhost:5432/designtime"

#: The runtime service's source, next to this one in the repository.
_REPO_RUNTIME = Path(__file__).resolve().parents[3] / "runtime"


def _runtime_dir() -> Path:
    return Path(os.getenv("RUNTIME_DIR") or _REPO_RUNTIME)


def _path_env(name: str, default: Path) -> Path:
    value = os.getenv(name)
    return Path(value) if value else default


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default_factory=lambda: os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL))
    engine_version: str = field(default_factory=lambda: os.getenv("ENGINE_VERSION", "2.1.3"))
    engine_range: str = field(default_factory=lambda: os.getenv("ENGINE_RANGE", ">=2.1.0 <3.0.0"))
    # DT-08: a field must appear in at least this share of a type's samples to be proposed.
    field_support_min: float = field(default_factory=lambda: float(os.getenv("FIELD_SUPPORT_MIN", "0.6")))
    # DT-10: a detection rule must match at least this many distinct samples.
    rule_support_min: int = field(default_factory=lambda: int(os.getenv("RULE_SUPPORT_MIN", "3")))
    # DT-18: minimum labelled samples per email type before a run may start.
    corpus_min_per_type: int = field(default_factory=lambda: int(os.getenv("CORPUS_MIN_PER_TYPE", "25")))
    corpus_min_heldout: int = field(default_factory=lambda: int(os.getenv("CORPUS_MIN_HELDOUT", "5")))
    # DT-22: held-out fraction, selected deterministically from the corpus id.
    heldout_fraction: float = field(default_factory=lambda: float(os.getenv("HELDOUT_FRACTION", "0.2")))

    # -- pattern learning: the isolated runtime and the configs it learns into --
    #: Runtime source; its ``extractor_service.cli`` runs each isolated extraction.
    runtime_dir: Path = field(default_factory=_runtime_dir)
    #: Interpreter with the runtime's requirements installed.
    runtime_python: str = field(default_factory=lambda: os.getenv("RUNTIME_PYTHON") or sys.executable)
    #: The config folders the runtime serves; learned versions are written here.
    runtime_config_root: Path = field(
        default_factory=lambda: _path_env("RUNTIME_CONFIG_ROOT", _runtime_dir() / "configs"))
    #: Where learning sources live (the runtime's INPUT_ROOT).
    learning_input_root: Path = field(
        default_factory=lambda: _path_env("LEARNING_INPUT_ROOT", _runtime_dir() / "data"))
    #: Force the isolated runtime onto one provider ("stub"); unset = each config's own.
    learning_model_provider: str | None = field(
        default_factory=lambda: os.getenv("LEARNING_MODEL_PROVIDER") or None)
    learning_max_iterations: int = field(
        default_factory=lambda: int(os.getenv("LEARNING_MAX_ITERATIONS", "3")))
    learning_regression_samples: int = field(
        default_factory=lambda: int(os.getenv("LEARNING_REGRESSION_SAMPLES", "20")))
    runtime_timeout_s: float = field(default_factory=lambda: float(os.getenv("RUNTIME_TIMEOUT_S", "300")))
    #: Scratch copies of the configs for in-flight learning calls; kept until a call finishes,
    #: so an interrupted call can resume. Put it on a volume in a deployment.
    learning_state_dir: Path = field(default_factory=lambda: _path_env(
        "LEARNING_STATE_DIR", Path(tempfile.gettempdir()) / "dataextractor-learning"))
    #: The runtime service, for fetching reviewer corrections (GET /review/corrections/export).
    runtime_url: str | None = field(default_factory=lambda: os.getenv("RUNTIME_URL") or None)
    #: When set, judgment tasks the gateway client knows go through the model gateway.
    model_gateway_url: str | None = field(default_factory=lambda: os.getenv("MODEL_GATEWAY_URL") or None)


def get_settings() -> Settings:
    return Settings()

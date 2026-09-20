"""Runtime configuration, read from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

DEFAULT_DATABASE_URL = "postgresql+psycopg2://designtime:designtime@localhost:5432/designtime"


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


def get_settings() -> Settings:
    return Settings()

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from extractor_service.api import create_app
from extractor_service.pipeline import Settings
from tests.samples import write_all

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def input_root(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    write_all(data)
    return data


@pytest.fixture
def config_root(tmp_path: Path) -> Path:
    dst = tmp_path / "configs"
    shutil.copytree(ROOT / "configs", dst)
    return dst


def data_of(response) -> list:
    """The records, whether the result came back plain (clean) or extended (flagged)."""
    body = response.json() if hasattr(response, "json") else response
    return body["data"] if isinstance(body, dict) else body


TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL",
                              "postgresql://designtime:designtime@127.0.0.1:5432/extractor_test")


@pytest.fixture
def db_schema():
    """A schema of its own for each test, dropped afterwards."""
    schema = "t_" + uuid.uuid4().hex[:12]
    yield schema
    from extractor_service.db import close_all, conninfo
    close_all()
    import psycopg
    with psycopg.connect(conninfo(TEST_DATABASE_URL), autocommit=True) as conn:
        conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


@pytest.fixture
def settings(config_root: Path, input_root: Path, tmp_path: Path, db_schema: str) -> Settings:
    return Settings(config_root=config_root, input_root=input_root, audit_dir=tmp_path / "audit",
                    database_url=TEST_DATABASE_URL, db_schema=db_schema, workers=0)


@pytest.fixture
def client(settings: Settings, monkeypatch) -> TestClient:
    for var in ("MODEL_GATEWAY_URL", "MODEL_GATEWAY_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    return TestClient(create_app(settings))


@pytest.fixture
def stub_client(settings: Settings) -> TestClient:
    """Every config forced onto the stub model, as MODEL_PROVIDER=stub does."""
    from dataclasses import replace
    return TestClient(create_app(replace(settings, model_provider="stub")))

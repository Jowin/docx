from __future__ import annotations

import shutil
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


@pytest.fixture
def settings(config_root: Path, input_root: Path, tmp_path: Path) -> Settings:
    return Settings(config_root=config_root, input_root=input_root, audit_dir=tmp_path / "audit")


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

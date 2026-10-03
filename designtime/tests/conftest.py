from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "samples"))

from dataextractor_designtime.contracts.corpus import Corpus  # noqa: E402
from dataextractor_designtime.registry.db import Base, get_engine, get_sessionmaker  # noqa: E402
from dataextractor_designtime.registry.repository import Registry  # noqa: E402

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+psycopg2://designtime:designtime@127.0.0.1:5432/designtime_test",
)


@pytest.fixture(scope="session")
def corpus_root(tmp_path_factory) -> Path:
    from generate_corpus import build

    root = tmp_path_factory.mktemp("corpus")
    build(root)
    return root


@pytest.fixture(scope="session")
def corpus_dict(corpus_root: Path) -> dict:
    from generate_corpus import build

    return build(corpus_root)


@pytest.fixture()
def corpus(corpus_dict: dict) -> Corpus:
    return Corpus(**corpus_dict)


@pytest.fixture(scope="session")
def db_url() -> str:
    engine = get_engine(TEST_DATABASE_URL)
    with engine.connect():
        pass
    return TEST_DATABASE_URL


@pytest.fixture()
def db_session(db_url: str):
    engine = get_engine(db_url)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    factory = get_sessionmaker(db_url)
    session = factory()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture()
def registry(db_session) -> Registry:
    return Registry(db_session)


def _drop_langgraph_tables(engine) -> None:
    """LangGraph's store and checkpointer keep their own tables; start each test without them."""
    from sqlalchemy import text

    from dataextractor_designtime.learning.memory import reset_setup_cache
    with engine.begin() as conn:
        for table in ("store", "store_vectors", "store_migrations", "vector_migrations", "checkpoints",
                      "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations"):
            conn.execute(text(f'DROP TABLE IF EXISTS "{table}" CASCADE'))
    reset_setup_cache()


@pytest.fixture()
def client(db_url: str):
    from fastapi.testclient import TestClient

    from dataextractor_designtime.main import app
    from dataextractor_designtime.registry.db import get_engine as _ge

    engine = _ge(db_url)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    _drop_langgraph_tables(engine)
    app.state.database_url = db_url
    with TestClient(app) as c:
        yield c

"""The local store bootstraps itself, and the Postgres migration matches the models.

Two failures this file guards against, both of which were real:

* ``budly sync`` against a brand-new SQLite file used to die with "no such table:
  courses", because only ``budly start`` created the schema. A CLI command that
  touches the store has to work on a store the server has never opened.
* The Budly v1 models store JSON in every dialect, but the Postgres tables were
  created by an earlier ``create_all`` with ``text[]``/``jsonb`` columns. The
  migration that reconciles them has to cover every JSON column, or a sync
  fails at the first write with a datatype mismatch.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy import JSON, text

from canvasbuddy import db
from canvasbuddy.config import Settings
from canvasbuddy.models import Base

_MIGRATION = (
    Path(__file__).resolve().parent.parent
    / "migrations"
    / "versions"
    / "c7d1f2a4b9e0_budly_v1_cross_dialect_types.py"
)
_MIGRATION_CALL = re.compile(r'_\w+_to_json\(\s*"(\w+)",\s*"(\w+)"\s*\)')


@pytest.fixture
def local_store(tmp_path, monkeypatch):
    """Point the module-level engine at a fresh SQLite file, exactly as a CLI run sees it."""
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'fresh.db').as_posix()}"
    settings = Settings(_env_file=None, database_url=database_url)
    monkeypatch.setattr(db, "get_settings", lambda: settings)
    db.get_engine.cache_clear()
    db.get_sessionmaker.cache_clear()
    db._store_ready.clear()
    yield database_url
    db.get_engine.cache_clear()
    db.get_sessionmaker.cache_clear()
    db._store_ready.clear()


async def test_session_scope_creates_the_schema(local_store) -> None:
    async with db.session_scope() as session:
        result = await session.execute(text("SELECT count(*) FROM courses"))
        assert result.scalar_one() == 0

    # A second scope must reuse the store rather than re-running create_all.
    async with db.session_scope() as session:
        await session.execute(text("SELECT count(*) FROM assignments"))
    assert local_store.startswith("sqlite+")
    assert db._store_ready == {local_store}


def test_migration_covers_every_json_column() -> None:
    converted = set(_MIGRATION_CALL.findall(_MIGRATION.read_text(encoding="utf-8")))
    expected = {
        (table.name, column.name)
        for table in Base.metadata.sorted_tables
        for column in table.columns
        if isinstance(column.type, JSON)
    }
    assert expected, "no JSON columns found; the guard itself is broken"
    assert expected - converted == set(), (
        "Budly v1 stores these columns as JSON but the Postgres migration does not "
        "convert them: sync will fail with a datatype mismatch"
    )

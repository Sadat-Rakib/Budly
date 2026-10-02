"""Budly v1: cross-dialect column types (jsonb/arrays -> json)

Revision ID: c7d1f2a4b9e0
Revises: b94ea274cdcd
Create Date: 2026-10-02 12:00:00.000000

Budly v1.0 stores data in plain JSON columns so the same models run on the
local SQLite store and on Postgres. On Postgres the values are identical to
what jsonb held; this only changes the declared column type. SQLite ignores
this migration (its tables are created directly).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c7d1f2a4b9e0"
down_revision: str | None = "b94ea274cdcd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _to_json(table: str, column: str) -> None:
    if not _is_postgres():
        return
    op.alter_column(
        table,
        column,
        type_=sa.JSON(),
        postgresql_using=f"{column}::text::json",
    )


def upgrade() -> None:
    _to_json("courses", "enrolled_sections")
    _to_json("assignments", "submission_types")
    _to_json("events", "payload")
    _to_json("chat_messages", "tool_calls")
    _to_json("digests", "items")


def downgrade() -> None:
    if not _is_postgres():
        return
    from sqlalchemy.dialects.postgresql import JSONB

    op.alter_column(
        "courses",
        "enrolled_sections",
        type_=sa.ARRAY(sa.Text()),
        postgresql_using="enrolled_sections::text[]",
    )
    op.alter_column(
        "assignments",
        "submission_types",
        type_=sa.ARRAY(sa.Text()),
        postgresql_using="submission_types::text[]",
    )
    op.alter_column("events", "payload", type_=JSONB(), postgresql_using="payload::jsonb")
    op.alter_column(
        "chat_messages", "tool_calls", type_=JSONB(), postgresql_using="tool_calls::jsonb"
    )
    op.alter_column("digests", "items", type_=JSONB(), postgresql_using="items::jsonb")

"""Course short code, parsed title, and user nickname

Revision ID: 458e37f3ce83
Revises: 0001
Create Date: 2026-09-09 12:01:14.940618
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "458e37f3ce83"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _existing_columns(table: str) -> set[str]:
    bind = op.get_bind()
    try:
        from sqlalchemy import inspect as sa_inspect

        return {c["name"] for c in sa_inspect(bind).get_columns(table)}
    except Exception:
        return set()


def upgrade() -> None:
    # 0001 uses Base.metadata.create_all, so fresh DBs already have these columns.
    # Add only what's missing to stay idempotent on both fresh and existing DBs.
    existing = _existing_columns("courses")
    if "short_code" not in existing:
        op.add_column("courses", sa.Column("short_code", sa.String(length=32), nullable=True))
    if "title" not in existing:
        op.add_column("courses", sa.Column("title", sa.Text(), nullable=True))
    if "nickname" not in existing:
        op.add_column("courses", sa.Column("nickname", sa.Text(), nullable=True))


def downgrade() -> None:
    existing = _existing_columns("courses")
    for col in ("nickname", "title", "short_code"):
        if col in existing:
            op.drop_column("courses", col)

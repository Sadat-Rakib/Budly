"""Initial schema.

Created from the model metadata rather than hand-written DDL. For a first migration
this is the safer of the two: it cannot drift from the models it is derived from.
Every migration after this one is generated normally with
``alembic revision --autogenerate``, which diffs against this state.

Revision ID: 0001
Revises:
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from canvasbuddy.models import Base

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())

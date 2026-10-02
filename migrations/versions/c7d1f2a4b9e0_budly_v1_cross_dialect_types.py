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


def _array_to_json(table: str, column: str) -> None:
    """text[] -> json.

    A text[] renders as ``{a,b}``, which is not valid JSON, so the cast has to go
    through ``to_json`` rather than the ``::text::json`` shorthand.
    """
    if not _is_postgres():
        return
    op.alter_column(
        table,
        column,
        type_=sa.JSON(),
        postgresql_using=f"to_json({column})",
    )


def _jsonb_to_json(table: str, column: str) -> None:
    if not _is_postgres():
        return
    op.alter_column(
        table,
        column,
        type_=sa.JSON(),
        postgresql_using=f'"{column}"::json',
    )


def upgrade() -> None:
    _array_to_json("courses", "enrolled_sections")
    _array_to_json("assignments", "submission_types")
    _jsonb_to_json("events", "payload")
    _jsonb_to_json("chat_messages", "tool_calls")
    _jsonb_to_json("digests", "items")


def downgrade() -> None:
    if not _is_postgres():
        return
    from sqlalchemy.dialects.postgresql import JSONB

    # Postgres refuses a subquery in a transform expression ("cannot use subquery in
    # transform expression"), and json_array_elements_text is set-returning, so the
    # array conversion needs a real function rather than an inline expression.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION _budly_json_to_text_array(j json) RETURNS text[]
        LANGUAGE plpgsql IMMUTABLE AS $fn$
        DECLARE out text[];
        BEGIN
          IF j IS NULL THEN RETURN NULL; END IF;
          SELECT array_agg(value) INTO out FROM json_array_elements_text(j) AS t(value);
          RETURN out;
        END
        $fn$
        """
    )
    try:
        for table, column in (
            ("courses", "enrolled_sections"),
            ("assignments", "submission_types"),
        ):
            op.alter_column(
                table,
                column,
                type_=sa.ARRAY(sa.Text()),
                postgresql_using=f"_budly_json_to_text_array({column})",
            )
        for table, column in (
            ("events", "payload"),
            ("chat_messages", "tool_calls"),
            ("digests", "items"),
        ):
            op.alter_column(table, column, type_=JSONB(), postgresql_using=f'"{column}"::jsonb')
    finally:
        op.execute("DROP FUNCTION IF EXISTS _budly_json_to_text_array(json)")

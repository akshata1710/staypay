"""add index on idempotency_keys.created_at (SP-104)

The cleanup worker deletes completed keys by age, so it needs an index on created_at.

The index is built CONCURRENTLY: idempotency_keys is large (that's the point of SP-104), and a plain
CREATE INDEX blocks every checkout INSERT until the build finishes. CONCURRENTLY can't run inside a
transaction, hence autocommit_block(). If a concurrent build fails it leaves an INVALID index behind;
IF NOT EXISTS would then skip it, so drop it first: DROP INDEX CONCURRENTLY ix_idempotency_keys_created_at.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-25 04:41:57.032469
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_idempotency_keys_created_at",
            "idempotency_keys",
            ["created_at"],
            unique=False,
            postgresql_concurrently=True,
            if_not_exists=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_idempotency_keys_created_at",
            table_name="idempotency_keys",
            postgresql_concurrently=True,
            if_exists=True,
        )

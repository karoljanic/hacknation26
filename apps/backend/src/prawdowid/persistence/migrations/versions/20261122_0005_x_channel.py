"""channel "x": direct messages to @prawdowid

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-22
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD = ("web", "discord", "sms", "whatsapp", "messenger", "demo")
NEW = (*OLD, "x")


# The initial schema's check got the naming-convention prefix twice.
NAME = "ck_submission_ck_submission_channel"


def _replace(values: tuple[str, ...]) -> None:
    allowed = ", ".join(f"'{value}'" for value in values)
    op.execute(f"ALTER TABLE submission DROP CONSTRAINT IF EXISTS {NAME}")
    op.execute(f"ALTER TABLE submission ADD CONSTRAINT {NAME} CHECK (channel IN ({allowed}))")


def upgrade() -> None:
    _replace(NEW)


def downgrade() -> None:
    op.execute("DELETE FROM submission WHERE channel = 'x'")
    _replace(OLD)

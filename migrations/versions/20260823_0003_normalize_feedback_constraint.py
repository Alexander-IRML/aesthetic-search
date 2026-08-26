"""Normalize the graded-feedback check-constraint name.

Revision ID: 20260823_0003
Revises: 20260823_0002
Create Date: 2026-08-23
"""

from typing import Sequence

from alembic import op


revision: str = "20260823_0003"
down_revision: str | None = "20260823_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    _replace_interaction_check(include_relevance_feedback=True)


def downgrade() -> None:
    _replace_interaction_check(include_relevance_feedback=True)


def _replace_interaction_check(*, include_relevance_feedback: bool) -> None:
    op.execute(
        """
        DO $$
        DECLARE item record;
        BEGIN
          FOR item IN
            SELECT conname
              FROM pg_constraint
             WHERE conrelid = 'product.interaction_events'::regclass
               AND contype = 'c'
               AND pg_get_constraintdef(oid) LIKE '%interaction_type%'
          LOOP
            EXECUTE format(
              'ALTER TABLE product.interaction_events DROP CONSTRAINT %I',
              item.conname
            );
          END LOOP;
        END
        $$
        """
    )
    feedback_value = ", 'relevance_feedback'" if include_relevance_feedback else ""
    op.execute(
        "ALTER TABLE product.interaction_events "
        "ADD CONSTRAINT ck_interaction_events_interaction_events_type "
        "CHECK (interaction_type IN ("
        "'click', 'like', 'dislike', 'save', 'unsave', "
        "'follow_artist', 'unfollow_artist', 'reformulation'"
        f"{feedback_value}))"
    )

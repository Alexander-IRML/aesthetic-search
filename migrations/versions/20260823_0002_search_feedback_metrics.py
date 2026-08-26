"""Add ranked candidate evidence and graded retrieval feedback.

Revision ID: 20260823_0002
Revises: 20260820_0001
Create Date: 2026-08-23
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260823_0002"
down_revision: str | None = "20260820_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The foundation revision imports project metadata for first-time installs.
    # Existing installations do not have this table, while a brand-new install
    # may already have it when this revision begins.
    inspector = sa.inspect(op.get_bind())
    if "retrieval_candidates" not in inspector.get_table_names(schema="product"):
        op.create_table(
            "retrieval_candidates",
            sa.Column("query_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("artwork_id", sa.Text(), nullable=False),
            sa.Column("artist_id", sa.Text(), nullable=False),
            sa.Column("candidate_rank", sa.Integer(), nullable=False),
            sa.Column("fusion_score", sa.Float(), nullable=True),
            sa.Column("clip_score", sa.Float(), nullable=True),
            sa.Column("clip_rank", sa.Integer(), nullable=True),
            sa.Column("style_score", sa.Float(), nullable=True),
            sa.Column("style_rank", sa.Integer(), nullable=True),
            sa.Column(
                "scores",
                postgresql.JSONB(astext_type=sa.Text()),
                server_default=sa.text("'{}'::jsonb"),
                nullable=False,
            ),
            sa.Column("model_bundle_version", sa.Text(), nullable=True),
            sa.Column("index_version", sa.Text(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.CheckConstraint("candidate_rank > 0", name="retrieval_candidates_rank"),
            sa.ForeignKeyConstraint(
                ["artist_id"],
                ["catalog.artists.artist_id"],
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["artwork_id"],
                ["catalog.artworks.artwork_id"],
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["query_id"],
                ["product.queries.query_id"],
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("query_id", "candidate_rank"),
            sa.UniqueConstraint(
                "query_id",
                "artwork_id",
                name="uq_retrieval_candidates_query_artwork",
            ),
            schema="product",
        )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_retrieval_candidates_artwork "
        "ON product.retrieval_candidates (artwork_id)"
    )
    op.execute("ALTER TABLE product.retrieval_candidates ENABLE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS retrieval_candidates_owner ON product.retrieval_candidates")
    op.execute(
        """
        CREATE POLICY retrieval_candidates_owner ON product.retrieval_candidates
        USING (
          EXISTS (
            SELECT 1 FROM product.queries q
             WHERE q.query_id = retrieval_candidates.query_id
               AND (
                 q.user_id = product.current_user_id()
                 OR q.session_id = product.current_session_id()
               )
          )
        )
        WITH CHECK (
          EXISTS (
            SELECT 1 FROM product.queries q
             WHERE q.query_id = retrieval_candidates.query_id
               AND (
                 q.user_id = product.current_user_id()
                 OR q.session_id = product.current_session_id()
               )
          )
        )
        """
    )

    op.execute(
        "ALTER TABLE product.interaction_events DROP CONSTRAINT IF EXISTS "
        "ck_interaction_events_interaction_events_type"
    )
    op.create_check_constraint(
        "interaction_events_type",
        "interaction_events",
        "interaction_type IN ('click', 'like', 'dislike', 'save', 'unsave', "
        "'follow_artist', 'unfollow_artist', 'reformulation', 'relevance_feedback')",
        schema="product",
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_interactions_feedback_created "
        "ON product.interaction_events (interaction_type, created_at)"
    )


def downgrade() -> None:
    op.drop_index(
        "ix_interactions_feedback_created",
        table_name="interaction_events",
        schema="product",
    )
    op.drop_constraint(
        "ck_interaction_events_interaction_events_type",
        "interaction_events",
        schema="product",
        type_="check",
    )
    op.execute(
        "DELETE FROM product.interaction_events WHERE interaction_type = 'relevance_feedback'"
    )
    op.create_check_constraint(
        "interaction_events_type",
        "interaction_events",
        "interaction_type IN ('click', 'like', 'dislike', 'save', 'unsave', "
        "'follow_artist', 'unfollow_artist', 'reformulation')",
        schema="product",
    )
    op.execute("DROP POLICY IF EXISTS retrieval_candidates_owner ON product.retrieval_candidates")
    op.drop_index(
        "ix_retrieval_candidates_artwork",
        table_name="retrieval_candidates",
        schema="product",
    )
    op.drop_table("retrieval_candidates", schema="product")

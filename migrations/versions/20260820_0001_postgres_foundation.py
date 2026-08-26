"""Create the ArtSearch PostgreSQL control plane.

Revision ID: 20260820_0001
Revises:
Create Date: 2026-08-20
"""

from typing import Sequence

from alembic import op

from artsearch.postgres.schema import SCHEMAS, metadata


revision: str = "20260820_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")
    for schema in SCHEMAS:
        op.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")

    metadata.create_all(bind=op.get_bind(), checkfirst=True)
    _install_rls()


def downgrade() -> None:
    for schema in reversed(SCHEMAS):
        op.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


def _install_rls() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION product.current_user_id()
        RETURNS uuid
        LANGUAGE sql
        STABLE
        AS $$
          SELECT NULLIF(current_setting('app.user_id', true), '')::uuid
        $$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION product.current_session_id()
        RETURNS uuid
        LANGUAGE sql
        STABLE
        AS $$
          SELECT NULLIF(current_setting('app.session_id', true), '')::uuid
        $$
        """
    )

    policies = {
        "users": "user_id = product.current_user_id()",
        "sessions": (
            "user_id = product.current_user_id() OR session_id = product.current_session_id()"
        ),
        "queries": (
            "user_id = product.current_user_id() OR session_id = product.current_session_id()"
        ),
        "interaction_events": (
            "user_id = product.current_user_id() OR session_id = product.current_session_id()"
        ),
        "saved_artworks": "user_id = product.current_user_id()",
        "followed_artists": "user_id = product.current_user_id()",
    }
    for table_name, expression in policies.items():
        op.execute(f"ALTER TABLE product.{table_name} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table_name}_owner ON product.{table_name} "
            f"USING ({expression}) WITH CHECK ({expression})"
        )

    op.execute("ALTER TABLE product.ranking_runs ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY ranking_runs_owner ON product.ranking_runs
        USING (
          EXISTS (
            SELECT 1 FROM product.queries q
             WHERE q.query_id = ranking_runs.query_id
               AND (
                 q.user_id = product.current_user_id()
                 OR q.session_id = product.current_session_id()
               )
          )
        )
        WITH CHECK (
          EXISTS (
            SELECT 1 FROM product.queries q
             WHERE q.query_id = ranking_runs.query_id
               AND (
                 q.user_id = product.current_user_id()
                 OR q.session_id = product.current_session_id()
               )
          )
        )
        """
    )
    op.execute("ALTER TABLE product.result_impressions ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY result_impressions_owner ON product.result_impressions
        USING (session_id = product.current_session_id())
        WITH CHECK (session_id = product.current_session_id())
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION product.prune_telemetry(retention interval DEFAULT '180 days')
        RETURNS bigint
        LANGUAGE plpgsql
        AS $$
        DECLARE
          deleted_count bigint;
        BEGIN
          DELETE FROM product.sessions
           WHERE COALESCE(ended_at, last_seen_at, started_at) < now() - retention;
          GET DIAGNOSTICS deleted_count = ROW_COUNT;
          RETURN deleted_count;
        END
        $$
        """
    )

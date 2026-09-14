"""Add the corpus discovery control plane.

Revision ID: 20260909_0004
Revises: 20260823_0003
Create Date: 2026-09-09
"""

from typing import Sequence

from alembic import op

from artsearch.postgres.schema import (
    discovery_campaigns,
    discovery_coverage_snapshots,
    discovery_creator_profiles,
    discovery_creators,
    discovery_edge_evidence,
    discovery_edges,
    discovery_entities,
    discovery_evidence,
    discovery_exclusions,
    discovery_frontier_tasks,
    discovery_media,
    discovery_observations,
    discovery_pipeline_events,
    discovery_posts,
    discovery_removal_requests,
    discovery_sources,
)


revision: str = "20260909_0004"
down_revision: str | None = "20260823_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS discovery")
    bind = op.get_bind()
    for table in (
        discovery_campaigns,
        discovery_entities,
        discovery_creators,
        discovery_sources,
        discovery_observations,
        discovery_edges,
        discovery_edge_evidence,
        discovery_posts,
        discovery_media,
        discovery_evidence,
        discovery_creator_profiles,
        discovery_frontier_tasks,
        discovery_pipeline_events,
        discovery_exclusions,
        discovery_removal_requests,
        discovery_coverage_snapshots,
    ):
        table.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    op.execute("DROP SCHEMA IF EXISTS discovery CASCADE")

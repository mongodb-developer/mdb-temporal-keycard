"""Index/collection bootstrap as a granted activity.

Runs the same ensure functions the infra script and the agent API use, but as
a Temporal activity under @grant, so in Keycard mode even setup work executes
with a per-execution minted Atlas credential and shows up in the audit log.
"""

from __future__ import annotations

from temporalio import activity

from keycardai.temporal import grant

from ..config import settings


@grant(settings.keycard_mongodb_resource)
@activity.defn
def bootstrap_indexes() -> dict:
    from infra.create_atlas_index import (
        ensure_atlas_indexes,
        ensure_collections_and_indexes,
    )

    boot = ensure_collections_and_indexes()
    created = ensure_atlas_indexes(collection=settings.knowledge_collection)
    return {**boot, "atlas_indexes": created}

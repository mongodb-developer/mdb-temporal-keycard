"""One-shot workflow wrapping the index/collection bootstrap activity."""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow


@workflow.defn
class BootstrapIndexesWorkflow:
    @workflow.run
    async def run(self) -> dict:
        return await workflow.execute_activity(
            "bootstrap_indexes",
            start_to_close_timeout=timedelta(minutes=2),
        )

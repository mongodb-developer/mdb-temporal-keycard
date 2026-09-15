"""Temporal worker: hosts the Part 1 workflows and activities.

Run:  uv run python -m pipeline.worker
"""

from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from temporalio.client import Client
from temporalio.contrib.openai_agents import ModelActivityParameters, OpenAIAgentsPlugin
from temporalio.worker import Worker

from agent.agent_workflow import DeepResearchAgent
from agent.tools import rerank_tool, vector_search_tool

from .activities import ALL_ACTIVITIES
from .config import settings
from .workflows import ALL_WORKFLOWS


async def main() -> None:
    # The durable research agent (OpenAI Agents SDK) is opt-in: it loads only when
    # OPENAI_API_KEY is set, because the plugin builds an OpenAI client at worker startup.
    # Without a key the worker still runs ingestion/backfill exactly as before.
    from .clients import keycard_enabled

    # Keycard mode: the interceptor mints a fresh credential for every activity
    # execution (each activity declares its resources with @grant), so the
    # upstream secrets never sit in .env or workflow history. The worker's own
    # identity is the client-secret credential from settings, passed explicitly
    # so nothing depends on process environment variables.
    interceptors: list = []
    credential = None
    if keycard_enabled():
        from keycardai.oauth.server import ClientSecret
        from keycardai.temporal import KeycardInterceptor

        credential = ClientSecret(
            (settings.keycard_client_id, settings.keycard_client_secret)
        )
        interceptors.append(
            KeycardInterceptor(settings.keycard_zone_url, credential=credential)
        )
        print(f"[worker] Keycard mode: credentials minted from {settings.keycard_zone_url}")

    plugins: list = []
    agent_workflows: list = []
    agent_activities: list = []
    # In Keycard mode the plugin's model credential comes from the Keycard
    # model provider: the OpenAI key mints from the vault per model call
    # (cached briefly), so rotation propagates without a worker restart and
    # nothing is exported into the environment.
    if credential is not None:
        from keycardai.temporal.openai_agents import KeycardOpenAIProvider

        plugins.append(
            OpenAIAgentsPlugin(
                model_params=ModelActivityParameters(
                    start_to_close_timeout=timedelta(seconds=60)
                ),
                model_provider=KeycardOpenAIProvider(
                    settings.keycard_zone_url,
                    settings.keycard_openai_resource,
                    credential=credential,
                ),
            )
        )
    elif settings.openai_api_key:
        os.environ["OPENAI_API_KEY"] = settings.openai_api_key
        plugins.append(
            OpenAIAgentsPlugin(
                model_params=ModelActivityParameters(
                    start_to_close_timeout=timedelta(seconds=60)
                )
            )
        )
    if plugins:
        agent_workflows = [DeepResearchAgent]
        agent_activities = [vector_search_tool, rerank_tool]
    else:
        print("[worker] OPENAI_API_KEY not set — durable research agent disabled")

    client = await Client.connect(
        settings.temporal_address,
        namespace=settings.temporal_namespace,
        plugins=plugins,
    )

    # Sync activities (pymongo / voyage / boto3) run in this thread pool; async
    # activities run on the worker event loop.
    with ThreadPoolExecutor(max_workers=16) as executor:
        worker = Worker(
            client,
            task_queue=settings.temporal_task_queue,
            workflows=[*ALL_WORKFLOWS, *agent_workflows],
            activities=[*ALL_ACTIVITIES, *agent_activities],
            activity_executor=executor,
            interceptors=interceptors,
        )
        print(
            f"[worker] connected to {settings.temporal_address} "
            f"(ns={settings.temporal_namespace}) on task queue '{settings.temporal_task_queue}'"
        )
        await worker.run()


if __name__ == "__main__":
    asyncio.run(main())

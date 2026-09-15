"""Deep-agent FastAPI backend.

  POST /research            {query}      -> {workflow_id}            (start durable research agent)
  GET  /research/{wf_id}                 -> {steps[], answer, done…} (poll live progress)
  GET  /keycard/access                   -> {allowed, policy, …}     (is the forbid policy active?)
  POST /keycard/access      {allowed}    -> {allowed, policy, …}     (activate or deactivate it, live)
  GET  /health

Run:  uv run python -m agent.api
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import timedelta

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from temporalio.client import Client
from temporalio.contrib.openai_agents import ModelActivityParameters, OpenAIAgentsPlugin

from infra import demo_policy
from infra.create_atlas_index import (
    ensure_atlas_indexes,
    ensure_collections_and_indexes,
)
from pipeline.config import settings

app = FastAPI(title="Temporal deep agent")
logger = logging.getLogger(__name__)

# Allow the Vite dev server (and any localhost origin) to call the API.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


async def _bootstrap_indexes_via_workflow() -> None:
    # In Keycard mode the bootstrap runs as a workflow, so the Mongo
    # credential is minted per activity execution like everything else.
    try:
        client = await _get_agent_client()
        result = await client.execute_workflow(
            "BootstrapIndexesWorkflow",
            id="bootstrap-indexes",
            task_queue=settings.temporal_task_queue,
        )
        logger.info("Bootstrap workflow completed: %s", result)
    except Exception:
        logger.exception("Bootstrap workflow failed")


@app.on_event("startup")
async def ensure_index_on_startup() -> None:
    from pipeline.clients import keycard_enabled

    if keycard_enabled():
        # Run in the background so /health and /research answer at once. The
        # bootstrap needs a worker and a reachable Atlas cluster; if either is
        # missing the workflow keeps retrying in Temporal without holding the
        # API hostage, and the outcome lands in this log.
        asyncio.create_task(_bootstrap_indexes_via_workflow())
        return
    try:
        boot = ensure_collections_and_indexes()
        if boot["collections"]:
            logger.info("Created MongoDB collections at startup: %s", ", ".join(boot["collections"]))
        if boot["indexes"]:
            logger.info("Ensured MongoDB indexes at startup: %s", ", ".join(boot["indexes"]))

        created = ensure_atlas_indexes(collection=settings.knowledge_collection)
        if created:
            logger.info("Created Atlas Search index at startup: %s", ", ".join(created))
        else:
            logger.info("Atlas Search index already present for '%s'", settings.knowledge_collection)
    except Exception:
        logger.exception("Failed to bootstrap MongoDB collections/indexes on startup")


class AccessRequest(BaseModel):
    allowed: bool
    resource: str | None = None
    mode: str = "policy"


def _require_keycard_mode() -> None:
    from pipeline.clients import keycard_enabled

    if not keycard_enabled():
        raise HTTPException(
            status_code=409,
            detail="Keycard mode is off (KEYCARD_ZONE_URL not set); nothing to toggle.",
        )


@app.get("/keycard/access")
async def keycard_access() -> dict:
    """Whether Keycard currently lets the worker application mint the Atlas credential.

    Reads which demo policy set version is active (baseline, or the one carrying
    the forbid policy) through the signed-in Keycard CLI, same as infra/demo_policy.py."""
    _require_keycard_mode()
    try:
        return await asyncio.to_thread(demo_policy.access_state, settings.keycard_mongodb_resource)
    except (LookupError, demo_policy.PolicyError) as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@app.post("/keycard/access")
async def keycard_set_access(req: AccessRequest) -> dict:
    """Activate the forbid policy (allowed=false) or the baseline (allowed=true), effective on
    the worker's next mint. The agent UI's Keycard switch calls this."""
    _require_keycard_mode()
    resource = req.resource or settings.keycard_mongodb_resource
    try:
        state = await asyncio.to_thread(demo_policy.set_access, resource, req.allowed, req.mode)
    except (LookupError, demo_policy.PolicyError) as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    logger.info("Keycard access for %s set to %s", resource, "allowed" if req.allowed else "denied")
    return state


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "agent_model": settings.agent_model}


_agent_client: Client | None = None


async def _get_agent_client() -> Client:
    """Temporal client configured with the OpenAI Agents plugin — matches the worker's
    data converter so agent-workflow results decode correctly."""
    global _agent_client
    if _agent_client is None:
        os.environ.setdefault("OPENAI_API_KEY", settings.openai_api_key)
        _agent_client = await Client.connect(
            settings.temporal_address,
            namespace=settings.temporal_namespace,
            plugins=[
                OpenAIAgentsPlugin(
                    model_params=ModelActivityParameters(
                        start_to_close_timeout=timedelta(seconds=60)
                    )
                )
            ],
        )
    return _agent_client


class ResearchRequest(BaseModel):
    query: str


@app.post("/research")
async def research(req: ResearchRequest) -> dict:
    """Start the durable research agent (OpenAI Agents SDK on Temporal). Returns the workflow
    id immediately; poll GET /research/{workflow_id} for live progress and the final answer."""
    from pipeline.clients import keycard_enabled

    if not (settings.openai_api_key or keycard_enabled()):
        raise HTTPException(
            status_code=503,
            detail=(
                "Research agent unavailable: set OPENAI_API_KEY in .env (or configure "
                "Keycard, which vaults it) and restart the worker."
            ),
        )
    client = await _get_agent_client()
    wf_id = f"agent-{uuid.uuid4().hex[:16]}"
    await client.start_workflow(
        "DeepResearchAgent",
        req.query,
        id=wf_id,
        task_queue=settings.temporal_task_queue,
    )
    return {"workflow_id": wf_id}


@app.get("/research/{workflow_id}")
async def research_status(workflow_id: str) -> dict:
    """Poll live progress + final answer for a research run (queries the workflow's `progress`)."""
    client = await _get_agent_client()
    handle = client.get_workflow_handle(workflow_id)
    desc = await handle.describe()
    status = desc.status.name if desc.status else "UNKNOWN"

    progress: dict = {
        "steps": [], "tool_calls": [], "answer": None, "model": None, "done": False,
    }
    try:
        progress = await handle.query("progress")
    except Exception:  # noqa: BLE001 - a failed/terminated run can't be queried
        pass

    # A terminal, non-completed status means the run won't produce an answer.
    if status not in ("RUNNING", "COMPLETED"):
        progress["done"] = True
    return {"workflow_id": workflow_id, "status": status, **progress}


def main() -> None:
    uvicorn.run(app, host="0.0.0.0", port=settings.agent_api_port)


if __name__ == "__main__":
    main()

"""DeepResearchAgent — a durable OpenAI Agents SDK agent over the Temporal docs.

The agent's reasoning loop runs inside this Temporal workflow; the model calls and the
vector_search / rerank tools execute as activities, so the whole trajectory is durable and
auditable in the Temporal UI. A hosted web-search tool supplements the docs — it runs inside
the model-call activity (OpenAI Responses API), not as a separate activity. Live progress is
exposed via the `progress` query (run hooks append human-readable steps as the agent works).
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta

from temporalio import workflow
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from agents import Agent, FunctionTool, RunHooks, Runner, WebSearchTool
    from temporalio.contrib.openai_agents.workflow import activity_as_tool

    from pipeline.config import settings

    from .tools import rerank_tool, vector_search_tool


_INSTRUCTIONS = (
    "You are a precise research assistant answering questions about Temporal, backed by a "
    "MongoDB Atlas knowledge base of Temporal documentation plus web search.\n"
    "- Prefer the ingested docs: decompose any multi-part or comparative question into its "
    "distinct sub-topics and call `vector_search_tool` SEPARATELY for each sub-topic with a "
    "focused query — do not cover several concepts in one broad search. For a genuinely "
    "single-topic question, one search is fine.\n"
    "- If a search returns thin or off-target results, reformulate the query and search again.\n"
    "- Call `rerank_tool` with the collected chunk_ids to prioritize the best chunks before "
    "answering.\n"
    "- Use web search to SUPPLEMENT the docs: for very recent changes, topics the knowledge "
    "base does not cover, or to corroborate a claim. The ingested docs are authoritative for "
    "how Temporal works — prefer them over the open web when they conflict.\n"
    "- Answer from your gathered sources. Cite inline as [n]: give the source_uri for "
    "knowledge-base chunks and the URL for web results, address each sub-topic, and make clear "
    "which claims came from the docs vs the web. If neither contains the answer, say so plainly.\n"
    "- If `vector_search_tool` or `rerank_tool` returns ACCESS DENIED BY KEYCARD POLICY, your answer "
    "MUST begin with this exact sentence: \"My access to the internal knowledge base has been "
    "revoked by Keycard policy.\" Do not retry the tool and do not ask for confirmation: go straight "
    "to web search, answer from those results alone, and mark every claim as web-sourced."
)


def _denial_as_tool_result(tool: FunctionTool, on_denied) -> FunctionTool:
    """Hand a Keycard policy denial to the model instead of failing the run.

    The worker's KeycardInterceptor raises the non-retryable KeycardAccessDenied
    before the tool body runs. Left alone, that aborts the whole agent workflow
    before the model hears about it. Returned as the tool's result, the agent
    can say what happened and fall back to the web. Every other failure still
    raises and retries under the activity policy.

    `on_denied(message)` runs in workflow context so the workflow can record the
    denial itself; the recorded activity failure drives it, so it replays
    deterministically.
    """
    inner = tool.on_invoke_tool

    async def on_invoke(ctx, input):  # signature fixed by the Agents SDK
        try:
            return await inner(ctx, input)
        except ActivityError as e:
            cause = e.cause
            if isinstance(cause, ApplicationError) and cause.type == "KeycardAccessDenied":
                on_denied(cause.message or "")
                return f"ACCESS DENIED BY KEYCARD POLICY: {cause.message}"
            raise

    return dataclasses.replace(tool, on_invoke_tool=on_invoke)


_DENIED_STEP = "Knowledge base access denied by Keycard policy"


# Human-readable progress labels keyed by tool name/type.
_TOOL_LABELS = {
    "vector_search_tool": "Searching the docs…",
    "rerank_tool": "Reranking results…",
    "WebSearchTool": "Searching the web…",
    "web_search": "Searching the web…",
    "web_search_call": "Searching the web…",
}


class _ProgressHooks(RunHooks):
    """Appends human-readable steps to a shared list as the agent runs.

    Runs inside the workflow, so it only mutates workflow state — deterministic on replay,
    since the hook sequence is driven by recorded activity results. Consecutive duplicate
    labels are collapsed to keep the feed clean.
    """

    def __init__(self, steps: list[str]) -> None:
        self._steps = steps

    def _add(self, label: str) -> None:
        if not self._steps or self._steps[-1] != label:
            self._steps.append(label)

    async def on_llm_start(self, context, agent, system_prompt, input_items) -> None:
        self._add("Reasoning…")

    async def on_tool_start(self, context, agent, tool) -> None:
        name = getattr(tool, "name", None) or type(tool).__name__
        self._add(_TOOL_LABELS.get(name, f"Using {name}…"))


@workflow.defn
class DeepResearchAgent:
    def __init__(self) -> None:
        self._steps: list[str] = []
        self._tool_calls: list[str] = []
        self._answer: str | None = None
        self._done: bool = False
        self._denials: list[str] = []

    @workflow.query
    def progress(self) -> dict:
        """Live progress for UI polling — read-only, safe to call at any time."""
        return {
            "steps": list(self._steps),
            "tool_calls": list(self._tool_calls),
            "answer": self._answer,
            "model": settings.agent_model,
            "done": self._done,
            "denials": list(self._denials),
        }

    @workflow.run
    async def run(self, query: str) -> dict:
        agent = Agent(
            name="Temporal docs researcher",
            model=settings.agent_model,
            instructions=_INSTRUCTIONS,
            tools=[
                _denial_as_tool_result(
                    activity_as_tool(
                        vector_search_tool, start_to_close_timeout=timedelta(seconds=30)
                    ),
                    self._record_denial,
                ),
                _denial_as_tool_result(
                    activity_as_tool(rerank_tool, start_to_close_timeout=timedelta(seconds=30)),
                    self._record_denial,
                ),
                # Hosted tool: runs inside the model-call activity (OpenAI Responses API),
                # not as a separate Temporal activity. Requires a web-search-capable model.
                WebSearchTool(),
            ],
        )
        result = await Runner.run(
            agent,
            query,
            max_turns=settings.agent_max_turns,
            hooks=_ProgressHooks(self._steps),
        )

        # Best-effort tool-call trajectory (the durable source of truth is workflow history).
        try:
            for item in getattr(result, "new_items", []):
                if type(item).__name__ == "ToolCallItem":
                    raw = getattr(item, "raw_item", None)
                    # function tools expose `.name`; hosted tools (web search) expose `.type`
                    label = getattr(raw, "name", None) or getattr(raw, "type", None)
                    if label:
                        self._tool_calls.append(label)
        except Exception:  # noqa: BLE001 - trajectory is diagnostic only
            self._tool_calls = []

        # Denials are reported in `denials` (progress query and result), recorded by
        # the workflow rather than the model, so the record is exact even when the
        # model glosses over the refusal or finds the same material on the open web.
        self._answer = result.final_output
        self._done = True
        self._add_final_step()
        return {
            "query": query,
            "answer": self._answer,
            "model": settings.agent_model,
            "tool_calls": list(self._tool_calls),
            "denials": list(self._denials),
        }

    def _record_denial(self, message: str) -> None:
        self._denials.append(message)
        if not self._steps or self._steps[-1] != _DENIED_STEP:
            self._steps.append(_DENIED_STEP)

    def _add_final_step(self) -> None:
        if not self._steps or self._steps[-1] != "Done":
            self._steps.append("Done")

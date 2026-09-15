import { useEffect, useState } from "react";
import type { AccessState, ProgressResponse, StartResponse } from "./types";

const EXAMPLES = [
  "Compare Signals, Queries, and Updates in Temporal — how does each work and when to use it?",
  "What is a Temporal Workflow and how does it work?",
  "How do I start a Temporal worker in Python?",
];

// Temporal Web UI (dev server). Used to deep-link the durable agent workflow run.
const TEMPORAL_UI = "http://localhost:8233";

function workflowUrl(id: string): string {
  return `${TEMPORAL_UI}/namespaces/default/workflows/${encodeURIComponent(id)}`;
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

// Progress step the workflow records when Keycard refuses a tool's credential.
const DENIED_STEP = "denied by Keycard policy";

export default function App() {
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [steps, setSteps] = useState<string[]>([]);
  const [res, setRes] = useState<ProgressResponse | null>(null);
  // Keycard switch: null until the API answers; stays null when Keycard mode is off.
  const [access, setAccess] = useState<AccessState | null>(null);
  const [accessBusy, setAccessBusy] = useState(false);
  const [accessError, setAccessError] = useState<string | null>(null);

  useEffect(() => {
    fetch("/keycard/access")
      .then(async (r) => (r.ok ? ((await r.json()) as AccessState) : null))
      .then((a) => setAccess(a))
      .catch(() => setAccess(null));
  }, []);

  async function toggleAccess() {
    if (!access || accessBusy) return;
    setAccessBusy(true);
    setAccessError(null);
    try {
      const r = await fetch("/keycard/access", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ allowed: !access.allowed }),
      });
      if (!r.ok) {
        let detail = `API ${r.status}`;
        try {
          const b = await r.json();
          if (b?.detail) detail = b.detail;
        } catch {
          // non-JSON body: keep the status message
        }
        throw new Error(detail);
      }
      setAccess((await r.json()) as AccessState);
    } catch (e) {
      setAccessError(e instanceof Error ? e.message : String(e));
    } finally {
      setAccessBusy(false);
    }
  }

  async function run(q: string) {
    const question = q.trim();
    if (!question || loading) return;
    setLoading(true);
    setError(null);
    setRes(null);
    setSteps([]);
    try {
      // 1. Start the durable workflow.
      const start = await fetch("/research", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: question }),
      });
      if (!start.ok) {
        let detail = `API ${start.status}`;
        try {
          const b = await start.json();
          if (b?.detail) detail = b.detail;
        } catch {
          // non-JSON body — keep the status message
        }
        throw new Error(detail);
      }
      const { workflow_id }: StartResponse = await start.json();

      // 2. Poll the progress query until the run finishes (bounded so a stuck run stops).
      for (let i = 0; i < 300; i++) {
        await sleep(600);
        const r = await fetch(`/research/${workflow_id}`);
        if (!r.ok) throw new Error(`API ${r.status}`);
        const p: ProgressResponse = await r.json();
        setSteps(p.steps ?? []);
        if (p.done || (p.status && p.status !== "RUNNING")) {
          setRes(p);
          if (p.status && p.status !== "COMPLETED" && !p.answer) {
            setError(`Workflow ${p.status}`);
          }
          return;
        }
      }
      throw new Error("Timed out waiting for the agent.");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="app">
      <header>
        <h1>Temporal Docs — Research Agent</h1>
        <p className="sub">Durable OpenAI agent on Temporal · vector search + rerank + web search</p>
      </header>

      {access && (
        <div className={`keycard ${access.allowed === false ? "denied" : "allowed"}`}>
          <div className="keycard-text">
            <span className="keycard-label">Keycard policy</span>
            <span className="keycard-desc">
              {access.policy ? (
                <>
                  <code>{access.policy}</code>{" "}
                  {access.allowed === false ? "is active: " : "is inactive: "}
                  <code>{access.application}</code>{" "}
                  {access.allowed === false ? "is forbidden" : "may be issued"} the{" "}
                  <code>{access.resource}</code> credential
                </>
              ) : (
                <>
                  <code>{access.application}</code>{" "}
                  {access.allowed === false ? "may not be issued" : "may be issued"} the{" "}
                  <code>{access.resource}</code> credential
                </>
              )}
            </span>
            {access.policy_set && (
              <span className="keycard-sub">
                active policy set <code>{access.policy_set}</code> v{access.policy_set_version}
              </span>
            )}
            {accessError && <span className="keycard-error">{accessError}</span>}
          </div>
          <button
            type="button"
            className="switch"
            role="switch"
            aria-checked={access.allowed !== false}
            disabled={accessBusy}
            onClick={toggleAccess}
            title="Activate or deactivate the forbid policy for the worker application, live"
          >
            <span className="knob" />
            <span className="switch-label">
              {accessBusy ? "Updating…" : access.allowed === false ? "Forbidden" : "Allowed"}
            </span>
          </button>
        </div>
      )}

      <form
        className="searchbar"
        onSubmit={(e) => {
          e.preventDefault();
          run(query);
        }}
      >
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Ask a question about the Temporal documentation…"
          autoFocus
        />
        <button type="submit" disabled={loading || !query.trim()}>
          {loading ? "Researching…" : "Ask"}
        </button>
      </form>

      <div className="examples">
        {EXAMPLES.map((ex) => (
          <button key={ex} className="chip" onClick={() => { setQuery(ex); run(ex); }}>
            {ex}
          </button>
        ))}
      </div>

      {error && <div className="error">Error: {error}</div>}

      {steps.length > 0 && (
        <div className="trajectory">
          <h3>{loading ? "Working…" : "What the agent did"}</h3>
          <ol className="tool-calls">
            {steps.map((s, i) => {
              const cls = [
                loading && i === steps.length - 1 ? "active" : "",
                s.includes(DENIED_STEP) ? "denied" : "",
              ].filter(Boolean).join(" ");
              return <li key={i} className={cls}>{s}</li>;
            })}
          </ol>
        </div>
      )}

      {res?.answer && (
        <div className="results">
          <div className="meta">
            model <code>{res.model}</code> ·{" "}
            <a href={workflowUrl(res.workflow_id)} target="_blank" rel="noreferrer">
              view workflow in Temporal UI ↗
            </a>
          </div>
          {res.denials && res.denials.length > 0 && (
            <div className="denial">
              <strong>Keycard policy denied this agent's knowledge-base access.</strong>{" "}
              {res.denials[0]} The answer below is web-sourced only.
            </div>
          )}
          <div className="answer">
            <div className="answer-head">
              <h2>Answer</h2>
            </div>
            <div className="answer-body">{res.answer}</div>
          </div>
        </div>
      )}
    </div>
  );
}

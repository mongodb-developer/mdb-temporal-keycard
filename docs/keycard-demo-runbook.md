# Keycard demo runbook

How to run the Keycard-enabled reference architecture and the on-stage
demo: kill the worker mid-query, rotate a credential, and watch the workflow
finish anyway. Verified end to end against a live Keycard zone, a live Atlas
M0 cluster, and the real Voyage and OpenAI APIs.

## Accounts and prerequisites

Tooling: `uv`, Docker, the Temporal CLI, Node 20+ (see
[RUNBOOK.md](RUNBOOK.md) for install commands), and the
[Keycard CLI](https://docs.keycard.ai/cli) signed in to your zone
(`keycard auth signin --zone <zone-id> --org <org-id>`).

External accounts:

- MongoDB Atlas: a free M0 cluster works (it supports Vector Search). You need
  one database user and the `mongodb+srv://` connection string with the
  password embedded. Add the demo machine's IP to the Atlas network allowlist,
  and remember the venue's IP on event day.
- Voyage AI: an API key, and add a payment method to the account. The free tier
  is capped at 3 requests per minute, which turns a 40-chunk ingest into a
  15-minute grind (durable, but slow). The free token allowance still applies
  after adding one.
- OpenAI: an API key for the research agent.

## One-time setup

```bash
# 1. Provision the Keycard zone: worker application + client credential,
#    three vault-backed resources, dependencies. Writes KEYCARD_* to .env.
#    Idempotent; blank secret skips that resource so you can stage.
MONGODB_URI='mongodb+srv://...' VOYAGE_API_KEY='...' OPENAI_API_KEY='...' \
  uv run python -m infra.provision_keycard

# 2. Local infra defaults (MinIO + Temporal are local; no Temporal account)
cat .env.example | grep -A4 "S3_ENDPOINT_URL" >> .env   # or copy the S3/MinIO block by hand

# 2b. Create the demo policy set for the on-stage policy beat (idempotent):
#     the forbid-agent-atlas policy and demo-zone-policies with two versions.
#     Nothing is activated; the switch on the agent page does that.
uv run python -m infra.demo_policy setup

# 3. Bring everything up, create the vector index, seed the corpus
make setup
make start          # MinIO, Temporal dev server, worker, trigger API, agent API + UI
make index          # Atlas index bootstrap; in Keycard mode it runs as a workflow
make seed           # sample document through the full durable pipeline
```

The worker prints `[worker] Keycard mode: credentials minted from <zone-url>`
at startup. `.env` holds only the Keycard client credential and resource
identifiers; the Mongo, Voyage, and OpenAI secrets exist solely in the zone's
vault. The client secret itself is the local-demo posture: on a platform that
issues workload identity (EKS IRSA, Azure federated tokens, a SPIRE cluster),
the SDK's discovery picks up the platform token file instead and the worker
starts with no secrets at all; see the README's "last secret" section.

Seed extra documents (source_uri follows the key):

```bash
make seed FILE=./how-keycard-works.md KEY=docs/how-keycard-works.md
```

Seeding Keycard's own docs makes the finale land: the agent answers Keycard
questions from content that was ingested through Keycard-minted credentials.

## The demo

Windows to have open: the agent UI (http://localhost:5173), the Temporal UI
(http://localhost:8233), and the Keycard console on the zone's audit log.

The agent UI's Keycard switch needs the agent API running on the machine
where the Keycard CLI is signed in (`keycard auth signin`), because the
policy flip rides that CLI session.

Before the demo, delete the `MONGODB_URI` line from `.env`. In Keycard mode
nothing on the demo path reads it (index creation routes through the bootstrap
workflow), and its absence makes the point literal: no database credential
lives on this machine. `make query` and other out-of-band scripts need it
back afterwards.

1. **Start a research query.** Use the agent UI, or:

   ```bash
   curl -s -X POST http://localhost:8090/research \
     -H 'Content-Type: application/json' \
     -d '{"query":"How does Keycard mint credentials for agents?"}'
   # note the workflow_id in the response
   ```

2. **Kill the worker mid-flight** (about 10 seconds in, while tool activities
   are running):

   ```bash
   kill $(cat .local/worker.pid)
   ```

3. **Rotate the credential while the worker is down.** In the Keycard console:
   the Atlas resource -> Credentials tab -> edit the vaulted value. For a true
   rotation, first reset the database user's password in Atlas, then vault the
   new connection string. (Skipping this step still demonstrates
   resume-with-fresh-mints; the rotation makes the point that revocation does
   not strand in-flight work.)

4. **Restart the worker and watch it finish:**

   ```bash
   PYTHONUNBUFFERED=1 nohup uv run python -u -m pipeline.worker > .local/worker.log 2>&1 &
   echo $! > .local/worker.pid
   curl -s http://localhost:8090/research/<workflow_id>   # poll until COMPLETED
   ```

   Recovery re-runs the interrupted activity, the activity mints fresh (the
   rotated credential, if you rotated), and the workflow completes. In the
   Temporal UI, walk the workflow history: inputs and results only, nothing
   credential-shaped. In the Keycard console, the audit log shows every mint
   attributed to `temporal-pipeline-worker`.

5. **Revoke the agent's database access by policy, then ask again.** This
   beat is about the agent's behavior, not the pipeline. Pick a question only
   the knowledge base can answer: the seeded `awesome-temporal.md` is also
   public on GitHub, so web search partly recovers it once access is denied
   and the contrast softens. Seeding one internal-looking document (a
   fictional runbook or design note) and asking about it makes the denied
   answer visibly empty-handed. Note the knowledge-base `source_uri` citations
   in the first answer:

   ```bash
   curl -s -X POST http://localhost:8090/research \
     -H 'Content-Type: application/json' \
     -d '{"query":"How do activity heartbeats work in Temporal?"}'
   curl -s http://localhost:8090/research/<workflow_id>   # poll until done
   ```

   Flip the **Keycard policy** switch at the top of the agent page from
   Allowed to Forbidden. That activates the version of the customer policy set
   `demo-zone-policies` that carries the managed defaults plus one policy:

   ```cedar
   @id("forbid-agent-atlas")
   forbid (principal is Keycard::Application, action, resource is Keycard::Resource)
   when { principal.identifier == "temporal-pipeline-worker" &&
          resource.identifier == "https://cluster.mongodb.net" };
   ```

   The worker application keeps its dependency on Atlas the whole time, so
   this is policy overriding an entitlement the agent still has, not
   de-provisioning. Cedar forbids win over permits; activation is atomic and
   the worker's next mint sees it. The switch calls `POST /keycard/access` on
   the agent API, which runs the same code as the terminal fallback:

   ```bash
   uv run python -m infra.demo_policy deny
   ```

   Ask the same question. `vector_search_tool` fails on its first attempt with
   `KeycardAccessDenied` (open the agent workflow in the Temporal UI to show
   it). The zone's own message names the policy: `Access to "MongoDB Atlas" is
   denied by Policy "forbid-agent-atlas" in version <n> of Policy Set
   "demo-zone-policies"`. The agent receives the denial as the tool's result
   and falls back to web search.
   The workflow itself, not the model, records the refusal: the progress feed
   shows a red "Knowledge base access denied by Keycard policy" step, the
   answer carries a red callout with the zone's denial text, and the API
   response lists it under `denials`. The OpenAI key is untouched, so the agent
   stays articulate; only retrieval is gone. Flip the switch back to Allowed
   (or `uv run python -m infra.demo_policy restore`) and ask a third time.

   Grounded citations return on the very next mint. No worker restart and no
   cache to flush: every tool call mints its own credential, so a policy change
   lands on the next call in either direction.

   Then go into the Keycard console and show the machinery: **Policy sets**
   has `demo-zone-policies` active with its two live versions (the baseline
   and the one with the forbid); **All policies** lists `forbid-agent-atlas` as a customer
   policy next to the three platform defaults, and opening it shows the Cedar
   above; **Activity** shows the Deny and the Allow decisions side by side,
   both attributed to `temporal-pipeline-worker`, the Deny naming the policy.
   The application's Dependencies tab still lists Atlas throughout.

   Target MongoDB for this beat rather than OpenAI: the OpenAI key sits behind
   the model provider's five-minute refresh window, while Atlas mints per tool
   call, so the Atlas denial lands in seconds. `infra.demo_policy` takes
   `--resource <identifier>` if you want to show the OpenAI case anyway.

## What to point at while it runs

- In the Temporal UI, open the activity retries. On the free Voyage tier,
  ingestion visibly absorbs rate-limit failures through retry policies without
  re-embedding completed chunks; in our verification the pipeline shrugged off
  roughly 300 rate-limit errors across two documents with zero lost work.
- Walk the workflow history: durable, replayable, persisted indefinitely, and
  credential-free. That last property is the reason minting happens inside
  activities rather than passing tokens through workflow state.
- Everything mints just in time now: Atlas and Voyage per activity execution
  (the dual-resource activities declare both in one `@grant`), and the OpenAI
  key per model call through `KeycardOpenAIProvider`, refreshed on a short
  window, so rotating any of the three vaulted secrets propagates without a
  worker restart.

## Troubleshooting

- Worker exits at startup with a credential error: the worker builds its
  ClientSecret from `.env` via settings and passes it to the interceptor
  explicitly. Check `KEYCARD_CLIENT_ID` / `KEYCARD_CLIENT_SECRET` are present
  in `.env`.
- `make seed` fails with `S3_BUCKET is not set`: the local MinIO block from
  `.env.example` is missing from `.env` (step 2 above).
- Research endpoint returns 503: the agent loads only when an OpenAI key is
  available; in Keycard mode that means the vaulted OpenAI resource exists and
  the worker restarted after it was vaulted.
- Embeds failing repeatedly with a rate-limit error: that is the Voyage free
  tier. The workflow will finish anyway; a payment method on the account makes
  it fast.

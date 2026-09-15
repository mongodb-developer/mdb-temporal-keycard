# Set up the Keycard demo on your own machine

For a second presenter joining a zone that is already provisioned. The zone
holds the worker application and the three vaulted secrets (Atlas connection
string, Voyage key, OpenAI key), and the Atlas cluster already has the
ingested corpus. You need none of those secrets, no Atlas account, no Docker,
and no ingestion. The one requirement is membership in the Keycard org that
owns the zone, because the policy switch on the agent page rides your CLI
session.

Written so a coding agent can follow it as-is. If you are an agent: never
print `.env`, and never echo the output of `keycard credential` commands.
Every step ends with a check you can verify.

Budget: about 15 minutes, most of it installs.

## 1. Tools

```bash
brew install uv temporal node keycardai/tap/keycard
```

Check:

```bash
uv --version && temporal --version && node --version && keycard version
```

Node must be 20 or newer; Docker is not needed for this path.

## 2. Repo

```bash
git clone https://github.com/Larry-Osakwe/mdb-temporal-keycard.git
cd mdb-temporal-keycard
git checkout keycard-integration
uv sync
npm --prefix agent/ui install
```

Check: `uv run python -c "import keycardai.temporal, temporalio; print('ok')"` prints `ok`.

## 3. Sign in to Keycard

Ask the zone owner for the zone id and org id. Then:

```bash
keycard auth signin --zone <zone-id> --org <org-id>
```

A browser window completes the sign-in. Check:

```bash
keycard agent api /zones/<zone-id> -X GET --zone <zone-id> --org <org-id>
```

That returns the zone as JSON (name, slug, ids). A 401 or 403 means your
invite has not landed or you signed in to the wrong org.

## 4. Mint your own worker credential

```bash
KEYCARD_PROVISION_ZONE_ID=<zone-id> KEYCARD_PROVISION_ORG_ID=<org-id> \
  uv run python -m infra.provision_keycard
```

With no `MONGODB_URI`, `VOYAGE_API_KEY`, or `OPENAI_API_KEY` in your
environment, this finds the existing application and resources, skips the
vault writes ("vault write skipped for ..." three times), mints a client
credential for you, and writes `KEYCARD_ZONE_URL`, `KEYCARD_CLIENT_ID`,
`KEYCARD_CLIENT_SECRET`, and the three `KEYCARD_*_RESOURCE` lines to a new
`.env`. It prints ids and statuses, never the secret.

Check: `grep -c '^KEYCARD_' .env` prints `6`. Do not add `MONGODB_URI` or any
API key to `.env`; the whole point is that they are not on this machine.

Re-running is safe. It sees the credential already in `.env` and mints nothing.

## 5. Start the stack

```bash
make demo-start
```

This starts the Temporal dev server, the worker, the agent API, and the
agent UI in the background, without Docker or MinIO. The worker starts
before the API on purpose: the API runs an index bootstrap workflow at
startup and waits for a worker to take it.

Check: the command ends with a status block reading `OK` on all five lines
(temporal, worker, agent-api, agent-ui, switch). A `FAIL` line names the log
to read. `curl -s localhost:8090/health` returns `{"ok":true,...}`, and

```bash
curl -s localhost:8090/keycard/access
```

returns `"allowed":true`, `"policy":"forbid-agent-atlas"`, and the name and
version of the policy set that is active right now. If it returns a 502, the
CLI session from step 3 is not usable from this shell.

The policy objects behind the switch (the `forbid-agent-atlas` policy and the
`demo-zone-policies` set with a baseline version and a forbid version) live
in the zone and already exist; the API reuses them. `uv run python -m
infra.demo_policy status` prints the same state from the terminal.

Logs: `make app-logs`. Stop everything: `make demo-stop`.

## 6. Run the beat once

Open http://localhost:5173. The Keycard policy switch sits above the search
bar and reads Allowed.

1. Ask: `According to the knowledge base, which training courses and
   podcasts are listed for Temporal?` Expect knowledge-base citations
   (`s3://temporal-datasources/...`) and a trajectory of two searches and
   two reranks.
2. Flip the switch to Forbidden. It turns red within a second or two; that
   activated the policy set version carrying `forbid-agent-atlas`. The
   application's dependency on Atlas is untouched.
3. Ask the same question. Expect one "Searching the docs…" step, then a red
   "Knowledge base access denied by Keycard policy" step, a red callout with
   the zone's message (`Access to "MongoDB Atlas" is denied by Policy
   "forbid-agent-atlas" in version <n> of Policy Set "demo-zone-policies"`), and
   a web-only answer.
4. Flip back to Allowed and ask again. Citations return, nothing restarted.

Also open http://localhost:8233 (each answer links its workflow run; the
denied run shows `vector_search_tool` failing once, non-retryable) and the
Keycard console's audit log for the zone, where the denied and allowed mints
sit next to each other.

The full on-stage script, including the worker-kill and rotation beats, is in
[keycard-demo-runbook.md](keycard-demo-runbook.md).

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `demo-start` stops at "worker did not connect" | Temporal not up, or `uv sync` incomplete | `tail .local/worker.log`; run `temporal server start-dev` by hand to see the error |
| Worker log says `CredentialDiscoveryError` or the switch 502s | `.env` missing `KEYCARD_*` lines, or CLI not signed in | Redo step 4, then `keycard auth signin` and `make demo-stop && make demo-start` |
| Answer with access has no `s3://` citations, or the `bootstrap-indexes` workflow in the Temporal UI keeps retrying with a Mongo connection error | Atlas network access list blocks your IP | Ask the cluster owner to allow your IP (or the venue's); the vaulted URI is fine |
| `KeycardAccessDenied` while the switch says Allowed | Policy set out of sync | `uv run python -m infra.demo_policy status`, then `restore` |
| Agent says "Research agent unavailable" | Worker not in Keycard mode and no `OPENAI_API_KEY` | Check `KEYCARD_ZONE_URL` in `.env`, restart |
| The switch is missing from the page | Agent API not in Keycard mode, or UI can't reach :8090 | `curl localhost:8090/keycard/access`; check `.local/agent-api.log` |

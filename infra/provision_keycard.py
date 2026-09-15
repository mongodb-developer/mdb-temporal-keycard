"""One-shot Keycard zone provisioning for the demo, via `keycard agent api`.

Creates, in the zone named by KEYCARD_PROVISION_ZONE_ID:

  1. an application for the Temporal worker, with a client-secret credential
  2. three resources on the zone's Keycard Vault provider:
       KEYCARD_MONGODB_RESOURCE  -> vaulted MongoDB connection string
       KEYCARD_VOYAGE_RESOURCE   -> vaulted Voyage API key
       KEYCARD_OPENAI_RESOURCE   -> vaulted OpenAI API key
  3. the three resources as dependencies of the application

Auth rides the signed-in Keycard CLI session (`keycard auth signin`); every
call goes through `keycard agent api`, the CLI's authenticated Management API
passthrough. Nothing here touches tokens directly.

Secrets come from MONGODB_URI / VOYAGE_API_KEY / OPENAI_API_KEY env vars. A
missing value still creates the resource and dependency and only skips the
vault write, so the skeleton can be provisioned before the secrets exist.

The client credential is written straight into the repo's .env (created or
appended, never printed). Re-runs are idempotent: existing application,
resources, and dependencies are reused, and a credential is only minted when
.env does not already carry one.

Usage:  uv run python -m infra.provision_keycard
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.parse
from pathlib import Path

ZONE_ID = os.environ.get("KEYCARD_PROVISION_ZONE_ID", "bsq01zgq46reqv1l2fj7hgjhgt")
ORG_ID = os.environ.get("KEYCARD_PROVISION_ORG_ID", "m4pm31dpupr5y8lm99n90n2xv9")
APP_NAME = os.environ.get("KEYCARD_PROVISION_APP_NAME", "temporal-pipeline-worker")
CLI_TIMEOUT_SECONDS = int(os.environ.get("KEYCARD_CLI_TIMEOUT", "15"))
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

RESOURCES = [
    # (env var for the secret value, resource identifier env, default identifier, name)
    ("MONGODB_URI", "KEYCARD_MONGODB_RESOURCE", "https://cluster.mongodb.net", "MongoDB Atlas"),
    ("VOYAGE_API_KEY", "KEYCARD_VOYAGE_RESOURCE", "https://api.voyageai.com", "Voyage AI"),
    ("OPENAI_API_KEY", "KEYCARD_OPENAI_RESOURCE", "https://api.openai.com", "OpenAI"),
]


def api(method: str, path: str, body: dict | None = None) -> dict:
    cmd = ["keycard", "agent", "api", path, "-X", method, "--zone", ZONE_ID, "--org", ORG_ID]
    if body is not None:
        cmd += ["-d", json.dumps(body)]
    # The CLI has been seen to stall on a first call and answer the retry at
    # once, so one retry is cheap insurance for a live demo.
    for attempt in range(2):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=CLI_TIMEOUT_SECONDS)
            break
        except subprocess.TimeoutExpired:
            if attempt == 1:
                return {"_failed": True,
                        "cli_error": f"keycard agent api did not answer within {CLI_TIMEOUT_SECONDS}s twice; "
                                     "check network and `keycard auth whoami --zone <zone-id>`"}
    text = out.stdout.strip()
    try:
        payload = json.loads(text) if text else {}
    except json.JSONDecodeError:
        payload = {"raw": text[:300]}
    if out.returncode != 0:
        payload.setdefault("cli_error", out.stderr.strip()[:300])
        payload["_failed"] = True
    if isinstance(payload, dict) and payload.get("status", 0) >= 400:
        payload["_failed"] = True
    return payload if isinstance(payload, dict) else {"items": payload}


def must(payload: dict, what: str) -> dict:
    if payload.get("_failed"):
        sys.exit(f"{what} failed: {json.dumps(payload)[:400]}")
    return payload


def find_items(payload: dict) -> list[dict]:
    if isinstance(payload.get("items"), list):
        return payload["items"]
    for key in ("data", "results"):
        if isinstance(payload.get(key), list):
            return payload[key]
    return []


def env_values() -> dict[str, str]:
    values: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                values[k.strip()] = v.strip()
    return values


def append_env(lines: list[str]) -> None:
    with ENV_FILE.open("a") as f:
        f.write("\n# --- added by infra/provision_keycard.py ---\n")
        f.write("\n".join(lines) + "\n")


def main() -> None:
    zone = must(api("GET", f"/zones/{ZONE_ID}"), f"GET zone {ZONE_ID}")
    print(f"zone: {zone.get('name', ZONE_ID)}")

    provs = must(api("GET", f"/zones/{ZONE_ID}/providers?type=keycard-vault"), "list providers")
    vaults = [p for p in find_items(provs) if p.get("type") == "keycard-vault"]
    if vaults:
        vault_id = vaults[0]["id"]
    else:
        created = must(api("POST", f"/zones/{ZONE_ID}/providers",
                           {"name": "Keycard Vault", "type": "keycard-vault"}),
                       "create vault provider")
        vault_id = created["id"]
    print(f"vault provider: {vault_id}")

    apps = must(api("GET", f"/zones/{ZONE_ID}/applications"), "list applications")
    existing = [a for a in find_items(apps) if a.get("name") == APP_NAME]
    if existing:
        app = existing[0]
        print(f"application (existing): {app['id']}")
    else:
        app = must(api("POST", f"/zones/{ZONE_ID}/applications",
                       {"name": APP_NAME, "identifier": APP_NAME}),
                   "create application")
        print(f"application: {app['id']}")

    env = env_values()
    new_env: list[str] = []
    if env.get("KEYCARD_CLIENT_ID") and env.get("KEYCARD_CLIENT_SECRET"):
        print(f"client credential (existing in .env): {env['KEYCARD_CLIENT_ID']}")
    else:
        cred = must(api("POST", f"/zones/{ZONE_ID}/application-credentials",
                        {"application_id": app["id"], "type": "password"}),
                    "create application credential")
        client_id, client_secret = cred.get("identifier"), cred.get("password")
        if not (client_id and client_secret):
            sys.exit(f"credential response missing identifier/password: keys={sorted(cred)}")
        print(f"client credential (minted, written to .env): {client_id}")
        new_env += [
            f"KEYCARD_ZONE_URL=https://{ZONE_ID}.keycard.cloud",
            f"KEYCARD_CLIENT_ID={client_id}",
            f"KEYCARD_CLIENT_SECRET={client_secret}",
        ]

    for secret_env, ident_env, default_ident, name in RESOURCES:
        identifier = os.environ.get(ident_env) or env.get(ident_env) or default_ident

        found = must(api("GET", f"/zones/{ZONE_ID}/resources?filter[identifier]="
                                + urllib.parse.quote(identifier, safe="")),
                     f"list resources for {identifier}")
        hits = [r for r in find_items(found) if r.get("identifier") == identifier]
        if hits:
            res = hits[0]
            print(f"resource (existing): {identifier} -> {res['id']}")
        else:
            res = must(api("POST", f"/zones/{ZONE_ID}/resources",
                           {"name": name, "identifier": identifier,
                            "credential_provider_id": vault_id}),
                       f"create resource {identifier}")
            print(f"resource: {identifier} -> {res['id']}")

        value = os.environ.get(secret_env, "")
        if value:
            secret = must(api("POST", f"/zones/{ZONE_ID}/secrets",
                              {"name": f"{name} credential", "entity_id": res["id"],
                               "data": {"type": "token", "token": value}}),
                          f"vault secret for {identifier}")
            print(f"vaulted: {identifier} (secret {secret.get('id', '?')})")
        else:
            print(f"vault write skipped for {identifier} ({secret_env} not set)")

        dep = api("PUT", f"/zones/{ZONE_ID}/applications/{app['id']}/dependencies/{res['id']}")
        if dep.get("_failed") and dep.get("status") != 409:
            must(dep, f"dependency {identifier}")
        print(f"dependency: {APP_NAME} -> {identifier}")

        if ident_env not in env:
            new_env.append(f"{ident_env}={identifier}")

    if new_env:
        append_env(new_env)
        print(f"\nwrote {len(new_env)} line(s) to {ENV_FILE} (secrets never printed)")
    else:
        print("\n.env already complete; nothing written")


if __name__ == "__main__":
    main()

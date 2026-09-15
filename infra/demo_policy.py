"""Forbid or allow the worker's access to one vaulted resource by Keycard policy, live.

  uv run python -m infra.demo_policy setup     # one-time: create the forbid policy and the demo policy set
  uv run python -m infra.demo_policy deny      # activate the set version that carries the forbid
  uv run python -m infra.demo_policy restore   # activate the baseline set version (defaults only)
  uv run python -m infra.demo_policy status    # which set version is active, and what that means
  uv run python -m infra.demo_policy reset     # hand the zone back to the platform default set
  uv run python -m infra.demo_policy tidy      # archive stray versions left by earlier runs

Defaults to the MongoDB resource (KEYCARD_MONGODB_RESOURCE in .env); pick
another with --resource <identifier>. The agent UI's Keycard switch calls the
same functions through agent/api.py.

How it works. The zone's default policy set permits an application direct
access to the resources in its dependency list. This module leaves that list
alone and adds a customer policy set with two immutable versions: a baseline
that pins exactly the managed default policies, and a second one that adds

    @id("forbid-agent-atlas")
    forbid (principal is Keycard::Application, action, resource is Keycard::Resource)
    when { principal.identifier == "temporal-pipeline-worker" &&
           resource.identifier == "https://cluster.mongodb.net" };

Cedar forbids win over permits, so activating the second version denies the
worker the Atlas credential even though the application is still entitled to
it. The token endpoint then names the determining policy in its error, which
is what the agent's KeycardAccessDenied carries onto the stage. Activation is
atomic and takes effect on the worker's next mint.

Rides the signed-in Keycard CLI session (`keycard auth signin`) through
`keycard agent api`, like provision_keycard.py. Prints ids and statuses only.

`--mode dependency` keeps the older mechanism (drop and re-add the resource on
the application's dependency list) as a fallback.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse

from .provision_keycard import APP_NAME, ZONE_ID, api, env_values, find_items

POLICY_NAME = os.environ.get("KEYCARD_DEMO_FORBID_POLICY", "forbid-agent-atlas")
POLICY_SET_NAME = os.environ.get("KEYCARD_DEMO_POLICY_SET", "demo-zone-policies")
DEFAULT_SET_NAME = "default-zone-policies"
SCHEMA_VERSION = os.environ.get("KEYCARD_POLICY_SCHEMA_VERSION", "2026-06-18")


class PolicyError(RuntimeError):
    """A Keycard API call failed; the message is safe to show a user."""


def _ok(payload: dict, what: str) -> dict:
    if payload.get("_failed"):
        raise PolicyError(f"{what} failed: {json.dumps(payload)[:300]}")
    return payload


def default_resource() -> str | None:
    return os.environ.get("KEYCARD_MONGODB_RESOURCE") or env_values().get("KEYCARD_MONGODB_RESOURCE")


# --- zone objects -----------------------------------------------------------

def resolve_app() -> dict:
    apps = _ok(api("GET", f"/zones/{ZONE_ID}/applications"), "list applications")
    hits = [a for a in find_items(apps) if a.get("name") == APP_NAME]
    if not hits:
        raise LookupError(f"application {APP_NAME!r} not found in zone {ZONE_ID}; run provision_keycard first")
    return hits[0]


def resolve_resource(identifier: str) -> dict:
    found = _ok(
        api("GET", f"/zones/{ZONE_ID}/resources?filter[identifier]="
                   + urllib.parse.quote(identifier, safe="")),
        f"list resources for {identifier}",
    )
    hits = [r for r in find_items(found) if r.get("identifier") == identifier]
    if not hits:
        raise LookupError(f"resource {identifier!r} not found in zone {ZONE_ID}")
    return hits[0]


# --- policy toggle -----------------------------------------------------------

def forbid_cedar(app_identifier: str, resource_identifier: str) -> str:
    # Matches on the documented `identifier` attributes rather than entity ids,
    # so it reads the same in the console as in the zone's data model.
    return (
        f'@id("{POLICY_NAME}")\n'
        "forbid (\n"
        "  principal is Keycard::Application,\n"
        "  action,\n"
        "  resource is Keycard::Resource\n"
        ") when {\n"
        f'  principal.identifier == "{app_identifier}" &&\n'
        f'  resource.identifier == "{resource_identifier}"\n'
        "};"
    )


def _squash(text: str | None) -> str:
    """Whitespace- and parenthesis-free fingerprint.

    The API stores Cedar canonicalized (extra parentheses, `when` on its own
    line), so byte equality never holds against what we submit.
    """
    return re.sub(r"[\s()]", "", text or "")


def _entries(version: dict) -> list[dict]:
    return [
        {"policy_id": e["policy_id"], "policy_version_id": e["policy_version_id"]}
        for e in (version.get("manifest") or {}).get("entries", [])
    ]


def _same_entries(a: list[dict], b: list[dict]) -> bool:
    key = lambda e: (e["policy_id"], e["policy_version_id"])
    return sorted(map(key, a)) == sorted(map(key, b))


def _policy_sets() -> list[dict]:
    return find_items(_ok(api("GET", f"/zones/{ZONE_ID}/policy-sets"), "list policy sets"))


def _set_versions(set_id: str) -> list[dict]:
    return find_items(_ok(api("GET", f"/zones/{ZONE_ID}/policy-sets/{set_id}/versions"),
                          "list policy set versions"))


def _baseline_entries() -> list[dict]:
    """The managed defaults at their newest versions on the current schema.

    The platform set's newest version pins them; its Cedar is byte-identical to
    what the zone runs today, so the baseline changes no decision.
    """
    default = next((s for s in _policy_sets() if s.get("name") == DEFAULT_SET_NAME), None)
    if not default:
        raise LookupError(f"{DEFAULT_SET_NAME} not found in zone {ZONE_ID}")
    versions = _set_versions(default["id"])
    on_schema = [v for v in versions if v.get("schema_version") == SCHEMA_VERSION]
    pick = max(on_schema or versions, key=lambda v: v.get("version", 0))
    return _entries(pick)


def ensure_toggle(resource_identifier: str) -> dict:
    """Create the forbid policy, its version, and the two-version demo set. Idempotent."""
    app = resolve_app()
    resolve_resource(resource_identifier)
    cedar = forbid_cedar(app.get("identifier") or APP_NAME, resource_identifier)

    policies = find_items(_ok(api("GET", f"/zones/{ZONE_ID}/policies"), "list policies"))
    policy = next((p for p in policies if p.get("name") == POLICY_NAME), None)
    if not policy:
        policy = _ok(api("POST", f"/zones/{ZONE_ID}/policies", {
            "name": POLICY_NAME,
            "description": f"Demo: forbid {APP_NAME} the {resource_identifier} credential",
        }), "create policy")

    versions = find_items(_ok(api("GET", f"/zones/{ZONE_ID}/policies/{policy['id']}/versions"),
                              "list policy versions"))
    live = sorted((v for v in versions if not v.get("archived_at")), key=lambda v: -v.get("version", 0))
    pv = next((v for v in live if _squash(v.get("cedar_raw")) == _squash(cedar)), None)
    if not pv:
        pv = _ok(api("POST", f"/zones/{ZONE_ID}/policies/{policy['id']}/versions",
                     {"cedar_raw": cedar, "schema_version": SCHEMA_VERSION}), "create policy version")

    pset = next((s for s in _policy_sets() if s.get("name") == POLICY_SET_NAME), None)
    if not pset:
        pset = _ok(api("POST", f"/zones/{ZONE_ID}/policy-sets",
                       {"name": POLICY_SET_NAME, "scope_type": "zone"}), "create policy set")

    baseline = _baseline_entries()
    with_forbid = baseline + [{"policy_id": policy["id"], "policy_version_id": pv["id"]}]
    set_versions = _set_versions(pset["id"])

    def find_or_create(entries: list[dict], what: str) -> dict:
        hit = next((v for v in set_versions if _same_entries(_entries(v), entries) and not v.get("archived_at")), None)
        if hit:
            return hit
        created = _ok(api("POST", f"/zones/{ZONE_ID}/policy-sets/{pset['id']}/versions",
                          {"manifest": {"entries": entries}, "schema_version": SCHEMA_VERSION}),
                      f"create {what} set version")
        set_versions.append(created)
        return created

    baseline_v = find_or_create(baseline, "baseline")
    forbid_v = find_or_create(with_forbid, "forbid")
    return {
        "policy_id": policy["id"],
        "policy_version_id": pv["id"],
        "policy_set_id": pset["id"],
        "baseline_version_id": baseline_v["id"],
        "baseline_version": baseline_v.get("version"),
        "forbid_version_id": forbid_v["id"],
        "forbid_version": forbid_v.get("version"),
    }


def _active() -> tuple[dict | None, dict | None]:
    """(active policy set, its active version), or (None, None)."""
    for s in _policy_sets():
        if s.get("active"):
            for v in _set_versions(s["id"]):
                if v.get("active"):
                    return s, v
            return s, None
    return None, None


def _activate(set_id: str, version_id: str) -> None:
    _ok(api("PATCH", f"/zones/{ZONE_ID}/policy-sets/{set_id}/versions/{version_id}", {"active": True}),
        "activate policy set version")


def policy_state(resource_identifier: str) -> dict:
    t = ensure_toggle(resource_identifier)
    s, v = _active()
    forbidden = bool(s and v and s["id"] == t["policy_set_id"] and v["id"] == t["forbid_version_id"])
    return {
        "mechanism": "policy",
        "application": APP_NAME,
        "resource": resource_identifier,
        "policy": POLICY_NAME,
        "policy_set": s.get("name") if s else None,
        "policy_set_version": v.get("version") if v else None,
        "allowed": not forbidden,
    }


def set_policy(resource_identifier: str, allowed: bool) -> dict:
    t = ensure_toggle(resource_identifier)
    _activate(t["policy_set_id"], t["baseline_version_id"] if allowed else t["forbid_version_id"])
    return {
        "mechanism": "policy",
        "application": APP_NAME,
        "resource": resource_identifier,
        "policy": POLICY_NAME,
        "policy_set": POLICY_SET_NAME,
        "policy_set_version": t["baseline_version"] if allowed else t["forbid_version"],
        "allowed": allowed,
    }


def reset_to_default() -> dict:
    """Re-activate the platform default set at the version it ran before the demo."""
    default = next((s for s in _policy_sets() if s.get("name") == DEFAULT_SET_NAME), None)
    if not default:
        raise LookupError(f"{DEFAULT_SET_NAME} not found")
    versions = _set_versions(default["id"])
    on_schema = [v for v in versions if v.get("schema_version") == SCHEMA_VERSION]
    pick = max(on_schema or versions, key=lambda v: v.get("version", 0))
    _activate(default["id"], pick["id"])
    return {"policy_set": DEFAULT_SET_NAME, "policy_set_version": pick.get("version"), "allowed": True}


def tidy(resource_identifier: str) -> dict:
    """Archive stray versions left by earlier runs: policy versions of the forbid
    policy other than the current one, and demo set versions other than the
    baseline and forbid pair. The active set version is never archived."""
    t = ensure_toggle(resource_identifier)
    archived = {"policy_versions": 0, "set_versions": 0}
    for v in _set_versions(t["policy_set_id"]):
        if v["id"] in (t["baseline_version_id"], t["forbid_version_id"]) or v.get("active") or v.get("archived_at"):
            continue
        out = api("DELETE", f"/zones/{ZONE_ID}/policy-sets/{t['policy_set_id']}/versions/{v['id']}")
        if not out.get("_failed"):
            archived["set_versions"] += 1
    versions = find_items(_ok(api("GET", f"/zones/{ZONE_ID}/policies/{t['policy_id']}/versions"),
                              "list policy versions"))
    for v in versions:
        if v["id"] == t["policy_version_id"] or v.get("archived_at"):
            continue
        out = api("DELETE", f"/zones/{ZONE_ID}/policies/{t['policy_id']}/versions/{v['id']}")
        if not out.get("_failed"):
            archived["policy_versions"] += 1
    return archived


# --- dependency toggle (fallback) --------------------------------------------

def has_dependency(app_id: str, res_id: str) -> bool | None:
    deps = api("GET", f"/zones/{ZONE_ID}/applications/{app_id}/dependencies")
    if deps.get("_failed"):
        return None
    return any(d.get("id") == res_id or d.get("resource_id") == res_id for d in find_items(deps))


def dependency_state(resource_identifier: str) -> dict:
    app = resolve_app()
    res = resolve_resource(resource_identifier)
    return {"mechanism": "dependency", "application": APP_NAME, "resource": resource_identifier,
            "allowed": has_dependency(app["id"], res["id"])}


def set_dependency(resource_identifier: str, allowed: bool) -> dict:
    app = resolve_app()
    res = resolve_resource(resource_identifier)
    path = f"/zones/{ZONE_ID}/applications/{app['id']}/dependencies/{res['id']}"
    out = api("PUT" if allowed else "DELETE", path)
    if out.get("_failed") and out.get("status") not in (404, 409):
        _ok(out, "update dependency")
    return {"mechanism": "dependency", "application": APP_NAME, "resource": resource_identifier,
            "allowed": allowed}


# --- public entry points used by agent/api.py --------------------------------

def access_state(resource_identifier: str, mode: str = "policy") -> dict:
    return policy_state(resource_identifier) if mode == "policy" else dependency_state(resource_identifier)


def set_access(resource_identifier: str, allowed: bool, mode: str = "policy") -> dict:
    return set_policy(resource_identifier, allowed) if mode == "policy" else set_dependency(resource_identifier, allowed)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["setup", "deny", "restore", "status", "reset", "tidy"])
    parser.add_argument("--resource", default=default_resource(),
                        help="resource identifier (default: KEYCARD_MONGODB_RESOURCE)")
    parser.add_argument("--mode", choices=["policy", "dependency"], default="policy")
    args = parser.parse_args()
    if not args.resource:
        sys.exit("no resource identifier: set KEYCARD_MONGODB_RESOURCE in .env or pass --resource")

    try:
        if args.action == "setup":
            t = ensure_toggle(args.resource)
            print(f"policy {POLICY_NAME}: {t['policy_id']} (version {t['policy_version_id']})")
            print(f"policy set {POLICY_SET_NAME}: {t['policy_set_id']}")
            print(f"  baseline version {t['baseline_version']}: managed defaults only")
            print(f"  forbid version   {t['forbid_version']}: defaults + {POLICY_NAME}")
            print("run `status` to see which is active; `deny` and `restore` switch between them")
            return
        if args.action == "tidy":
            n = tidy(args.resource)
            print(f"archived {n['set_versions']} stray set version(s) and {n['policy_versions']} stray policy version(s)")
            return
        if args.action == "reset":
            state = reset_to_default()
            print(f"active set: {state['policy_set']} v{state['policy_set_version']} (platform default)")
            return
        if args.action == "status":
            state = access_state(args.resource, args.mode)
        else:
            state = set_access(args.resource, allowed=(args.action == "restore"), mode=args.mode)
    except (LookupError, PolicyError) as e:
        sys.exit(str(e))

    print(f"application: {state['application']}")
    print(f"resource:    {state['resource']}")
    if state["mechanism"] == "policy":
        print(f"active set:  {state.get('policy_set')} v{state.get('policy_set_version')}")
        verdict = "ALLOWED" if state["allowed"] else f"FORBIDDEN by {state['policy']}"
        print(f"access:      {verdict}; the worker's next mint sees it")
    elif state["allowed"] is None:
        print("dependency: unknown (list endpoint unavailable)")
    else:
        print(f"dependency: {'PRESENT (access allowed)' if state['allowed'] else 'ABSENT (access denied)'}")


if __name__ == "__main__":
    main()

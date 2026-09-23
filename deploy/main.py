"""Landible deploy shim.

Runs natively on the host (not in Docker — it calls `docker compose` on the host
and `git` against the host checkout at /opt/landible). Receives a Bearer-
authenticated POST (e.g. from n8n on every GitHub push to main), then converges
the box to origin/main and rolls the compose stack:

    git -C /opt/landible fetch origin --prune
    git -C /opt/landible reset --hard origin/main      # pure mirror
    docker compose -f compose/docker-compose.yml pull
    docker compose -f compose/docker-compose.yml up -d --remove-orphans

The native services that run from this same checkout (this shim, and landible-mcp)
aren't in compose, so they self-update after the reset: if their code/deps
changed, the shim re-syncs + restarts them (landible-mcp synchronously; itself via
a deferred systemd-run so it survives killing itself). See _redeploy_mcp.

Two deliberate choices:
  * reset --hard origin/main (not pull --rebase) — the box must be a pure mirror,
    so a checkout can never wedge on a local change.
  * no `compose build` step — every image is pinned, nothing is built locally.

reset --hard only rewrites tracked files; the gitignored runtime state (.env,
*-data/ volumes, deploy/state/) is never touched. Unit files are never touched
either: install/refresh them with scripts/install-units.sh.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

PROJECT_DIR = Path(os.environ.get("LANDIBLE_PROJECT_DIR", "/opt/landible"))
COMPOSE_FILE = PROJECT_DIR / "compose" / "docker-compose.yml"

# landible-mcp is a NATIVE uv+systemd service (not in compose) that runs straight
# from the checkout at PROJECT_DIR/mcp — same as this shim runs from /deploy. So
# after a reset we re-sync its deps + restart it (see _redeploy_mcp). uv lives in
# root's home; override UV_BIN only if it moves.
MCP_DIR = PROJECT_DIR / "mcp"
UV_BIN = os.environ.get("UV_BIN", "/root/.local/bin/uv")

# Outbound webhook targets (operator data with embedded secrets), managed over
# MCP and fed by the book pollers in mcp/systemd — see the WEBHOOKS section
# at the bottom of this file. Stored under a gitignored state dir so the deploy
# `git reset --hard` (tracked files only) never clobbers it.
STATE_DIR = PROJECT_DIR / "deploy" / "state"
WEBHOOKS_FILE = STATE_DIR / "webhooks.json"

app = FastAPI(title="landible-deploy")

# uvicorn routes stdlib logging to its own handlers, so this lands in
# `journalctl -u landible-deploy`.
log = logging.getLogger("landible-deploy")

# Serializes deploys: a second concurrent POST /api/deploy (double-push, n8n
# retry) gets 409 instead of racing git reset --hard + compose up on one tree.
_deploy_lock = threading.Lock()

# Long-running compose services /api/health expects to be `up`: every service
# in compose/docker-compose.yml. A new service belongs here too, or its outage
# never shows as `degraded`.
EXPECTED_SERVICES = {
    "audiobookshelf", "libation", "qbittorrent-mam", "chaptarr",
    "prowlarr", "flaresolverr", "autoheal",
}

# Files whose change means the running shim (native uvicorn, no --reload) is now
# stale and must restart to pick up new code/deps. The .service unit is NOT here:
# applying a unit change needs `systemctl daemon-reload` (scripts/install-units.sh).
_SHIM_FILES = ["deploy/main.py", "deploy/pyproject.toml", "deploy/uv.lock"]

# Same idea for the native landible-mcp service: a change here means re-sync deps +
# restart it. mcp/systemd/*.service is excluded for the same reason as the shim's
# own unit above (a unit change needs daemon-reload: scripts/install-units.sh).
_MCP_PATHS = ["mcp/src", "mcp/pyproject.toml", "mcp/uv.lock"]


def _require_bearer(request: Request) -> None:
    """Bearer-token check. Returns silently on auth; raises 401/503 otherwise."""
    token = os.environ.get("DEPLOY_TOKEN", "")
    if not token:
        raise HTTPException(503, "DEPLOY_TOKEN not configured")
    if not hmac.compare_digest(request.headers.get("authorization", ""), f"Bearer {token}"):
        raise HTTPException(401, "invalid deploy token")


def _compose_ps() -> dict[str, dict]:
    """Per-service {state, health} from `docker compose ps`, keyed by service.
    `--format json` emits one object per line; `--all` so a crashed container
    shows as exited rather than vanishing."""
    out = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), "ps", "--format", "json", "--all"],
        capture_output=True, text=True, timeout=15,
    )
    out.check_returncode()
    services: dict[str, dict] = {}
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        name = row.get("Service")
        if name:
            services[name] = {"state": row.get("State"), "health": row.get("Health") or None}
    return services


def _read_commit() -> str | None:
    """Short git SHA of the deployed code, read fresh each call (~5ms) to avoid
    the stale-cache footgun where the shim reports yesterday's commit."""
    try:
        out = subprocess.run(
            ["git", "-C", str(PROJECT_DIR), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=2,
        )
        return out.stdout.strip() or None
    except Exception:
        return None


def _paths_changed(before: str | None, after: str | None, paths: list[str]) -> bool:
    """True if the just-pulled commits touched any of `paths` (files or dirs)."""
    if not before or not after or before == after:
        return False
    out = subprocess.run(
        ["git", "-C", str(PROJECT_DIR), "diff", "--name-only", before, after, "--", *paths],
        capture_output=True, text=True, timeout=10,
    )
    return out.returncode == 0 and bool(out.stdout.strip())


def _shim_files_changed(before: str | None, after: str | None) -> bool:
    """True if the just-pulled commits changed the shim's own runtime files."""
    return _paths_changed(before, after, _SHIM_FILES)


def _redeploy_mcp() -> dict:
    """Re-sync deps + restart the native landible-mcp service. Returns a step dict.

    landible-mcp is a SEPARATE systemd unit, so (unlike the shim's own restart)
    this runs synchronously and reports the outcome. Best-effort: a failure comes
    back as status=error rather than raising, so a broken mcp deploy does NOT 500
    the compose deploy that already succeeded — the error is visible in the
    response + journal, but the stack roll still counts as done.
    """
    steps = [
        ([UV_BIN, "sync", "--frozen"], "mcp uv sync", str(MCP_DIR)),
        (["systemctl", "restart", "landible-mcp"], "mcp restart", None),
    ]
    for cmd, label, cwd in steps:
        try:
            out = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=300)
            if out.returncode != 0:
                log.error("mcp redeploy FAILED at '%s' (rc=%s)", label, out.returncode)
                return {"step": label, "status": "error",
                        "output": (out.stdout + out.stderr).strip()[-500:]}
        except Exception as e:
            log.error("mcp redeploy FAILED at '%s': %s", label, e)
            return {"step": label, "status": "error", "output": str(e)}
    return {"step": "mcp redeploy", "status": "ok"}


def _schedule_self_restart() -> str:
    """Detached, deferred restart of this shim so it survives killing itself.
    --on-active=2 lets the deploy response return first; --collect GCs the
    transient unit. Best-effort: failures come back in the note, never raise."""
    try:
        subprocess.run(
            ["systemd-run", "--on-active=2", "--collect", "--quiet",
             "systemctl", "restart", "landible-deploy"],
            check=True, capture_output=True, text=True, timeout=10,
        )
        return "shim code changed — self-restart scheduled (~2s)"
    except Exception as e:
        stderr = getattr(e, "stderr", "") or str(e)
        return f"shim code changed but self-restart scheduling failed: {stderr.strip()}"


@app.get("/api/health")
def health() -> JSONResponse:
    """Stack-aware health for the n8n watchdog. `degraded` if any expected
    service isn't running or is `unhealthy`; a running service with no
    healthcheck counts as ok. Always HTTP 200 so a watchdog can tell
    'shim answered, stack degraded' from 'shim is down'."""
    base = {"commit": _read_commit(), "timestamp": datetime.now(timezone.utc).isoformat()}
    try:
        services = _compose_ps()
    except Exception as e:
        return JSONResponse({"status": "unknown", "error": str(e), **base})
    containers, degraded = [], False
    for name in sorted(EXPECTED_SERVICES):
        svc = services.get(name)
        if svc is None:
            containers.append({"name": name, "state": "missing", "health": None})
            degraded = True
            continue
        containers.append({"name": name, **svc})
        if svc["state"] != "running" or svc["health"] == "unhealthy":
            degraded = True
    return JSONResponse({"status": "degraded" if degraded else "ok", "containers": containers, **base})


@app.post("/api/deploy")
def deploy(request: Request) -> JSONResponse:
    """Converge to origin/main and roll the compose stack. Bearer-authed.
    Steps run in order; first failure short-circuits with HTTP 500. Output is
    tail-truncated to 500 chars/step so n8n's 'last response' stays readable.

    A plain `def` (not `async`) so FastAPI runs it in a threadpool — the blocking
    subprocess calls below would otherwise stall the event loop for the whole
    deploy, hanging /api/health (the watchdog endpoint) along with it.
    """
    _require_bearer(request)
    if not _deploy_lock.acquire(blocking=False):
        raise HTTPException(409, "a deploy is already running")
    try:
        sha_before = _read_commit()
        log.info("deploy start (at %s)", sha_before)

        steps = [
            (["git", "-C", str(PROJECT_DIR), "fetch", "origin", "--prune"], "git fetch"),
            (["git", "-C", str(PROJECT_DIR), "reset", "--hard", "origin/main"], "git reset --hard origin/main"),
            (["docker", "compose", "-f", str(COMPOSE_FILE), "pull"], "compose pull"),
            (["docker", "compose", "-f", str(COMPOSE_FILE), "up", "-d", "--remove-orphans"], "compose up -d"),
        ]

        results: list[dict] = []
        for cmd, label in steps:
            try:
                out = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
                results.append({
                    "step": label,
                    "status": "ok" if out.returncode == 0 else "error",
                    "output": (out.stdout + out.stderr).strip()[-500:],
                })
                if out.returncode != 0:
                    log.error("deploy FAILED at '%s' (rc=%s)", label, out.returncode)
                    return JSONResponse({"status": "error", "results": results}, status_code=500)
            except Exception as e:
                results.append({"step": label, "status": "error", "output": str(e)})
                log.error("deploy FAILED at '%s': %s", label, e)
                return JSONResponse({"status": "error", "results": results}, status_code=500)

        sha_after = _read_commit()

        # The native landible-mcp service runs from the checkout but compose never
        # touches it — re-sync + restart it when its code/deps changed.
        mcp = _redeploy_mcp() if _paths_changed(sha_before, sha_after, _MCP_PATHS) else None

        # The stack is refreshed, but THIS shim (native uvicorn, no --reload) still
        # runs the pre-reset code. If the reset changed the shim's own files, restart.
        note = _schedule_self_restart() if _shim_files_changed(sha_before, sha_after) else None
        log.info(
            "deploy ok (%s -> %s)%s%s", sha_before, sha_after,
            " [mcp redeploy]" if mcp else "", " [self-restart]" if note else "",
        )
        body = {"status": "ok", "results": results}
        if mcp:
            body["mcp"] = mcp
        if note:
            body["note"] = note
        return JSONResponse(body)
    finally:
        _deploy_lock.release()


# ===========================================================================
# WEBHOOKS — outbound event dispatch (Home Assistant or any endpoint)
# ===========================================================================
#
# Relay stack events ("a book is ready", "the MAM account is at risk") to
# operator-configured HTTP targets. The source is the pollers in mcp/systemd
# (book_events, mam_health, mam_stats, digest, unit_health), which POST one
# already-deduped event each to /api/events/books.
# Targets live in the gitignored deploy/state/webhooks.json; these endpoints are
# the only writer because landible-mcp has no access to this host's filesystem.


def _load_webhooks() -> list:
    """Read webhooks.json as a list. Missing file = no targets (normal, silent).

    A file that EXISTS but won't parse is logged LOUDLY before returning [] —
    silently swallowing it would drop every notification with zero trace, the
    exact "looks healthy, does nothing" failure we keep getting bitten by.
    """
    if not WEBHOOKS_FILE.exists():
        return []
    try:
        data = json.loads(WEBHOOKS_FILE.read_text())
    except (OSError, json.JSONDecodeError) as e:
        log.error("webhooks.json exists but is unreadable (%s) — delivering to NO targets", e)
        return []
    return data if isinstance(data, list) else []


def _write_webhooks(targets: list) -> None:
    # Atomic write (tmp + replace): a crash mid-write can't leave a half-written
    # file that _load_webhooks then reads as "no targets". Not serialized against
    # concurrent writers, which is fine — the only writer is one operator via MCP.
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = WEBHOOKS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(targets, indent=2))
    tmp.replace(WEBHOOKS_FILE)


def _redact_url(url: str) -> str:
    """Mask the secret-bearing parts of a target URL for list output.

    HA webhook URLs embed the secret in the path (.../api/webhook/<id>) and some
    targets carry it in the query string. Keep scheme + host so the target stays
    recognisable; drop path + query.
    """
    try:
        p = urlsplit(url)
    except ValueError:
        return "***"
    if not p.scheme or not p.netloc:
        return "***"
    path = "/***" if p.path and p.path != "/" else p.path
    return urlunsplit((p.scheme, p.netloc, path, "", ""))


def _targets_for_event(config: list, event: str) -> list:
    """Enabled targets subscribed to `event`."""
    return [
        t for t in config
        if isinstance(t, dict) and t.get("enabled", True) and event in (t.get("events") or [])
    ]


async def _fanout(event: str, data: dict) -> list[dict]:
    """POST {event, data, timestamp} to every enabled target subscribed to event.

    Fire-and-forget: a target that errors (or returns non-2xx) is logged AND
    recorded in the returned per-target result list rather than raising, so one
    dead endpoint can't fail the relay or the other targets. The log line is the
    only durable record a failed delivery leaves — don't drop it. No retry queue:
    the caller (a poller) retries when every target failed (see events_books).
    """
    targets = _targets_for_event(_load_webhooks(), event)
    if not targets:
        return []
    payload = {"event": event, "data": data, "timestamp": datetime.now(timezone.utc).isoformat()}
    results: list[dict] = []
    async with httpx.AsyncClient(timeout=10.0) as client:
        for t in targets:
            name = t.get("name")
            try:
                r = await client.post(t["url"], json=payload, headers=t.get("headers") or {})
                ok = 200 <= r.status_code < 300
                if not ok:
                    log.warning("webhook %r returned HTTP %s for %s", name, r.status_code, event)
                results.append({"name": name, "status": r.status_code, "ok": ok})
            except Exception as e:
                log.warning("webhook %r delivery failed for %s: %s", name, event, e)
                results.append({"name": name, "status": None, "ok": False, "error": str(e)})
    return results


def _require_inbound_secret(request: Request) -> None:
    """Validate the inbound relay shared secret (the pollers' Authorization header).

    The secret rides in an `Authorization` header — NOT the URL query. uvicorn's
    access log records the request path + query, so a `?token=<secret>` would
    leak the secret into journald on every event; a header doesn't. Accept the bare secret or a
    'Bearer '-prefixed form so it can be pasted either way. compare_digest keeps
    the check constant-time. Fail closed (503) if the secret isn't configured.
    """
    secret = os.environ.get("WEBHOOK_INBOUND_SECRET", "")
    if not secret:
        raise HTTPException(503, "WEBHOOK_INBOUND_SECRET not configured")
    presented = request.headers.get("authorization", "")
    if not (hmac.compare_digest(presented, secret)
            or hmac.compare_digest(presented, f"Bearer {secret}")):
        raise HTTPException(401, "invalid inbound webhook secret")


@app.get("/api/webhooks")
async def webhooks_list(request: Request) -> dict:
    """List configured webhook targets — URLs redacted (they embed secrets)."""
    _require_bearer(request)
    targets = _load_webhooks()
    redacted = [
        {
            "name": t.get("name"),
            "url": _redact_url(t.get("url", "")),
            "events": t.get("events") or [],
            "enabled": t.get("enabled", True),
            "has_headers": bool(t.get("headers")),
        }
        for t in targets
    ]
    return {"webhooks": redacted, "total": len(redacted)}


@app.put("/api/webhooks/{name}")
async def webhooks_upsert(name: str, request: Request) -> dict:
    """Upsert a target by name. Body: {url, events, headers?, enabled?}.

    Replaces any existing entry with the same name, so editing a target can't
    leave a stale duplicate behind.
    """
    _require_bearer(request)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "body must be a JSON object")
    url = body.get("url")
    events = body.get("events")
    if not url or not isinstance(url, str):
        raise HTTPException(400, "body must include a non-empty 'url' string")
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "url must be http(s)")
    if not isinstance(events, list) or not events:
        raise HTTPException(400, "body must include a non-empty 'events' list")
    entry = {
        "name": name,
        "url": url,
        "events": [str(e) for e in events],
        "headers": body.get("headers") or {},
        "enabled": bool(body.get("enabled", True)),
    }
    existing = _load_webhooks()
    kept = [t for t in existing if t.get("name") != name]
    kept.append(entry)
    _write_webhooks(kept)
    return {"status": "ok", "name": name, "events": entry["events"],
            "enabled": entry["enabled"], "total": len(kept)}


@app.delete("/api/webhooks/{name}")
async def webhooks_delete(name: str, request: Request) -> dict:
    """Delete a target by name. Idempotent — 'not_found' if it wasn't present."""
    _require_bearer(request)
    existing = _load_webhooks()
    kept = [t for t in existing if t.get("name") != name]
    if len(kept) == len(existing):
        return {"name": name, "status": "not_found", "total": len(kept)}
    _write_webhooks(kept)
    return {"name": name, "status": "deleted", "total": len(kept)}


@app.post("/api/webhooks/{name}/test")
async def webhooks_test(name: str, request: Request) -> JSONResponse:
    """Fire a synthetic landible.test event at one target and report delivery.

    Bypasses the enabled/subscription filter on purpose — this is a "does this
    endpoint work?" probe, so it should fire regardless of those flags.
    """
    _require_bearer(request)
    target = next((t for t in _load_webhooks() if t.get("name") == name), None)
    if not target:
        raise HTTPException(404, f"no webhook target named {name!r}")
    payload = {"event": "landible.test", "data": {"message": "landible webhook test"},
               "timestamp": datetime.now(timezone.utc).isoformat()}
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            r = await client.post(target["url"], json=payload, headers=target.get("headers") or {})
            return JSONResponse({"name": name, "delivered": 200 <= r.status_code < 300,
                                 "status": r.status_code, "response_tail": r.text[:200]})
        except Exception as e:
            return JSONResponse({"name": name, "delivered": False, "error": str(e)}, status_code=502)


# Audiobook events. Chaptarr's Webhook can't fire on download or import failure
# (supportsOnDownloadFailure/ImportFailure are false), so the source is the
# landible-* timers on this host (mcp/systemd/): they poll ABS, Chaptarr,
# qbittorrent-mam, MAM and systemd, and POST one already-deduped event each.
#
# Adding a new event: the poller emits it, it gets a line here, AND every target
# that should see it adds the landible.* name to its `events` list BEFORE the
# poller's first run. The relay answers 200 with `targets: 0` when nobody
# subscribes, and the pollers treat that as "not delivered" and fail their unit.
_BOOK_EVENT_MAP = {
    "book_ready": "landible.book_ready",      # Audiobookshelf has it — playable now
    "book_failed": "landible.book_failed",    # Chaptarr downloadFailed / bookImportIncomplete
    "book_stuck": "landible.book_stuck",      # a request not imported after 24 h
    "book_suspect": "landible.book_suspect",  # the imported file may not be the book asked for
    "mam_health": "landible.mam_health",      # landible-mam-health/-mam-stats timers: MAM account at risk
    "book_digest": "landible.book_digest",    # landible-book-digest timer: the weekly summary
    "unit_failed": "landible.unit_failed",    # landible-unit-health timer: a systemd unit is failed
}
_BOOK_EVENT_FIELDS = ("title", "author", "source", "message")


@app.post("/api/events/books")
async def events_books(request: Request) -> JSONResponse:
    """Inbound relay: the landible-* timers (mcp/systemd) -> fan-out.

    Body: {"event": <a _BOOK_EVENT_MAP key>, title, author, source, message?}.
    Only the allow-listed fields are passed on. Unknown events are acknowledged
    but not relayed.

    The caller retries: 502 when every subscribed
    target failed, so the poller keeps its mark and re-posts next run. A partial
    failure stays 200, since a retry would push the working target twice.
    """
    _require_inbound_secret(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "body must be JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "body must be a JSON object")
    name = body.get("event")
    event = _BOOK_EVENT_MAP.get(name) if isinstance(name, str) else None
    if not event:
        return JSONResponse({"status": "ignored", "event": name})
    data = {k: body.get(k) for k in _BOOK_EVENT_FIELDS}
    deliveries = await _fanout(event, data)
    all_failed = bool(deliveries) and not any(d["ok"] for d in deliveries)
    return JSONResponse({"status": "failed" if all_failed else "relayed", "event": event,
                         "targets": len(deliveries), "deliveries": deliveries},
                        status_code=502 if all_failed else 200)

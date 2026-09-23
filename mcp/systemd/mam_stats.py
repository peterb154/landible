#!/usr/bin/env python3
"""MAM account stats: poll MAM's "Load User Data" endpoint hourly.

Run by landible-mam-stats.timer. No model in the loop.

Calls GET /jsonLoad.php?snatch_summary (on MAM's approved-API list) from the
home IP with the `mam_id` cookie, then writes mam-stats.json from an ALLOW-LIST
of fields. The MCP's mam_stats tool and the weekly digest read that file; the
MCP never calls MAM and never sees the cookie.

The cookie (invariant 3): a dedicated IP-locked session (e.g. `landible-stats`), NOT Prowlarr's, kept
in MAM_COOKIE_FILE (root-only, outside /opt/landible). MAM rotates mam_id: a
Set-Cookie with a new value is written back to that file. The cookie is never
printed, never in argv, never in mam-stats.json.

Alerts (`mam_health` via the shim, once per condition, re-armed when it clears):
  rejected   MAM answered with HTML / 401 / 403: the session is dead, re-enter mam_id
  offline    MAM sees the client as not connectable ("no"/"offline"; "yes" = fine)
  hnr        MAM counts a hit & run

A network error is recorded in the file but doesn't alert (the next hour
retries; an internet outage would drop the push anyway). One request per run,
never a retry loop.

Pure helpers are unit-tested in tests/test_mam_stats.py; main() does the I/O.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone

MAM_URL = "https://www.myanonamouse.net/jsonLoad.php?snatch_summary"
COOKIE_FILE = os.environ.get("MAM_COOKIE_FILE", "/etc/landible/mam_id")
STATS_FILE = os.environ.get(
    "MAM_STATS_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "mam-stats.json"),
)

# Top-level scalars copied as-is. Anything not listed never reaches the file.
SCALARS = ("classname", "seedbonus", "ratio", "wedges", "vip_until", "connectable")
# Snatch-summary buckets: {"count": n}; `unsat` also has `limit`.
COUNTS = ("unsat", "leeching", "sSat", "seedUnsat", "seedHnr", "inactHnr", "inactUnsat", "inactSat")


class Rejected(Exception):
    """MAM refused the session (HTML instead of JSON, or 401/403)."""


# ---------------------------------------------------------------- pure helpers

def _int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def slim(raw: dict) -> dict:
    """The allow-listed stats from a jsonLoad.php body."""
    out = {k: raw.get(k) for k in SCALARS}
    out["uploaded_bytes"] = _int(raw.get("uploaded_bytes"))
    out["downloaded_bytes"] = _int(raw.get("downloaded_bytes"))
    for k in COUNTS:
        out[f"{k}_count"] = _int((raw.get(k) or {}).get("count"))
    out["unsat_limit"] = _int((raw.get("unsat") or {}).get("limit"))
    return out


def parse(body: bytes) -> dict:
    """The JSON body, or Rejected: MAM answers a dead session with an HTML page."""
    try:
        raw = json.loads(body)
    except ValueError:
        raise Rejected("MAM answered with HTML, not JSON")
    if not isinstance(raw, dict) or not raw.get("username"):
        raise Rejected("MAM's reply has no username")
    return raw


def rotated(set_cookies: list, current: str) -> str | None:
    """A new mam_id from the response's Set-Cookie headers, else None."""
    for header in set_cookies:
        name, _, rest = header.partition("=")
        if name.strip() == "mam_id":
            value = rest.split(";", 1)[0].strip()
            if value and value != current:
                return value
    return None


def conditions(stats: dict | None, error: str | None, rejected: bool) -> tuple[dict, set]:
    """(active conditions, conditions judged this run).

    A network error judges nothing; a rejection judges only `rejected`.
    """
    if rejected:
        return {"rejected": f"MAM session rejected ({error}): create a new IP-locked session and "
                            f"put its mam_id in {COOKIE_FILE}"}, {"rejected"}
    if stats is None:
        return {}, set()
    out = {}
    # Live MAM sends "yes"; "no" is the assumed negative, "offline" per
    # third-party docs. Any other value is ignored rather than guessed at.
    if stats.get("connectable") in ("no", "offline"):
        out["offline"] = "MAM sees qbittorrent-mam as not connectable (check the router port forward)"
    hnr = stats["seedHnr_count"] + stats["inactHnr_count"]
    if hnr:
        out["hnr"] = f"MAM counts {hnr} hit & run(s): seed them back to 72 h"
    return out, {"rejected", "offline", "hnr"}


def event(message: str) -> dict:
    return {"event": "mam_health", "title": "MAM account", "source": "mam", "message": message}


def run(stats: dict | None, error: str | None, rejected: bool, prev: dict | None, post,
        now: str) -> tuple[dict, bool]:
    """New file contents + whether every push landed.

    The last good stats stay in the file on a failed fetch; `error` and
    `fetched_at` tell readers how old they are.
    """
    prev = prev or {}
    alerts = dict(prev.get("alerts") or {})
    active, judged = conditions(stats, error, rejected)
    for key in judged - set(active):
        alerts.pop(key, None)
    ok = True
    try:
        for key, message in sorted(active.items()):
            if key not in alerts:
                post(event(message))
                print(f"[mam-stats] alert: {key}")
                alerts[key] = True
    except OSError as e:
        print(f"[mam-stats] ERROR push failed, retrying next run: {e}")
        ok = False
    if stats is not None:
        return {"stats": stats, "fetched_at": now, "error": None, "alerts": alerts}, ok
    return {"stats": prev.get("stats"), "fetched_at": prev.get("fetched_at"),
            "error": error, "failed_at": now, "alerts": alerts}, ok


# ---------------------------------------------------------------------- I/O

def _load(path: str) -> dict | None:
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def _write(path: str, text: str, mode: int) -> None:
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    os.fchmod(fd, mode)   # O_CREAT's mode doesn't apply to a leftover .tmp
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def fetch(cookie: str) -> tuple[dict, list]:
    """(raw body, Set-Cookie headers). Raises Rejected or OSError."""
    req = urllib.request.Request(MAM_URL, headers={
        "Cookie": f"mam_id={cookie}", "Accept": "application/json", "User-Agent": "landible-mam-stats",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return parse(r.read()), r.headers.get_all("Set-Cookie") or []
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise Rejected(f"HTTP {e.code}")
        raise


class NotDelivered(OSError):
    """The shim accepted the event and handed it to nobody.

    An OSError on purpose: the push is already wrapped in `except OSError`,
    which fails the unit and retries next run, making a missing subscription
    noisy and self-correcting instead of silently dropping the alert.
    """


def _post_event(shim_url: str, secret: str, ev: dict) -> None:
    req = urllib.request.Request(
        f"{shim_url}/api/events/books", data=json.dumps(ev).encode(), method="POST",
        headers={"Authorization": secret, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
    # 200 with targets: 0 means no webhook wants this event, so it reached
    # nobody. An older shim that doesn't report `targets` is trusted.
    try:
        body = json.loads(raw) if raw else None
    except ValueError as e:
        # `except OSError` misses ValueError, so an unparseable 200 would escape
        # the retry path; "cannot tell" must read as "not delivered".
        raise NotDelivered(f"the shim answered with a non-JSON body: {e}") from e
    if isinstance(body, dict) and body.get("targets") == 0:
        raise NotDelivered(
            f"the shim relayed {body.get('event')!r} to 0 targets — "
            "no webhook subscribes to it (PUT /api/webhooks/{name})"
        )


def main() -> None:
    shim_url = os.environ.get("DEPLOY_HEALTH_URL", "http://localhost:8090").rstrip("/")
    secret = os.environ["WEBHOOK_INBOUND_SECRET"]
    with open(COOKIE_FILE) as f:
        cookie = f.read().strip()
    stats, error, rejected = None, None, False
    try:
        raw, set_cookies = fetch(cookie)
        stats = slim(raw)
        new = rotated(set_cookies, cookie)
        if new:
            _write(COOKIE_FILE, new + "\n", 0o600)
            print("[mam-stats] MAM rotated the session cookie; saved")
    except Rejected as e:
        error, rejected = str(e), True
    except OSError as e:   # URLError/HTTPError/timeout; messages never include the cookie
        error = str(e) or type(e).__name__
    if error:
        print(f"[mam-stats] fetch failed: {error}")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    state, ok = run(stats, error, rejected, _load(STATS_FILE),
                    lambda ev: _post_event(shim_url, secret, ev), now)
    _write(STATS_FILE, json.dumps(state, indent=2) + "\n", 0o644)
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""MAM account health: watch qbittorrent-mam, push when the account is at risk.

Run by landible-mam-health.timer every 5 min. No model in the loop.

Each condition below pushes one `mam_health` event through the shim, then stays
quiet until it clears (which re-arms it):

  guard            unsatisfied torrents >= MAM_UNSATISFIED_CAP (requests refused)
  t:<hash>:paused  a torrent paused/stopped       \\
  t:<hash>:error   a torrent errored / files gone  |  hit & run risk if seeded < 72 h
  t:<hash>:moved   save_path left /music/books/mam |
  t:<hash>:removed a torrent vanished from qBt     /  (never re-arms: the hash is gone)
  client           login/HTTP fails, or qBt reports connection_status disconnected
  limits           a share ratio / seeding-time limit got enabled (qBt would stop seeding)

`firewalled` isn't a condition: it flaps while idle.

State is mam-torrents.json: the last torrent snapshot (to spot removals) and the
alerts currently raised. An alert is only recorded after the shim accepted it,
so a shim outage retries next run.

qbittorrent-mam is READ-ONLY here: login + three GETs. Never pause, resume or
delete a MAM torrent. The WebUI password comes via env, never argv or logs.
Pure helpers are unit-tested in tests/test_mam_health.py; main() does the I/O.
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import urllib.parse
import urllib.request

SEED_DIR = "/music/books/mam"
SATISFIED_SEED_S = 72 * 3600   # same rule as landible_mcp.books (can't import the package)
MAM_UNSATISFIED_CAP = int(os.environ.get("MAM_UNSATISFIED_CAP", "15"))
QBT = "qbittorrent-mam"

STATE_FILE = os.environ.get(
    "MAM_HEALTH_STATE_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "mam-torrents.json"),
)


# ---------------------------------------------------------------- pure helpers

def unsatisfied_count(torrents: list) -> int:
    """Copy of landible_mcp.books.unsatisfied_count: incomplete, or seeded < 72 h."""
    return sum(
        1 for t in torrents
        if (t.get("progress") or 0) < 1 or (t.get("seeding_time") or 0) < SATISFIED_SEED_S
    )


def seeded_72h(t: dict) -> bool:
    return (t.get("progress") or 0) >= 1 and (t.get("seeding_time") or 0) >= SATISFIED_SEED_S


def risk(seeded: bool, seeding_time: int) -> str:
    if seeded:
        return "seeded, lower priority"
    return f"seeded {seeding_time // 3600}h; <72 h = hit & run risk"


def torrent_problem(t: dict) -> tuple[str, str] | None:
    """(kind, what) for a torrent that needs attention, else None."""
    state = t.get("state") or ""
    if state.startswith(("paused", "stopped")):
        return "paused", "paused"
    if state in ("error", "missingFiles"):
        return "error", f"in state {state}"
    path = (t.get("save_path") or "").rstrip("/")
    if path and path != SEED_DIR:   # empty while qBt is still fetching metadata
        return "moved", f"moved to {t.get('save_path')}"
    return None


def conditions(torrents: list, transfer: dict, prefs: dict, known: dict) -> dict:
    """Every condition active now: {key: {"title": ..., "message": ...}}.

    `known` is the previous snapshot ({hash: {name, seeded_72h, ...}}), used to
    spot torrents that disappeared.
    """
    out = {}
    n = unsatisfied_count(torrents)
    if n >= MAM_UNSATISFIED_CAP:
        out["guard"] = {"title": QBT, "message": f"Guard full: {n}/{MAM_UNSATISFIED_CAP} unsatisfied; requests refused"}
    if transfer.get("connection_status") == "disconnected":
        out["client"] = {"title": QBT, "message": f"{QBT} listener not bound (connection_status disconnected)"}
    if prefs.get("max_ratio_enabled") or prefs.get("max_seeding_time_enabled"):
        out["limits"] = {"title": QBT, "message": "Share limit enabled: qBt would stop seeding"}
    current = set()
    for t in torrents:
        current.add(t["hash"])
        problem = torrent_problem(t)
        if problem:
            kind, what = problem
            name = t.get("name")
            out[f"t:{t['hash']}:{kind}"] = {
                "title": name,
                "message": f"{name} {what} ({risk(seeded_72h(t), t.get('seeding_time') or 0)})",
            }
    for h, old in known.items():
        if h not in current:
            name = old.get("name")
            out[f"t:{h}:removed"] = {
                "title": name,
                "message": f"{name} removed from {QBT} "
                           f"({'seeded, lower priority' if old.get('seeded_72h') else '<72 h = hit & run risk'})",
            }
    return out


def snapshot(torrents: list) -> dict:
    return {
        t["hash"]: {
            "name": t.get("name"), "added_on": t.get("added_on"), "completion_on": t.get("completion_on"),
            "save_path": t.get("save_path"), "seeded_72h": seeded_72h(t),
        }
        for t in torrents
    }


def event(cond: dict) -> dict:
    return {"event": "mam_health", "title": cond["title"], "source": "mam", "message": cond["message"]}


def run(fetched: tuple | None, error: str | None, state: dict | None, post) -> tuple[dict, bool]:
    """Push each newly active condition once; return (new state, all posted?).

    `fetched` is (torrents, transfer, prefs), or None with `error` set when qBt
    couldn't be read. Then only `client` is judged: the other alerts and the
    snapshot are kept as they were, since nothing is known about them.
    """
    if state is None:
        print("[mam-health] first run: seeding the snapshot")
        state = {"torrents": {}, "alerts": {}}
    alerts = dict(state["alerts"])
    if fetched is None:
        active = {"client": {"title": QBT, "message": f"{QBT} down: {error}"}}
        judged = {"client"}
        new_torrents = state["torrents"]
    else:
        torrents, transfer, prefs = fetched
        active = conditions(torrents, transfer, prefs, state["torrents"])
        judged = set(alerts) | set(active)
        new_torrents = snapshot(torrents)
    for key in judged - set(active):   # cleared: re-arm
        alerts.pop(key, None)
        print(f"[mam-health] cleared: {key}")
    ok = True
    removed_posted = set()
    try:
        for key, cond in sorted(active.items()):
            if key in alerts:
                continue
            post(event(cond))
            print(f"[mam-health] alert: {key}")
            if key.endswith(":removed"):
                removed_posted.add(key)   # the hash leaves the snapshot: can't fire again
            else:
                alerts[key] = True
    except OSError as e:
        print(f"[mam-health] ERROR push failed, retrying next run: {e}")
        ok = False
    # A removal whose push didn't land stays in the snapshot, so it's retried.
    for key in active:
        if key.endswith(":removed") and key not in removed_posted:
            h = key.split(":")[1]
            new_torrents[h] = state["torrents"][h]
    return {"torrents": new_torrents, "alerts": alerts}, ok


# ---------------------------------------------------------------------- I/O

def _load_state() -> dict | None:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def _save_state(state: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


def fetch_qbt(url: str, user: str, password: str) -> tuple:
    """(torrents, transfer, prefs) from qbittorrent-mam. Read-only."""
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    login = urllib.request.Request(
        f"{url}/api/v2/auth/login",
        data=urllib.parse.urlencode({"username": user, "password": password}).encode(),
        headers={"Referer": url},
    )
    with opener.open(login, timeout=15) as r:
        if r.status != 204 and r.read().decode().strip() != "Ok.":
            raise RuntimeError("login refused")

    def get(path):
        with opener.open(f"{url}/api/v2/{path}", timeout=15) as r:
            return json.loads(r.read())

    return get("torrents/info"), get("transfer/info"), get("app/preferences")


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
            "no webhook subscribes to it (landible_webhook_set)"
        )


def main() -> None:
    qbt_url = os.environ.get("QBT_MAM_URL", "http://localhost:8081").rstrip("/")
    shim_url = os.environ.get("DEPLOY_HEALTH_URL", "http://localhost:8090").rstrip("/")
    secret = os.environ["WEBHOOK_INBOUND_SECRET"]
    fetched, error = None, None
    try:
        fetched = fetch_qbt(qbt_url, os.environ.get("QBT_MAM_USER", "admin"), os.environ["QBT_MAM_PASSWORD"])
    except (OSError, RuntimeError, ValueError) as e:   # ValueError: a non-JSON body
        error = str(e) or type(e).__name__
    state, ok = run(fetched, error, _load_state(), lambda ev: _post_event(shim_url, secret, ev))
    _save_state(state)
    if not ok:   # fail the unit so a stuck push (bad secret, shim down) shows in systemctl --failed
        raise SystemExit(1)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Failed systemd units -> the shim -> a push.

Run by landible-unit-health.timer every 15 min. No model in the loop.

Several landible timers deliberately fail their unit so a problem is visible
in `systemctl --failed` (book_events, mam_health, mam_stats). That only helps
if something looks. A unit can otherwise sit failed for weeks and surface only
when someone runs the command by chance — which is the whole argument for this
file. It sweeps every failed unit on this host, not just landible's.

A SWEEP, not `OnFailure=` per unit: the failures that matter are often in
units nobody would have thought to annotate (a mail relay, a mount). It costs up to one interval of
delay and catches everything on the box, including units added later.

Dedupe is `failed_seen` in unit-health-state.json, keyed by unit name. A unit
that stays failed alerts once; one that recovers is dropped from the set, so
the NEXT failure alerts again. A mark only advances after the shim accepted
the event, so a shim outage retries next run.

Known and accepted: the alert path runs over the shim, so a shim outage cannot
report itself, and this unit failing reports nothing. It is a backstop for the
other units, not for itself.

Pure helpers are unit-tested in tests/test_unit_health.py; main() does the I/O.
"""
from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.environ.get("UNIT_HEALTH_STATE_FILE",
                            os.path.join(_HERE, "unit-health-state.json"))
JOURNAL_LINES = 3      # enough to say why, short enough for a phone notification
MESSAGE_CHARS = 400


class NotDelivered(OSError):
    """The shim accepted the event and handed it to nobody.

    An OSError on purpose: the push is already wrapped in `except OSError`,
    which leaves the mark alone and retries next run, so a missing subscription
    is noisy and self-correcting rather than silent data loss.
    """


# ---------------------------------------------------------------- pure helpers

def failed_units(listing: str) -> list[str]:
    """Unit names from `systemctl --failed --no-legend --plain` output.

    The first column is the unit. Lines that do not look like one are skipped
    rather than guessed at — systemd prints a summary line when a locale makes
    `--no-legend` less than total.
    """
    out = []
    for line in (listing or "").splitlines():
        # systemd prefixes a failed unit with a bullet, as its own token.
        name = line.strip().lstrip("\u25cf*").strip().split(" ")[0]
        if name.endswith((".service", ".timer", ".mount", ".socket", ".path")):
            out.append(name)
    return out


def failure_event(unit: str, detail: str) -> dict:
    return {
        "event": "unit_failed",
        "title": unit,
        "source": "systemd",
        "message": (detail or "no detail").strip()[:MESSAGE_CHARS],
    }


def to_alert(failed: list[str], seen: set) -> list[str]:
    """Units that are failed and haven't been alerted, oldest name first."""
    return sorted(set(failed) - set(seen))


def still_failing(failed: list[str], seen: set) -> set:
    """`seen`, minus anything that has recovered — so its next failure alerts."""
    return {u for u in seen if u in set(failed)}


# ---------------------------------------------------------------------- I/O

def _run(args: list[str]) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def list_failed() -> str:
    return _run(["systemctl", "--failed", "--no-legend", "--plain", "--no-pager"])


def why(unit: str) -> str:
    """The last few journal lines for a unit — what a human would look at first."""
    return _run(["journalctl", "-u", unit, "-n", str(JOURNAL_LINES),
                 "--no-pager", "-o", "cat"]).strip()


def _load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


def run(failed: list[str], state: dict, post, detail=why) -> bool:
    """Post one event per newly-failed unit; return True if every post landed."""
    seen = set(state.get("failed_seen") or [])
    ok = True
    try:
        for unit in to_alert(failed, seen):
            post(failure_event(unit, detail(unit)))
            print(f"[unit-health] failed: {unit}")
            seen.add(unit)
    except OSError as e:
        print(f"[unit-health] ERROR push failed, retrying next run: {e}")
        ok = False
    # Recovered units drop out, so their next failure is a new alert.
    state["failed_seen"] = sorted(still_failing(failed, seen))
    return ok


def main() -> None:
    shim_url = os.environ.get("DEPLOY_HEALTH_URL", "http://localhost:8090").rstrip("/")
    secret = os.environ["WEBHOOK_INBOUND_SECRET"]

    def post(event):
        req = urllib.request.Request(
            f"{shim_url}/api/events/books", data=json.dumps(event).encode(), method="POST",
            headers={"Authorization": secret, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
        try:
            body = json.loads(raw) if raw else None
        except ValueError as e:
            raise NotDelivered(f"the shim answered with a non-JSON body: {e}") from e
        # 200 with targets: 0 means no webhook wants this event.
        if isinstance(body, dict) and body.get("targets") == 0:
            raise NotDelivered(
                f"the shim relayed {body.get('event')!r} to 0 targets — "
                "no webhook subscribes to it (landible_webhook_set)"
            )

    state = _load_state()
    ok = run(failed_units(list_failed()), state, post)
    _save_state(state)
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

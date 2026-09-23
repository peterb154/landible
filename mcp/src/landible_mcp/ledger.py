"""JSON ledger this server is the only writer of (books.json)."""
from __future__ import annotations

import json
import os


def load(path: str) -> dict:
    """The ledger at `path`, or {} if it doesn't exist yet.

    A corrupt file RAISES rather than reading as {}: the next save would then
    overwrite every request with an empty ledger. (A read-only consumer can
    instead treat an unreadable file as None and skip.)
    """
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save(path: str, ledger: dict) -> None:
    """Write atomically (tmp + rename) so a reader (the event/digest timers)
    never sees a half-written file.

    Callers load, mutate and save with no `await` in between, so the event loop
    can't interleave another tool call's write — no lock needed.
    """
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(ledger, f, indent=2, sort_keys=True)
    os.replace(tmp, path)

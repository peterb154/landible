#!/usr/bin/env python3
"""Weekly audiobook digest: one push summing up the week.

Run by landible-book-digest.timer (Sunday morning). No model in
the loop. Posts one `book_digest` event through the shim with:

  Books      added to Audiobookshelf in the last 7 days + the newest one
  Libation   books downloaded / errored / not downloaded yet (its SQLite DB, read-only)
  MAM        torrents seeding, the unsatisfied guard (ours and MAM's own)
  Account    class, ratio, upload, points short of VIP, connectable, hit & runs
             (from mam-stats.json, written by mam_stats.py; never the cookie)

Every source is optional: one that's down reads "unavailable" and the rest
still go out. Stateless: a failed push fails the unit, and systemd retries it
every 30 min (Restart=on-failure in the .service).

Pure helpers are unit-tested in tests/test_digest.py; main() does the I/O.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
LIBATION_DB = os.environ.get("LIBATION_DB", "/opt/landible/compose/libation-data/db/LibationContext.db")
MAM_STATS_FILE = os.environ.get("MAM_STATS_FILE", os.path.join(_HERE, "mam-stats.json"))
MAM_UNSATISFIED_CAP = int(os.environ.get("MAM_UNSATISFIED_CAP", "15"))
VIP_POINTS = 5000        # same as landible_mcp.books (can't import the package)
GIB = 1024 ** 3
WEEK = timedelta(days=7)
STATS_FRESH = timedelta(hours=2)   # the poller runs hourly
ABS_PAGE = 50            # book_events.fetch_abs_items reads this many newest items

# Libation's LiberatedStatus enum (UserDefinedItem.BookStatus).
LIB_NOT_LIBERATED, LIB_LIBERATED, LIB_ERROR = 0, 1, 2


# ---------------------------------------------------------------- pure helpers

def books_line(items: list | None, now: datetime) -> str:
    if items is None:
        return "Books: ABS unavailable"
    since_ms = (now - WEEK).timestamp() * 1000
    new = [i for i in items if (i.get("addedAt") or 0) > since_ms]
    newest = max(items, key=lambda i: i.get("addedAt") or 0, default=None)
    if newest is None:
        return "Books: library empty"
    meta = (newest.get("media") or {}).get("metadata") or {}
    when = datetime.fromtimestamp(newest["addedAt"] / 1000, timezone.utc).strftime("%b %-d")
    count = f"{len(new)}+" if len(new) >= ABS_PAGE else str(len(new))
    return f"Books: {count} added this week (newest: {meta.get('title')}, {when})"


def libation_line(counts: dict | None) -> str:
    if counts is None:
        return "Libation: DB unavailable"
    line = (f"Libation: {counts.get(LIB_LIBERATED, 0)} downloaded, "
            f"{counts.get(LIB_ERROR, 0)} errors, {counts.get(LIB_NOT_LIBERATED, 0)} pending")
    return line + " ⚠️" if counts.get(LIB_ERROR) else line


def mam_line(torrents: list | None, stats: dict | None, unsatisfied_count) -> str:
    if torrents is None:
        local = "qbittorrent-mam unavailable"
    else:
        seeding = sum(1 for t in torrents if (t.get("progress") or 0) >= 1)
        local = f"{seeding} seeding, guard {unsatisfied_count(torrents)}/{MAM_UNSATISFIED_CAP}"
    if stats:
        local += f" (MAM: {stats.get('unsat_count')}/{stats.get('unsat_limit')} unsatisfied)"
    return f"MAM: {local}"


def account_line(file: dict | None, now: datetime) -> str:
    stats = (file or {}).get("stats")
    if not stats:
        return "Account: no MAM stats yet"
    points = stats.get("seedbonus") or 0
    short = max(0, VIP_POINTS - points)
    vip = "enough for VIP" if not short else f"{short:,} short of VIP"
    hnr = (stats.get("seedHnr_count") or 0) + (stats.get("inactHnr_count") or 0)
    line = (f"Account: {stats.get('classname')}, ratio {stats.get('ratio')}, "
            f"{(stats.get('uploaded_bytes') or 0) / GIB:.1f} GiB up, {points:,} pts ({vip}); "
            f"connectable {stats.get('connectable')}, {hnr} H&R")
    fetched = file.get("fetched_at")
    if file.get("error") or not fetched or now - datetime.fromisoformat(fetched) > STATS_FRESH:
        line += f" [stale since {fetched}" + (f": {file['error']}]" if file.get("error") else "]")
    return line


def build(items, counts, torrents, stats_file, now, unsatisfied_count) -> dict:
    stats = (stats_file or {}).get("stats")
    lines = [
        books_line(items, now),
        libation_line(counts),
        mam_line(torrents, stats, unsatisfied_count),
        account_line(stats_file, now),
    ]
    return {"event": "book_digest", "title": "Weekly audiobooks", "source": "digest",
            "message": "\n".join(lines)}


# ---------------------------------------------------------------------- I/O

def libation_counts(path: str) -> dict:
    """{BookStatus: n}. Read-only: mode=ro, so a running Libation is never blocked by a write lock."""
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
        return dict(db.execute("SELECT BookStatus, count(*) FROM UserDefinedItem GROUP BY BookStatus"))


def _load(path: str) -> dict | None:
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return None


def _try(label, fn):
    try:
        return fn()
    except Exception as e:   # one source down must not cost the whole digest
        print(f"[digest] {label} unavailable: {e or type(e).__name__}")
        return None


def main() -> None:
    # Siblings in this dir (python puts the script's dir on sys.path).
    import book_events
    import mam_health

    abs_url = os.environ.get("ABS_URL", "http://localhost:13378").rstrip("/")
    qbt_url = os.environ.get("QBT_MAM_URL", "http://localhost:8081").rstrip("/")
    shim_url = os.environ.get("DEPLOY_HEALTH_URL", "http://localhost:8090").rstrip("/")
    secret = os.environ["WEBHOOK_INBOUND_SECRET"]

    items = _try("ABS", lambda: book_events.fetch_abs_items(abs_url, os.environ["ABS_API_KEY"]))
    counts = _try("Libation", lambda: libation_counts(LIBATION_DB))
    qbt = _try("qbittorrent-mam", lambda: mam_health.fetch_qbt(
        qbt_url, os.environ.get("QBT_MAM_USER", "admin"), os.environ["QBT_MAM_PASSWORD"]))
    torrents = qbt[0] if qbt else None
    ev = build(items, counts, torrents, _load(MAM_STATS_FILE), datetime.now(timezone.utc),
               mam_health.unsatisfied_count)
    print(ev["message"])
    mam_health._post_event(shim_url, secret, ev)


if __name__ == "__main__":
    main()

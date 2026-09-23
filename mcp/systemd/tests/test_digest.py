"""Unit tests for the weekly audiobook digest — no network, no env.

digest.py is a standalone script in the parent dir; load it by path.
"""
import importlib.util
import pathlib
import sqlite3
from datetime import datetime, timezone

_dir = pathlib.Path(__file__).resolve().parents[1]


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _dir / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dg = _load("digest")
mh = _load("mam_health")

NOW = datetime(2026, 9, 27, 14, 0, tzinfo=timezone.utc)
DAY_MS = 24 * 3600 * 1000
NOW_MS = int(NOW.timestamp() * 1000)
STATS = {"stats": {"classname": "User", "ratio": 1.8, "uploaded_bytes": 12 * dg.GIB, "seedbonus": 3200,
                   "connectable": "yes", "unsat_count": 3, "unsat_limit": 50,
                   "seedHnr_count": 0, "inactHnr_count": 0},
         "fetched_at": "2026-09-27T13:00:00+00:00", "error": None}


def _item(title, days_ago):
    return {"addedAt": NOW_MS - days_ago * DAY_MS, "media": {"metadata": {"title": title}}}


def _torrent(progress=1, seeded_h=100):
    return {"progress": progress, "seeding_time": seeded_h * 3600}


def test_full_digest():
    ev = dg.build([_item("Dune", 1), _item("Old", 30), _item("Emma", 3)], {1: 145, 2: 1},
                  [_torrent(), _torrent(seeded_h=5), _torrent(progress=0.2, seeded_h=0)], STATS, NOW,
                  mh.unsatisfied_count)
    assert ev["event"] == "book_digest" and ev["title"] == "Weekly audiobooks"
    assert ev["message"].splitlines() == [
        "Books: 2 added this week (newest: Dune, Sep 26)",
        "Libation: 145 downloaded, 1 errors, 0 pending ⚠️",
        "MAM: 2 seeding, guard 2/15 (MAM: 3/50 unsatisfied)",
        "Account: User, ratio 1.8, 12.0 GiB up, 3,200 pts (1,800 short of VIP); connectable yes, 0 H&R",
    ]


def test_every_source_down_still_builds():
    ev = dg.build(None, None, None, None, NOW, mh.unsatisfied_count)
    assert ev["message"].splitlines() == [
        "Books: ABS unavailable", "Libation: DB unavailable",
        "MAM: qbittorrent-mam unavailable", "Account: no MAM stats yet",
    ]


def test_stale_stats_and_page_cap():
    stale = {**STATS, "error": "HTTP 403"}
    assert "stale since 2026-09-27T13:00:00+00:00: HTTP 403" in dg.account_line(stale, NOW)
    # Poller stopped: no error recorded, just old.
    old = {**STATS, "fetched_at": "2026-09-20T13:00:00+00:00"}
    assert dg.account_line(old, NOW).endswith("[stale since 2026-09-20T13:00:00+00:00]")
    assert "stale" not in dg.account_line(STATS, NOW)
    many = [_item(f"B{i}", 1) for i in range(dg.ABS_PAGE)]
    assert dg.books_line(many, NOW).startswith("Books: 50+ added")


def test_libation_counts_reads_the_db_read_only(tmp_path):
    path = tmp_path / "LibationContext.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE UserDefinedItem (BookId INTEGER PRIMARY KEY, BookStatus INTEGER NOT NULL)")
        db.executemany("INSERT INTO UserDefinedItem VALUES (?, ?)", [(1, 1), (2, 1), (3, 2), (4, 0)])
    assert dg.libation_counts(str(path)) == {0: 1, 1: 2, 2: 1}

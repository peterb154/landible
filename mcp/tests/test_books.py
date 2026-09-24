"""Audiobook tools: guard, release pick, content check, ledger, redaction.

Fixtures in fixtures/ are real Chaptarr responses with the Prowlarr
apikey and link redacted, and MAM torrent ids and infohashes replaced by fakes. Every backend is a strict fake: an
unexpected call (say, anything that would remove a MAM torrent) fails the test.
"""
import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from landible_mcp import books, ledger

FIXTURES = Path(__file__).parent / "fixtures"
HOUR = 3600


def _fixture(name):
    return json.loads((FIXTURES / name).read_text())


def _torrent(progress=1.0, seeded_h=100):
    return {"progress": progress, "seeding_time": seeded_h * HOUR}


class FakeApi:
    """Routes (method, path) to canned JSON; records every call in order."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def handler(self, request):
        key = (request.method, request.url.path)
        assert key in self.routes, f"unexpected call {key} {dict(request.url.params)}"
        is_json = request.headers.get("content-type") == "application/json"
        self.calls.append((request.method, request.url.path, json.loads(request.content) if is_json else None))
        route = self.routes[key]
        result = route(request) if callable(route) else route
        return result if isinstance(result, httpx.Response) else httpx.Response(200, json=result)

    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]


@pytest.fixture
def fake(monkeypatch, tmp_path):
    monkeypatch.setattr(books, "BOOK_LEDGER", str(tmp_path / "books.json"))
    monkeypatch.setattr(books, "MAM_STATS_FILE", str(tmp_path / "mam-stats.json"))

    def install(client, routes):
        api = FakeApi(routes)
        monkeypatch.setattr(books, client, httpx.AsyncClient(base_url="http://fake", transport=httpx.MockTransport(api.handler)))
        return api

    # Any backend a test doesn't set up refuses every call.
    for client in ("_chaptarr", "_abs", "_qbt_mam"):
        install(client, {})
    return install


def _qbt(fake, torrents):
    return fake("_qbt_mam", {
        ("POST", "/api/v2/auth/login"): httpx.Response(204),
        ("GET", "/api/v2/torrents/info"): torrents,
    })


def _abs(fake, found=(), path=None, tags=None, searchable=True):
    """An ABS with one book library. `found` = search hits as (title, author);
    `path` + `tags` add a scanned item (the imported book).

    `searchable=False` is the mislabeled upload: the item is in the
    library at `path`, but ABS's search can't find it under the wanted title,
    so only the path walk turns it up.
    """
    hits = [{"libraryItem": {"media": {"metadata": {"title": t, "authorName": a}}}} for t, a in found]
    item = {"id": "i1", "path": path, "media": {"audioFiles": [{"metaTags": tags or {}}]}} if path else None
    if item and searchable:
        hits.append({"libraryItem": item})
    return fake("_abs", {
        ("GET", "/api/libraries"): {"libraries": [{"id": "lib1", "name": "Audiobooks", "mediaType": "book"}]},
        ("GET", "/api/libraries/lib1/search"): {"book": hits},
        ("GET", "/api/libraries/lib1/items"): {"results": [item] if item else []},
        ("GET", "/api/items/i1"): item or {},
    })


def _assert_redacted(result):
    text = json.dumps(result).lower()
    for secret in ("apikey", "downloadurl", "prowlarr", "link=", "redacted", "indexerid"):
        assert secret not in text, f"tool output leaks {secret!r}"


# ---- guard ----

def test_unsatisfied_counts_incomplete_and_under_72h_only():
    torrents = [_torrent(progress=0.5, seeded_h=0), _torrent(seeded_h=71), _torrent(seeded_h=72), _torrent(seeded_h=500)]
    assert books.unsatisfied_count(torrents) == 2


def test_guard_allows_at_cap_minus_one_and_refuses_at_cap(fake):
    cap = books.MAM_UNSATISFIED_CAP
    _qbt(fake, [_torrent(seeded_h=1)] * (cap - 1))
    assert asyncio.run(books.guard())["ok"] is True
    _qbt(fake, [_torrent(seeded_h=1)] * cap)
    assert asyncio.run(books.guard())["ok"] is False


def _stats_file(age_h=0.5, **stats):
    fetched = (datetime.now(timezone.utc) - timedelta(hours=age_h)).isoformat(timespec="seconds")
    base = {"classname": "Mouse", "seedbonus": 3200, "ratio": 1.4, "uploaded_bytes": 10 * books.GIB,
            "unsat_count": 2, "unsat_limit": 20, "connectable": "yes", "vip_until": None}
    ledger.save(books.MAM_STATS_FILE, {"stats": {**base, **stats}, "fetched_at": fetched, "error": None, "alerts": {}})


def test_guard_uses_mams_count_when_higher_and_fresh(fake):
    cap = books.MAM_UNSATISFIED_CAP
    _qbt(fake, [_torrent(seeded_h=1)] * 2)
    _stats_file(unsat_count=cap)
    g = asyncio.run(books.guard())
    assert g["unsatisfied"] == cap and g["local"] == 2 and g["mam"] == cap and g["ok"] is False


def test_guard_ignores_stale_mam_count(fake):
    _qbt(fake, [_torrent(seeded_h=1)] * 2)
    _stats_file(age_h=3, unsat_count=books.MAM_UNSATISFIED_CAP)
    g = asyncio.run(books.guard())
    assert g["unsatisfied"] == 2 and g["mam"] is None and g["ok"] is True


def test_guard_without_stats_file_is_local_only(fake):
    _qbt(fake, [_torrent(seeded_h=1)] * 3)
    assert asyncio.run(books.guard())["unsatisfied"] == 3


def test_class_progress_with_live_infinite_ratio():
    # Live jsonLoad.php, 2026-09-19: nothing downloaded yet -> ratio "∞".
    stats = {"classname": "User", "seedbonus": 6000, "ratio": "∞", "uploaded_bytes": 10753206442}
    p = books.class_progress(stats, datetime(2026, 9, 19, tzinfo=timezone.utc))
    assert p["power_user"]["ratio"] == {"now": "∞", "need": 2.0, "met": True}
    assert p["power_user"]["upload"] == {"gib": 10.0, "need_gib": 25, "met": False}
    assert books.ratio_value("1.37") == 1.37 and books.ratio_value(None) == 0.0


def test_class_progress_power_user_and_vip(monkeypatch):
    monkeypatch.setattr(books, "MAM_JOINED", "2026-09-16")
    stats = {"classname": "User", "seedbonus": 3200, "ratio": 2.3, "uploaded_bytes": 30 * books.GIB}
    p = books.class_progress(stats, datetime(2026, 10, 1, tzinfo=timezone.utc))
    pu = p["power_user"]
    assert pu["reached"] is False and pu["time"] == {"eligible_on": "2026-10-14", "met": False}
    assert pu["upload"]["met"] and pu["ratio"]["met"]
    assert p["vip"] == {"reached": False, "needs_power_user_first": True,
                        "points": 3200, "points_needed": 5000, "points_short": 1800}
    p = books.class_progress({**stats, "classname": "Power User", "seedbonus": 6000},
                             datetime(2026, 10, 20, tzinfo=timezone.utc))
    assert p["power_user"]["reached"] and p["vip"]["points_short"] == 0
    assert p["vip"]["needs_power_user_first"] is False and p["vip"]["reached"] is False
    # A placeholder vip_until must not read as VIP.
    assert books.class_progress({**stats, "vip_until": "0000-00-00 00:00:00"}, datetime(2026, 10, 1, tzinfo=timezone.utc))["vip"]["reached"] is False
    assert books.class_progress({**stats, "classname": "VIP"}, datetime(2026, 10, 1, tzinfo=timezone.utc))["vip"]["reached"] is True


@pytest.mark.parametrize("joined", ["", "not-a-date"])
def test_class_progress_without_a_join_date_is_unknown_not_a_crash(monkeypatch, joined):
    monkeypatch.setattr(books, "MAM_JOINED", joined)
    stats = {"classname": "User", "seedbonus": 0, "ratio": 1.0, "uploaded_bytes": 0}
    time = books.class_progress(stats, datetime(2026, 10, 1, tzinfo=timezone.utc))["power_user"]["time"]
    assert time["eligible_on"] is None and time["met"] is None
    assert "MAM_JOINED" in time["note"]


def test_mam_stats_tool(fake):
    _qbt(fake, [_torrent(seeded_h=1), _torrent(seeded_h=100), _torrent(progress=0.3, seeded_h=0)])
    _stats_file()
    r = asyncio.run(books.mam_stats())
    assert r["available"] and r["stale"] is False and r["vip"]["points_short"] == 1800
    assert r["local"] == {"torrents": 3, "seeding": 2, "unsatisfied": 2, "cap": books.MAM_UNSATISFIED_CAP}
    assert "alerts" not in json.dumps(r)


def test_mam_stats_tool_without_file(fake):
    r = asyncio.run(books.mam_stats())
    assert r["available"] is False and "landible-mam-stats" in r["message"]


def test_mam_stats_tool_flags_stale(fake):
    _qbt(fake, [])
    _stats_file(age_h=5)
    assert asyncio.run(books.mam_stats())["stale"] is True


def test_request_at_cap_touches_nothing_in_chaptarr(fake):
    qbt = _qbt(fake, [_torrent(seeded_h=1)] * books.MAM_UNSATISFIED_CAP)
    # _chaptarr stays the refuse-everything fake: any call fails the test.
    result = asyncio.run(books.request("Project Hail Mary", "gr:79106958"))
    assert result["status"] == "guard" and result["success"] is False
    assert qbt.writes() == [("POST", "/api/v2/auth/login", None)]   # read-only on qbittorrent-mam
    assert ledger.load(books.BOOK_LEDGER) == {}


# ---- release pick ----

def test_pick_keeps_chaptarr_ranking():
    releases = _fixture("chaptarr_release.json")["releases"]
    release, reason = books.pick_release(releases)
    assert reason == "ok" and release["title"].endswith("[ENG / M4B]")


def test_pick_skips_vip_and_rejected():
    # downloadAllowed is explicit: _grabbable fails closed without it.
    ok = {"approved": True, "downloadAllowed": True, "title": "Book [ENG / MP3]"}
    vip = {**ok, "title": "Book [ENG / M4B] [VIP]"}
    assert books.pick_release([vip, ok]) == (ok, "ok")
    assert books.pick_release([vip]) == (None, "vip_only")
    assert books.pick_release([{**ok, "approved": False}]) == (None, "none_found")
    assert books.pick_release([]) == (None, "none_found")


def test_slim_release_drops_prowlarr_links():
    for r in _fixture("chaptarr_release.json")["releases"]:
        _assert_redacted(books.slim_release(r))


# ---- content check ----

WINGS_TAGS = {"tagAlbum": "Wings of War: Great Combat Tales of Allied and Axis Pilots During World War II",
              "tagArtist": "James P. Busha, Steve Hinton - foreword"}
WINGS_WANT = ("Wings of War: The World War II Fighter Plane That Saved the Allies", "David White")


WRONG_TAGS = {"tagAlbum": "The Da Vinci Code", "tagArtist": "Dan Brown"}


def test_mislabeled_upload_with_the_right_title_is_suspect_not_deleted():
    # MAM 100012: the title matches, the author doesn't. One disagreeing signal
    # isn't enough to delete anything; a human checks.
    verdict, detail = books.content_verdict(*WINGS_WANT, WINGS_TAGS)
    assert verdict == "suspect" and "Busha" in detail


def test_title_and_author_both_wrong_is_a_mismatch():
    assert books.content_verdict(*WINGS_WANT, WRONG_TAGS)[0] == "mismatch"


@pytest.mark.parametrize("title, author, tags", [
    ("The Hobbit, or There and Back Again", "J.R.R. Tolkien", {"tagAlbum": "The Hobbit", "tagArtist": "J.R.R. Tolkien"}),
    ("Slaughterhouse-Five", "Kurt Vonnegut Jr.", {"tagAlbum": "Slaughterhouse-Five", "tagArtist": "Kurt Vonnegut"}),
    ("Dune (Dune, #1)", "Frank Herbert", {"tagAlbum": "Dune", "tagArtist": "Frank Herbert"}),
    ("A Farewell to Arms", "Ernest Hemingway",                 # real ABS tags
     {"tagAlbum": "A Farewell to Arms (Unabridged)", "tagArtist": "Ernest Hemingway", "tagTitle": "A Farewell to Arms"}),
])
def test_the_right_book_is_ok(title, author, tags):
    assert books.content_verdict(title, author, tags)[0] == "ok"


def test_narrator_in_the_artist_tag_is_suspect_at_worst():
    tags = {"tagAlbum": "Project Hail Mary", "tagArtist": "Ray Porter"}
    assert books.content_verdict("Project Hail Mary", "Andy Weir", tags)[0] == "suspect"
    assert books.content_verdict("Project Hail Mary", "Andy Weir", {**tags, "tagComposer": "Andy Weir"})[0] == "ok"


def test_short_titles_do_not_match_longer_ones_in_the_library():
    assert not books.in_abs("It", "Stephen King", [{"title": "It Ends with Us", "author": "Stephen King"}])
    assert books.in_abs("It", "Stephen King", [{"title": "It", "author": "Stephen King"}])


def test_surname_first_is_ok_and_untagged_is_unverified():
    tags = {"tagAlbum": "A Farewell to Arms", "tagArtist": "Hemingway, Ernest"}
    assert books.content_verdict("A Farewell to Arms", "Ernest Hemingway", tags)[0] == "ok"
    assert books.content_verdict("A Farewell to Arms", "Ernest Hemingway", {})[0] == "unverified"


def test_abs_path_maps_the_file_to_its_item_folder():
    path = "/music/books/audiobooks/Ernest Hemingway/A Farewell to Arms - John Slattery/x.m4b"
    assert books.abs_item_path(path) == "/audiobooks/Ernest Hemingway/A Farewell to Arms - John Slattery"


# ---- ledger states from history ----

WINGS = _fixture("chaptarr_history_wings.json")["records"]   # newest first: failed, deleted, imported, grabbed


def _records(*event_types):
    return [r for r in WINGS if r["eventType"] in event_types]


def test_grabbed_is_downloading_with_the_hash():
    u = books.derive({"requested_at": "2026-09-18T19:00:00Z"}, _records("grabbed"))
    assert u["state"] == "downloading"
    assert u["torrent_hash"] == "deadbeefdeadbeefdeadbeefdeadbeef00000005"
    assert u["grab_history_id"] == 6


def test_imported_waits_for_verification():
    u = books.derive({"requested_at": "2026-09-18T19:00:00Z"}, _records("grabbed", "bookFileImported"))
    assert u["state"] == "verifying" and u["file_id"] == 311
    assert u["grab_history_id"] == 6


def test_an_import_whose_grab_aged_out_carries_no_grab_id():
    """The grab leaves the 20-event window before the import does.

    The ledger's leftover id belongs to an EARLIER grab, so it must not survive
    into this import — blocklisting it would ban a release that was never the
    problem.
    """
    entry = {"requested_at": "2026-09-18T19:00:00Z", "grab_history_id": 6,
             "torrent_hash": "deadbeefdeadbeefdeadbeefdeadbeef00000005"}
    u = books.derive(entry, _records("bookFileImported"))
    assert u["state"] == "verifying" and u["file_id"] == 311
    assert u["grab_history_id"] is None


def test_newest_event_wins_and_old_history_is_ignored():
    assert books.derive({"requested_at": "2026-09-18T19:00:00Z"}, WINGS)["state"] == "failed"
    # A request made after all of it (a re-request) starts clean.
    assert books.derive({"requested_at": "2026-09-19T00:00:00Z"}, WINGS) == {}


# ---- flows ----

def _hail_mary_hit():
    return _fixture("chaptarr_lookup_hail_mary.json")[0]


def _releases_for_hail_mary():
    """The captured release fixture, re-titled for the book the tests request.

    `chaptarr_release.json` is a real MAM response for *A Farewell to Arms*,
    reused by the Hail Mary request tests. Every other field is genuine, but a
    release titled for a different book is not something MAM returns for an
    approved Hail Mary search — and now that mismatch is a `choose`,
    which is the correct behaviour and exactly what these tests are not about.
    """
    def retitle(t):
        return (t or "").replace("A Farewell to Arms by Ernest Hemingway",
                                 "Project Hail Mary by Andy Weir")

    fx = _fixture("chaptarr_release.json")
    return {**fx, "releases": [{**r, "title": retitle(r.get("title"))} for r in fx["releases"]]}


def test_request_adds_then_grabs_only_the_picked_release(fake):
    _qbt(fake, [_torrent(seeded_h=1)] * 3)
    _abs(fake, found=[("The Martian", "Andy Weir")])   # same author, other book: not a hit
    releases = _releases_for_hail_mary()
    chaptarr = fake("_chaptarr", {
        ("GET", "/api/v1/book/lookup"): _fixture("chaptarr_lookup_hail_mary.json"),
        ("GET", "/api/v1/author"): [{"id": 3, "authorName": "Ernest Hemingway"}],
        ("POST", "/api/v1/book"): {"id": 900, "duration": "16:10:00"},
        ("GET", "/api/v1/release"): releases,
        ("POST", "/api/v1/release"): {},
    })

    result = asyncio.run(books.request("Project Hail Mary", "gr:79106958"))

    assert result["status"] == "grabbed" and result["book_id"] == 900
    added = chaptarr.writes()[0][2]
    assert added["monitored"] and added["audiobookMonitored"] and not added["ebookMonitored"]
    assert added["author"]["addOptions"] == {"monitor": "none", "searchForMissingBooks": False}
    assert added["author"]["audiobookMonitorNewItems"] == "none"
    first = releases["releases"][0]
    assert chaptarr.writes()[1] == ("POST", "/api/v1/release", {"guid": first["guid"], "indexerId": 1, "bookId": 900})
    entry = ledger.load(books.BOOK_LEDGER)["900"]
    assert entry["state"] == "downloading" and entry["title"] == "Project Hail Mary"
    assert result["guard"]["unsatisfied"] == 4
    _assert_redacted(result)


def test_request_with_nothing_safe_records_it_and_grabs_nothing(fake):
    _qbt(fake, [])
    _abs(fake)
    vip = {"approved": True, "title": "Project Hail Mary [VIP]", "size": 400_000_000,
           "downloadUrl": "http://prowlarr:9696/1/download?apikey=REDACTED"}
    chaptarr = fake("_chaptarr", {
        ("GET", "/api/v1/book/lookup"): [_hail_mary_hit()],
        ("GET", "/api/v1/author"): [],
        ("POST", "/api/v1/book"): {"id": 900},
        ("GET", "/api/v1/release"): {"releases": [], "hiddenReleases": [vip]},
    })

    result = asyncio.run(books.request("Project Hail Mary", "gr:79106958"))

    assert result["status"] == "vip_only"
    assert [w[1] for w in chaptarr.writes()] == ["/api/v1/book"]
    assert ledger.load(books.BOOK_LEDGER)["900"]["state"] == "requested"
    _assert_redacted(result)


def test_status_on_wrong_content_blocklists_and_drops_only_the_library_copy(fake):
    ledger.save(books.BOOK_LEDGER, {"17623": {
        "title": WINGS_WANT[0], "author": WINGS_WANT[1], "state": "downloading",
        "requested_at": "2026-09-18T19:00:00Z",
    }})
    _qbt(fake, [])
    chaptarr = fake("_chaptarr", {
        ("GET", "/api/v1/queue"): {"records": []},
        ("GET", "/api/v1/history"): {"records": _records("grabbed", "bookFileImported")},
        ("POST", "/api/v1/history/failed/6"): {},
        ("DELETE", "/api/v1/bookfile/311"): {},
    })
    _abs(fake, path="/audiobooks/David White/Wings of War", tags=WRONG_TAGS)

    result = asyncio.run(books.status(None))

    assert [w[:2] for w in chaptarr.writes()] == [
        ("POST", "/api/v1/history/failed/6"),
        ("DELETE", "/api/v1/bookfile/311"),
    ]
    book = result["books"][0]
    assert book["state"] == "failed" and book["content"] == "mismatch"
    assert "wrong content" in book["summary"] and "still seeding" in book["summary"]
    assert ledger.load(books.BOOK_LEDGER)["17623"]["state"] == "failed"
    _assert_redacted(result)


def test_status_waits_while_abs_has_not_scanned(fake):
    ledger.save(books.BOOK_LEDGER, {"17623": {
        "title": WINGS_WANT[0], "author": WINGS_WANT[1], "state": "downloading",
        "requested_at": "2026-09-18T19:00:00Z",
    }})
    _qbt(fake, [])
    chaptarr = fake("_chaptarr", {
        ("GET", "/api/v1/queue"): {"records": []},
        ("GET", "/api/v1/history"): {"records": _records("grabbed", "bookFileImported")},
    })
    _abs(fake)

    result = asyncio.run(books.status(None))

    assert chaptarr.writes() == []
    assert result["books"][0]["state"] == "verifying"


def test_request_for_a_book_chaptarr_already_has_monitors_it_instead_of_adding(fake):
    _qbt(fake, [])
    _abs(fake)
    hit = {**_hail_mary_hit(), "localBookId": "77"}
    chaptarr = fake("_chaptarr", {
        ("GET", "/api/v1/book/lookup"): [hit],
        ("GET", "/api/v1/book/77"): {"id": 77, "monitored": False, "audiobookMonitored": False,
                                     "statistics": {"bookFileCount": 0}},
        ("PUT", "/api/v1/book/77"): {"id": 77},
        ("GET", "/api/v1/release"): {"releases": []},
    })

    result = asyncio.run(books.request("Project Hail Mary", "gr:79106958"))

    assert result["status"] == "none_found"
    put = chaptarr.writes()[0]
    assert put[:2] == ("PUT", "/api/v1/book/77") and put[2]["monitored"] and put[2]["audiobookMonitored"]


def test_request_for_a_book_already_on_disk_does_nothing(fake):
    _qbt(fake, [])
    _abs(fake)
    chaptarr = fake("_chaptarr", {
        ("GET", "/api/v1/book/lookup"): [{**_hail_mary_hit(), "localBookId": "77"}],
        ("GET", "/api/v1/book/77"): {"id": 77, "statistics": {"bookFileCount": 1}},
    })

    assert asyncio.run(books.request("Project Hail Mary", "gr:79106958"))["status"] == "in_library"
    assert chaptarr.writes() == []


def test_request_for_a_book_abs_already_has_stops_before_chaptarr_writes(fake):
    # Chaptarr's lookup gives Farewell to Arms a gr: id and no local id, though
    # the library has it (hc:271546); only ABS knows.
    _qbt(fake, [])
    _abs(fake, found=[("A Farewell to Arms", "Ernest Hemingway")])
    chaptarr = fake("_chaptarr", {
        ("GET", "/api/v1/book/lookup"): [{"title": "A Farewell to Arms", "foreignBookId": "gr:4652599",
                                          "localBookId": "0", "author": {"authorName": "Ernest Hemingway"}}],
    })

    assert asyncio.run(books.request("A Farewell to Arms", "gr:4652599"))["status"] == "in_library"
    assert chaptarr.writes() == []


def test_new_book_by_a_known_author_reuses_the_local_author(fake):
    _qbt(fake, [])
    _abs(fake)
    local = {"id": 107, "authorName": "Ernest Hemingway", "foreignAuthorId": "hc:213618"}
    chaptarr = fake("_chaptarr", {
        ("GET", "/api/v1/book/lookup"): [{"title": "The Sun Also Rises", "foreignBookId": "gr:1",
                                          "localBookId": "0", "author": {"authorName": "Ernest Hemingway",
                                                                         "foreignAuthorId": "gr:1455"}}],
        ("GET", "/api/v1/author"): [{"id": 3, "authorName": "Ernest Gaines"}, local],
        ("POST", "/api/v1/book"): {"id": 901},
        ("GET", "/api/v1/release"): {"releases": []},
    })

    asyncio.run(books.request("The Sun Also Rises", "gr:1"))

    assert chaptarr.writes()[0][2]["author"] == local


def test_local_id_falls_back_to_the_local_audiobook_copy():
    assert books._local_id({"localBookId": "0", "localAudiobookBooks": [{"id": 16383}]}) == 16383
    assert books._local_id({"localBookId": "0", "localAudiobookBooks": []}) == 0
    assert books._local_id({"localBookId": "12"}) == 12


def _wings_downloading():
    ledger.save(books.BOOK_LEDGER, {"17623": {
        "title": WINGS_WANT[0], "author": WINGS_WANT[1], "state": "downloading",
        "requested_at": "2026-09-18T19:00:00Z",
    }})


def _imported_chaptarr(fake, delete=None):
    routes = {
        ("GET", "/api/v1/queue"): {"records": []},
        ("GET", "/api/v1/history"): {"records": _records("grabbed", "bookFileImported")},
        ("POST", "/api/v1/history/failed/6"): {},
    }
    if delete is not None:
        routes[("DELETE", "/api/v1/bookfile/311")] = delete
    return fake("_chaptarr", routes)


def test_status_marks_the_right_book_imported_and_changes_nothing(fake):
    _wings_downloading()
    _qbt(fake, [])
    chaptarr = fake("_chaptarr", {
        ("GET", "/api/v1/queue"): {"records": []},
        ("GET", "/api/v1/history"): {"records": _records("grabbed", "bookFileImported")},
    })
    _abs(fake, path="/audiobooks/David White/Wings of War",
         tags={"tagAlbum": "Wings of War", "tagArtist": "David Fairbank White"})

    book = asyncio.run(books.status(None))["books"][0]

    assert (book["state"], book["content"]) == ("imported", "ok")
    assert chaptarr.writes() == []


def test_suspect_import_is_kept_and_flagged(fake):
    _wings_downloading()
    _qbt(fake, [])
    chaptarr = fake("_chaptarr", {
        ("GET", "/api/v1/queue"): {"records": []},
        ("GET", "/api/v1/history"): {"records": _records("grabbed", "bookFileImported")},
    })
    _abs(fake, path="/audiobooks/David White/Wings of War", tags=WINGS_TAGS)

    book = asyncio.run(books.status(None))["books"][0]

    assert (book["state"], book["content"]) == ("imported", "suspect")
    assert "check it's the right book" in book["summary"]
    assert chaptarr.writes() == []


def test_library_copy_already_gone_still_counts_as_done(fake):
    _wings_downloading()
    _qbt(fake, [])
    _imported_chaptarr(fake, delete=httpx.Response(404))
    _abs(fake, path="/audiobooks/David White/Wings of War", tags=WRONG_TAGS)

    assert asyncio.run(books.status(None))["books"][0]["state"] == "failed"


def test_failed_delete_keeps_the_wrong_content_reason(fake):
    _wings_downloading()
    _qbt(fake, [])
    _imported_chaptarr(fake, delete=httpx.Response(500))
    _abs(fake, path="/audiobooks/David White/Wings of War", tags=WRONG_TAGS)

    book = asyncio.run(books.status(None))["books"][0]

    # The error is reported on the book, and the ledger already says why.
    assert "check_error" in book
    saved = ledger.load(books.BOOK_LEDGER)["17623"]
    assert saved["state"] == "failed" and "wrong content" in saved["reason"]


def test_a_mismatch_blocklists_nothing_when_the_grab_is_gone_and_says_so(fake):
    """The grab has aged out; only the import is left in the window.

    The library copy still goes — `file_id` came from that same import — but
    nothing is blocklisted, and the summary says which one didn't happen.
    """
    ledger.save(books.BOOK_LEDGER, {"17623": {
        "title": WINGS_WANT[0], "author": WINGS_WANT[1], "state": "downloading",
        "requested_at": "2026-09-18T19:00:00Z", "grab_history_id": 6,   # an EARLIER grab
    }})
    _qbt(fake, [])
    chaptarr = fake("_chaptarr", {
        ("GET", "/api/v1/queue"): {"records": []},
        ("GET", "/api/v1/history"): {"records": _records("bookFileImported")},
        ("POST", "/api/v1/history/failed/6"): {},   # available, and must stay untouched
        ("DELETE", "/api/v1/bookfile/311"): {},
    })
    _abs(fake, path="/audiobooks/David White/Wings of War", tags=WRONG_TAGS)

    book = asyncio.run(books.status(None))["books"][0]

    assert [w[:2] for w in chaptarr.writes()] == [("DELETE", "/api/v1/bookfile/311")]
    assert (book["state"], book["content"]) == ("failed", "mismatch")
    assert "NOT blocklisted" in book["summary"] and "still seeding" in book["summary"]
    assert ledger.load(books.BOOK_LEDGER)["17623"]["grab_history_id"] is None


def test_an_import_with_no_recorded_path_is_unverified_not_an_exception(fake):
    """Chaptarr's history had no importedPath: settle the book, don't raise."""
    ledger.save(books.BOOK_LEDGER, {"17623": {
        "title": WINGS_WANT[0], "author": WINGS_WANT[1], "state": "verifying",
        "requested_at": "2026-09-18T19:00:00Z", "imported_path": None,
    }})
    _qbt(fake, [])
    fake("_chaptarr", {
        ("GET", "/api/v1/queue"): {"records": []},
        ("GET", "/api/v1/history"): {"records": []},
    })
    _abs(fake)

    book = asyncio.run(books.status(None))["books"][0]

    assert (book["state"], book["content"]) == ("imported", "unverified")
    assert "check_error" not in book


def test_a_non_http_failure_on_one_book_still_leaves_the_others(fake):
    """A malformed entry used to escape status() and take down every book."""
    ledger.save(books.BOOK_LEDGER, {
        # imported_path outside Chaptarr's root: abs_item_path raises ValueError.
        "1": {"title": "A", "author": "X", "state": "verifying",
              "requested_at": "2026-09-18T00:00:00Z", "imported_path": "/elsewhere/a.m4b"},
        "2": {"title": "B", "author": "Y", "state": "imported", "requested_at": "2026-09-17T00:00:00Z"},
    })
    _qbt(fake, [])
    fake("_chaptarr", {
        ("GET", "/api/v1/queue"): {"records": []},
        ("GET", "/api/v1/history"): {"records": []},
    })
    _abs(fake)

    result = asyncio.run(books.status(None))

    assert [b["book_id"] for b in result["books"]] == [1, 2]
    assert "check_error" not in result["books"][1]
    # The type is named: a bug must be tellable from a backend being down.
    assert result["books"][0]["check_error"].startswith("ValueError: ")


def test_one_backend_error_does_not_hide_the_other_books(fake):
    ledger.save(books.BOOK_LEDGER, {
        "1": {"title": "A", "author": "X", "state": "downloading", "requested_at": "2026-09-18T00:00:00Z"},
        "2": {"title": "B", "author": "Y", "state": "imported", "requested_at": "2026-09-17T00:00:00Z"},
    })
    _qbt(fake, [])
    fake("_chaptarr", {
        ("GET", "/api/v1/queue"): {"records": []},
        ("GET", "/api/v1/history"): httpx.Response(503),
    })

    result = asyncio.run(books.status(None))

    assert [b["book_id"] for b in result["books"]] == [1, 2]
    assert "check_error" in result["books"][0] and "check_error" not in result["books"][1]


def test_parallel_requests_at_cap_minus_one_grab_only_once(fake, monkeypatch):
    # The fake transport never yields, so force one between guard and grab
    # (the real lookup/search are network waits) or the two never interleave.
    real_lookup = books._lookup

    async def slow_lookup(title):
        await asyncio.sleep(0.01)
        return await real_lookup(title)

    monkeypatch.setattr(books, "_lookup", slow_lookup)
    torrents = [_torrent(seeded_h=1)] * (books.MAM_UNSATISFIED_CAP - 1)

    def grab(request):
        torrents.append(_torrent(progress=0))   # the new torrent shows up in qbt
        return {}

    _qbt(fake, lambda request: list(torrents))
    _abs(fake)
    ids = iter([900, 901])
    chaptarr = fake("_chaptarr", {
        ("GET", "/api/v1/book/lookup"): _fixture("chaptarr_lookup_hail_mary.json"),
        ("GET", "/api/v1/author"): [],
        ("POST", "/api/v1/book"): lambda request: {"id": next(ids)},
        ("GET", "/api/v1/release"): _releases_for_hail_mary(),
        ("POST", "/api/v1/release"): grab,
    })

    async def both():
        return await asyncio.gather(
            books.request("Project Hail Mary", "gr:79106958"),
            books.request("Project Hail Mary", "gr:91054370"),
        )

    statuses = sorted(r["status"] for r in asyncio.run(both()))
    assert statuses == ["grabbed", "guard"]
    assert sum(1 for w in chaptarr.writes() if w[1] == "/api/v1/release") == 1


def test_book_status_is_gated_like_a_write(monkeypatch):
    from landible_mcp import server

    class NoSecret:
        headers = {}

    monkeypatch.setattr(server, "MCP_SHARED_SECRET", "s3cret")
    monkeypatch.setattr(server, "get_http_request", lambda: NoSecret())
    with pytest.raises(PermissionError):
        asyncio.run(server.landible_book_status())


# ---- a mislabeled import must still be checked ----

# MAM's BBC Radio 4 dramatization of The Grapes of Wrath, imported 2026-09-20.
# Its tags name the radio serial, so ABS's search never finds it under the
# requested title and the check sat unrun for 33 h.
GRAPES_WANT = ("The Grapes of Wrath", "John Steinbeck")
GRAPES_TAGS = {"tagAlbum": "Classic Serial", "tagTitle": "The Grapes of Wrath Episode 3",
               "tagArtist": "BBC Radio 4"}


def _grapes_downloading():
    ledger.save(books.BOOK_LEDGER, {"9247": {
        "title": GRAPES_WANT[0], "author": GRAPES_WANT[1], "state": "downloading",
        "foreign_book_id": "gr:2931549", "requested_at": "2026-09-18T19:00:00Z",
    }})


def test_import_abs_cannot_find_by_title_is_still_verified(fake):
    _grapes_downloading()
    _qbt(fake, [])
    chaptarr = fake("_chaptarr", {
        ("GET", "/api/v1/queue"): {"records": []},
        ("GET", "/api/v1/history"): {"records": _records("grabbed", "bookFileImported")},
    })
    _abs(fake, path="/audiobooks/David White/Wings of War", tags=GRAPES_TAGS, searchable=False)

    book = asyncio.run(books.status(None))["books"][0]

    # Before, this stayed "verifying" forever, so the wrong book was never flagged.
    assert (book["state"], book["content"]) == ("imported", "suspect")
    assert chaptarr.writes() == []


def test_a_genuinely_unscanned_import_still_waits(fake):
    _grapes_downloading()
    _qbt(fake, [])
    fake("_chaptarr", {
        ("GET", "/api/v1/queue"): {"records": []},
        ("GET", "/api/v1/history"): {"records": _records("grabbed", "bookFileImported")},
    })
    _abs(fake)   # nothing in the library at all

    assert asyncio.run(books.status(None))["books"][0]["state"] == "verifying"


def test_search_shows_a_requested_book_chaptarr_lost_the_link_to():
    book_ledger = {"9247": {"title": GRAPES_WANT[0], "foreign_book_id": "gr:2931549", "state": "verifying"}}
    hit = {"title": GRAPES_WANT[0], "foreignBookId": "gr:2931549", "localBookId": "0",
           "author": {"authorName": GRAPES_WANT[1]}}

    slim = books.slim_lookup(hit, book_ledger, [])

    assert slim["request_state"] == "verifying"
    assert slim["in_library"] is True


def test_a_book_never_requested_is_still_reported_as_new():
    hit = {"title": "Dune", "foreignBookId": "gr:1", "localBookId": "0", "author": {"authorName": "Frank Herbert"}}

    slim = books.slim_lookup(hit, {"9247": {"foreign_book_id": "gr:2931549", "state": "imported"}}, [])

    assert slim["request_state"] is None and slim["in_library"] is False


# ---- the in-library check has to know which format it is asked about ----
#
# The live case: The Grapes of Wrath is in the
# Ebooks library and NOT in Audiobooks, and every unabridged audiobook of it on
# MAM is [VIP] — so it cannot be there. Both ABS libraries are
# `mediaType: book`, so searching both marked the audiobook as held too, and
# the request path refused it as already in the library.

GRAPES_HIT = {"title": GRAPES_WANT[0], "foreignBookId": "gr:2931549", "localBookId": "0",
              "author": {"authorName": GRAPES_WANT[1]}}


def _abs_two_libraries(fake, audiobooks=(), ebooks=()):
    """ABS as a live install really serves it: two `mediaType: book` libraries, told
    apart only by the folder each points at (/audiobooks vs /ebooks)."""
    def hits(found):
        return {"book": [{"libraryItem": {"media": {"metadata": {"title": t, "authorName": a}}}}
                         for t, a in found]}
    return fake("_abs", {
        ("GET", "/api/libraries"): {"libraries": [
            {"id": "audio", "name": "Audiobooks", "mediaType": "book",
             "folders": [{"fullPath": "/audiobooks"}]},
            {"id": "ebook", "name": "Ebooks", "mediaType": "book",
             "folders": [{"fullPath": "/ebooks"}]},
        ]},
        ("GET", "/api/libraries/audio/search"): hits(audiobooks),
        ("GET", "/api/libraries/ebook/search"): hits(ebooks),
    })


def test_library_format_reads_the_folder_not_the_name():
    """The folder is the compose bind mount; the name is typed in the ABS UI."""
    assert books.library_format({"name": "Ebooks", "folders": [{"fullPath": "/ebooks"}]}) == "ebook"
    assert books.library_format({"name": "Anything", "folders": [{"fullPath": "/ebooks/x"}]}) == "ebook"
    assert books.library_format({"name": "Ebooks", "folders": [{"fullPath": "/audiobooks"}]}) == "audiobook"
    # The older single-library shape, which only ever held audiobooks.
    assert books.library_format({"name": "Audiobooks"}) == "audiobook"


def test_an_ebook_in_the_library_does_not_hide_the_missing_audiobook(fake):
    """THE format bug: ebook present, audiobook absent and VIP-locked."""
    _abs_two_libraries(fake, audiobooks=[], ebooks=[GRAPES_WANT])
    fake("_chaptarr", {("GET", "/api/v1/book/lookup"): [GRAPES_HIT]})

    audio = asyncio.run(books.search(GRAPES_WANT[0], GRAPES_WANT[1], 10, "audiobook"))
    ebook = asyncio.run(books.search(GRAPES_WANT[0], GRAPES_WANT[1], 10, "ebook"))

    assert audio[0]["in_library"] is False, "the audiobook is not in the library"
    assert ebook[0]["in_library"] is True, "the ebook is"
    assert (audio[0]["format"], ebook[0]["format"]) == ("audiobook", "ebook")


def test_requesting_the_audiobook_is_not_refused_because_the_ebook_is_held(fake):
    """The format bug's `done when`, on the request path rather than search."""
    _abs_two_libraries(fake, audiobooks=[], ebooks=[GRAPES_WANT])
    _qbt(fake, [])
    fake("_chaptarr", {
        ("GET", "/api/v1/book/lookup"): [GRAPES_HIT],
        # The hit carries no author id, so the book is added against Chaptarr's
        # own Steinbeck rather than creating a second copy of him.
        ("GET", "/api/v1/author"): [{"id": 3, "authorName": GRAPES_WANT[1]}],
        ("POST", "/api/v1/book"): {"id": 9247},
        ("GET", "/api/v1/release"): {"releases": [], "hiddenReleases": []},
    })

    result = asyncio.run(books.request(GRAPES_WANT[0], "gr:2931549"))

    assert result["status"] != "in_library", result["message"]


def test_requesting_the_ebook_is_still_refused_when_the_ebook_is_held(fake):
    """The scoping must not simply stop refusing — the held format still counts."""
    _abs_two_libraries(fake, audiobooks=[], ebooks=[GRAPES_WANT])
    _qbt(fake, [])
    fake("_chaptarr", {("GET", "/api/v1/book/lookup"): [GRAPES_HIT]})

    result = asyncio.run(books.request(GRAPES_WANT[0], "gr:2931549", None, "ebook"))

    assert result["status"] == "in_library"
    assert "ebook library" in result["message"]


def test_hasfiles_on_the_audiobook_record_says_nothing_about_the_ebook():
    """Chaptarr's lookup returns audiobook hits, so `hasFiles` is audio-only."""
    hit = {**GRAPES_HIT, "hasFiles": True}

    assert books.slim_lookup(hit, {}, [], "audiobook")["in_library"] is True
    assert books.slim_lookup(hit, {}, [], "ebook")["in_library"] is False


def test_the_ledger_entry_of_the_other_format_is_not_reported():
    """Both ledger keys are audiobook-shaped, so a title held in both formats
    matched twice and returned whichever came first."""
    book_ledger = {
        "9247": {"foreign_book_id": "gr:2931549", "format": "audiobook", "state": "failed"},
        "20670": {"foreign_book_id": "gr:2931549", "format": "ebook", "state": "imported"},
    }

    audio = books.slim_lookup(GRAPES_HIT, book_ledger, [], "audiobook")
    ebook = books.slim_lookup(GRAPES_HIT, book_ledger, [], "ebook")

    assert (audio["request_state"], audio["in_library"]) == ("failed", False)
    assert (ebook["request_state"], ebook["in_library"]) == ("imported", True)


def test_a_pre_156_entry_with_no_format_still_reads_as_an_audiobook():
    """Ebook requests didn't exist when those entries were written."""
    book_ledger = {"9247": {"foreign_book_id": "gr:2931549", "state": "imported"}}

    assert books.slim_lookup(GRAPES_HIT, book_ledger, [], "audiobook")["request_state"] == "imported"
    assert books.slim_lookup(GRAPES_HIT, book_ledger, [], "ebook")["request_state"] is None
    assert books.slim_entry("9247", book_ledger["9247"], None)["format"] == "audiobook"


def test_status_says_which_format_each_entry_is():
    entry = {"title": GRAPES_WANT[0], "format": "ebook", "state": "imported"}

    assert books.slim_entry("20670", entry, None)["format"] == "ebook"


def test_no_format_still_spans_both_libraries(fake):
    """What the Kindle search and the path walk rely on: an audiobook can ship
    a PDF supplement, so "find this file wherever it lives" must not scope."""
    _abs_two_libraries(fake)

    both = asyncio.run(books._abs_book_libraries())
    audio = asyncio.run(books._abs_book_libraries("audiobook"))

    assert [lib["id"] for lib in both] == ["audio", "ebook"]
    assert [lib["id"] for lib in audio] == ["audio"]


def test_the_ebook_folder_is_normalised_so_a_trailing_slash_cannot_hide_it():
    """ABS reports the folder as "/ebooks"; configured as "/ebooks/" it would
    match nothing and quietly call every ebook absent."""
    assert not books.ABS_EBOOK_FOLDER.endswith("/")


def test_a_non_canonical_stored_format_is_not_lost_to_both_filters():
    """`request` wrote the format through unchecked at first."""
    book_ledger = {"20670": {"foreign_book_id": "gr:2931549", "format": "Ebook", "state": "imported"}}

    assert books.slim_lookup(GRAPES_HIT, book_ledger, [], "ebook")["request_state"] == "imported"
    assert books.slim_lookup(GRAPES_HIT, book_ledger, [], "audiobook")["request_state"] is None


def test_an_unknown_format_is_treated_as_audiobook_not_as_no_library(fake):
    """It must not scope the check to nothing and call every book absent."""
    _abs_two_libraries(fake, audiobooks=[GRAPES_WANT], ebooks=[])
    fake("_chaptarr", {("GET", "/api/v1/book/lookup"): [GRAPES_HIT]})

    hits = asyncio.run(books.search(GRAPES_WANT[0], None, 10, "AUDIOBOOK "))

    assert hits[0]["in_library"] is True and hits[0]["format"] == "audiobook"


# ---- ask before burning a MAM slot ----

def _release(title, size_mb, approved=True, guid=None):
    """A release as Chaptarr returns one. `downloadAllowed` tracks `approved`:
    it is derived from the same rejections, and `_grabbable` reads both."""
    return {"title": title, "size": size_mb * 1_000_000, "approved": approved,
            "rejected": not approved, "downloadAllowed": approved,
            "guid": guid or title, "indexerId": 1, "seeders": 20}


GRAPES_RELEASES = {
    "releases": [
        _release("BBC R4 The Grapes of Wrath by John Steinbeck [ENG / MP3]", 157),
        _release("LATW The Grapes of Wrath by John Steinbeck [ENG / MP3]", 108),
    ],
    "hiddenReleases": [
        _release("The Grapes of Wrath: Penguin Modern Classics by John Steinbeck [VIP]", 1126),
    ],
}


@pytest.mark.parametrize("title, flagged", [
    ("BBC R4 The Grapes of Wrath by John Steinbeck [ENG / MP3]", True),
    ("LATW The Grapes of Wrath [ENG / MP3]", True),
    ("The Grapes of Wrath (Dramatized) [ENG / M4B]", True),
    ("The Grapes of Wrath - Abridged [ENG / MP3]", True),
    ("The Grapes of Wrath [Unabridged] by John Steinbeck", False),
    ("The Grapes of Wrath: Penguin Modern Classics", False),
])
def test_dramatizations_and_abridgements_are_never_auto_picked(title, flagged):
    """Not "ask about it" any more — `pick_release` refuses outright."""
    only_this = [_release(title, 500)]
    picked, reason = books.pick_release(only_this)
    assert (picked is None) is flagged
    assert reason == ("dramatization_only" if flagged else "ok")
    slim = books._slim_releases(only_this)[0]
    # `grabbable` follows the whole gate, so a dramatization Chaptarr approved
    # still reads false; `nameable` stays true (the override path is open).
    assert slim["grabbable"] is not flagged
    assert slim["nameable"] is True


def test_a_release_far_smaller_than_the_biggest_is_flagged():
    small, big = _release("The Grapes of Wrath [ENG / MP3]", 157), _release("The Grapes of Wrath [VIP]", 1126)
    assert "157 MB against 1126 MB" in books.release_concerns(small, [small, big])[0]
    assert books.release_concerns(big, [small, big]) == []


# ---- a Chaptarr-APPROVED release is still not necessarily this book ----
#
# MAM files translations under the same work, so Chaptarr approves them and
# `would_take` takes them on `approved` alone. The Greek edition of The Martian
# was grabbed and imported that way. For an ebook nothing downstream catches it:
# the content check reads AUDIO tags and is skipped entirely.

MARTIAN = ("The Martian", "Andy Weir")
GREEK_MARTIAN = "Άνθρωπος στον Άρη - Andy Weir"   # verbatim from MAM, grabbed 2026-09-23


def test_a_translation_in_another_script_is_flagged():
    assert "different script" in books.different_edition(GREEK_MARTIAN, *MARTIAN)


def test_ordinary_releases_are_not_flagged_as_translations():
    """The whole risk of this check is false positives on real releases."""
    for title in ("The Martian - Andy Weir (2011) Retail EPUB",
                  "The Martian [ENG / EPUB]",
                  "The Martian by Andy Weir [EPUB AZW3]",
                  "Red Eagles: America's Secret MiGs by Steve Davies"):
        want = MARTIAN if "Martian" in title else ("Red Eagles", "Steve Davies")
        assert books.different_edition(title, *want) is None, title


def test_a_same_script_translation_is_flagged_on_shared_words():
    """"El Marciano" is Latin script, so only the word overlap catches it."""
    assert "shares no words" in books.different_edition("El Marciano - Andy Weir", *MARTIAN)


def test_a_legitimately_non_latin_request_is_not_flagged_by_script():
    """The script check must only fire when the WANTED title is Latin."""
    assert books.different_edition("Άνθρωπος στον Άρη", "Άνθρωπος στον Άρη", "Andy Weir") is None


def test_the_author_name_alone_does_not_count_as_a_shared_word():
    """Every MAM release carries the author, so it must not mask a translation."""
    assert books.different_edition("Andy Weir - Άνθρωπος στον Άρη", *MARTIAN) is not None


def test_an_english_release_is_preferred_over_a_higher_ranked_translation():
    """The guard should not just ask — it should take the copy we can read."""
    greek = _release(GREEK_MARTIAN, 5)               # ranked first by Chaptarr
    english = _release("The Martian - Andy Weir [ENG / EPUB]", 4)

    picked, reason = books.pick_release([greek, english], *MARTIAN)

    assert reason == "ok" and picked is english
    assert books.release_concerns(picked, [greek, english], *MARTIAN) == []


def test_when_every_release_is_a_translation_the_best_is_still_offered():
    """Falling through to none_found would hide releases the user could name."""
    greek = _release(GREEK_MARTIAN, 5)
    spanish = _release("El Marciano - Andy Weir", 4)

    picked, reason = books.pick_release([greek, spanish], *MARTIAN)

    assert reason == "ok" and picked is greek, "still returned, so it becomes a choose"
    assert books.release_concerns(picked, [greek, spanish], *MARTIAN)


def test_the_greek_martian_reaches_release_concerns_and_becomes_a_choose():
    """The regression: it was APPROVED, so nothing looked at its title."""
    greek = _release(GREEK_MARTIAN, 5)
    assert greek["approved"] is True
    assert books.would_take(greek) is True, "Chaptarr approved it, so the old path grabbed it"
    concerns = books.release_concerns(greek, [greek], *MARTIAN)
    assert concerns and "different script" in concerns[0]


def test_a_release_for_a_different_book_entirely_is_no_longer_grabbed(fake):
    """What the Hail Mary fixtures were accidentally asserting before.

    A release whose title is a different book by a different author used to be
    grabbed outright whenever Chaptarr approved it. Now it is a `choose`.
    """
    _qbt(fake, [_torrent(seeded_h=1)] * 3)
    _abs(fake)
    fake("_chaptarr", {
        ("GET", "/api/v1/book/lookup"): _fixture("chaptarr_lookup_hail_mary.json"),
        ("GET", "/api/v1/author"): [{"id": 3, "authorName": "Ernest Hemingway"}],
        ("POST", "/api/v1/book"): {"id": 900, "duration": "16:10:00"},
        ("GET", "/api/v1/release"): _fixture("chaptarr_release.json"),   # A Farewell to Arms
        ("POST", "/api/v1/release"): {},
    })

    result = asyncio.run(books.request("Project Hail Mary", "gr:79106958"))

    assert result["status"] == "choose"
    assert "shares no words" in "; ".join(result["concerns"])


def _grapes_chaptarr(fake, releases=None):
    return fake("_chaptarr", {
        ("GET", "/api/v1/book/lookup"): [{
            "title": GRAPES_WANT[0], "foreignBookId": "gr:2931549", "localBookId": "0",
            "author": {"authorName": GRAPES_WANT[1]}, "editions": [{"monitored": False}],
        }],
        ("GET", "/api/v1/author"): [],
        ("POST", "/api/v1/book"): {"id": 9247},
        ("GET", "/api/v1/release"): releases or GRAPES_RELEASES,
        ("POST", "/api/v1/release"): {},
    })


def test_a_dramatization_is_not_grabbed_and_the_reply_says_why(fake):
    """Grapes of Wrath: the only non-VIP releases are a BBC serial and a LATW
    stage play. Neither is the book, so nothing is taken — and the message
    must say that rather than implying MAM had nothing."""
    _qbt(fake, [])
    _abs(fake)
    chaptarr = _grapes_chaptarr(fake)

    result = asyncio.run(books.request(GRAPES_WANT[0], "gr:2931549"))

    assert result["status"] == "dramatization_only"
    assert "dramatizations or abridgements" in result["message"]
    assert "pick one by name" in result["message"]        # the affordance survives
    # Chaptarr APPROVED these; we skipped them. Saying it rejected them would
    # be a false explanation in the reply that exists to explain.
    assert "they are not the book" in result["message"]
    assert "Chaptarr rejected" not in result["message"]
    # Nothing may read as grabbable in a reply that just refused to take
    # anything — the BBC release is Chaptarr-approved, and used to say
    # `grabbable: true` right under a message saying it was skipped.
    assert not any(r["grabbable"] for r in result["releases"])
    bbc = [r for r in result["releases"] if "BBC" in r["title"]][0]
    assert bbc["grabbable"] is False and bbc["nameable"] is True
    assert [w[1] for w in chaptarr.writes()] == ["/api/v1/book"]   # nothing grabbed
    assert ledger.load(books.BOOK_LEDGER)["9247"]["state"] == "requested"
    # The VIP copy is shown so the user can be told the unabridged exists.
    vip = [r for r in result["releases"] if "[VIP]" in r["title"]][0]
    assert vip["nameable"] is False and result["releases"][0] == vip   # biggest first
    _assert_redacted(result)


def test_a_dramatization_can_still_be_had_by_naming_it(fake):
    """The override path skips pick_release, so nothing here blocks it."""
    _qbt(fake, [])
    _abs(fake)
    chaptarr = _grapes_chaptarr(fake)
    bbc = "BBC R4 The Grapes of Wrath by John Steinbeck [ENG / MP3]"

    result = asyncio.run(books.request(GRAPES_WANT[0], "gr:2931549", bbc))

    assert result["status"] == "grabbed"
    assert chaptarr.writes()[1][1] == "/api/v1/release"


def test_request_grabs_exactly_the_release_the_user_picked(fake):
    _qbt(fake, [])
    _abs(fake)
    chosen = "LATW The Grapes of Wrath by John Steinbeck [ENG / MP3]"
    chaptarr = _grapes_chaptarr(fake)

    result = asyncio.run(books.request(GRAPES_WANT[0], "gr:2931549", chosen))

    assert result["status"] == "grabbed"
    assert chaptarr.writes()[1] == ("POST", "/api/v1/release", {"guid": chosen, "indexerId": 1, "bookId": 9247})
    assert ledger.load(books.BOOK_LEDGER)["9247"]["state"] == "downloading"


def test_a_vip_release_cannot_be_picked_by_name(fake):
    # MAM 406s a VIP torrent whoever asks, so naming it cannot override that.
    _qbt(fake, [])
    _abs(fake)
    chaptarr = _grapes_chaptarr(fake)

    result = asyncio.run(books.request(
        GRAPES_WANT[0], "gr:2931549", "The Grapes of Wrath: Penguin Modern Classics by John Steinbeck [VIP]"))

    assert result["status"] == "vip_only"
    assert [w[1] for w in chaptarr.writes()] == ["/api/v1/book"]


def test_a_title_no_longer_listed_is_stale(fake):
    _qbt(fake, [])
    _abs(fake)
    chaptarr = _grapes_chaptarr(fake)

    result = asyncio.run(books.request(GRAPES_WANT[0], "gr:2931549", "Something MAM never had"))

    assert result["status"] == "stale"
    assert [w[1] for w in chaptarr.writes()] == ["/api/v1/book"]


# Chaptarr's title matcher calls the Of Mice and Men radio play "a different
# book by this author" and hides it. That is advisory: naming it wins.
REJECTED = "Release appears to match a different book by this author"
MICE_RELEASES = {
    "releases": [_release("Of Mice and Men by John Steinbeck [ENG / MP3] [VIP]", 244, approved=False)],
    "hiddenReleases": [
        {**_release("BBC R4X CS - John Steinbeck - Of Mice and Men", 54, approved=False),
         "rejections": [REJECTED]},
    ],
}


def test_nothing_auto_grabbable_still_offers_the_rejected_release(fake):
    _qbt(fake, [])
    _abs(fake)
    _grapes_chaptarr(fake, MICE_RELEASES)

    result = asyncio.run(books.request(GRAPES_WANT[0], "gr:2931549"))

    assert result["status"] == "none_found"
    assert "pick one by name" in result["message"]
    assert "Chaptarr rejected them" in result["message"]   # here it really did
    # Nothing was taken, so nothing reads as grabbable — saying otherwise in a
    # none_found reply contradicts the message it comes with. The BBC one
    # is still `nameable`: the user can override Chaptarr and have it by name.
    bbc = [r for r in result["releases"] if "BBC" in r["title"]][0]
    assert bbc["grabbable"] is False and bbc["nameable"] is True
    assert REJECTED in bbc["rejections"][0]
    # [VIP] is neither: MAM 406s it for this account whoever asks.
    vip = [r for r in result["releases"] if "[VIP]" in r["title"]][0]
    assert vip["grabbable"] is False and vip["nameable"] is False


def test_naming_a_rejected_release_grabs_it_and_says_what_was_overridden(fake):
    _qbt(fake, [])
    _abs(fake)
    chosen = "BBC R4X CS - John Steinbeck - Of Mice and Men"
    chaptarr = _grapes_chaptarr(fake, MICE_RELEASES)

    result = asyncio.run(books.request(GRAPES_WANT[0], "gr:2931549", chosen))

    assert result["status"] == "grabbed"
    assert chaptarr.writes()[1] == ("POST", "/api/v1/release", {"guid": chosen, "indexerId": 1, "bookId": 9247})
    assert REJECTED in result["message"] and "overridden because you named it" in result["message"]


def test_a_clean_release_is_still_grabbed_without_asking(fake):
    _qbt(fake, [])
    _abs(fake)
    good = {"releases": [_release("The Grapes of Wrath by John Steinbeck [ENG / M4B]", 573)],
            "hiddenReleases": []}
    chaptarr = _grapes_chaptarr(fake, good)

    result = asyncio.run(books.request(GRAPES_WANT[0], "gr:2931549"))

    assert result["status"] == "grabbed"
    assert chaptarr.writes()[1][1] == "/api/v1/release"


def test_a_book_already_downloading_is_not_grabbed_twice(fake):
    _grapes_downloading()
    _qbt(fake, [])
    _abs(fake)
    chaptarr = _grapes_chaptarr(fake)

    result = asyncio.run(books.request(GRAPES_WANT[0], "gr:2931549"))

    assert result["status"] == "already_requested"
    assert chaptarr.writes() == []


def test_a_deleted_book_can_be_requested_again(fake):
    # The ledger says imported, but the library no longer has it: the library
    # decides, not the ledger.
    ledger.save(books.BOOK_LEDGER, {"9247": {
        "title": GRAPES_WANT[0], "author": GRAPES_WANT[1], "state": "imported",
        "foreign_book_id": "gr:2931549", "requested_at": "2026-09-18T19:00:00Z",
    }})
    _qbt(fake, [])
    _abs(fake)
    good = {"releases": [_release("The Grapes of Wrath by John Steinbeck [ENG / M4B]", 573)], "hiddenReleases": []}
    chaptarr = _grapes_chaptarr(fake, good)

    result = asyncio.run(books.request(GRAPES_WANT[0], "gr:2931549"))

    assert result["status"] == "grabbed"
    assert chaptarr.writes()[1][1] == "/api/v1/release"


def test_a_title_relayed_with_different_spacing_or_case_still_matches():
    assert books.same_title("BBC R4X CS - Of  Mice and Men", "bbc r4x cs - of mice and men")
    assert books.same_title("The Grapes of Wrath\n[ENG / MP3]", "The Grapes of Wrath [ENG / MP3]")
    assert not books.same_title("Of Mice and Men", "Of Mice and Men [VIP]")
    assert not books.same_title(None, "Of Mice and Men")


def test_a_release_named_with_sloppy_spacing_is_still_grabbed(fake):
    _qbt(fake, [])
    _abs(fake)
    chaptarr = _grapes_chaptarr(fake, MICE_RELEASES)

    result = asyncio.run(books.request(
        GRAPES_WANT[0], "gr:2931549", "bbc r4x cs -  john steinbeck - of mice and men"))

    assert result["status"] == "grabbed"
    assert chaptarr.writes()[1][2]["guid"] == "BBC R4X CS - John Steinbeck - Of Mice and Men"


# The Pearl, live 2026-09-21: a 1048 MB mystery anthology rides along in the
# results because "Pearl S. Buck" is in its author string. Chaptarr rejects it
# as a different book, so it must not be the yardstick for a novella.
MISMATCH = "Release appears to match a different book by this author"


def test_a_rejected_other_book_is_not_the_size_yardstick():
    pick = _release("The Pearl by John Steinbeck [ENG / M4B]", 74)
    anthology = {**_release("The Best American Mystery Stories of the Century", 1048, approved=False),
                 "rejections": [MISMATCH]}
    other_edition = _release("The Pearl by John Steinbeck [ENG / MP3]", 109)

    assert books.release_concerns(pick, [pick, anthology, other_edition]) == []


def test_a_vip_copy_of_the_same_book_is_still_the_yardstick():
    # The case the rule exists for: the real unabridged is there, just VIP.
    bbc = _release("BBC R4 The Grapes of Wrath by John Steinbeck [ENG / MP3]", 157)
    vip = {**_release("The Grapes of Wrath: Penguin Modern Classics [VIP]", 1126, approved=False),
           "rejections": ["Contains these ignored terms: [VIP]"]}

    concerns = books.release_concerns(bbc, [bbc, vip])

    assert any("1126 MB" in c for c in concerns)


def test_the_pearl_is_grabbed_without_asking(fake):
    _qbt(fake, [])
    _abs(fake)
    pearl = {
        "releases": [_release("The Pearl by John Steinbeck [ENG / M4B]", 74),
                     _release("The Pearl by John Steinbeck [ENG / MP3]", 109)],
        "hiddenReleases": [
            {**_release("The Best American Mystery Stories of the Century", 1048, approved=False),
             "rejections": [MISMATCH]},
        ],
    }
    chaptarr = _grapes_chaptarr(fake, pearl)

    result = asyncio.run(books.request(GRAPES_WANT[0], "gr:2931549"))

    assert result["status"] == "grabbed"
    assert chaptarr.writes()[1][1] == "/api/v1/release"


# ---- a crowded bibliography gets every release rejected ----

# Live: East of Eden (9250) came back releases=0,
# hiddenReleases=6. Five of Steinbeck's records contain "East of Eden" (the
# novel, "Part One", two omnibuses, a five-novel collection), so Chaptarr's
# by-title-within-the-author matcher can't choose and rejects the lot.
EOE_WANT = ("East of Eden", "John Steinbeck")


def _rejected(title, size_mb, rejections=None, **over):
    """A release as Chaptarr really returns a rejected one.

    `rejected: True` and `downloadAllowed: False` are derived from the same
    rejection and are load-bearing — `_grabbable` reads both. Leaving them out
    is what let a fix that flipped only `approved` pass its tests and still do
    nothing on the live box. Distinct guid: three of the six share a
    title, and the guid is what gets grabbed.
    """
    return {**_release(title, size_mb, approved=False, guid=f"{title}#{size_mb}"),
            "rejected": True, "downloadAllowed": False,
            "rejections": rejections if rejections is not None else [MISMATCH], **over}


EOE_RELEASES = {
    "releases": [],
    "hiddenReleases": [
        _rejected("East of Eden by John Steinbeck [ENG / MP3]", 736),
        _rejected("BBC R4 CS - East Of Eden by John Steinbeck [ENG / MP3] [VIP]", 166,
                  ["Contains these ignored terms: [VIP]", MISMATCH]),
        _rejected("East of Eden by John Steinbeck [ENG / M4B]", 776),
        _rejected("East of Eden by John Steinbeck [ENG / M4B]", 726),
        _rejected("East of Eden by John Steinbeck [ENG / M4B]", 1503, indexerFlags=1),
        _rejected("East Of Eden by John Steinbeck [ENG / M4B]", 752),
    ],
}


def _eoe_chaptarr(fake, releases=None):
    return fake("_chaptarr", {
        ("GET", "/api/v1/book/lookup"): [{
            "title": EOE_WANT[0], "foreignBookId": "gr:2574991", "localBookId": "0",
            "author": {"authorName": EOE_WANT[1]}, "editions": [{"monitored": False}],
        }],
        ("GET", "/api/v1/author"): [],
        ("POST", "/api/v1/book"): {"id": 9250},
        ("GET", "/api/v1/release"): releases or EOE_RELEASES,
        ("POST", "/api/v1/release"): {},
    })


def test_the_captured_response_drives_the_whole_decision():
    """Against Chaptarr's REAL reply, not a shape I believed it had.

    The hand-written fixtures in this file carry the fields I knew to look at;
    this one carries all 45, including the three a rejection sets together and
    the `rejectionDetails` category. An earlier fix passed its tests and did nothing on
    the live box because `downloadAllowed` was absent here and defaulted open.
    """
    body = _fixture("chaptarr_releases_east_of_eden.json")
    releases = (body["releases"] or []) + [{**r, "approved": False} for r in body["hiddenReleases"]]
    assert len(releases) == 6 and not body["releases"]      # every one rejected
    assert all(r["downloadAllowed"] is False for r in releases)

    picked, reason = books.pick_release(books.reconsider(releases, *EOE_WANT))

    assert reason == "ok"
    assert round(picked["size"] / 1e6) == 1503
    assert picked["indexerFlags"] & 1                        # the freeleech one
    assert "M4B" in picked["title"]
    # Chaptarr's own rank would have taken a 736 MB MP3, and ranks the BBC radio
    # play above every full reading — populated, but not a signal to follow.
    assert picked["rank"] == 5
    assert books.release_concerns(picked, releases, *EOE_WANT) == []


@pytest.mark.parametrize("category", [None, "Matching"])
def test_the_pearl_anthology_is_advisory_but_never_vouched(category):
    """`Matching` is a category, not one reason.

    It covers "I cannot choose between this author's records", which we
    override, and could equally cover "this is not the book", which we must
    not. The 1048 MB mystery anthology that listed a "Pearl S. Buck" story
    passes the advisory gate either way — `release_is_this_book` is the
    layer that holds it out, so pin it directly rather than trusting the gate.
    """
    anthology = _rejected("The Best American Mystery Stories of the Century", 1048)
    if category:
        anthology["rejectionDetails"] = [{"reason": "Release appears to match a different book "
                                                    "by this author", "category": category}]
    assert books._only_advisory(anthology) is True          # the gate lets it through
    assert books.release_is_this_book(anthology["title"], "The Pearl", "John Steinbeck") is False
    assert books.vouched_for(anthology, "The Pearl", "John Steinbeck") is False

    rec = books.reconsider([anthology], "The Pearl", "John Steinbeck")
    assert books.pick_release(rec) == (None, "none_found")


def test_several_releases_are_grabbable_while_nothing_is_grabbed(fake):
    """`grabbable` is "passes our gate", not "this is the one"."""
    _qbt(fake, [])
    _abs(fake)
    # The biggest is the doubtful one, so it is picked AND raises a concern.
    releases = {"releases": [], "hiddenReleases": [
        _rejected("East of Eden & Grapes Of Wrath by John Steinbeck [ENG / M4B]", 2400),
        _rejected("East of Eden by John Steinbeck [ENG / M4B]", 1503),
    ]}
    _eoe_chaptarr(fake, releases)

    result = asyncio.run(books.request(EOE_WANT[0], "gr:2574991"))

    assert result["status"] == "choose"                      # nothing grabbed
    assert sum(r["grabbable"] for r in result["releases"]) == 2
    # `best_pick` is what the concerns are about — the docstring says so now.
    assert result["best_pick"]["size_mb"] == 2400
    assert "nameable: false" in result["message"]            # not `grabbable: false`


def test_our_matcher_sorts_the_real_east_of_eden_releases():
    vouched = [r for r in EOE_RELEASES["hiddenReleases"] if books.vouched_for(r, *EOE_WANT)]
    # Five of six: the BBC [VIP] one is out on both counts.
    assert len(vouched) == 5
    assert all("VIP" not in r["title"] for r in vouched)


@pytest.mark.parametrize("title, is_book", [
    ("East of Eden by John Steinbeck [ENG / M4B]", True),
    ("East Of Eden by John Steinbeck [ENG / MP3]", True),          # case only
    ("Of Mice and Men by John Steinbeck [ENG / M4B]", False),      # a different book
    ("BBC R4 CS - East Of Eden by John Steinbeck [ENG / MP3]", False),
    # One-way matching would take the whole collection for the one novel.
    ("The Grapes of Wrath / The Moon Is Down / Cannery Row / East of Eden / Of Mice and Men", False),
])
def test_only_the_book_itself_matches_both_ways(title, is_book):
    assert books.release_is_this_book(title, *EOE_WANT) is is_book


@pytest.mark.parametrize("release, title, author, is_book", [
    # The real miss: every co-author inflated the reverse match until it failed.
    ("The Phoenix Project by Gene Kim, Kevin Behr, George Spafford [ENG / EPUB MOBI]",
     "The Phoenix Project", "Gene Kim", True),
    # The requested author need not be listed first.
    ("The Phoenix Project by Kevin Behr, Gene Kim, George Spafford [ENG / EPUB]",
     "The Phoenix Project", "Gene Kim", True),
    ("Good Omens by Neil Gaiman & Terry Pratchett [ENG / M4B]", "Good Omens", "Terry Pratchett", True),
    ("Good Omens by Neil Gaiman and Terry Pratchett [ENG / M4B]", "Good Omens", "Neil Gaiman", True),
    # "by" inside the title: only the author list is cut, not the title.
    ("Stand by Me by Stephen King [ENG / M4B]", "Stand by Me", "Stephen King", True),
    # Author-first shape has no "by": untouched, and still a match on the title.
    ("Ernest Hemingway - A Farewell to Arms [John Slattery]", "A Farewell to Arms", "Ernest Hemingway", True),
    # Same author, same shape, different book: "the" and "project" are not
    # enough. Single-author releases had this hole before the co-author fix.
    ("The Unicorn Project by Gene Kim, Kevin Behr [ENG / EPUB]", "The Phoenix Project", "Gene Kim", False),
    ("The Unicorn Project by Gene Kim [ENG / EPUB]", "The Phoenix Project", "Gene Kim", False),
    ("Of Mice and Men by John Steinbeck [ENG / M4B]", "Of Mice and Men", "John Steinbeck", True),
])
def test_a_co_author_list_does_not_defeat_the_match(release, title, author, is_book):
    assert books.release_is_this_book(release, title, author) is is_book


def test_a_crowded_bibliography_still_grabs_the_best_release(fake):
    """Every release advisory-rejected: ours picks the 1503 MB freeleech M4B."""
    _qbt(fake, [])
    _abs(fake)
    chaptarr = _eoe_chaptarr(fake)

    result = asyncio.run(books.request(EOE_WANT[0], "gr:2574991"))

    assert result["status"] == "grabbed"
    assert chaptarr.writes()[1][1] == "/api/v1/release"
    # The biggest of the five it vouched for, and the freeleech one.
    assert result["release"]["size_mb"] == 1503
    assert result["release"]["freeleech"] is True
    assert chaptarr.writes()[1][2]["guid"] == "East of Eden by John Steinbeck [ENG / M4B]#1503"


@pytest.mark.parametrize("title, size_mb", [
    ("East of Eden, Part One by John Steinbeck [ENG / M4B]", 400),      # a fragment
    ("East of Eden & Grapes Of Wrath by John Steinbeck [ENG / M4B]", 2400),  # bundled
])
def test_a_fragment_or_a_bundle_is_asked_about_not_grabbed(fake, title, size_mb):
    """Both match both ways, and neither is reachable by the size rule alone."""
    _qbt(fake, [])
    _abs(fake)
    chaptarr = _eoe_chaptarr(fake, {"releases": [], "hiddenReleases": [_rejected(title, size_mb)]})

    result = asyncio.run(books.request(EOE_WANT[0], "gr:2574991"))

    assert result["status"] == "choose"
    assert any("part of the book" in c for c in result["concerns"])
    assert [w[:2] for w in chaptarr.writes()] == [("POST", "/api/v1/book")]   # nothing grabbed


@pytest.mark.parametrize("want, author, release, bundled", [
    # The marker alone means nothing: these are one book each.
    ("Pride & Prejudice", "Jane Austen", "Pride & Prejudice by Jane Austen [ENG / M4B]", False),
    ("Crime & Punishment", "Fyodor Dostoevsky",
     "Crime & Punishment by Fyodor Dostoevsky [ENG / M4B]", False),
    # What makes it a bundle is the words the request does NOT have.
    ("East of Eden", "John Steinbeck",
     "East of Eden & Grapes Of Wrath by John Steinbeck [ENG / M4B]", True),
    ("East of Eden", "John Steinbeck",
     "East of Eden, Part One by John Steinbeck [ENG / M4B]", True),
    ("East of Eden", "John Steinbeck", "East of Eden by John Steinbeck [ENG / M4B]", False),
])
def test_an_ampersand_in_the_wanted_title_is_not_a_bundle(want, author, release, bundled):
    assert books.bundled_or_partial(release, want, author) is bundled


def test_a_rejection_that_is_not_the_title_matcher_still_blocks(fake):
    """Only Chaptarr's by-title-within-author guess is advisory; quality is not."""
    _qbt(fake, [])
    _abs(fake)
    releases = {"releases": [], "hiddenReleases": [
        _rejected("East of Eden by John Steinbeck [ENG / M4B]", 900, ["Quality MP3-64 is not wanted"]),
    ]}
    chaptarr = _eoe_chaptarr(fake, releases)

    result = asyncio.run(books.request(EOE_WANT[0], "gr:2574991"))

    assert result["status"] == "none_found"
    assert [w[:2] for w in chaptarr.writes()] == [("POST", "/api/v1/book")]


def test_vip_is_never_reconsidered(fake):
    """MAM 406s a [VIP] release whoever asks, so it is not ours to override."""
    _qbt(fake, [])
    _abs(fake)
    releases = {"releases": [], "hiddenReleases": [
        _rejected("East of Eden by John Steinbeck [ENG / M4B] [VIP]", 1503,
                  ["Contains these ignored terms: [VIP]", MISMATCH]),
    ]}
    chaptarr = _eoe_chaptarr(fake, releases)

    result = asyncio.run(books.request(EOE_WANT[0], "gr:2574991"))

    assert result["status"] == "vip_only"
    assert [w[:2] for w in chaptarr.writes()] == [("POST", "/api/v1/book")]


def test_chaptarr_approved_releases_still_win_over_ours(fake):
    """Its own approval is the stronger signal, so a smaller approved one leads."""
    releases = [
        _rejected("East of Eden by John Steinbeck [ENG / M4B]", 1503),
        _release("East of Eden by John Steinbeck [ENG / M4B]", 800),
    ]
    pick, reason = books.pick_release(books.reconsider(releases, *EOE_WANT))
    assert reason == "ok" and pick["size"] == 800 * 1_000_000


# ---- Chaptarr files a finished download under the wrong book ----

# Live: the grab was made for Of Mice and Men (9249), but
# Chaptarr's import matcher re-resolved the file to "Oxford Literature
# Companions: Of Mice and Men" (9415) — one of the 600 records the author sync
# pulled for Steinbeck — and blocked the import.
MICE_BLOCKED = "Rejected: Completed download was grabbed for 'Of Mice and Men' (BookId 9249), but import matched 'Oxford Literature Companions: Of Mice and Men' (BookId 9415)."
MICE_FOLDER = "/music/books/mam/Classic Serial - John Steinbeck - Of Mice and Men -  Bob129"
MICE_FILE = f"{MICE_FOLDER}/Classic Serial - John Steinbeck - Of Mice and Men.mp3"
WINGS_HASH = "deadbeefdeadbeefdeadbeefdeadbeef00000005"   # the grab in the history fixture
# The queue item's own downloadId — a DIFFERENT grab from the history fixture
# above, which is the point: these tests pair an Of Mice and Men download with
# Wings of War history, and the queue item is the one actually being imported.
# It matches the live ledger's hash for 9249, so it is the right answer.
MICE_HASH = "deadbeefdeadbeefdeadbeefdeadbeef00000003"
MP3 = {"quality": {"id": 10, "name": "MP3"}, "revision": {"version": 1}}


def _blocked_queue(book_id=9249):
    return {"records": [{
        "id": 1341969168, "bookId": book_id, "authorId": 31, "outputPath": MICE_FOLDER,
        "downloadId": "DEADBEEFDEADBEEFDEADBEEFDEADBEEF00000003",
        "title": "Classic Serial - John Steinbeck - Of Mice and Men -  Bob129",
        "size": 54214495, "sizeleft": 0, "status": "completed",
        "trackedDownloadState": "importBlocked", "trackedDownloadStatus": "warning",
        "statusMessages": [{"title": "x.mp3", "messages": [MICE_BLOCKED]}],
    }]}


# 109 editions come back for this book; only one is the monitored audiobook, and
# Chaptarr's own manualimport pick was an ebook edition of the wrong book.
MICE_EDITIONS = [
    {"id": 16089, "monitored": False, "isEbook": True, "title": "Of Mice and Men"},
    {"id": 14983, "monitored": True, "isEbook": False, "title": "Of Mice and Men"},
    {"id": 15062, "monitored": False, "isEbook": False, "title": "Hiirtest ja inimestest"},
]
MICE_CANDIDATES = [
    {"id": 30768089, "path": MICE_FILE, "quality": MP3, "indexerFlags": 0, "additionalFile": False,
     "book": {"id": 9415}, "editionId": 16089, "rejections": []},
    {"id": 30768090, "path": f"{MICE_FOLDER}/cover.jpg", "quality": MP3, "additionalFile": True},
]


def _mice_downloading(**extra):
    ledger.save(books.BOOK_LEDGER, {"9249": {
        "title": "Of Mice and Men", "author": "John Steinbeck", "state": "downloading",
        "foreign_book_id": "gr:40283", "requested_at": "2026-09-18T19:00:00Z",
        "grab_history_id": 6, **extra,
    }})


def test_a_stale_ledger_hash_does_not_suppress_the_new_grabs_force(fake):
    """The aged-out-grab shape, one field over.

    A re-grab whose `grabbed` record has aged out of the 20-event window leaves
    the ledger holding the PREVIOUS grab's hash while the queue holds the new
    download. Reading the ledger first made `grab_token` return the old hash,
    which matched the old `import_forced_for` — so `forced_once` said "already
    done" and the new blocked import was never forced at all.
    """
    _mice_downloading(torrent_hash=WINGS_HASH, import_forced_for=WINGS_HASH,
                      import_forced_at="2026-09-20T09:00:00Z")
    _qbt(fake, [])
    _abs(fake)
    chaptarr = fake("_chaptarr", {
        ("GET", "/api/v1/queue"): _blocked_queue(),
        ("GET", "/api/v1/history"): {"records": []},      # the grab has aged out
        ("GET", "/api/v1/edition"): MICE_EDITIONS,
        ("GET", "/api/v1/manualimport"): MICE_CANDIDATES,
        ("POST", "/api/v1/command"): {"id": 1},
    })

    asyncio.run(books.status(None))

    # The download in the queue is what counts, so this grab gets its own force.
    assert [w[1] for w in chaptarr.writes()] == ["/api/v1/command"]
    assert ledger.load(books.BOOK_LEDGER)["9249"]["import_forced_for"] == MICE_HASH


def test_grab_token_prefers_the_download_in_front_of_it():
    item = {"downloadId": "DEADBEEFDEADBEEFDEADBEEFDEADBEEF00000003"}
    stale = {"torrent_hash": WINGS_HASH, "grab_history_id": 6}
    assert books.grab_token(stale, item) == MICE_HASH        # the queue wins
    assert books.grab_token(stale, None) == WINGS_HASH       # no item: the ledger
    assert books.grab_token({"grab_history_id": 6}, None) == "6"   # last resort
    assert books.grab_token({}, None) is None


def test_blocked_import_is_redone_against_the_book_it_was_grabbed_for(fake):
    _mice_downloading()
    _qbt(fake, [])
    _abs(fake)
    chaptarr = fake("_chaptarr", {
        ("GET", "/api/v1/queue"): _blocked_queue(),
        ("GET", "/api/v1/history"): {"records": _records("grabbed")},
        ("GET", "/api/v1/edition"): MICE_EDITIONS,
        ("GET", "/api/v1/manualimport"): MICE_CANDIDATES,
        ("POST", "/api/v1/command"): {"id": 1},
    })

    book = asyncio.run(books.status(None))["books"][0]

    (method, path, body), = chaptarr.writes()
    assert (method, path) == ("POST", "/api/v1/command")
    # copy, never move: the MAM torrent has to keep seeding (hit & run).
    assert body["name"] == "ManualImport" and body["importMode"] == "copy"
    # Our book and our monitored audiobook edition, not the ones Chaptarr matched.
    assert body["files"] == [{
        "path": MICE_FILE, "authorId": 31, "bookId": 9249, "editionId": 14983,
        "quality": MP3, "indexerFlags": 0, "disableReleaseSwitching": True,
    }]
    assert "re-imported against this one" in book["summary"]
    assert ledger.load(books.BOOK_LEDGER)["9249"]["import_forced_for"] == MICE_HASH


def test_a_blocked_import_is_forced_once_per_grab_not_every_poll(fake):
    _mice_downloading(import_forced_for=MICE_HASH, import_forced_at="2026-09-21T17:00:00Z")
    _qbt(fake, [])
    chaptarr = fake("_chaptarr", {
        ("GET", "/api/v1/queue"): _blocked_queue(),
        ("GET", "/api/v1/history"): {"records": _records("grabbed")},
    })

    asyncio.run(books.status(None))

    assert chaptarr.writes() == []


def test_a_re_grab_of_the_same_book_gets_its_own_attempt(fake):
    # The earlier attempt was for a different grab, so this one is not spent.
    _mice_downloading(import_forced_for="an-earlier-grab", import_forced_at="2026-09-20T17:00:00Z")
    _qbt(fake, [])
    _abs(fake)
    chaptarr = fake("_chaptarr", {
        ("GET", "/api/v1/queue"): _blocked_queue(),
        ("GET", "/api/v1/history"): {"records": _records("grabbed")},
        ("GET", "/api/v1/edition"): MICE_EDITIONS,
        ("GET", "/api/v1/manualimport"): MICE_CANDIDATES,
        ("POST", "/api/v1/command"): {"id": 1},
    })

    asyncio.run(books.status(None))

    assert [w[1] for w in chaptarr.writes()] == ["/api/v1/command"]
    assert ledger.load(books.BOOK_LEDGER)["9249"]["import_forced_for"] == MICE_HASH


def test_a_download_still_running_is_left_alone(fake):
    _mice_downloading()
    _qbt(fake, [])
    queue = _blocked_queue()
    queue["records"][0].update({"sizeleft": 20_000_000, "status": "downloading",
                                "trackedDownloadState": "downloading", "statusMessages": []})
    chaptarr = fake("_chaptarr", {
        ("GET", "/api/v1/queue"): queue,
        ("GET", "/api/v1/history"): {"records": _records("grabbed")},
    })

    book = asyncio.run(books.status(None))["books"][0]

    assert chaptarr.writes() == []
    assert book["state"] == "downloading" and "63%" in book["summary"]


def test_no_monitored_audiobook_edition_records_why_and_imports_nothing(fake):
    _mice_downloading()
    _qbt(fake, [])
    _abs(fake)
    chaptarr = fake("_chaptarr", {
        ("GET", "/api/v1/queue"): _blocked_queue(),
        ("GET", "/api/v1/history"): {"records": _records("grabbed")},
        ("GET", "/api/v1/edition"): [e for e in MICE_EDITIONS if e["isEbook"]],
    })

    asyncio.run(books.status(None))

    assert chaptarr.writes() == []
    entry = ledger.load(books.BOOK_LEDGER)["9249"]
    assert "no single monitored audiobook edition" in entry["reason"]
    # Nothing was imported, so the one attempt is not spent: a human can fix the
    # monitoring in Chaptarr's UI and the next poll picks it up.
    assert "import_forced_for" not in entry


def test_the_monitored_audiobook_edition_is_the_one_picked():
    assert books.monitored_edition(MICE_EDITIONS) == 14983
    assert books.monitored_edition([{"id": 1, "monitored": True, "isEbook": True}]) is None
    assert books.monitored_edition([]) is None


def test_only_the_real_files_are_handed_to_manual_import():
    files = books.import_files(MICE_CANDIDATES, "9249", 31, 14983)
    assert [f["path"] for f in files] == [MICE_FILE]
    assert books.import_files([{"additionalFile": True, "path": "cover.jpg"}], "9249", 31, 14983) == []


def test_import_blocked_only_reads_the_tracked_state():
    assert books.import_blocked(_blocked_queue()["records"][0])
    assert not books.import_blocked({"trackedDownloadState": "downloading"})
    assert not books.import_blocked(None)


def test_a_skipped_force_is_retried_once_the_edition_is_there(fake):
    # First poll: nothing monitored, so nothing imported. Second: fixed in the UI.
    _mice_downloading()
    _qbt(fake, [])
    _abs(fake)
    editions = [e for e in MICE_EDITIONS if e["isEbook"]]
    chaptarr = fake("_chaptarr", {
        ("GET", "/api/v1/queue"): _blocked_queue(),
        ("GET", "/api/v1/history"): {"records": _records("grabbed")},
        ("GET", "/api/v1/edition"): lambda request: list(editions),
        ("GET", "/api/v1/manualimport"): MICE_CANDIDATES,
        ("POST", "/api/v1/command"): {"id": 1},
    })

    asyncio.run(books.status(None))
    assert chaptarr.writes() == []

    editions[:] = MICE_EDITIONS
    asyncio.run(books.status(None))

    assert [w[1] for w in chaptarr.writes()] == ["/api/v1/command"]
    assert ledger.load(books.BOOK_LEDGER)["9249"]["import_forced_for"] == MICE_HASH


def test_two_monitored_audiobook_editions_are_not_guessed_between(fake):
    _mice_downloading()
    _qbt(fake, [])
    _abs(fake)
    both = MICE_EDITIONS + [{"id": 15062, "monitored": True, "isEbook": False, "title": "Egerek es emberek"}]
    chaptarr = fake("_chaptarr", {
        ("GET", "/api/v1/queue"): _blocked_queue(),
        ("GET", "/api/v1/history"): {"records": _records("grabbed")},
        ("GET", "/api/v1/edition"): both,
    })

    asyncio.run(books.status(None))

    assert chaptarr.writes() == []
    assert "no single monitored audiobook edition" in ledger.load(books.BOOK_LEDGER)["9249"]["reason"]


def test_chaptarr_may_not_re_decide_the_edition_we_hand_it():
    # The override is pointless if Chaptarr switches the edition back on import.
    assert books.import_files(MICE_CANDIDATES, "9249", 31, 14983)[0]["disableReleaseSwitching"] is True


def test_a_grab_with_no_hash_anywhere_is_not_imported_at_all(fake):
    # Without a stable id there is nothing to remember the import by, and an
    # unremembered import repeats every poll. Refusing beats looping.
    ledger.save(books.BOOK_LEDGER, {"9249": {
        "title": "Of Mice and Men", "author": "John Steinbeck", "state": "downloading",
        "foreign_book_id": "gr:40283", "requested_at": "2026-09-18T19:00:00Z",
    }})
    _qbt(fake, [])
    _abs(fake)
    queue = _blocked_queue()
    del queue["records"][0]["downloadId"]
    chaptarr = fake("_chaptarr", {
        ("GET", "/api/v1/queue"): queue,
        ("GET", "/api/v1/history"): {"records": []},   # no grab record read yet either
    })

    for _ in range(3):
        asyncio.run(books.status(None))

    assert chaptarr.writes() == []
    assert "no torrent hash" in ledger.load(books.BOOK_LEDGER)["9249"]["reason"]


def test_a_blocked_import_is_remembered_before_the_history_is_read(fake):
    # The ledger entry _request writes has no hash and no history id until
    # derive() has seen the grab; the queue item's downloadId carries it.
    ledger.save(books.BOOK_LEDGER, {"9249": {
        "title": "Of Mice and Men", "author": "John Steinbeck", "state": "downloading",
        "foreign_book_id": "gr:40283", "requested_at": "2026-09-18T19:00:00Z",
    }})
    _qbt(fake, [])
    _abs(fake)
    chaptarr = fake("_chaptarr", {
        ("GET", "/api/v1/queue"): _blocked_queue(),
        ("GET", "/api/v1/history"): {"records": []},
        ("GET", "/api/v1/edition"): MICE_EDITIONS,
        ("GET", "/api/v1/manualimport"): MICE_CANDIDATES,
        ("POST", "/api/v1/command"): {"id": 1},
    })

    for _ in range(3):
        asyncio.run(books.status(None))

    assert [w[1] for w in chaptarr.writes()] == ["/api/v1/command"]   # once, not three times
    assert ledger.load(books.BOOK_LEDGER)["9249"]["import_forced_for"] == "deadbeefdeadbeefdeadbeefdeadbeef00000003"


def test_a_book_we_could_not_import_says_so_in_the_summary(fake):
    # The reason is useless in the ledger if the person asking never sees it.
    _mice_downloading()
    _qbt(fake, [])
    _abs(fake)
    fake("_chaptarr", {
        ("GET", "/api/v1/queue"): _blocked_queue(),
        ("GET", "/api/v1/history"): {"records": _records("grabbed")},
        ("GET", "/api/v1/edition"): [e for e in MICE_EDITIONS if e["isEbook"]],
    })

    book = asyncio.run(books.status(None))["books"][0]

    assert "no single monitored audiobook edition" in book["summary"]
    assert "needs a hand" in book["summary"]


def test_a_forced_import_does_not_also_claim_it_needs_a_hand():
    forced = {"state": "downloading", "import_forced_at": "2026-09-21T17:55:51Z", "reason": None}
    assert "needs a hand" not in books.summarize(forced, None)


def test_a_stale_forced_flag_cannot_claim_an_import_that_did_not_happen(fake):
    # Force-imported once, then re-grabbed outside our flow (a hand grab in
    # Chaptarr's UI): derive() puts the book back to downloading without the
    # wholesale entry replacement _request does, so the old flag survives.
    ledger.save(books.BOOK_LEDGER, {"9249": {
        "title": "Of Mice and Men", "author": "John Steinbeck", "state": "downloading",
        "requested_at": "2026-09-18T19:00:00Z",
        "import_forced_at": "2026-09-20T01:00:00Z",
        "import_forced_for": "the-previous-grabs-hash",
    }})
    _qbt(fake, [])
    _abs(fake)
    fake("_chaptarr", {
        ("GET", "/api/v1/queue"): _blocked_queue(),
        ("GET", "/api/v1/history"): {"records": _records("grabbed")},
        ("GET", "/api/v1/edition"): [e for e in MICE_EDITIONS if e["isEbook"]],
    })

    book = asyncio.run(books.status(None))["books"][0]

    assert "no single monitored audiobook edition" in book["summary"]
    assert "re-imported against this one" not in book["summary"]
    assert ledger.load(books.BOOK_LEDGER)["9249"]["import_forced_at"] is None


# ---- the history window, closed at the root ----

def test_history_is_read_per_book_and_deep_enough_to_hold_all_of_it(fake):
    """The five window bugs were guarded one reader at a time; the window itself
    is what made them possible, so pin it shut.

    Measured on a live install: the most-churned book in the ledger has 7
    history records and `totalRecords` equals page 1 for every book. The query
    is per-book, so the page only ever has to cover one book's events.
    """
    seen = {}

    def history(request):
        seen.update(dict(request.url.params))
        return {"records": []}

    ledger.save(books.BOOK_LEDGER, {"9250": {
        "title": "East of Eden", "author": "John Steinbeck", "state": "downloading",
        "requested_at": "2026-09-21T20:44:01Z",
    }})
    _qbt(fake, [])
    _abs(fake)
    fake("_chaptarr", {("GET", "/api/v1/queue"): {"records": []},
                       ("GET", "/api/v1/history"): history})

    asyncio.run(books.status(None))

    assert seen["bookId"] == "9250"                 # per book, so the page is cheap
    assert int(seen["pageSize"]) >= 100             # far past any real book's depth
    assert seen["sortDirection"] == "descending"


# ---- the three real dramatizations, and the VIP truth ----

# Verbatim from MAM. 9247 and 9249 were both grabbed, imported and
# later flagged `suspect`; the Pearl one was the 42 MB candidate for the mislabeled-import bug.
REAL_DRAMATIZATIONS = [
    "BBC R4 The Grapes of Wrath by John Steinbeck [ENG / MP3]",
    "BBC R4X CS - John Steinbeck - Of Mice and Men (R4 CS July 2013 rpt Mar 2010) by John Steinbeck [ENG / MP3]",
    "BBC R4 CS - East Of Eden by John Steinbeck [ENG / MP3]",
]


@pytest.mark.parametrize("title", REAL_DRAMATIZATIONS)
def test_the_three_that_were_actually_wrong_are_never_auto_picked(title):
    picked, reason = books.pick_release([_release(title, 164)])
    assert picked is None and reason == "dramatization_only"
    assert books._slim_releases([_release(title, 164)])[0]["nameable"] is True


def test_the_reply_says_the_unabridged_exists_and_needs_vip(fake):
    """MAM has seven unabridged Grapes of Wrath copies, all [VIP]. Saying "the
    only releases on MAM are dramatizations" is false, and hides the one fact
    the user can act on."""
    _qbt(fake, [])
    _abs(fake)
    _grapes_chaptarr(fake)

    result = asyncio.run(books.request(GRAPES_WANT[0], "gr:2931549"))

    assert result["status"] == "dramatization_only"
    # Not a claim about MAM's catalogue — a claim about this account.
    assert "this account can take" in result["message"]
    assert "the only releases on MAM" not in result["message"]
    # And the actionable truth.
    assert "unabridged copy does exist" in result["message"]
    assert "[VIP]" in result["message"] and "1126 MB" in result["message"]


def test_a_vip_study_guide_is_not_counted_as_an_unabridged_copy(fake):
    """MAM lists a 47 MB "CliffsNotes: The Grapes of Wrath" as [VIP]. Counting
    it overstates what waiting for VIP actually buys — the reply would promise
    8 unabridged copies when there are 7 (live, 2026-09-21)."""
    _qbt(fake, [])
    _abs(fake)
    # Same shape as the live reply: VIP ones are hidden, the takeable one is not.
    releases = {
        "releases": [_release("BBC R4 The Grapes of Wrath by John Steinbeck [ENG / MP3]", 164)],
        "hiddenReleases": [
            _release("The Grapes of Wrath: Penguin Modern Classics by John Steinbeck [VIP]", 1126),
            _release("CliffsNotes: The Grapes of Wrath by John Steinbeck, Kelly McGrath [ENG / MP3] [VIP]", 47),
        ],
    }
    _grapes_chaptarr(fake, releases)

    result = asyncio.run(books.request(GRAPES_WANT[0], "gr:2931549"))

    assert result["status"] == "dramatization_only"
    assert "(1 of them, largest 1126 MB)" in result["message"]     # not 2
    # And every release the message talks about is actually in the list.
    assert len(result["releases"]) == 3


# ---- send to Kindle ----

def _ebook_item(iid, title, fmt="epub", size_mb=1.2, lib="Ebooks"):
    return {"id": iid, "path": f"/{lib.lower()}/{title}",
            "media": {"metadata": {"title": title, "authorName": "Someone"},
                      "ebookFile": {"ebookFormat": fmt,
                                    "metadata": {"filename": f"{title}.{fmt}",
                                                 "size": int(size_mb * 1e6)}}}}


def _abs_kindle(fake, items, send=None):
    """ABS with a book library whose search returns `items`."""
    routes = {
        ("GET", "/api/libraries"): {"libraries": [{"id": "l1", "name": "Ebooks", "mediaType": "book"}]},
        ("GET", "/api/libraries/l1/search"): {"book": [{"libraryItem": i} for i in items]},
        ("POST", "/api/emails/send-ebook-to-device"): send if send is not None else {"ok": True},
    }
    for i in items:
        routes[("GET", f"/api/items/{i['id']}")] = i
    return fake("_abs", routes)


@pytest.mark.parametrize("fmt, size_mb, why", [
    ("epub", 1.2, None),
    ("azw3", 1.2, "Amazon only takes EPUB or PDF"),
    ("mobi", 1.2, "Amazon only takes EPUB or PDF"),
    ("epub", 62.0, "Amazon's limit is 50 MB"),
    ("pdf", 0.6, None),
])
def test_what_amazon_would_silently_drop_is_refused_here(fmt, size_mb, why):
    """Amazon bounces nothing, so an unchecked send just never arrives."""
    ebook = {"ebookFormat": fmt, "metadata": {"size": int(size_mb * 1e6)}}
    blocker = books.kindle_blocker(ebook)
    assert (blocker is None) is (why is None)
    if why:
        assert why in blocker


def test_no_ebook_file_is_not_sendable():
    assert "no ebook file" in books.kindle_blocker(None)


def test_sending_names_the_file_so_a_supplement_is_obvious(fake, monkeypatch):
    """The live test sent a 0.6 MB Audible PDF supplement by accident. A title
    alone cannot tell that from a 600-page novel, so the reply must."""
    monkeypatch.setattr(books, "KINDLE_DEVICE", "Test Kindle")
    item = _ebook_item("i1", "Platform Revolution", fmt="pdf", size_mb=0.6)
    abs_api = _abs_kindle(fake, [item])

    result = asyncio.run(books.kindle("Platform Revolution"))

    assert result["status"] == "sent"
    assert result["book"]["format"] == "pdf" and result["book"]["size_mb"] == 0.6
    assert result["book"]["filename"] == "Platform Revolution.pdf"
    assert result["device"] == books.KINDLE_DEVICE
    (method, path, body), = abs_api.writes()
    assert (method, path) == ("POST", "/api/emails/send-ebook-to-device")
    assert body == {"libraryItemId": "i1", "deviceName": books.KINDLE_DEVICE}


def test_several_matches_ask_rather_than_guess(fake):
    items = [_ebook_item("i1", "East of Eden", size_mb=1.4),
             _ebook_item("i2", "East of Eden", fmt="pdf", size_mb=0.2)]
    abs_api = _abs_kindle(fake, items)

    result = asyncio.run(books.kindle("East of Eden"))

    assert result["status"] == "ambiguous"
    assert abs_api.writes() == []                       # nothing sent
    assert {c["size_mb"] for c in result["candidates"]} == {1.4, 0.2}


def test_a_book_with_no_ebook_is_reported_not_sent(fake):
    audio_only = {"id": "i9", "path": "/audiobooks/x",
                  "media": {"metadata": {"title": "The Pearl"}, "audioFiles": [{}]}}
    abs_api = _abs_kindle(fake, [audio_only])

    result = asyncio.run(books.kindle("The Pearl"))

    assert result["status"] == "not_found"
    assert abs_api.writes() == []


@pytest.mark.parametrize("code, expect", [
    (404, "no e-reader device named"),
    (403, "not allowed to use"),
])
def test_abs_refusals_say_which_one_it_was(fake, code, expect):
    item = _ebook_item("i1", "Alice")
    _abs_kindle(fake, [item], send=httpx.Response(code))

    result = asyncio.run(books.kindle("Alice", "Test Kindle"))

    assert result["status"] == "no_device"
    assert expect in result["message"]



def test_no_device_configured_or_passed_sends_nothing(fake, monkeypatch):
    """No personal default: without ABS_KINDLE_DEVICE or `device`, say so."""
    monkeypatch.setattr(books, "KINDLE_DEVICE", "")
    abs_api = _abs_kindle(fake, [_ebook_item("i1", "Alice")])

    result = asyncio.run(books.kindle("Alice"))

    assert result["status"] == "no_device"
    assert "ABS_KINDLE_DEVICE" in result["message"]
    assert abs_api.writes() == []


def test_an_explicit_device_needs_no_default(fake, monkeypatch):
    monkeypatch.setattr(books, "KINDLE_DEVICE", "")
    abs_api = _abs_kindle(fake, [_ebook_item("i1", "Alice")])

    result = asyncio.run(books.kindle("Alice", "Reader"))

    assert (result["status"], result["device"]) == ("sent", "Reader")
    assert abs_api.writes()[0][2]["deviceName"] == "Reader"

# ---- ebook requests ----

# Verbatim from MAM, 2026-09-21: the only real East of Eden ebooks. Two AZW3 and
# a MOBI that Amazon refuses, against one multi-format release that it accepts.
EOE_EBOOKS = [
    "East of Eden by John Steinbeck [ENG / AZW3 EPUB MOBI PDF]",
    "East of Eden by John Steinbeck [ENG / AZW3]",
    "East of Eden by John Steinbeck [ENG / MOBI]",
]


@pytest.mark.parametrize("title, ok", [
    (EOE_EBOOKS[0], True),      # multi-format: has EPUB
    (EOE_EBOOKS[1], False),     # AZW3 only
    (EOE_EBOOKS[2], False),     # MOBI only
    ("Some Book [ENG / EPUB]", True),
    ("Some Book [ENG / PDF]", True),
])
def test_only_releases_amazon_accepts_count_as_kindle_ready(title, ok):
    assert books.kindle_ready({"title": title}) is ok


def test_quality_name_alone_is_not_trusted_for_format():
    """Chaptarr names only the FIRST format, so a multi-format release reads
    'AZW3' in `quality` while its title says it also has EPUB."""
    r = {"title": EOE_EBOOKS[0], "quality": {"quality": {"name": "AZW3"}}}
    assert books.kindle_ready(r) is True


def test_the_ebook_record_is_found_by_the_work_both_formats_share():
    """A title has a SEPARATE record per format, joined by baseBookId."""
    hit = {"title": "East of Eden", "baseBookId": "hc:338117", "foreignBookId": "gr:2574991"}
    records = [
        {"id": 9250, "mediaType": "audiobook", "foreignBookId": "hc:338117", "title": "East of Eden"},
        {"id": 20670, "mediaType": "ebook", "foreignBookId": "hc:338117", "title": "East of Eden"},
        {"id": 999, "mediaType": "ebook", "foreignBookId": "hc:99", "title": "East of Eden Letters"},
    ]
    assert books.ebook_record(records, hit)["id"] == 20670


def test_a_longer_title_is_not_the_ebook_we_asked_for():
    """No baseBookId, so it falls back to titles — both ways round."""
    hit = {"title": "East of Eden"}
    only_letters = [{"id": 999, "mediaType": "ebook", "title": "Journal of a Novel: The East of Eden Letters"}]
    assert books.ebook_record(only_letters, hit) is None


def test_enabling_an_author_never_pulls_the_whole_bibliography():
    """Without this, enabling Steinbeck makes 588 ebooks 'wanted'."""
    fields = books.author_ebook_fields()
    assert fields["ebookMonitorNewItems"] == "none"
    assert fields["ebookRootFolderPath"] == books.EBOOK_ROOT_FOLDER
    assert fields["ebookQualityProfileId"] == books.EBOOK_QUALITY_PROFILE


def test_an_ebook_is_not_run_through_the_audio_tag_check(fake):
    """It has no audio tags, so the check would wait for a scan that never
    satisfies it — the shape that hung a book in `verifying` for ever."""
    ledger.save(books.BOOK_LEDGER, {"20670": {
        "title": "East of Eden", "author": "John Steinbeck", "state": "verifying",
        "format": "ebook", "imported_path": "/music/books/ebooks/John Steinbeck/East of Eden.epub",
        "requested_at": "2026-09-21T20:00:00Z",
    }})
    _qbt(fake, [])
    _abs(fake)
    fake("_chaptarr", {("GET", "/api/v1/queue"): {"records": []},
                       ("GET", "/api/v1/history"): {"records": []}})

    book = asyncio.run(books.status(None))["books"][0]

    assert (book["state"], book["content"]) == ("imported", "unverified")
    assert "reads audio tags" in book["summary"]
    assert "check it's the right book" not in book["summary"]   # nothing to check


def test_new_ebook_author_payload_enables_ebooks_without_pulling_the_bibliography():
    """Both monitor switches are load-bearing: 588 books came in without them."""
    p = books.new_ebook_author_payload({"authorName": "Gene Kim", "foreignAuthorId": "gr:328437"})

    assert p["ebookMonitored"] is True
    assert p["ebookMonitorNewItems"] == "none"
    assert p["addOptions"] == {"monitor": "none", "searchForMissingBooks": False}
    assert p["ebookRootFolderPath"] == books.EBOOK_ROOT_FOLDER
    # this author is here for an ebook; don't start monitoring their audiobooks
    assert p["audiobookMonitored"] is False
    assert p["audiobookMonitorNewItems"] == "none"
    assert p["id"] == 0


def test_an_author_absent_locally_is_added_rather_than_refused(fake):
    """Gene Kim, Steve Davies and Christopher Scotton all resolve upstream with
    bookCount 0 — only the local row was missing."""
    posted = {}

    def capture(request):
        posted.update(json.loads(request.content))
        return {"id": 77, "authorName": "Gene Kim"}

    fake("_chaptarr", {
        ("GET", "/api/v1/author"): [],                                  # nothing local
        ("GET", "/api/v1/author/lookup"): [{"authorName": "Gene Kim", "foreignAuthorId": "gr:328437"}],
        ("POST", "/api/v1/author"): capture,
        ("GET", "/api/v1/author/77"): {"id": 77, "authorName": "Gene Kim", **books.author_ebook_fields()},
        ("GET", "/api/v1/book"): [{"id": 501, "mediaType": "ebook", "title": "The Phoenix Project",
                                   "monitored": True, "ebookMonitored": True}],
    })
    hit = {"title": "The Phoenix Project", "author": {"authorName": "Gene Kim"}}

    book, why = asyncio.run(books._ebook_book_record(hit))

    assert why is None and book["id"] == 501
    assert posted["authorName"] == "Gene Kim"
    assert posted["ebookMonitorNewItems"] == "none", "must not pull the bibliography"


def test_a_chaptarr_failure_while_adding_falls_back_to_no_author(fake):
    """A backend failure must not raise out of a request — but it must not be
    silently mistaken for an unresolvable name either; it prints the cause."""
    fake("_chaptarr", {
        ("GET", "/api/v1/author"): [],
        ("GET", "/api/v1/author/lookup"): httpx.Response(500),
    })

    book, why = asyncio.run(books._ebook_book_record({"title": "X", "author": {"authorName": "Gene Kim"}}))

    assert book is None and why == "no_author"


def test_a_lookup_that_returns_a_different_author_is_not_added(fake):
    """A fuzzy upstream match must not silently add the wrong person."""
    fake("_chaptarr", {
        ("GET", "/api/v1/author"): [],
        ("GET", "/api/v1/author/lookup"): [{"authorName": "Gene Kimball", "foreignAuthorId": "gr:99"}],
    })

    book, why = asyncio.run(books._ebook_book_record({"title": "X", "author": {"authorName": "Gene Kim"}}))

    assert book is None and why == "no_author"


@pytest.mark.parametrize("author_known, enabled, expect", [
    # Now an author missing locally is ADDED, so the only way to still
    # fail here is the metadata source not resolving the name either.
    (False, False, "could not add"),         # unresolvable upstream too
    (True, False, "only just started"),      # just enabled: records may still be coming
    (True, True, "None of"),                 # enabled long ago: a real miss
])
def test_the_ebook_failures_are_reported_as_different_things(fake, monkeypatch, author_known, enabled, expect):
    """One message for both would be false half the time — the same shape as
    every other message bug fixed today."""
    monkeypatch.setattr(books, "EBOOK_RECORD_POLL_S", 0)
    _qbt(fake, [])
    _abs(fake)
    ebook_fields = books.author_ebook_fields() if enabled else {}
    routes = {
        ("GET", "/api/v1/book/lookup"): [{
            "title": EOE_WANT[0], "foreignBookId": "gr:2574991", "localBookId": "0",
            "author": {"authorName": EOE_WANT[1]}, "editions": [{"monitored": False}],
        }],
        ("GET", "/api/v1/author"): ([{"id": 31, "authorName": EOE_WANT[1]}] if author_known else []),
        ("GET", "/api/v1/author/lookup"): [],   # nothing to add either
        ("GET", "/api/v1/author/31"): {"id": 31, "authorName": EOE_WANT[1], **ebook_fields},
        ("PUT", "/api/v1/author/31"): {"id": 31, "authorName": EOE_WANT[1]},
        # Author known but only an audiobook record exists for this title.
        ("GET", "/api/v1/book"): [{"id": 9250, "mediaType": "audiobook",
                                   "title": EOE_WANT[0], "foreignBookId": "gr:2574991"}],
    }
    chaptarr = fake("_chaptarr", routes)

    result = asyncio.run(books.request(EOE_WANT[0], "gr:2574991", None, "ebook"))

    assert result["status"] == "no_ebook_record"
    assert expect in result["message"]
    assert not [w for w in chaptarr.writes() if w[1] == "/api/v1/release"]   # nothing grabbed


# The five ebook records a both-ways title match really returns for "East of
# Eden" among Steinbeck's 588 (measured live). Only one is it.
REAL_EBOOK_RECORDS = [
    {"id": 20670, "mediaType": "ebook", "foreignBookId": "hc:338117", "title": "East of Eden"},
    {"id": 20712, "mediaType": "ebook", "foreignBookId": "gr:298983526", "title": "East of Eden & Grapes Of Wrath"},
    {"id": 20943, "mediaType": "ebook", "foreignBookId": "gr:758040", "title": "East of Eden: Curriculum Unit"},
    {"id": 20953, "mediaType": "ebook", "foreignBookId": "gr:101374219", "title": "East of Eden: Dramatisation"},
    {"id": 21227, "mediaType": "ebook", "foreignBookId": "gr:296864970", "title": "East of Den"},
]


def test_the_right_ebook_is_picked_out_of_a_real_bibliography():
    """A lookup hit carries the AUDIOBOOK's foreign id (gr:) while the ebook
    record has the work's (hc:), so the join falls to the title — where five
    records match and four are junk."""
    hit = {"title": "East of Eden", "foreignBookId": "gr:2574991"}
    assert books.ebook_record(REAL_EBOOK_RECORDS, hit)["id"] == 20670


def test_the_shared_work_id_wins_when_the_hit_has_one():
    hit = {"title": "East of Eden", "baseBookId": "hc:338117"}
    assert books.ebook_record(REAL_EBOOK_RECORDS, hit)["id"] == 20670


@pytest.mark.parametrize("title", [
    "East of Eden: Dramatisation", "East of Eden: Curriculum Unit",
    "East of Eden & Grapes Of Wrath",
])
def test_the_junk_records_are_never_returned_on_their_own(title):
    """Even as the ONLY candidate, a dramatization, study guide or omnibus is
    not the book that was asked for."""
    only_junk = [r for r in REAL_EBOOK_RECORDS if r["title"] == title]
    assert books.ebook_record(only_junk, {"title": "East of Eden"}) is None


@pytest.mark.parametrize("record_title, same", [
    ("East of Eden", True),
    ("East of Eden (Penguin Classics)", True),     # an edition, same work
    ("East of Eden: Curriculum Unit", False),      # a subtitle is a different work
    ("East of Eden: Dramatisation", False),
    ("East of Eden & Grapes Of Wrath", False),
    ("East of Den", False),                        # typo record
    ("", False),
])
def test_only_bracketed_edition_noise_still_counts_as_the_same_work(record_title, same):
    assert books._same_work(record_title, "East of Eden") is same


# ---- embedding a cover, without breaking the torrent ----

MINIMAL_OPF = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">'
    '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
    '<dc:title>The Grapes of Wrath</dc:title><dc:identifier id="id">x</dc:identifier>'
    '</metadata><manifest>'
    '<item id="t" href="text.html" media-type="application/xhtml+xml"/>'
    '</manifest><spine><itemref idref="t"/></spine></package>'
)


def _make_epub(path, opf=MINIMAL_OPF, opf_name="OEBPS/book.opf"):
    import zipfile
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", "<container/>")
        z.writestr(opf_name, opf)
        z.writestr("OEBPS/text.html", "<html><body>chapter</body></html>")
    return path


def test_a_book_with_no_declared_cover_gets_one(tmp_path):
    import zipfile
    epub = _make_epub(tmp_path / "book.epub")
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"\xff\xd8\xff-jpeg-bytes")

    assert books.epub_cover_state(str(epub))[1] is False
    assert books.embed_cover(str(epub), cover.read_bytes()) is True

    opf, declared = books.epub_cover_state(str(epub))
    assert declared is True
    with zipfile.ZipFile(epub) as z:
        names = z.namelist()
        assert names[0] == "mimetype"                       # still a valid EPUB
        assert z.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
        assert "OEBPS/landible-cover.jpg" in names
        assert z.read("OEBPS/landible-cover.jpg") == cover.read_bytes()
        assert z.read("OEBPS/text.html") == b"<html><body>chapter</body></html>"
        xml = z.read(opf).decode()
        assert 'name="cover"' in xml and 'properties="cover-image"' in xml


def test_the_seeding_copy_is_left_byte_for_byte_alone(tmp_path):
    """The library file and the torrent are ONE file with two names. Editing in
    place would corrupt the torrent and turn a seed into a hit & run."""
    seeding = _make_epub(tmp_path / "seeding.epub")
    before = seeding.read_bytes()
    library = tmp_path / "library.epub"
    os.link(seeding, library)                               # exactly what the import does
    assert os.stat(seeding).st_ino == os.stat(library).st_ino
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"jpeg")

    assert books.embed_cover(str(library), cover.read_bytes()) is True

    assert seeding.read_bytes() == before                   # the torrent is untouched
    assert os.stat(seeding).st_ino != os.stat(library).st_ino   # the link was broken
    assert os.stat(seeding).st_nlink == 1
    assert books.epub_cover_state(str(library))[1] is True
    assert not list(tmp_path.glob("*.landible-tmp"))         # no debris


def test_a_book_that_already_has_a_cover_is_not_rewritten(tmp_path):
    opf = MINIMAL_OPF.replace("</metadata>", '<meta name="cover" content="c"/></metadata>')
    epub = _make_epub(tmp_path / "book.epub", opf=opf)
    before = epub.read_bytes()
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"jpeg")

    assert books.embed_cover(str(epub), cover.read_bytes()) is False
    assert epub.read_bytes() == before


def test_no_cover_to_embed_is_not_an_error(tmp_path):
    epub = _make_epub(tmp_path / "book.epub")
    assert books.embed_cover(str(epub), None) is False
    assert books.embed_cover(str(epub), b"") is False
    assert books.find_cover(None) is None
    assert books.find_cover(str(tmp_path / "nope")) is None


def test_the_cover_is_found_beside_the_book(tmp_path):
    (tmp_path / "Cover.JPG").write_bytes(b"jpeg")           # case as uploaders write it
    assert books.find_cover(str(tmp_path)).endswith("Cover.JPG")


def test_the_download_folder_is_found_by_the_link_not_a_stored_path(tmp_path, monkeypatch):
    """The library copy and the seeding copy are the same inode, so the link is
    the join — it cannot go stale the way a remembered path can."""
    mam = tmp_path / "mam" / "Some Book (123)"
    mam.mkdir(parents=True)
    seeding = mam / "book.epub"
    seeding.write_bytes(b"x")
    library = tmp_path / "library.epub"
    os.link(seeding, library)
    monkeypatch.setattr(books, "MAM_ROOT", str(tmp_path / "mam"))

    assert books.source_folder(str(library)) == str(mam)
    assert books.source_folder(str(tmp_path / "not-a-file")) is None


# ---- fetching a cover worth having ----

def _jpeg(width, height, payload_bytes):
    """A JPEG with real SOF0 dimensions, padded to a chosen size.

    Size vs pixels is the signal that separates a photograph from line art, so
    the tests have to control both.
    """
    sof = b"\xff\xc0\x00\x11\x08" + height.to_bytes(2, "big") + width.to_bytes(2, "big")
    head = b"\xff\xd8" + sof
    return head + b"\x00" * max(0, payload_bytes - len(head))


# The three real images this had to tell apart, measured on 2026-09-22.
OL_PLACEHOLDER = b"<html>not found</html>" + b" " * 21          # 43 bytes, HTTP 200
GOODREADS_THUMB = _jpeg(125, 193, 6302)
PENGUIN_LOGO = _jpeg(590, 750, 33938)                           # 0.077 B/px — line art
REAL_COVER = _jpeg(308, 475, 28106)                             # 0.192 B/px — a photo


@pytest.mark.parametrize("data, usable, why", [
    (REAL_COVER, True, "the Penguin Modern Classics cover"),
    (OL_PLACEHOLDER, False, "Open Library's 43-byte 200 for an unknown ISBN"),
    (GOODREADS_THUMB, False, "125x193 is a thumbnail, not a cover"),
    (PENGUIN_LOGO, False, "a publisher colophon, not cover art"),
    (b"", False, "nothing at all"),
])
def test_only_a_real_cover_is_usable(data, usable, why):
    assert bool(books.cover_quality(data)) is usable, why


def test_the_bigger_real_cover_wins():
    assert books.cover_quality(_jpeg(600, 900, 90000)) > books.cover_quality(REAL_COVER)


def test_isbns_come_from_the_epub_itself(tmp_path):
    """9780141185064 identified the exact printing we downloaded."""
    opf = MINIMAL_OPF.replace("<dc:identifier id=\"id\">x</dc:identifier>",
                              "<dc:identifier id=\"id\">9780141185064</dc:identifier>")
    epub = _make_epub(tmp_path / "b.epub", opf=opf, opf_name="OEBPS/9780141185064.opf")
    assert books.epub_isbns(str(epub)) == ["9780141185064"]


def test_open_library_is_preferred_over_what_the_uploader_shipped(tmp_path):
    epub = _make_epub(tmp_path / "b.epub", opf_name="OEBPS/9780141185064.opf")
    shipped = tmp_path / "cover.jpg"
    shipped.write_bytes(PENGUIN_LOGO)

    got = books.best_cover(str(epub), str(shipped), fetch=lambda url: REAL_COVER)

    assert got == REAL_COVER          # not the logo, even though it is bigger


def test_the_shipped_cover_is_used_when_the_lookup_has_nothing(tmp_path):
    epub = _make_epub(tmp_path / "b.epub", opf_name="OEBPS/9780141185064.opf")
    shipped = tmp_path / "cover.jpg"
    shipped.write_bytes(REAL_COVER)

    got = books.best_cover(str(epub), str(shipped), fetch=lambda url: OL_PLACEHOLDER)

    assert got == REAL_COVER


def test_a_cover_service_that_is_down_never_fails_the_import(tmp_path):
    """Best-effort: a book landing in the library must not wait on a picture."""
    epub = _make_epub(tmp_path / "b.epub", opf_name="OEBPS/9780141185064.opf")

    def boom(url):
        raise OSError("connection refused")

    assert books.best_cover(str(epub), None, fetch=boom) is None
    assert books.embed_cover(str(epub), None) is False


def test_the_epub2_form_is_always_written(tmp_path):
    """The real MAM file is package version=2.0, where properties="cover-image"
    means nothing — writing only the EPUB 3 form shows a blank DOC tile."""
    import zipfile
    epub = _make_epub(tmp_path / "b.epub")
    assert books.embed_cover(str(epub), REAL_COVER) is True
    with zipfile.ZipFile(epub) as z:
        opf = next(n for n in z.namelist() if n.endswith(".opf"))
        xml = z.read(opf).decode()
    assert '<meta name="cover" content="landible-cover"' in xml


# ---- cancelling a request that will never finish ----

def _stuck_download(**over):
    ledger.save(books.BOOK_LEDGER, {"9249": {
        "title": "Of Mice and Men", "author": "John Steinbeck", "state": "downloading",
        "requested_at": "2026-09-18T19:00:00Z", **over,
    }})


def test_cancelling_stops_chaptarr_but_never_the_torrent(fake):
    """Removing or pausing a MAM torrent is a hit & run — the invariant this
    whole file is built around, and cancelling is not a reason to break it."""
    _stuck_download()
    queue = {"records": [{"id": 77, "bookId": 9249, "title": "Of Mice and Men"}]}
    chaptarr = fake("_chaptarr", {("GET", "/api/v1/queue"): queue,
                                  ("DELETE", "/api/v1/queue/77"): {}})

    result = asyncio.run(books.cancel("9249"))

    assert result["status"] == "cancelled"
    (method, path, _body), = chaptarr.writes()
    assert (method, path) == ("DELETE", "/api/v1/queue/77")
    assert ledger.load(books.BOOK_LEDGER)["9249"]["state"] == "cancelled"
    # The user will cancel expecting capacity back. Say plainly that they do not get it.
    assert "keeps seeding" in result["message"]
    assert "does NOT give the MAM slot back" in result["message"]


def test_cancelling_when_chaptarr_has_already_forgotten_it(fake):
    """The ledger is what actually stops status() and book_stuck acting."""
    _stuck_download()
    fake("_chaptarr", {("GET", "/api/v1/queue"): {"records": []}})

    result = asyncio.run(books.cancel("9249"))

    assert result["status"] == "cancelled"
    assert "not in Chaptarr's queue" in result["message"]
    assert ledger.load(books.BOOK_LEDGER)["9249"]["state"] == "cancelled"


@pytest.mark.parametrize("state", ["imported", "cancelled"])
def test_there_is_nothing_to_cancel_for_these(fake, state):
    _stuck_download(state=state)
    chaptarr = fake("_chaptarr", {("GET", "/api/v1/queue"): {"records": []}})

    result = asyncio.run(books.cancel("9249"))

    assert result["status"] == "not_cancellable"
    assert chaptarr.writes() == []
    assert ledger.load(books.BOOK_LEDGER)["9249"]["state"] == state


def test_cancelling_a_book_that_was_never_requested(fake):
    ledger.save(books.BOOK_LEDGER, {})
    fake("_chaptarr", {})
    assert asyncio.run(books.cancel("1234"))["status"] == "not_found"


def test_a_cancelled_book_drops_out_of_the_live_checks(fake):
    """The third consumer that switches on state — missing one is how a bug
    happened, so all three are asserted."""
    _stuck_download(state="cancelled", reason="cancelled by request")
    _qbt(fake, [])
    _abs(fake)
    chaptarr = fake("_chaptarr", {("GET", "/api/v1/queue"): {"records": []}})

    book = asyncio.run(books.status(None))["books"][0]

    assert book["state"] == "cancelled"
    assert "still seeding" in book["summary"]         # summarize() knows it
    assert chaptarr.writes() == []                    # and status() left it alone


# ---- retracting a wrong import ----

def _wrong_ebook(**over):
    # The real case: a Greek edition of The Martian, imported as an ebook,
    # which the (audio) tag check can never catch.
    ledger.save(books.BOOK_LEDGER, {"22885": {
        "title": "The Martian", "author": "Andy Weir", "format": "ebook",
        "state": "imported", "content": "unverified", "grab_history_id": 501,
        "file_id": 9001, "torrent_hash": "deadbeef", **over,
    }})


def test_retract_blocklists_unmonitors_and_removes_the_library_copy(fake):
    _wrong_ebook()
    chaptarr = fake("_chaptarr", {
        ("POST", "/api/v1/history/failed/501"): {},
        ("PUT", "/api/v1/book/monitor"): {},
        ("DELETE", "/api/v1/bookfile/9001"): {},
    })
    # qbittorrent-mam is left with no routes: any call to it fails the test.

    result = asyncio.run(books.retract("22885", "Greek edition"))

    assert result["status"] == "retracted"
    # Blocklist BEFORE the delete: the other order can leave a deleted book
    # whose release is still eligible to be grabbed again.
    assert [(m, p) for m, p, _ in chaptarr.writes()] == [
        ("POST", "/api/v1/history/failed/501"),
        ("PUT", "/api/v1/book/monitor"),
        ("DELETE", "/api/v1/bookfile/9001"),
    ]
    assert chaptarr.writes()[1][2] == {"bookIds": [22885], "monitored": False}
    entry = ledger.load(books.BOOK_LEDGER)["22885"]
    assert entry["state"] == "retracted"
    assert "Greek edition" in entry["reason"] and "release blocklisted" in entry["reason"]
    assert "keeps seeding" in result["message"]


def test_retract_when_the_grab_has_aged_out_of_history(fake):
    """No blocklist possible — say so rather than failing the whole retract."""
    _wrong_ebook()
    fake("_chaptarr", {
        ("POST", "/api/v1/history/failed/501"): httpx.Response(404),
        ("PUT", "/api/v1/book/monitor"): {},
        ("DELETE", "/api/v1/bookfile/9001"): {},
    })

    result = asyncio.run(books.retract("22885", "Greek edition"))

    assert result["status"] == "retracted"
    assert "NOT blocklisted" in result["message"]
    assert "NOT blocklisted" in ledger.load(books.BOOK_LEDGER)["22885"]["reason"]


def test_retract_with_no_file_on_record_says_to_check_by_hand(fake):
    _wrong_ebook(file_id=None, grab_history_id=None)
    chaptarr = fake("_chaptarr", {("PUT", "/api/v1/book/monitor"): {}})

    result = asyncio.run(books.retract("22885", "wrong book"))

    assert result["status"] == "retracted"
    assert [(m, p) for m, p, _ in chaptarr.writes()] == [("PUT", "/api/v1/book/monitor")]
    assert "check the library by hand" in result["message"]


@pytest.mark.parametrize("state", ["requested", "downloading", "verifying", "failed", "cancelled", "retracted"])
def test_only_an_imported_book_can_be_retracted(fake, state):
    _wrong_ebook(state=state)
    chaptarr = fake("_chaptarr", {})

    result = asyncio.run(books.retract("22885", "x"))

    assert result["status"] == "not_retractable"
    assert chaptarr.writes() == []
    assert ledger.load(books.BOOK_LEDGER)["22885"]["state"] == state


def test_retracting_a_book_that_was_never_requested(fake):
    ledger.save(books.BOOK_LEDGER, {})
    assert asyncio.run(books.retract("1234", "x"))["status"] == "not_found"


def test_a_retracted_book_is_not_reported_as_in_the_library(fake):
    """The bug this exists for: status kept calling the Greek Martian imported."""
    _wrong_ebook(state="retracted", reason="Greek edition; release blocklisted")
    _qbt(fake, [])
    _abs(fake)
    chaptarr = fake("_chaptarr", {("GET", "/api/v1/queue"): {"records": []}})

    book = asyncio.run(books.status(None))["books"][0]

    assert book["state"] == "retracted"
    assert "not in the library" in book["summary"]
    assert chaptarr.writes() == []


def test_a_missing_library_item_does_not_count_as_in_the_library(fake):
    """After a retract the files are gone but ABS can keep the item (isMissing),
    and a re-request must not be refused as `in_library` because of it."""
    fake("_abs", {
        ("GET", "/api/libraries"): {"libraries": [{"id": "lib1", "name": "Ebooks", "mediaType": "book"}]},
        ("GET", "/api/libraries/lib1/search"): {"book": [
            {"libraryItem": {"isMissing": True, "media": {"metadata": {"title": "The Martian", "authorName": "Andy Weir"}}}},
        ]},
    })
    assert asyncio.run(books._abs_search("The Martian")) == []


def test_cancel_points_an_imported_book_at_retract(fake):
    _wrong_ebook()
    fake("_chaptarr", {("GET", "/api/v1/queue"): {"records": []}})
    assert "landible_book_retract" in asyncio.run(books.cancel("22885"))["message"]


# ---- the library entry is a different book ----

@pytest.mark.parametrize("item_title, flagged", [
    # The real incident: East of Eden imported correctly and sat in the library
    # under the dead radio play whose folder had just been deleted.
    ("Classic Serial - John Steinbeck - Of Mice and Men", True),
    ("East of Eden", False),
    ("East of Eden (Penguin Classics)", False),      # an edition, same book
    ("", False),                                     # missing metadata, not a mislabel
    (None, False),
])
def test_a_relabelled_library_entry_is_noticed(item_title, flagged):
    why = books.library_mismatch(item_title, "East of Eden")
    assert bool(why) is flagged
    if flagged:
        assert item_title in why


def test_the_file_can_be_right_while_the_library_is_wrong(fake):
    """Nothing else catches this: the content check reads the FILE's tags and
    finds them correct, and in_library comes from Chaptarr's file count."""
    ledger.save(books.BOOK_LEDGER, {"9250": {
        "title": "East of Eden", "author": "John Steinbeck", "state": "verifying",
        "requested_at": "2026-09-21T20:00:00Z",
        "imported_path": "/music/books/audiobooks/John Steinbeck/East of Eden/x.m4b",
    }})
    _qbt(fake, [])
    # ABS shows the item under the OLD book's name, with the right file inside.
    item = {"id": "i1", "path": "/audiobooks/John Steinbeck/East of Eden",
            "media": {"metadata": {"title": "Classic Serial - John Steinbeck - Of Mice and Men"},
                      "audioFiles": [{"metaTags": {"tagAlbum": "East of Eden",
                                                   "tagArtist": "John Steinbeck"}}]}}
    fake("_abs", {
        ("GET", "/api/libraries"): {"libraries": [{"id": "l1", "name": "Audiobooks", "mediaType": "book"}]},
        ("GET", "/api/libraries/l1/search"): {"book": [{"libraryItem": item}]},
        ("GET", "/api/libraries/l1/items"): {"results": [item]},
        ("GET", "/api/items/i1"): item,
    })
    fake("_chaptarr", {("GET", "/api/v1/queue"): {"records": []},
                       ("GET", "/api/v1/history"): {"records": []}})

    book = asyncio.run(books.status(None))["books"][0]

    assert book["content"] == "ok"                   # the file really is fine
    assert "the library lists it as" in book["summary"]
    # The dangerous instinct is to delete the odd-looking entry. Say not to.
    assert "Do NOT delete it" in book["summary"]
    assert ledger.load(books.BOOK_LEDGER)["9250"]["library_mismatch"]


def test_a_correctly_labelled_import_records_no_mismatch(fake):
    ledger.save(books.BOOK_LEDGER, {"9250": {
        "title": "East of Eden", "author": "John Steinbeck", "state": "verifying",
        "requested_at": "2026-09-21T20:00:00Z",
        "imported_path": "/music/books/audiobooks/John Steinbeck/East of Eden/x.m4b",
    }})
    _qbt(fake, [])
    item = {"id": "i1", "path": "/audiobooks/John Steinbeck/East of Eden",
            "media": {"metadata": {"title": "East of Eden"},
                      "audioFiles": [{"metaTags": {"tagAlbum": "East of Eden",
                                                   "tagArtist": "John Steinbeck"}}]}}
    fake("_abs", {
        ("GET", "/api/libraries"): {"libraries": [{"id": "l1", "name": "Audiobooks", "mediaType": "book"}]},
        ("GET", "/api/libraries/l1/search"): {"book": [{"libraryItem": item}]},
        ("GET", "/api/libraries/l1/items"): {"results": [item]},
        ("GET", "/api/items/i1"): item,
    })
    fake("_chaptarr", {("GET", "/api/v1/queue"): {"records": []},
                       ("GET", "/api/v1/history"): {"records": []}})

    book = asyncio.run(books.status(None))["books"][0]

    assert book["content"] == "ok"
    assert "library lists it as" not in book["summary"]
    assert ledger.load(books.BOOK_LEDGER)["9250"].get("library_mismatch") is None


# ---- the ebook-record race (#20) ----

FOUR_WINDS_HIT = {"title": "The Four Winds", "foreignBookId": "gr:79888572", "localBookId": "0",
                  "author": {"authorName": "Kristin Hannah"}}
FOUR_WINDS_EBOOK = {"id": 29976, "mediaType": "ebook", "foreignBookId": "hc:259391", "title": "The Four Winds",
                    "authorId": 43, "monitored": True, "ebookMonitored": True}


def _hannah(fake, enabled, book_reads):
    """Kristin Hannah in Chaptarr; `book_reads` is what each GET /book returns, in turn."""
    reads = iter(book_reads)
    return fake("_chaptarr", {
        ("GET", "/api/v1/author"): [{"id": 43, "authorName": "Kristin Hannah"}],
        ("GET", "/api/v1/author/43"): {"id": 43, **(books.author_ebook_fields() if enabled else {})},
        ("PUT", "/api/v1/author/43"): {"id": 43},
        ("GET", "/api/v1/book"): lambda request: next(reads),
    })


def test_the_ebook_record_is_waited_for_after_enabling_the_author(fake, monkeypatch):
    """The Four Winds: refused as `no_ebook_record`, and Chaptarr created the
    record seconds later. Enabling the author starts a background refresh."""
    monkeypatch.setattr(books, "EBOOK_RECORD_POLL_S", 0)
    audio_only = [{"id": 12514, "mediaType": "audiobook", "title": "The Four Winds"}]
    chaptarr = _hannah(fake, enabled=False, book_reads=[audio_only, audio_only, [*audio_only, FOUR_WINDS_EBOOK]])

    book, why = asyncio.run(books._ebook_book_record(FOUR_WINDS_HIT))

    assert why is None and book["id"] == 29976
    assert [c[1] for c in chaptarr.calls].count("/api/v1/book") == 3


def test_an_author_enabled_long_ago_is_not_waited_for(fake, monkeypatch):
    monkeypatch.setattr(books, "EBOOK_RECORD_POLL_S", 60)   # a wait would hang the test
    chaptarr = _hannah(fake, enabled=True, book_reads=[[]])

    book, why = asyncio.run(books._ebook_book_record(FOUR_WINDS_HIT))

    assert book is None and why == "no_record"
    assert [c[1] for c in chaptarr.calls].count("/api/v1/book") == 1


def test_records_that_never_come_say_so_after_the_wait(fake, monkeypatch):
    monkeypatch.setattr(books, "EBOOK_RECORD_POLL_S", 0)
    chaptarr = _hannah(fake, enabled=False, book_reads=[[]] * books.EBOOK_RECORD_POLLS)

    book, why = asyncio.run(books._ebook_book_record(FOUR_WINDS_HIT))

    assert book is None and why == "records_pending"
    assert [c[1] for c in chaptarr.calls].count("/api/v1/book") == books.EBOOK_RECORD_POLLS


# The real edition list's shape for The Four Winds' ebook record (55 editions, one monitored).
FOUR_WINDS_EDITIONS = [
    {"id": 74165, "bookId": 29976, "monitored": True, "isEbook": True, "format": "ebook"},
    {"id": 74166, "bookId": 29976, "monitored": False, "isEbook": True, "format": "ebook"},
]


def test_an_ebook_record_has_an_ebook_edition_to_import_against():
    assert books.monitored_edition(FOUR_WINDS_EDITIONS, "ebook") == 74165
    assert books.monitored_edition(FOUR_WINDS_EDITIONS) is None
    assert books.monitored_edition(MICE_EDITIONS) == 14983


# ---- a .torrent handed in (#20) ----

def _bencode(v):
    if isinstance(v, int):
        return b"i%de" % v
    if isinstance(v, str):
        v = v.encode()
    if isinstance(v, bytes):
        return b"%d:%s" % (len(v), v)
    if isinstance(v, list):
        return b"l" + b"".join(_bencode(x) for x in v) + b"e"
    return b"d" + b"".join(_bencode(k) + _bencode(v[k]) for k in sorted(v)) + b"e"


EPUB_INFO = {"name": "The Four Winds - Kristin Hannah.epub", "length": 1234, "piece length": 16384, "pieces": b"x" * 20}
EPUB_TORRENT = _bencode({"announce": "https://tracker.invalid/announce", "info": EPUB_INFO})
EPUB_HASH = __import__("hashlib").sha1(_bencode(EPUB_INFO)).hexdigest()
EPUB_B64 = __import__("base64").b64encode(EPUB_TORRENT).decode()
EPUB_PATH = f"{books.MAM_ROOT}/The Four Winds - Kristin Hannah.epub"


def test_torrent_info_reads_the_hash_and_files():
    info = books.torrent_info(EPUB_TORRENT)
    assert info == {"hash": EPUB_HASH, "name": EPUB_INFO["name"], "files": [EPUB_INFO["name"]]}

    multi = _bencode({"info": {"name": "Book", "piece length": 1, "pieces": b"",
                               "files": [{"length": 1, "path": ["01.mp3"]}, {"length": 1, "path": ["art", "cover.jpg"]}]}})
    assert books.torrent_info(multi)["files"] == ["01.mp3", "art/cover.jpg"]


@pytest.mark.parametrize("junk", [b"", b"<html>login</html>", EPUB_TORRENT[:40], _bencode({"announce": "x"})])
def test_anything_but_a_torrent_is_refused(junk):
    with pytest.raises(ValueError):
        books.torrent_info(junk)


def test_a_bad_torrent_touches_nothing(fake):
    # Every backend is a refusing fake, so any call would fail the test.
    result = asyncio.run(books.add_torrent("The Four Winds", "gr:79888572", "bm90IGEgdG9ycmVudA==", "ebook"))
    assert result["status"] == "bad_torrent"


def test_an_ebook_torrent_amazon_would_refuse_touches_nothing(fake):
    azw3 = __import__("base64").b64encode(_bencode({"info": {**EPUB_INFO, "name": "The Four Winds.azw3"}})).decode()
    result = asyncio.run(books.add_torrent("The Four Winds", "gr:79888572", azw3, "ebook"))
    assert result["status"] == "no_kindle_format"


def _hand_qbt(fake, present=False, takes=True):
    """qbittorrent-mam: `present` = the torrent is there before the add; `takes` = the add works."""
    state = {"there": present, "added": []}
    torrent = {"hash": EPUB_HASH, "name": EPUB_INFO["name"], "progress": 0, "state": "downloading",
               "content_path": EPUB_PATH, "seeding_time": 0}

    def info(request):
        assert request.url.params.get("hashes") in (None, EPUB_HASH)
        return [torrent] if state["there"] else []

    def add(request):
        state["added"].append(request.content)
        state["there"] = takes
        return httpx.Response(200, text="Ok.")

    api = fake("_qbt_mam", {
        ("POST", "/api/v2/auth/login"): httpx.Response(204),
        ("GET", "/api/v2/torrents/info"): info,
        ("POST", "/api/v2/torrents/createCategory"): httpx.Response(409),
        ("POST", "/api/v2/torrents/add"): add,
    })
    return api, state


def _four_winds_chaptarr(fake):
    return fake("_chaptarr", {
        ("GET", "/api/v1/book/lookup"): [FOUR_WINDS_HIT],
        ("GET", "/api/v1/author"): [{"id": 43, "authorName": "Kristin Hannah"}],
        ("GET", "/api/v1/author/43"): {"id": 43, **books.author_ebook_fields()},
        ("GET", "/api/v1/book"): [FOUR_WINDS_EBOOK],
    })


def test_a_handed_in_ebook_is_added_seeding_for_ever_and_tracked(fake):
    qbt, state = _hand_qbt(fake)
    _abs(fake)
    chaptarr = _four_winds_chaptarr(fake)

    result = asyncio.run(books.add_torrent("The Four Winds", "gr:79888572", EPUB_B64, "ebook"))

    assert result["status"] == "added" and result["book_id"] == 29976
    (form,) = state["added"]
    assert EPUB_TORRENT in form                                   # the file itself went up
    for field, value in (("category", books.HAND_CATEGORY), ("savepath", books.MAM_ROOT),
                         ("ratioLimit", "-1"), ("seedingTimeLimit", "-1")):
        assert b'name="%s"\r\n\r\n%s\r\n' % (field.encode(), value.encode()) in form
    assert chaptarr.writes() == []                                # nothing searched or grabbed in Chaptarr
    entry = ledger.load(books.BOOK_LEDGER)["29976"]
    assert entry["state"] == "downloading" and entry["hand_added"] and entry["torrent_hash"] == EPUB_HASH
    assert entry["format"] == "ebook" and entry["author_id"] == 43


def test_a_torrent_already_in_the_client_is_tracked_not_added_again(fake):
    qbt, state = _hand_qbt(fake, present=True)
    _abs(fake)
    _four_winds_chaptarr(fake)

    result = asyncio.run(books.add_torrent("The Four Winds", "gr:79888572", EPUB_B64, "ebook"))

    assert result["status"] == "adopted"
    assert state["added"] == [] and not [c for c in qbt.calls if c[1] == "/api/v2/torrents/createCategory"]
    assert ledger.load(books.BOOK_LEDGER)["29976"]["state"] == "downloading"


def test_an_add_the_client_did_not_take_is_a_failure(fake):
    _hand_qbt(fake, takes=False)
    _abs(fake)
    _four_winds_chaptarr(fake)

    result = asyncio.run(books.add_torrent("The Four Winds", "gr:79888572", EPUB_B64, "ebook"))

    assert result["status"] == "add_failed"
    assert ledger.load(books.BOOK_LEDGER)["29976"]["state"] == "failed"


def test_a_handed_in_torrent_is_refused_at_the_cap_before_anything_is_added(fake):
    _qbt(fake, [_torrent(progress=0.5)] * books.MAM_UNSATISFIED_CAP)   # no add route: an add would fail

    result = asyncio.run(books.add_torrent("The Four Winds", "gr:79888572", EPUB_B64, "ebook"))

    assert result["status"] == "guard"


def _hand_downloading(**extra):
    ledger.save(books.BOOK_LEDGER, {"29976": {
        "title": "The Four Winds", "author": "Kristin Hannah", "format": "ebook", "state": "downloading",
        "foreign_book_id": "gr:79888572", "requested_at": "2026-09-24T16:00:00Z", "hand_added": True,
        "torrent_hash": EPUB_HASH, "author_id": 43, **extra,
    }})


def _hand_status_qbt(fake, progress):
    """`progress` None = the torrent is gone from the client."""
    if progress is None:
        return _qbt(fake, [])
    return _qbt(fake, [{"hash": EPUB_HASH, "progress": progress, "content_path": EPUB_PATH, "seeding_time": 0,
                        "state": "uploading" if progress >= 1 else "downloading"}])


EPUB_CANDIDATE = {"path": EPUB_PATH, "additionalFile": False, "indexerFlags": 0,
                  "quality": {"quality": {"id": 3, "name": "EPUB"}, "revision": {"version": 1}}}


def test_a_finished_handed_in_torrent_is_imported_against_its_book(fake):
    _hand_downloading()
    _hand_status_qbt(fake, 1.0)
    manual_folders = []
    chaptarr = fake("_chaptarr", {
        ("GET", "/api/v1/queue"): {"records": []},     # Chaptarr doesn't watch this category
        ("GET", "/api/v1/history"): {"records": []},
        ("GET", "/api/v1/edition"): FOUR_WINDS_EDITIONS,
        ("GET", "/api/v1/manualimport"): lambda r: manual_folders.append(r.url.params["folder"]) or [EPUB_CANDIDATE],
        ("POST", "/api/v1/command"): {"id": 1},
    })

    book = asyncio.run(books.status(None))["books"][0]

    # The torrent's own path, never the whole MAM folder (21 files live).
    assert manual_folders == [EPUB_PATH]
    (method, path, body), = chaptarr.writes()
    assert body["name"] == "ManualImport" and body["importMode"] == "copy"
    assert body["files"] == [{"path": EPUB_PATH, "authorId": 43, "bookId": 29976, "editionId": 74165,
                              "quality": EPUB_CANDIDATE["quality"], "indexerFlags": 0,
                              "disableReleaseSwitching": True}]
    assert "handed-in torrent has finished" in book["summary"]
    assert ledger.load(books.BOOK_LEDGER)["29976"]["import_forced_for"] == EPUB_HASH


def test_a_handed_in_import_is_sent_once_not_every_poll(fake):
    _hand_downloading(import_forced_for=EPUB_HASH, import_forced_at="2026-09-24T16:05:00Z")
    _hand_status_qbt(fake, 1.0)
    chaptarr = fake("_chaptarr", {
        ("GET", "/api/v1/queue"): {"records": []},
        ("GET", "/api/v1/history"): {"records": []},
    })

    asyncio.run(books.status(None))

    assert chaptarr.writes() == []


def test_a_handed_in_torrent_still_downloading_shows_its_progress(fake):
    _hand_downloading()
    _hand_status_qbt(fake, 0.5)
    chaptarr = fake("_chaptarr", {
        ("GET", "/api/v1/queue"): {"records": []},
        ("GET", "/api/v1/history"): {"records": []},
    })

    book = asyncio.run(books.status(None))["books"][0]

    assert chaptarr.writes() == []
    assert book["summary"].startswith("Downloading from MAM: 50%")


def test_a_handed_in_torrent_gone_from_the_client_says_so(fake):
    _hand_downloading()
    _hand_status_qbt(fake, None)
    fake("_chaptarr", {
        ("GET", "/api/v1/queue"): {"records": []},
        ("GET", "/api/v1/history"): {"records": []},
    })

    book = asyncio.run(books.status(None))["books"][0]

    assert "no longer in qbittorrent-mam" in book["summary"]


def test_a_handed_in_import_then_settles_like_any_other(fake):
    """After the ManualImport, Chaptarr's history carries it the rest of the way."""
    _hand_downloading(import_forced_for=EPUB_HASH, import_forced_at="2026-09-24T16:05:00Z")
    _hand_status_qbt(fake, 1.0)
    fake("_chaptarr", {
        ("GET", "/api/v1/queue"): {"records": []},
        ("GET", "/api/v1/history"): {"records": [{
            "eventType": "bookFileImported", "date": "2026-09-24T16:05:30Z",
            "data": {"importedPath": "/music/books/ebooks/Kristin Hannah/The Four Winds/The Four Winds.pdf", "fileId": "901"},
        }]},
    })

    book = asyncio.run(books.status(None))["books"][0]

    assert book["state"] == "imported" and book["content"] == "unverified"


def test_retracting_a_handed_in_book_does_not_blame_an_aged_out_grab():
    assert "handed in" in books.not_blocklisted({"hand_added": True})
    assert "aged out" in books.not_blocklisted({})

"""Unit tests for the audiobook push poller — no network, no env.

book_events.py is a standalone script in the parent dir; load it by path. Shapes
below are trimmed copies of live ABS items and Chaptarr history/since records.
"""
import importlib.util
import pathlib
from datetime import datetime, timezone

_p = pathlib.Path(__file__).resolve().parents[1] / "book_events.py"
_spec = importlib.util.spec_from_file_location("book_events", _p)
be = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(be)


def _item(added, path="/audiobooks/Matthew B. Crawford/Shop Class as Soulcraft",
          title="Shop Class as Soulcraft", author="Matthew B. Crawford"):
    return {"id": f"abs-{added}", "addedAt": added, "path": path,
            "media": {"metadata": {"title": title, "authorName": author}}}


IMPORTED = {
    "id": 11, "eventType": "bookFileImported",
    "data": {"importedPath": "/music/books/audiobooks/Matthew B. Crawford/Shop Class as Soulcraft/"
                             "Shop Class as Soulcraft.m4b"},
}
FAILED = {
    "id": 9, "eventType": "downloadFailed", "sourceTitle": "Wings of War release",
    "book": {"title": "Wings of War"}, "author": {"authorName": "David Fairbank White"},
    "data": {"message": "Manually marked as failed"},
}
GRABBED = {"id": 10, "eventType": "grabbed", "data": {}}


class _Poster:
    def __init__(self, fail_after=None):
        self.sent, self.fail_after = [], fail_after

    def __call__(self, event):
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            raise OSError("shim down")
        self.sent.append(event)


# ---- pure helpers ----

def test_abs_folder_maps_chaptarr_path_to_abs_item_folder():
    assert be.abs_folder(IMPORTED["data"]["importedPath"]) == \
        "/audiobooks/Matthew B. Crawford/Shop Class as Soulcraft"


def test_ready_event_labels_mam_by_imported_folder_else_audible():
    mam = be.mam_folders([IMPORTED, FAILED, GRABBED])
    assert be.ready_event(_item(1), mam) == {
        "event": "book_ready", "title": "Shop Class as Soulcraft",
        "author": "Matthew B. Crawford", "source": "mam"}
    libation = _item(2, path="/audiobooks/Ernest Hemingway/A Farewell to Arms")
    assert be.ready_event(libation, mam)["source"] == "audible"


def test_failed_event_prefers_book_title_and_keeps_message():
    assert be.failed_event(FAILED) == {
        "event": "book_failed", "title": "Wings of War", "author": "David Fairbank White",
        "source": "mam", "message": "Manually marked as failed"}
    bare = {"id": 1, "eventType": "bookImportIncomplete", "sourceTitle": "Some.Release", "data": {}}
    assert be.failed_event(bare)["title"] == "Some.Release"


def test_new_failures_only_failure_types_after_mark():
    later = {**FAILED, "id": 12, "eventType": "bookImportIncomplete"}
    assert [r["id"] for r in be.new_failures([later, IMPORTED, FAILED, GRABBED], 0)] == [9, 12]
    assert [r["id"] for r in be.new_failures([later, FAILED], 9)] == [12]


def test_new_abs_items_caps_to_newest():
    items = [_item(t) for t in range(1, 10)]
    assert [i["addedAt"] for i in be.new_abs_items(items, 0)] == [5, 6, 7, 8, 9]
    assert be.new_abs_items(items, 9) == []


# ---- run(): seeding, posting once, retry ----

def test_first_run_seeds_without_posting():
    post = _Poster()
    state, ok = be.run([_item(100), _item(50)], [FAILED, IMPORTED], None, post)
    assert ok and post.sent == []
    assert state == {"abs_added_at": 100, "chaptarr_last_id": 11}


def test_new_item_and_failure_post_once_then_rerun_posts_nothing():
    post = _Poster()
    history = [IMPORTED, {**FAILED, "id": 12}]
    state, ok = be.run([_item(200), _item(100)], history, {"abs_added_at": 100, "chaptarr_last_id": 11}, post)
    assert ok
    assert [e["event"] for e in post.sent] == ["book_ready", "book_failed"]
    assert post.sent[0]["source"] == "mam"
    assert state == {"abs_added_at": 200, "chaptarr_last_id": 12}

    be.run([_item(200), _item(100)], history, state, post)
    assert len(post.sent) == 2  # nothing new


def test_failed_post_keeps_mark_for_retry():
    post = _Poster(fail_after=1)
    state, ok = be.run([_item(300), _item(200)], [], {"abs_added_at": 100, "chaptarr_last_id": 0}, post)
    assert not ok                        # main() exits 1 so the unit shows failed
    assert state["abs_added_at"] == 200  # 200 accepted, 300 retried next run

    post.fail_after = None
    be.run([_item(300), _item(200)], [], state, post)
    assert [e["title"] for e in post.sent] == ["Shop Class as Soulcraft"] * 2
    assert state["abs_added_at"] == 300


def test_non_network_error_raises_instead_of_being_swallowed():
    def buggy(event):
        raise KeyError("title")

    try:
        be.run([_item(200)], [], {"abs_added_at": 100, "chaptarr_last_id": 0}, buggy)
        assert False, "a bug must fail loudly"
    except KeyError:
        pass


# ---- stuck requests ----

NOW = datetime(2026, 9, 20, 3, 0, tzinfo=timezone.utc)
OLD = "2026-09-19T02:12:00Z"     # > 24 h before NOW
RECENT = "2026-09-19T12:00:00Z"  # < 24 h


def _ledger(**over):
    base = {"title": "Project Hail Mary", "author": "Andy Weir", "state": "requested",
            "requested_at": OLD, "reason": "only [VIP] releases found"}
    return {**base, **over}


def test_stuck_request_pushes_once_across_reruns():
    post, state = _Poster(), {}
    ledger = {"143": _ledger(), "200": _ledger(requested_at=RECENT)}
    assert be.run_stuck(ledger, [], state, post, lambda _id: 0, NOW)
    assert post.sent == [{"event": "book_stuck", "title": "Project Hail Mary", "author": "Andy Weir",
                          "source": "mam", "message": "only [VIP] releases found"}]
    be.run_stuck(ledger, [], state, post, lambda _id: 0, NOW)
    assert len(post.sent) == 1


def test_imported_failed_or_gone_never_push():
    post, state, looked_up = _Poster(), {}, []

    def files(book_id):
        looked_up.append(book_id)
        return {"1": 1, "2": None}.get(book_id, 0)

    ledger = {"1": _ledger(), "2": _ledger(), "3": _ledger(state="failed"),
              "17623": _ledger(state="downloading")}   # failure in Chaptarr history
    history = [{"id": 5, "eventType": "downloadFailed", "bookId": 17623}]
    assert be.run_stuck(ledger, history, state, post, files, NOW)
    assert post.sent == [] and sorted(looked_up) == ["1", "2"]
    be.run_stuck(ledger, history, state, post, files, NOW)
    assert sorted(looked_up) == ["1", "2"]   # imported/gone are not looked up again


def test_stuck_push_failure_retries_and_rerequest_is_new():
    post, state = _Poster(fail_after=0), {}
    ledger = {"143": _ledger(reason=None)}
    assert not be.run_stuck(ledger, [], state, post, lambda _id: 0, NOW)
    assert state["stuck_done"] == []
    post.fail_after = None
    be.run_stuck(ledger, [], state, post, lambda _id: 0, NOW)
    assert post.sent[0]["message"] == "not imported after 24 h"

    ledger["143"]["requested_at"] = "2026-09-19T02:30:00Z"   # re-requested: stuck again later
    be.run_stuck(ledger, [], state, post, lambda _id: 0, NOW)
    assert len(post.sent) == 2
    assert state["stuck_done"] == ["143@2026-09-19T02:30:00Z"]   # the old key is pruned


# ---- suspect imports ----

def _suspect(**over):
    base = {"title": "Of Mice and Men", "author": "John Steinbeck", "state": "imported",
            "content": "suspect", "content_detail": "got 'Of Mice and Men' by 'BBC'",
            "imported_at": "2026-09-21T04:00:00Z"}
    return {**base, **over}


def test_suspect_import_pushes_once_then_stays_quiet():
    post, state = _Poster(), {}
    ledger = {"9249": _suspect(),
              "9250": _suspect(content="ok"),          # passed the check
              "9251": _suspect(content="unverified")}  # no tags: says nothing, so no push
    assert be.run_suspect(ledger, state, post)
    assert post.sent == [{"event": "book_suspect", "title": "Of Mice and Men",
                          "author": "John Steinbeck", "source": "mam",
                          "message": "got 'Of Mice and Men' by 'BBC'"}]
    be.run_suspect(ledger, state, post)
    assert len(post.sent) == 1


def test_suspect_push_failure_retries_and_a_reimport_alerts_again():
    post, state = _Poster(fail_after=0), {}
    ledger = {"9249": _suspect(content_detail=None)}
    assert not be.run_suspect(ledger, state, post)
    assert state["suspect_done"] == []     # nothing marked, so next run tries again

    post.fail_after = None
    be.run_suspect(ledger, state, post)
    # No detail recorded, but the kind of problem is still named.
    assert post.sent[0]["message"] == "its tags don't fully match"

    ledger["9249"]["imported_at"] = "2026-09-22T09:00:00Z"   # re-imported: check it again
    be.run_suspect(ledger, state, post)
    assert len(post.sent) == 2
    assert state["suspect_done"] == ["9249@2026-09-22T09:00:00Z"]   # the old key is pruned


# ---- delivery is not the same as acceptance ----

def _relayed(targets, event="book_suspect"):
    """What the shim answers a push: 200 either way, `targets` is the truth."""
    return {"status": "relayed", "event": f"landible.{event}", "targets": targets, "deliveries": []}


def test_zero_targets_is_not_delivery():
    be.delivered(_relayed(1))          # fine
    be.delivered(None)                 # older shim, no body: trusted
    be.delivered({"status": "ok"})     # older shim, no `targets`: trusted
    try:
        be.delivered(_relayed(0))
        assert False, "a push nobody received must not count as delivered"
    except be.NotDelivered as e:
        assert "0 targets" in str(e)


def test_not_delivered_is_an_oserror_so_the_mark_stays_put():
    """The retry path is `except OSError`; NotDelivered rides it deliberately."""
    assert issubclass(be.NotDelivered, OSError)

    def post(_event):
        raise be.NotDelivered("relayed to 0 targets")

    state, ledger = {}, {"9249": _suspect()}
    assert not be.run_suspect(ledger, state, post)      # the unit fails
    assert state["suspect_done"] == []                  # nothing marked: it retries

    sent = []
    be.run_suspect(ledger, state, sent.append)          # a target exists now
    assert len(sent) == 1 and state["suspect_done"] == ["9249@2026-09-21T04:00:00Z"]


def test_an_unsubscribed_ready_or_failure_also_retries():
    state = {"abs_added_at": 100, "chaptarr_last_id": 0}

    def post(_event):
        raise be.NotDelivered("relayed to 0 targets")

    new_state, ok = be.run([_item(200)], [FAILED], state, post)
    assert not ok
    assert new_state["abs_added_at"] == 100 and new_state["chaptarr_last_id"] == 0


def test_a_non_json_body_reads_as_not_delivered():
    """`except OSError` misses ValueError, so an unparseable 200 would escape
    the retry path entirely. Cannot tell == not delivered."""
    class _R:
        def read(self): return b"<html>502 Bad Gateway</html>"
        def __enter__(self): return self
        def __exit__(self, *a): return False
    import urllib.request
    real = urllib.request.urlopen
    urllib.request.urlopen = lambda *a, **k: _R()
    try:
        be._http("POST", "http://shim/api/events/books", {}, {"event": "x"})
        assert False, "an unreadable answer must not count as delivered"
    except be.NotDelivered as e:
        assert "non-JSON" in str(e)
    finally:
        urllib.request.urlopen = real


def test_one_stream_failing_does_not_resend_the_other():
    """book_ready and book_failed keep separate high-water marks, so a failed
    failure-push must not make the delivered ready-push repeat every 5 min."""
    sent = []

    def post(event):
        if event["event"] == "book_failed":
            raise be.NotDelivered("relayed to 0 targets")
        sent.append(event)

    state = {"abs_added_at": 100, "chaptarr_last_id": 0}
    new_state, ok = be.run([_item(200)], [FAILED], state, post)

    assert not ok                                   # the unit fails, as it should
    assert len(sent) == 1                           # ready went out once
    assert new_state["abs_added_at"] == 200         # ...and its mark advanced
    assert new_state["chaptarr_last_id"] == 0       # the failure retries on its own

    be.run([_item(200)], [FAILED], new_state, lambda e: sent.append(e))
    assert [e["event"] for e in sent] == ["book_ready", "book_failed"]   # no repeat


def test_a_cancelled_request_never_alerts_as_stuck():
    """book_stuck exists for a request nobody has dealt with. A cancelled one
    has been dealt with — that is what cancelling means."""
    post, state = _Poster(), {}
    ledger = {"9249": _ledger(state="cancelled")}
    assert be.run_stuck(ledger, [], state, post, lambda _id: 0, NOW)
    assert post.sent == []


def test_a_mislabelled_library_entry_asks_for_a_look_too():
    """Different cause, same ask: the FILE is right, Audiobookshelf is showing
    it as another book. It shares book_suspect rather than needing a second
    event type, and the message says which problem it is."""
    post, state = _Poster(), {}
    entry = _suspect(content="ok", content_detail=None,
                     library_mismatch="the library lists it as 'Classic Serial - Of Mice and Men'")
    assert be.run_suspect({"9250": entry}, state, post)
    assert len(post.sent) == 1
    assert post.sent[0]["message"].startswith("the file is right but the library lists it as")


def test_a_clean_import_still_asks_for_nothing():
    post, state = _Poster(), {}
    clean = _suspect(content="ok", content_detail=None)
    assert be.run_suspect({"9250": clean}, state, post)
    assert post.sent == []
    assert be.needs_a_look(clean) is None

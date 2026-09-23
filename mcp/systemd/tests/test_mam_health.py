"""Unit tests for the MAM health alerts — no network, no env.

mam_health.py is a standalone script in the parent dir; load it by path. Torrent
shapes are trimmed copies of live qbittorrent-mam torrents/info rows.
"""
import importlib.util
import pathlib

import pytest

_p = pathlib.Path(__file__).resolve().parents[1] / "mam_health.py"
_spec = importlib.util.spec_from_file_location("mam_health", _p)
mh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mh)

DAY = 24 * 3600
TRANSFER = {"connection_status": "connected"}
PREFS = {"max_ratio_enabled": False, "max_seeding_time_enabled": False}


def _t(h, seeded=DAY, state="stalledUP", save_path="/music/books/mam/", progress=1):
    return {"hash": h, "name": f"Book {h}", "state": state, "progress": progress,
            "seeding_time": seeded, "save_path": save_path, "added_on": 1, "completion_on": 2}


class _Poster:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def __call__(self, event):
        if self.fail:
            raise OSError("shim down")
        self.sent.append(event)


def _run(torrents, state, post, transfer=TRANSFER, prefs=PREFS):
    return mh.run((torrents, transfer, prefs), None, state, post)


def test_healthy_pushes_nothing_and_first_run_seeds():
    post = _Poster()
    state, ok = _run([_t("a"), _t("b", seeded=4 * DAY)], None, post)
    assert ok and post.sent == []
    assert set(state["torrents"]) == {"a", "b"} and state["alerts"] == {}
    assert state["torrents"]["b"]["seeded_72h"] is True


def test_first_run_alerts_current_conditions():
    post = _Poster()
    _run([_t("a", state="stoppedUP")], None, post)
    assert post.sent == [{"event": "mam_health", "title": "Book a", "source": "mam",
                          "message": "Book a paused (seeded 24h; <72 h = hit & run risk)"}]


def _cycle(bad, good, key, contains, **kw):
    """`bad` pushes once, stays quiet on rerun, clears on `good`, then pushes again."""
    post = _Poster()
    state, _ = _run(good(), None, post)
    for _ in range(2):
        state, ok = _run(bad(), state, post, **kw.get("bad_kw", {}))
        assert ok and len(post.sent) == 1 and key in state["alerts"]
        assert contains in post.sent[0]["message"]
        state, _ = _run(bad(), state, post, **kw.get("bad_kw", {}))
        assert len(post.sent) == 1               # still active: no repeat
        state, _ = _run(good(), state, post)
        assert key not in state["alerts"]        # cleared: re-armed
        post.sent.clear()


def test_paused_error_moved_each_push_once_and_rearm():
    good = lambda: [_t("a")]  # noqa: E731
    _cycle(lambda: [_t("a", state="pausedUP")], good, "t:a:paused", "paused")
    _cycle(lambda: [_t("a", state="missingFiles")], good, "t:a:error", "missingFiles")
    _cycle(lambda: [_t("a", save_path="/music/downloads")], good, "t:a:moved", "moved to /music/downloads")


def test_empty_save_path_is_not_moved():
    assert mh.torrent_problem(_t("a", save_path="", state="metaDL")) is None


def test_seeded_torrent_says_lower_priority():
    post = _Poster()
    _run([_t("a", seeded=4 * DAY, state="error")], {"torrents": {}, "alerts": {}}, post)
    assert post.sent[0]["message"] == "Book a in state error (seeded, lower priority)"


def test_guard_limits_and_disconnected():
    full = lambda: [_t(str(i)) for i in range(mh.MAM_UNSATISFIED_CAP)]  # noqa: E731
    _cycle(full, lambda: [_t("0")], "guard", f"Guard full: {mh.MAM_UNSATISFIED_CAP}/")
    good = lambda: [_t("a")]  # noqa: E731
    _cycle(good, good, "limits", "Share limit", bad_kw={"prefs": {**PREFS, "max_ratio_enabled": True}})
    _cycle(good, good, "client", "listener not bound",
           bad_kw={"transfer": {"connection_status": "disconnected"}})


def test_client_down_alerts_once_and_keeps_other_state():
    post = _Poster()
    state, _ = _run([_t("a", state="pausedUP")], None, post)
    for _ in range(2):
        state, ok = mh.run(None, "Connection refused", state, post)
    assert ok and [e["message"] for e in post.sent][1:] == ["qbittorrent-mam down: Connection refused"]
    assert set(state["alerts"]) == {"t:a:paused", "client"} and "a" in state["torrents"]
    state, _ = _run([_t("a", state="pausedUP")], state, post)
    assert set(state["alerts"]) == {"t:a:paused"} and len(post.sent) == 2


def test_removed_pushes_once_never_rearms():
    post = _Poster()
    state, _ = _run([_t("a"), _t("b")], None, post)
    for _ in range(2):
        state, ok = _run([_t("a")], state, post)
    assert ok and [e["message"] for e in post.sent] == ["Book b removed from qbittorrent-mam (<72 h = hit & run risk)"]
    assert set(state["torrents"]) == {"a"}


def test_failed_push_retries_next_run():
    state, _ = _run([_t("a"), _t("b", state="pausedUP")], None, _Poster(fail=True))
    assert state["alerts"] == {}
    state, ok = _run([_t("a")], state, _Poster(fail=True))
    assert not ok and "b" in state["torrents"]   # removal kept for retry
    post = _Poster()
    state, ok = _run([_t("a")], state, post)
    assert ok and len(post.sent) == 1 and "removed" in post.sent[0]["message"]
    assert set(state["torrents"]) == {"a"}


def test_unsatisfied_count_matches_the_mcp_copy():
    books = pytest.importorskip("landible_mcp.books")
    torrents = [_t("a"), _t("b", seeded=4 * DAY), _t("c", seeded=4 * DAY, progress=0.5)]
    assert mh.unsatisfied_count(torrents) == books.unsatisfied_count(torrents) == 2
    assert mh.SATISFIED_SEED_S == books.SATISFIED_SEED_S


# ---- a push nobody received is not a success ----

class _Resp:
    def __init__(self, body): self._b = body.encode()
    def read(self): return self._b
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _post_with(monkeypatch, mod, body):
    monkeypatch.setattr(mod.urllib.request, "urlopen", lambda *a, **k: _Resp(body))
    mod._post_event("http://shim", "secret", {"event": "mam_health"})


def test_zero_targets_raises_not_delivered(monkeypatch):
    assert issubclass(mh.NotDelivered, OSError)   # rides the existing retry path
    try:
        _post_with(monkeypatch, mh,
                   '{"status":"relayed","event":"landible.mam_health","targets":0,"deliveries":[]}')
        assert False, "a push nobody received must not count as delivered"
    except mh.NotDelivered as e:
        assert "0 targets" in str(e)


def test_a_real_delivery_and_an_older_shim_are_both_fine(monkeypatch):
    _post_with(monkeypatch, mh, '{"status":"relayed","targets":1,"deliveries":[{"ok":true}]}')
    _post_with(monkeypatch, mh, "")            # older shim: empty body
    _post_with(monkeypatch, mh, '{"status":"ok"}')   # older shim: no `targets`

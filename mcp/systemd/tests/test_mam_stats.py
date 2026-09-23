"""Unit tests for the MAM stats poller — no network, no env.

mam_stats.py is a standalone script in the parent dir; load it by path. RAW is the
jsonLoad.php?snatch_summary shape (fields per Prowlarr/tracker-tracker docs).
"""
import importlib.util
import json
import pathlib

import pytest

_p = pathlib.Path(__file__).resolve().parents[1] / "mam_stats.py"
_spec = importlib.util.spec_from_file_location("mam_stats", _p)
ms = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ms)

COOKIE = "SECRET-mam-id-abc123"
NOW = "2026-09-20T12:00:00+00:00"
RAW = {
    "uid": 12345, "username": "someone", "email": "x@example.com", "ip": "203.0.113.9",
    "classname": "Mouse", "seedbonus": 5812, "ratio": 1.37, "wedges": 11, "vip_until": None,
    "connectable": "yes", "uploaded_bytes": "12884901888", "downloaded_bytes": 9395240960,
    "unsat": {"count": 3, "limit": 20, "name": "Unsatisfied"},
    "leeching": {"count": 0}, "sSat": {"count": 1}, "seedUnsat": {"count": 3},
    "seedHnr": {"count": 0}, "inactHnr": {"count": 0}, "inactUnsat": {"count": 0}, "inactSat": {"count": 0},
    "country_name": "United States",
}


class _Poster:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def __call__(self, event):
        if self.fail:
            raise OSError("shim down")
        self.sent.append(event)


def _ok(raw=RAW, prev=None, post=None):
    return ms.run(ms.slim(raw), None, False, prev, post or _Poster(), NOW)


def test_slim_keeps_only_the_allow_list():
    s = ms.slim(RAW)
    assert s["seedbonus"] == 5812 and s["classname"] == "Mouse"
    assert s["uploaded_bytes"] == 12884901888   # MAM sends a string sometimes
    assert s["unsat_count"] == 3 and s["unsat_limit"] == 20
    for leak in ("uid", "username", "email", "ip", "country_name"):
        assert leak not in s
        assert str(RAW[leak]) not in json.dumps(s)


def test_parse_rejects_html_and_bodies_without_username():
    with pytest.raises(ms.Rejected):
        ms.parse(b"<html><body>Login</body></html>")
    with pytest.raises(ms.Rejected):
        ms.parse(b'{"error": "not logged in"}')
    assert ms.parse(json.dumps(RAW).encode())["username"] == "someone"


def test_rotated_cookie_is_picked_up():
    headers = ["other=1; path=/", "mam_id=NEWVALUE; expires=Wed; path=/; domain=.myanonamouse.net"]
    assert ms.rotated(headers, COOKIE) == "NEWVALUE"
    assert ms.rotated([f"mam_id={COOKIE}; path=/"], COOKIE) is None
    assert ms.rotated([], COOKIE) is None


def test_healthy_run_pushes_nothing():
    post = _Poster()
    state, ok = _ok(post=post)
    assert ok and post.sent == [] and state["alerts"] == {}
    assert state["stats"]["ratio"] == 1.37 and state["fetched_at"] == NOW and state["error"] is None


def test_rejected_alerts_once_and_keeps_last_stats():
    good, _ = _ok()
    post = _Poster()
    state, ok = ms.run(None, "MAM answered with HTML, not JSON", True, good, post, NOW)
    assert ok and len(post.sent) == 1 and "rejected" in post.sent[0]["message"]
    assert state["stats"] == good["stats"] and state["error"]
    # Next hour: still rejected -> no second push (no retry loop, no spam).
    state, _ = ms.run(None, "HTTP 403", True, state, post, NOW)
    assert len(post.sent) == 1
    # Fixed -> re-armed; a later rejection alerts again.
    state, _ = _ok(prev=state, post=post)
    assert state["alerts"] == {}
    ms.run(None, "HTTP 403", True, state, post, NOW)
    assert len(post.sent) == 2


def test_network_error_records_but_does_not_alert_or_clear():
    state, _ = _ok(raw={**RAW, "connectable": "offline"})
    post = _Poster()
    state, ok = ms.run(None, "timed out", False, state, post, NOW)
    assert ok and post.sent == [] and state["error"] == "timed out"
    assert state["alerts"] == {"offline": True}   # not judged, so kept


def test_connectable_values():
    for value, alerts in (("yes", False), ("no", True), ("offline", True), (None, False)):
        active, _ = ms.conditions(ms.slim({**RAW, "connectable": value}), None, False)
        assert ("offline" in active) is alerts, value


def test_offline_and_hnr_alert_once_each():
    post = _Poster()
    bad = {**RAW, "connectable": "offline", "inactHnr": {"count": 1}}
    state, _ = _ok(raw=bad, post=post)
    state, _ = _ok(raw=bad, prev=state, post=post)
    assert set(state["alerts"]) == {"offline", "hnr"} and len(post.sent) == 2
    assert "connectable" in post.sent[1]["message"] and "hit & run" in post.sent[0]["message"]


def test_failed_push_retries_next_run():
    bad = {**RAW, "connectable": "offline"}
    state, ok = _ok(raw=bad, post=_Poster(fail=True))
    assert not ok and state["alerts"] == {}
    post = _Poster()
    _ok(raw=bad, prev=state, post=post)
    assert len(post.sent) == 1


def test_cookie_never_reaches_the_file_or_events(tmp_path, monkeypatch, capsys):
    """Redaction: drive main() end to end with fake MAM + shim."""
    cookie_file = tmp_path / "mam_id"
    cookie_file.write_text(COOKIE + "\n")
    (tmp_path / "mam_id.tmp").write_text("leftover")
    (tmp_path / "mam_id.tmp").chmod(0o644)   # a crash leftover must not loosen the rotated cookie
    stats_file = tmp_path / "mam-stats.json"
    monkeypatch.setattr(ms, "COOKIE_FILE", str(cookie_file))
    monkeypatch.setattr(ms, "STATS_FILE", str(stats_file))
    monkeypatch.setenv("WEBHOOK_INBOUND_SECRET", "s")
    posted = []
    monkeypatch.setattr(ms, "_post_event", lambda url, secret, ev: posted.append(ev))

    seen = []

    def fake_fetch(cookie):
        seen.append(cookie)
        return {**RAW, "connectable": "offline"}, [f"mam_id=ROTATED-{COOKIE}; path=/"]

    monkeypatch.setattr(ms, "fetch", fake_fetch)
    ms.main()
    assert seen == [COOKIE]
    assert cookie_file.read_text().strip() == f"ROTATED-{COOKIE}"   # rotation saved
    assert oct(cookie_file.stat().st_mode & 0o777) == "0o600"
    out = stats_file.read_text() + json.dumps(posted) + capsys.readouterr().out
    assert COOKIE not in out and posted   # the offline alert went out, cookie-free

    def rejected_fetch(cookie):
        raise ms.Rejected("MAM answered with HTML, not JSON")

    monkeypatch.setattr(ms, "fetch", rejected_fetch)
    ms.main()
    out = stats_file.read_text() + json.dumps(posted) + capsys.readouterr().out
    assert COOKIE not in out


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
    assert issubclass(ms.NotDelivered, OSError)   # rides the existing retry path
    try:
        _post_with(monkeypatch, ms,
                   '{"status":"relayed","event":"landible.mam_health","targets":0,"deliveries":[]}')
        assert False, "a push nobody received must not count as delivered"
    except ms.NotDelivered as e:
        assert "0 targets" in str(e)


def test_a_real_delivery_and_an_older_shim_are_both_fine(monkeypatch):
    _post_with(monkeypatch, ms, '{"status":"relayed","targets":1,"deliveries":[{"ok":true}]}')
    _post_with(monkeypatch, ms, "")            # older shim: empty body
    _post_with(monkeypatch, ms, '{"status":"ok"}')   # older shim: no `targets`

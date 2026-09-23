"""Tests for the webhook config + book-event relay logic.

Run from deploy/:  uv run --group dev pytest

_require_bearer is monkeypatched off and WEBHOOKS_FILE is redirected to a tmp
path so the pure upsert/delete + selection logic is exercised without auth,
network, or the real state dir. The actual HTTP fan-out (_fanout / the /test
endpoint) talks to live targets, so it's left to manual smoke-testing.
"""
import asyncio
import json

import main


class _FakeRequest:
    def __init__(self, body=None, headers=None):
        self._body = body
        self.headers = headers or {}

    async def json(self):
        return self._body


def _setup(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "_require_bearer", lambda r: None)
    monkeypatch.setattr(main, "WEBHOOKS_FILE", tmp_path / "webhooks.json")
    monkeypatch.setattr(main, "STATE_DIR", tmp_path)


def _seed(tmp_path, targets):
    (tmp_path / "webhooks.json").write_text(json.dumps(targets))


def _read(tmp_path):
    return json.loads((tmp_path / "webhooks.json").read_text())


# ---- pure logic ----

def test_targets_for_event_selects_enabled_and_subscribed():
    config = [
        {"name": "ha", "url": "x", "events": ["landible.download_complete"], "enabled": True},
        {"name": "disabled", "url": "x", "events": ["landible.download_complete"], "enabled": False},
        {"name": "other-event", "url": "x", "events": ["landible.download_failed"], "enabled": True},
        {"name": "multi", "url": "x", "events": ["landible.download_complete", "landible.download_failed"]},
    ]
    selected = main._targets_for_event(config, "landible.download_complete")
    # ha + multi (multi has no 'enabled' key — defaults to True)
    assert [t["name"] for t in selected] == ["ha", "multi"]


def test_targets_for_event_tolerates_garbage_entries():
    config = ["not-a-dict", {"name": "ok", "events": ["e"]}, {"name": "no-events"}]
    assert [t["name"] for t in main._targets_for_event(config, "e")] == ["ok"]


def test_redact_url_masks_path_and_query():
    assert main._redact_url("https://ha.local/api/webhook/abc123secret") == "https://ha.local/***"
    assert main._redact_url("https://x.io/hook?token=shh") == "https://x.io/***"
    assert main._redact_url("https://x.io") == "https://x.io"
    assert main._redact_url("not a url") == "***"


# ---- config upsert/delete ----

def test_upsert_inserts_and_replaces(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _seed(tmp_path, [])
    resp = asyncio.run(main.webhooks_upsert("ha", _FakeRequest(
        {"url": "https://ha/api/webhook/x", "events": ["landible.download_complete"]})))
    assert resp["status"] == "ok"
    assert resp["total"] == 1
    assert _read(tmp_path)[0]["enabled"] is True  # default

    # upsert same name replaces (no duplicate), updates fields
    asyncio.run(main.webhooks_upsert("ha", _FakeRequest(
        {"url": "https://ha/api/webhook/y", "events": ["landible.download_failed"], "enabled": False})))
    data = _read(tmp_path)
    assert len(data) == 1
    assert data[0]["url"].endswith("/y")
    assert data[0]["events"] == ["landible.download_failed"]
    assert data[0]["enabled"] is False


def test_upsert_rejects_missing_url_or_events(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _seed(tmp_path, [])
    for bad in ({"events": ["e"]}, {"url": "https://x"}, {"url": "https://x", "events": []}):
        try:
            asyncio.run(main.webhooks_upsert("t", _FakeRequest(bad)))
            assert False, f"expected HTTPException for {bad}"
        except main.HTTPException as e:
            assert e.status_code == 400


def test_upsert_rejects_non_http_url(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _seed(tmp_path, [])
    try:
        asyncio.run(main.webhooks_upsert("t", _FakeRequest({"url": "ftp://x/y", "events": ["e"]})))
        assert False, "expected HTTPException for ftp:// url"
    except main.HTTPException as e:
        assert e.status_code == 400


def test_delete_removes_and_reports_not_found(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _seed(tmp_path, [
        {"name": "ha", "url": "x", "events": ["e"]},
        {"name": "keep", "url": "x", "events": ["e"]},
    ])
    resp = asyncio.run(main.webhooks_delete("ha", _FakeRequest()))
    assert resp["status"] == "deleted"
    assert [t["name"] for t in _read(tmp_path)] == ["keep"]

    resp2 = asyncio.run(main.webhooks_delete("ghost", _FakeRequest()))
    assert resp2["status"] == "not_found"


def test_list_redacts_urls(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _seed(tmp_path, [
        {"name": "ha", "url": "https://ha.local/api/webhook/secret", "events": ["e"],
         "headers": {"Authorization": "Bearer t"}},
    ])
    resp = asyncio.run(main.webhooks_list(_FakeRequest()))
    entry = resp["webhooks"][0]
    assert entry["url"] == "https://ha.local/***"   # secret path masked
    assert entry["has_headers"] is True             # presence flagged, value not leaked
    assert "headers" not in entry


# ---- inbound relay auth (security-critical: keeps the LAN from spoofing events) ----

def test_inbound_secret_requires_config(monkeypatch):
    monkeypatch.delenv("WEBHOOK_INBOUND_SECRET", raising=False)
    try:
        main._require_inbound_secret(_FakeRequest(headers={"authorization": "anything"}))
        assert False, "expected 503 when secret unconfigured"
    except main.HTTPException as e:
        assert e.status_code == 503  # fail closed, not open


def test_inbound_secret_accepts_bare_and_bearer(monkeypatch):
    monkeypatch.setenv("WEBHOOK_INBOUND_SECRET", "s3cret")
    # both forms the operator might put in a poller's Authorization header pass
    main._require_inbound_secret(_FakeRequest(headers={"authorization": "s3cret"}))
    main._require_inbound_secret(_FakeRequest(headers={"authorization": "Bearer s3cret"}))


def test_inbound_secret_rejects_mismatch_and_missing(monkeypatch):
    monkeypatch.setenv("WEBHOOK_INBOUND_SECRET", "s3cret")
    for hdr in ({"authorization": "wrong"}, {}):
        try:
            main._require_inbound_secret(_FakeRequest(headers=hdr))
            assert False, f"expected 401 for {hdr}"
        except main.HTTPException as e:
            assert e.status_code == 401


# ---- relay branching (network stubbed via _fanout monkeypatch) ----

def _stub_fanout(monkeypatch):
    """Replace _fanout with a recorder; returns the list it captures (event, data) into."""
    monkeypatch.setattr(main, "_require_inbound_secret", lambda r: None)
    captured = []

    async def fake_fanout(event, data):
        captured.append((event, data))
        return [{"name": "ha", "ok": True}]

    monkeypatch.setattr(main, "_fanout", fake_fanout)
    return captured


# ---- book relay ----

def test_events_books_relays_ready_with_allow_listed_fields(monkeypatch):
    captured = _stub_fanout(monkeypatch)
    payload = {"event": "book_ready", "title": "Shop Class as Soulcraft",
               "author": "Matthew B. Crawford", "source": "mam", "extra": "dropped"}
    body = json.loads(asyncio.run(main.events_books(_FakeRequest(payload))).body)
    assert body["status"] == "relayed"
    assert captured == [("landible.book_ready", {"title": "Shop Class as Soulcraft",
                                                "author": "Matthew B. Crawford",
                                                "source": "mam", "message": None})]


def test_events_books_relays_failed(monkeypatch):
    captured = _stub_fanout(monkeypatch)
    payload = {"event": "book_failed", "title": "Wings of War", "author": "David Fairbank White",
               "source": "mam", "message": "Manually marked as failed"}
    asyncio.run(main.events_books(_FakeRequest(payload)))
    event, data = captured[0]
    assert event == "landible.book_failed"
    assert data["message"] == "Manually marked as failed"


def test_events_books_relays_stuck_and_mam_health(monkeypatch):
    captured = _stub_fanout(monkeypatch)
    for name in ("book_stuck", "mam_health"):
        payload = {"event": name, "title": "Book a", "source": "mam", "message": "Book a paused"}
        asyncio.run(main.events_books(_FakeRequest(payload)))
    assert [e for e, _ in captured] == ["landible.book_stuck", "landible.mam_health"]
    assert captured[1][1] == {"title": "Book a", "author": None, "source": "mam", "message": "Book a paused"}


def test_events_books_relays_suspect(monkeypatch):
    captured = _stub_fanout(monkeypatch)
    payload = {"event": "book_suspect", "title": "Of Mice and Men", "author": "John Steinbeck",
               "source": "mam", "message": "got 'Of Mice and Men' by 'BBC'"}
    asyncio.run(main.events_books(_FakeRequest(payload)))
    assert captured == [("landible.book_suspect",
                         {"title": "Of Mice and Men", "author": "John Steinbeck",
                          "source": "mam", "message": "got 'Of Mice and Men' by 'BBC'"})]


def test_events_books_relays_digest(monkeypatch):
    captured = _stub_fanout(monkeypatch)
    payload = {"event": "book_digest", "title": "Weekly audiobooks", "source": "digest", "message": "Books: 2"}
    asyncio.run(main.events_books(_FakeRequest(payload)))
    assert captured == [("landible.book_digest",
                         {"title": "Weekly audiobooks", "author": None, "source": "digest", "message": "Books: 2"})]


def test_events_books_ignores_unknown_event(monkeypatch):
    captured = _stub_fanout(monkeypatch)
    for payload in ({"event": "grab"}, {}, {"event": ["book_ready"]}):
        body = json.loads(asyncio.run(main.events_books(_FakeRequest(payload))).body)
        assert body["status"] == "ignored"
    assert captured == []


def test_events_books_rejects_non_dict_body(monkeypatch):
    _stub_fanout(monkeypatch)
    try:
        asyncio.run(main.events_books(_FakeRequest(["nope"])))
        assert False, "expected 400 for non-dict body"
    except main.HTTPException as e:
        assert e.status_code == 400


def test_events_books_requires_inbound_secret(monkeypatch):
    monkeypatch.setenv("WEBHOOK_INBOUND_SECRET", "s3cret")
    try:
        asyncio.run(main.events_books(_FakeRequest({"event": "book_ready"},
                                                   headers={"authorization": "wrong"})))
        assert False, "expected 401"
    except main.HTTPException as e:
        assert e.status_code == 401


def test_events_books_502_only_when_every_target_failed(monkeypatch):
    monkeypatch.setattr(main, "_require_inbound_secret", lambda r: None)
    results = {}

    async def fake_fanout(event, data):
        return results["deliveries"]

    monkeypatch.setattr(main, "_fanout", fake_fanout)
    req = _FakeRequest({"event": "book_ready", "title": "x"})
    for deliveries, code in (
        ([{"name": "ha", "ok": False}], 502),                              # poller retries
        ([{"name": "ha", "ok": True}, {"name": "b", "ok": False}], 200),   # no double push
        ([], 200),                                                         # no subscribers
    ):
        results["deliveries"] = deliveries
        assert asyncio.run(main.events_books(req)).status_code == code


def test_events_books_relays_unit_failed(monkeypatch):
    captured = _stub_fanout(monkeypatch)
    payload = {"event": "unit_failed", "title": "x.service", "source": "systemd", "message": "boom"}
    asyncio.run(main.events_books(_FakeRequest(payload)))
    assert [e for e, _ in captured] == ["landible.unit_failed"]


def test_book_event_map_is_all_landible():
    assert main._BOOK_EVENT_MAP
    assert all(v == f"landible.{k}" for k, v in main._BOOK_EVENT_MAP.items())


def test_webhook_test_fires_landible_test(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _seed(tmp_path, [{"name": "ha", "url": "https://ha/x", "events": ["e"], "enabled": False}])
    sent = []

    class _Resp:
        status_code = 200
        text = "ok"

    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            sent.append(json)
            return _Resp()

    monkeypatch.setattr(main.httpx, "AsyncClient", _Client)
    body = json.loads(asyncio.run(main.webhooks_test("ha", _FakeRequest())).body)
    assert body["delivered"] is True
    assert sent[0]["event"] == "landible.test"   # fires even though the target is disabled

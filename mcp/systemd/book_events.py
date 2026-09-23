#!/usr/bin/env python3
"""Audiobook push events: poll ABS + Chaptarr, POST new events to the shim.

Run by landible-book-events.timer every 5 min. No model in the loop.

  book_ready  — a new item in Audiobookshelf. "Ready" means "ABS has it", so this
                covers every source (Libation and Chaptarr/MAM) once, at the point
                the book is playable. source = "mam" if Chaptarr imported it into
                that folder, else "audible".
  book_failed — a Chaptarr history record of type downloadFailed or
                bookImportIncomplete. Chaptarr's Webhook can't fire on
                failures, so we read its history instead.
  book_stuck  — a books.json request still not imported 24 h after it was made
                Read-only on books.json; "imported" comes from
                Chaptarr's bookFileCount, since the ledger's state is only
                advanced when the MCP's status() runs.
  book_suspect — the MCP's content check found the imported file only half
                matches the book it was requested as: a BBC radio play
                filed under the novel, an audiobook tagged with its narrator.
                A human decides; nothing is deleted. Only "suspect" pushes —
                "unverified" (the file carries no tags at all) is common on MAM
                and says nothing about the content, so it stays in status().

Dedupe is two high-water marks in book-events-state.json (ABS addedAt, Chaptarr
history id), plus two done-sets: `stuck_done`, the requests already alerted as
stuck or found imported, keyed "<book id>@<requested_at>" so a re-request is a
new key, and `suspect_done`, keyed "<book id>@<imported_at>" so a re-import of
the same book alerts again but a suspect nobody has dealt with stays quiet. A
mark only advances after the shim **delivered** that event — accepted is not
enough, because the relay answers 200 with `targets: 0` when no webhook wants
the event, and taking that for success loses the alert for good with nothing in
any log. A shim outage, or an event nobody subscribes to, leaves the
mark alone and retries next run. The first run only seeds the marks: no pushes
for the books already in the library.

Pure helpers are unit-tested in tests/test_book_events.py; main() does the I/O.
Never touches books.json: the MCP stays that ledger's only writer.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

CHAPTARR_ROOT = "/music/books/audiobooks"  # Chaptarr's root folder...
ABS_ROOT = "/audiobooks"                   # ...is this dir inside the ABS container
FAILED_EVENTS = {"downloadFailed", "bookImportIncomplete"}
HISTORY_DAYS = 14        # Chaptarr history window read for failures + MAM folders;
                         # a failure still unposted after this long is dropped
ABS_PAGE = 50            # newest ABS items read per run
MAX_READY_PER_RUN = 5    # a bulk Libation sync must not become 50 iPhone pushes
STUCK_AFTER = timedelta(hours=24)

_HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.environ.get("BOOK_EVENTS_STATE_FILE", os.path.join(_HERE, "book-events-state.json"))
BOOK_LEDGER = os.environ.get("BOOK_LEDGER_FILE", os.path.join(_HERE, "books.json"))


# ---------------------------------------------------------------- pure helpers

def abs_folder(imported_path: str) -> str:
    """Chaptarr's imported file path -> the ABS item folder holding it."""
    rel = os.path.relpath(os.path.dirname(imported_path), CHAPTARR_ROOT)
    return os.path.join(ABS_ROOT, rel)


def mam_folders(history: list) -> set:
    """ABS folders Chaptarr imported into (i.e. books that came from MAM)."""
    return {
        abs_folder(r["data"]["importedPath"])
        for r in history
        if r.get("eventType") == "bookFileImported" and (r.get("data") or {}).get("importedPath")
    }


def new_abs_items(items: list, mark: int) -> list:
    """Items added after `mark` (ms), oldest first, capped at the newest MAX_READY_PER_RUN.

    The mark then moves to the newest one posted, so the older items the cap
    dropped are never pushed.
    """
    new = sorted((i for i in items if (i.get("addedAt") or 0) > mark), key=lambda i: i["addedAt"])
    return new[-MAX_READY_PER_RUN:]


def ready_event(item: dict, mam: set) -> dict:
    meta = (item.get("media") or {}).get("metadata") or {}
    return {
        "event": "book_ready",
        "title": meta.get("title"),
        "author": meta.get("authorName"),
        "source": "mam" if item.get("path") in mam else "audible",
    }


def new_failures(history: list, last_id: int) -> list:
    """Failure records after `last_id`, oldest first."""
    return sorted(
        (r for r in history if r.get("eventType") in FAILED_EVENTS and r.get("id", 0) > last_id),
        key=lambda r: r["id"],
    )


def failed_event(record: dict) -> dict:
    return {
        "event": "book_failed",
        "title": (record.get("book") or {}).get("title") or record.get("sourceTitle"),
        "author": (record.get("author") or {}).get("authorName"),
        "source": "mam",
        "message": (record.get("data") or {}).get("message"),
    }


def seed_state(items: list, history: list) -> dict:
    """First run: marks at the current newest, so nothing already there is pushed."""
    return {
        "abs_added_at": max((i.get("addedAt") or 0 for i in items), default=0),
        "chaptarr_last_id": max((r.get("id", 0) for r in history), default=0),
    }


def stuck_key(book_id: str, entry: dict) -> str:
    return f"{book_id}@{entry.get('requested_at')}"


def stuck_candidates(ledger: dict, done: set, failed_ids: set, now: datetime) -> list:
    """(book id, entry) for requests older than STUCK_AFTER not yet alerted or imported.

    Skips ledger `failed` and books with a failure in Chaptarr's history: those
    already got a book_failed.
    """
    out = []
    for book_id, e in ledger.items():
        try:
            requested = datetime.fromisoformat(e["requested_at"].replace("Z", "+00:00"))
        except (KeyError, AttributeError, ValueError):
            continue
        if (now - requested < STUCK_AFTER or stuck_key(book_id, e) in done
                or e.get("state") in ("failed", "imported", "cancelled")
                or int(book_id) in failed_ids):
            continue
        out.append((book_id, e))
    return out


def stuck_event(entry: dict) -> dict:
    return {
        "event": "book_stuck",
        "title": entry.get("title"),
        "author": entry.get("author"),
        "source": "mam",
        "message": entry.get("reason") or "not imported after 24 h",
    }


def suspect_key(book_id: str, entry: dict) -> str:
    """Keyed on the import, not the request: a re-import of the same book alerts again."""
    return f"{book_id}@{entry.get('imported_at')}"


def needs_a_look(entry: dict) -> str | None:
    """Why a human should check this import, or None.

    Two unrelated causes, one question for the user. The content check reads
    the FILE's tags; `library_mismatch` is Audiobookshelf showing a correct
    file under another book's name. Either way the ask is "go and look
    at this book", so they share `book_suspect` rather than needing a second
    event type — and the message says which it is.
    """
    if entry.get("content") == "suspect":
        return entry.get("content_detail") or "its tags don't fully match"
    if entry.get("library_mismatch"):
        return f"the file is right but {entry['library_mismatch']}"
    return None


def suspect_candidates(ledger: dict, done: set) -> list:
    """(book id, entry) for imports worth a look that nobody has been told about."""
    return [
        (book_id, e) for book_id, e in ledger.items()
        if needs_a_look(e) and suspect_key(book_id, e) not in done
    ]


def suspect_event(entry: dict) -> dict:
    return {
        "event": "book_suspect",
        "title": entry.get("title"),
        "author": entry.get("author"),
        "source": "mam",
        "message": needs_a_look(entry) or "the imported file may not be this book",
    }


# ---------------------------------------------------------------------- I/O

class NotDelivered(OSError):
    """The shim accepted the event and handed it to nobody.

    An OSError on purpose: every push here is already wrapped in `except
    OSError`, which leaves the dedupe mark where it is, fails the unit and
    retries next run. That is exactly right for an unsubscribed event — it
    makes a missing subscription noisy and self-correcting instead of losing
    the alert for good, which is what a bare 200 used to do.
    """


def delivered(response: object) -> None:
    """Raise unless the shim actually handed the event to a subscriber.

    The relay answers 200 with `targets: 0` when no webhook target wants the
    event, so the HTTP status alone says nothing about delivery. An older shim
    that doesn't report `targets` is trusted, or a deploy ordering would turn
    every push into a failure.
    """
    if isinstance(response, dict) and response.get("targets") == 0:
        raise NotDelivered(
            f"the shim relayed {response.get('event')!r} to 0 targets — "
            "no webhook subscribes to it (landible_webhook_set)"
        )


def _http(method, url, headers, data=None, timeout=30):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method,
                                 headers={**headers, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError as e:
        # A 200 carrying something that isn't JSON (a proxy error page) means we
        # cannot tell whether the event was delivered. `except OSError` does not
        # catch ValueError, so this would escape the retry path entirely; and
        # "cannot tell" must read as "not delivered", the same fail-closed rule
        # as `downloadAllowed` in books.py.
        raise NotDelivered(f"the shim answered {method} {url} with a non-JSON body: {e}") from e


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


def fetch_abs_items(abs_url: str, key: str) -> list:
    h = {"Authorization": f"Bearer {key}"}
    items = []
    for lib in _http("GET", f"{abs_url}/api/libraries", h).get("libraries") or []:
        if lib.get("mediaType") != "book":
            continue
        q = urllib.parse.urlencode({"sort": "addedAt", "desc": 1, "limit": ABS_PAGE, "minified": 1})
        items += _http("GET", f"{abs_url}/api/libraries/{lib['id']}/items?{q}", h).get("results") or []
    return items


def fetch_history(chaptarr_url: str, key: str, now: datetime) -> list:
    since = (now - timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    q = urllib.parse.urlencode({"date": since, "includeBook": "true", "includeAuthor": "true"})
    return _http("GET", f"{chaptarr_url}/api/v1/history/since?{q}", {"X-Api-Key": key}) or []


def run(items: list, history: list, state: dict | None, post) -> tuple[dict, bool]:
    """Post every new event via `post(event)`; return (new state, all posted?).

    A post that fails with a network/HTTP error (OSError) stops that stream,
    leaving its mark on the last event the shim accepted, so the rest are
    retried next run. Anything else is a bug and raises.
    """
    if state is None:
        state = seed_state(items, history)
        print(f"[book-events] first run: seeded {state} — no pushes")
        return state, True
    ok = True
    mam = mam_folders(history)
    try:
        for item in new_abs_items(items, state["abs_added_at"]):
            post(ready_event(item, mam))
            print(f"[book-events] ready: {item['path']}")
            state["abs_added_at"] = item["addedAt"]
    except OSError as e:
        print(f"[book-events] ERROR ready push failed, retrying next run: {e}")
        ok = False
    try:
        for rec in new_failures(history, state["chaptarr_last_id"]):
            post(failed_event(rec))
            print(f"[book-events] failed: history id {rec['id']}")
            state["chaptarr_last_id"] = rec["id"]
    except OSError as e:
        print(f"[book-events] ERROR failure push failed, retrying next run: {e}")
        ok = False
    return state, ok


def run_stuck(ledger: dict, history: list, state: dict, post, file_count, now: datetime) -> bool:
    """Post book_stuck once per stuck request; return True if every post landed.

    `file_count(book_id)` is Chaptarr's bookFileCount (None if Chaptarr no
    longer has the book). Imported or gone books are marked done without a
    push, so they aren't looked up again every run.
    """
    keys = {stuck_key(k, e) for k, e in ledger.items()}
    done = {k for k in state.get("stuck_done", []) if k in keys}   # drop entries the ledger lost
    failed_ids = {r.get("bookId") for r in history if r.get("eventType") in FAILED_EVENTS}
    ok = True
    try:
        for book_id, e in stuck_candidates(ledger, done, failed_ids, now):
            files = file_count(book_id)
            if files is None or files > 0:
                done.add(stuck_key(book_id, e))
                continue
            post(stuck_event(e))
            print(f"[book-events] stuck: book {book_id}")
            done.add(stuck_key(book_id, e))
    except OSError as e:
        print(f"[book-events] ERROR stuck check or push failed, retrying next run: {e}")
        ok = False
    state["stuck_done"] = sorted(done)
    return ok


def run_suspect(ledger: dict, state: dict, post) -> bool:
    """Post book_suspect once per flagged import; return True if every post landed.

    No Chaptarr lookup: the verdict is already in the ledger, written by the
    MCP's content check. This only carries it to a human.

    No MAX_READY_PER_RUN-style cap on purpose: that cap drops the older items
    for good, which is the "reaches nobody" bug this exists to fix. Suspects are
    rare and each one needs a person to look.
    """
    keys = {suspect_key(k, e) for k, e in ledger.items()}
    done = {k for k in state.get("suspect_done", []) if k in keys}   # drop entries the ledger lost
    ok = True
    try:
        for book_id, e in suspect_candidates(ledger, done):
            post(suspect_event(e))
            print(f"[book-events] suspect: book {book_id}")
            done.add(suspect_key(book_id, e))
    except OSError as e:
        print(f"[book-events] ERROR suspect push failed, retrying next run: {e}")
        ok = False
    state["suspect_done"] = sorted(done)
    return ok


def _load_ledger() -> dict:
    try:
        with open(BOOK_LEDGER) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def main() -> None:
    abs_url = os.environ.get("ABS_URL", "http://localhost:13378").rstrip("/")
    chaptarr_url = os.environ.get("CHAPTARR_URL", "http://localhost:8789").rstrip("/")
    shim_url = os.environ.get("DEPLOY_HEALTH_URL", "http://localhost:8090").rstrip("/")
    secret = os.environ["WEBHOOK_INBOUND_SECRET"]

    def post(event):
        delivered(_http("POST", f"{shim_url}/api/events/books", {"Authorization": secret}, event))

    items = fetch_abs_items(abs_url, os.environ["ABS_API_KEY"])
    chaptarr_key = os.environ["CHAPTARR_API_KEY"]
    now = datetime.now(timezone.utc)
    history = fetch_history(chaptarr_url, chaptarr_key, now)
    state, ok = run(items, history, _load_state(), post)

    def file_count(book_id):
        try:
            book = _http("GET", f"{chaptarr_url}/api/v1/book/{book_id}", {"X-Api-Key": chaptarr_key})
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise
        return (book.get("statistics") or {}).get("bookFileCount") or 0

    book_ledger = _load_ledger()
    ok = run_stuck(book_ledger, history, state, post, file_count, now) and ok
    ok = run_suspect(book_ledger, state, post) and ok
    _save_state(state)
    if not ok:   # fail the unit so a stuck push (bad secret, shim down) shows in systemctl --failed
        raise SystemExit(1)


if __name__ == "__main__":
    main()

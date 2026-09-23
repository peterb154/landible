"""Landible MCP server: audiobooks + ebooks as MCP tools.

Meant to be federated by an MCP gateway. Runs next to the stack and reaches
every backend over published ports on localhost:
  - Chaptarr (grabber), qbittorrent-mam (read-only guard), Audiobookshelf
    (library + send-to-Kindle) — all the logic lives in books.py.
  - The deploy shim's bearer-gated /api/webhooks CRUD, for the webhook tools.

Writes (and anything that can act, like book_status) are gated by a shared
secret; reads are not.
"""
from __future__ import annotations

import os
import secrets
from typing import Any
from urllib.parse import quote

import httpx
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_request
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import books

# The deploy shim serves the webhook CRUD. It is bearer-gated, so DEPLOY_TOKEN
# (same value as the shim's own .env) goes on every call.
DEPLOY_HEALTH_URL = os.environ.get("DEPLOY_HEALTH_URL", "http://localhost:8090")
DEPLOY_TOKEN = os.environ.get("DEPLOY_TOKEN", "")

# Shared secret guarding the WRITE tools. Reads are unguarded — the network
# perimeter is enough. Writes cost MAM slots, disk and email, so they get a
# defense-in-depth check. When unset (local dev) the check is a no-op; in
# production this server and the gateway hold the same value and the gateway
# forwards it as X-MCP-Secret.
MCP_SHARED_SECRET = os.environ.get("MCP_SHARED_SECRET", "")


def _require_secret() -> None:
    """Reject the call if X-MCP-Secret is missing or wrong (no-op in local dev)."""
    if not MCP_SHARED_SECRET:
        return
    req = get_http_request()
    presented = req.headers.get("x-mcp-secret") or req.headers.get("X-MCP-Secret") or ""
    if not secrets.compare_digest(presented.encode(), MCP_SHARED_SECRET.encode()):
        raise PermissionError("write tools require a valid X-MCP-Secret header")


_deploy = httpx.AsyncClient(
    base_url=DEPLOY_HEALTH_URL,
    headers={"Authorization": f"Bearer {DEPLOY_TOKEN}"} if DEPLOY_TOKEN else {},
    timeout=20.0,
)


mcp = FastMCP(
    "landible",
    instructions="""\
Tools for a self-hosted AUDIOBOOK + EBOOK stack: Chaptarr (search, requests,
acquisition from MAM), qbittorrent-mam (seeding), Audiobookshelf (the library,
and sending ebooks to a Kindle).

Workflow for "get me <audiobook or ebook>":
  1. landible_book_search(title, author, format=) — pick the right work (not a
     summary or study guide) and note its `foreign_book_id`. Never guess an id.
     Pass the SAME `format` you intend to request ("audiobook" or "ebook"):
     the two are separate libraries, and `in_library` answers only for the one
     you asked about. A title can be held as an ebook and still be missing as
     an audiobook, which is the normal case, not an oddity.
  2. Confirm the specific book with the user, then landible_book_request(title,
     foreign_book_id, format=). It grabs a torrent that must seed for weeks — confirm
     first. Relay `message`; `guard` means too many MAM torrents are still
     unsatisfied, so wait rather than retry. On `choose` nothing was grabbed:
     show `releases` and ask which one, then call again with `release_title`.
  3. landible_book_status follows it: relay each book's `summary`. `imported`
     means it's in Audiobookshelf; `failed` gives the reason (including "wrong
     content", which was already blocklisted and removed from the library).
  - An ebook does not go to a Kindle by itself: once it is `imported`, confirm
    the book and send it with landible_book_kindle.
  - landible_book_cancel stops tracking a request that will never finish. It
    never touches the torrent (a MAM torrent must keep seeding), so it does
    NOT free a MAM slot — say so when you relay it.
  - landible_audiobooks answers "what audiobooks / ebooks do we have / what's new?".
  - landible_mam_stats answers "how is the MAM account doing / how close to VIP?".
""",
)


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    """Cheap liveness probe — self-contained, no backend calls."""
    return JSONResponse({"status": "ok"})


# ============================================================================
# WEBHOOKS — outbound event dispatch (Home Assistant or any endpoint)
# ============================================================================
#
# Relay book events to operator-configured HTTP targets. The target list (with
# embedded secrets) lives in the deploy shim's gitignored state; these tools
# proxy to the shim's bearer-gated /api/webhooks CRUD because this server has
# no access to that file. Nothing here is HA-specific.


@mcp.tool
async def landible_webhook_list() -> dict:
    """List the configured outbound webhook targets.

    Target URLs embed secrets (an HA webhook id, a token in the query), so they
    come back REDACTED — scheme + host only, e.g. "https://ha.local/***".
    `has_headers` flags whether custom headers are set without revealing them.

    Returns {webhooks: [{name, url, events, enabled, has_headers}], total}.
    """
    r = await _deploy.get("/api/webhooks")
    r.raise_for_status()
    return r.json()


@mcp.tool
async def landible_webhook_set(
    name: str,
    url: str,
    events: list[str],
    headers: dict[str, str] | None = None,
    enabled: bool = True,
) -> dict:
    """Add or update an outbound webhook target (upsert by name).

    CONFIRM WITH THE USER FIRST — this writes an endpoint (and any embedded
    secret) that landible will POST real events to.

    `url` is any endpoint accepting an HTTP POST (e.g. an HA webhook
    https://ha.local/api/webhook/<id>). `events` is the list this target wants —
    any of "landible.book_ready", "landible.book_failed", "landible.book_stuck",
    "landible.book_suspect", "landible.mam_health", "landible.book_digest"
    and "landible.unit_failed" (a systemd unit on the host has failed).
    `headers` carries auth the target needs (e.g. {"Authorization": "Bearer .."}).
    `enabled=False` keeps the config but stops delivery.

    Re-calling with the same `name` REPLACES that target (no duplicate). The
    payload landible sends is {event, data, timestamp}, where data carries
    {title, author, source, message}. Returns
    {status, name, events, enabled, total}.
    """
    _require_secret()
    body: dict[str, Any] = {"url": url, "events": events, "enabled": enabled}
    if headers is not None:
        body["headers"] = headers
    r = await _deploy.put(f"/api/webhooks/{quote(name, safe='')}", json=body)
    r.raise_for_status()
    return r.json()


@mcp.tool
async def landible_webhook_delete(name: str) -> dict:
    """Delete an outbound webhook target by name.

    Idempotent — returns status "deleted" (with the new total) or "not_found".
    """
    _require_secret()
    r = await _deploy.delete(f"/api/webhooks/{quote(name, safe='')}")
    r.raise_for_status()
    return r.json()


@mcp.tool
async def landible_webhook_test(name: str) -> dict:
    """Fire a synthetic `landible.test` event at one target and report delivery.

    Use this to confirm a freshly-added target actually receives events. Fires
    regardless of the target's `enabled`/subscription settings — it's a raw
    "does this endpoint work?" probe. Returns {name, delivered, status,
    response_tail} on a reachable target, or {name, delivered: false, error}
    if the POST itself failed.
    """
    _require_secret()
    r = await _deploy.post(f"/api/webhooks/{quote(name, safe='')}/test")
    # 502 here means the target was unreachable — surface the body, don't raise.
    if r.status_code not in (200, 502):
        r.raise_for_status()
    return r.json()


# ----------------------------------------------------------------------------
# Books (Chaptarr + qbittorrent-mam + Audiobookshelf; logic in books.py)
# ----------------------------------------------------------------------------


@mcp.tool
async def landible_book_search(title: str, author: str | None = None, limit: int = 10,
                               format: str = "audiobook") -> list[dict]:
    """Look up a book by TITLE (metadata only; this never searches MAM).

    Pass the title alone in `title` (adding the author there ranks study guides
    first) and the author in `author` to filter. Each hit has `title`, `author`,
    `year`, `foreign_book_id` (the argument to landible_book_request), `format`,
    `in_library` and `request_state` (null if never requested). Skip summaries,
    study guides and box sets unless asked for.

    `format` is "audiobook" (default) or "ebook", and it is the format the
    ANSWER is about: `in_library` and `request_state` are read from that
    format's library and ledger entry only. The two are held separately, so the
    same title can be in one and not the other — search with the format you are
    actually going to request, or you will be told about the wrong one.

    `foreign_book_id` is the audiobook record's id either way (Chaptarr's
    lookup returns audiobook hits only); pass it to landible_book_request with
    the matching `format` and it resolves the ebook record from there.
    """
    return await books.search(title, author, limit, format)


@mcp.tool
async def landible_book_request(
    title: str, foreign_book_id: str, release_title: str | None = None,
    format: str = "audiobook",
) -> dict:
    """Request an audiobook or ebook from MAM. CONFIRM THE SPECIFIC BOOK WITH THE USER FIRST.

    `format` is "audiobook" (default) or "ebook". They are independent: the same
    title has a separate Chaptarr record per format, so asking for the ebook of
    a book already in the audiobook library is a normal, fresh request — and
    both draw on the SAME MAM cap, each holding a slot for 72 h.

    An ebook lands in the Ebooks library; it does NOT go to a Kindle by itself.
    Send it with landible_book_kindle once `landible_book_status` says `imported`.

    Ebook-only statuses: `no_ebook_record` (Chaptarr's metadata lists the
    audiobook only — nothing to search for), and `no_kindle_format` (every
    release is AZW3 or MOBI, which Amazon refuses; naming one still grabs it
    but it will never reach a Kindle).

    `title` and `foreign_book_id` come from ONE landible_book_search hit. Refuses
    (`status: guard`) when too many MAM torrents haven't seeded 72 h yet;
    otherwise adds the book to Chaptarr (never the author's other books),
    searches MAM through it and grabs the best non-VIP release.

    Returns {success, status, message, ...}. `status`: `grabbed` (downloading
    now; follow with landible_book_status), `guard`, `in_library`,
    `already_requested`, `rejected` (bad id; search again), `none_found`,
    `vip_only`, or `dramatization_only` — the only releases this account
    can TAKE are radio plays, stage adaptations or abridgements, which are never
    grabbed automatically because they are a different work from the book. This
    does not mean MAM lacks the book: an unabridged copy may well exist behind
    `[VIP]`, and the `message` says so when it does. Nothing was grabbed;
    `releases` shows what was there, and any non-VIP one can be had by name.

    `choose` means nothing was grabbed because the best release the account can
    take looks wrong (`concerns` says why — a dramatization, an abridgement, or
    far too small). SHOW THE USER `releases` and let them pick, then call this
    again with `release_title` set to their choice's exact title.

    Each release carries two different answers, so don't read one for the other:
    `grabbable` is "this release passes our gate", NOT "this is the one being
    taken" — several can be true at once, and in a `choose` reply several are
    while nothing was grabbed, so never relay it as "you are getting this".
    `best_pick` names the release a `choose` reply is about. `nameable` is "the user can have it by naming it", true for
    anything that isn't `[VIP]`, including a release Chaptarr rejected
    (`rejections` says why); that is the field to offer an override from, and
    how to get a release after `none_found`. `nameable: false` means `[VIP]`:
    MAM refuses those for this account whoever asks, so offer them only as
    "this exists but needs VIP". Relay `message`.
    """
    _require_secret()
    return await books.request(title, foreign_book_id, release_title, format)


@mcp.tool
async def landible_book_status(query: str | None = None) -> dict:
    """Where's my audiobook? Every requested book with its state, already decided.

    NOT read-only: checking a book is what settles it, so this call can act.
    On a wrong-content import it blocklists the release and deletes the library
    copy (the torrent keeps seeding), and when Chaptarr has filed a finished
    download under a different book of the same author it re-imports that file
    against the book it was grabbed for (once per grab). Both only ever
    touch books the ledger says were requested here. Don't call it to "just
    look" if that matters — but it is the only way to advance a stuck book.

    Each book has a `format` ("audiobook" or "ebook") — the same title can be
    listed twice, once per format, and they are separate requests.

    Each book has `state` (`requested` = nothing grabbed yet, `downloading`,
    `verifying` = imported, waiting for Audiobookshelf's scan, `imported`,
    `failed`) and a plain-English `summary`: relay the summary. `content` is the
    embedded-tag check: `ok`; `suspect` or `unverified` = ask the user to check
    it's the right book; `mismatch` = wrong book, already blocklisted and removed
    from the library (the torrent keeps seeding). `guard` is the MAM unsatisfied count vs its cap. `query` filters
    by a title/author substring.
    """
    # Gated like a write: on a wrong-content import this blocklists the release
    # and deletes the library copy.
    _require_secret()
    return await books.status(query)


@mcp.tool
async def landible_book_cancel(book_id: int) -> dict:
    """Stop tracking a book request that will never finish. CONFIRM WITH THE USER FIRST.

    For a grab that downloaded but which Chaptarr refuses to file, so it sits
    in `downloading` for ever and keeps raising `book_stuck`. It marks the
    ledger entry cancelled and stops Chaptarr tracking it.

    It NEVER touches the torrent — that keeps seeding, because removing or
    pausing a MAM torrent is a hit & run. So cancelling does **not** give the
    MAM slot back: the grab already counted against the unsatisfied cap, and
    only 72 h of seeding clears it. Say that when you relay the result, or the
    user will cancel expecting to free capacity.

    `book_id` is from landible_book_status. Returns {success, status, message}.
    `status`: `cancelled`, `not_found`, or `not_cancellable` (already imported
    or already cancelled — an imported book is removed from the library by
    hand, not here).
    """
    _require_secret()
    return await books.cancel(str(book_id))


@mcp.tool
async def landible_book_kindle(query: str, device: str | None = None) -> dict:
    """Email a book that is ALREADY in the library to a Kindle. CONFIRM THE BOOK FIRST.

    This is a separate step from requesting one on purpose: a book arriving in
    the library and a book going to the device are different decisions. It
    sends nothing that is not already there — to get a new book, use
    landible_book_request and wait for `imported`.

    `query` is a title fragment. `device` is the Audiobookshelf e-reader device
    name; omit it to use the server's default (ABS_KINDLE_DEVICE). Audiobookshelf
    does the sending, so delivery depends on Amazon accepting mail from the
    configured sender for that Kindle.

    Returns {success, status, message, ...}. `status`:
      `sent`        — on its way; relay `message` and the size.
      `not_found`   — nothing matching has an ebook file at all.
      `ambiguous`   — several match; SHOW the user `candidates` and call again
                      with a more exact title. `size_mb` and `filename` are how
                      to tell a real book from a PDF supplement.
      `unsendable`  — Amazon would drop it (not EPUB/PDF, or over 50 MB).
                      Refused here on purpose: Amazon bounces nothing, so an
                      unchecked send just never arrives and nobody learns why.
      `no_device`   — no device was given and no default is configured, the
                      Kindle is not set up, or the `mcp` user is not allowed to
                      use it. `message` says which.

    NOTE an Audible audiobook can carry a PDF supplement, which counts as an
    ebook file and is a legitimate thing to send — so a title may match an
    audiobook. The reply always names the file, so say which one is going.
    """
    # Gated like a write: it sends real email to a real device.
    _require_secret()
    return await books.kindle(query, device)


@mcp.tool
async def landible_audiobooks(limit: int = 20) -> dict:
    """The audiobook library (Audiobookshelf): total books + the most recently added.

    Covers Audible (Libation) and MAM books alike. For "did my request land?"
    use landible_book_status.
    """
    return await books.audiobooks(limit)


@mcp.tool
async def landible_mam_stats() -> dict:
    """MAM account stats: points, ratio, upload, class, and progress to Power User / VIP.

    Read from the hourly landible-mam-stats poller's file (this server never
    calls MAM). `power_user` and `vip` say what's met and what's short ("how
    close am I to VIP?": `vip.points_short`, plus `vip.needs_power_user_first`).
    `power_user.time.eligible_on` is null when the join date isn't configured.
    `mam` has MAM's own counts: `unsat_count`/`unsat_limit`, hit & runs
    (`seedHnr_count`, `inactHnr_count`), `connectable`. `local` is
    qbittorrent-mam's view. `stale: true` = the poller hasn't succeeded for
    2 h; say so, and relay `last_error`.
    """
    return await books.mam_stats()


def main() -> None:
    mcp.run(
        transport="http",
        host=os.environ.get("MCP_HOST", "0.0.0.0"),
        port=int(os.environ.get("MCP_PORT", "8087")),
    )


if __name__ == "__main__":
    main()

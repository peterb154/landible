"""Audiobook requests: Chaptarr (grabber) + qbittorrent-mam (guard) + ABS (check).

The flow is Claude -> this module -> Chaptarr -> Prowlarr -> MAM.
Rules this file must keep (the invariants):
  - Nothing here talks to MAM or Prowlarr, and no tool output carries a
    Prowlarr URL or key: every result is built from an allow-list of fields
    (`_slim_*`), never by passing an API object through.
  - qbittorrent-mam is READ-ONLY from here (login + torrents/info). Removing or
    pausing a MAM torrent is a hit & run.
  - A request is refused BEFORE anything is added to Chaptarr once the guard
    count reaches MAM_UNSATISFIED_CAP.

The ledger (books.json) is keyed by Chaptarr's local bookId; this server is its
only writer, same pattern as requests.json. Chaptarr's DB holds ~17k books
(whole bibliographies of the Libation authors), so only filtered calls here.

WHICH GRAB IS THIS? (read before touching a ledger field across polls)
---------------------------------------------------------------------
Five bugs have come from one property: **the ledger and Chaptarr's live state
disagree during windows.** Every field written by one poll and read by a later
one is a claim about a grab that may no longer be the current one.

The window itself is now closed at the root: `derive`'s history query is
per-book and reads `HISTORY_PAGE` (200) records, against a real maximum of 7
for the most-churned book in the ledger. A `grabbed` record can no longer age
out while its download is still in the queue — which is what the guards below
were each written to survive. They stay as belt and braces, not as the only
defence.

The rule, still: **prefer live state over the ledger whenever something live is
in hand.** The queue item IS the download; Chaptarr's history IS what happened.
The ledger is a cache, and a stale cache here is not a slow answer — it is a
confident wrong one, acted on.

Fixed instances:
  - a blocked download imported against the book it was grabbed for, keyed
    by grab so it happens once.
  - say in the summary when a blocked import could not be redone.
  - `grab_history_id` blocklisted a release that was never the problem, and
    `suspect` reached nobody.
  - the release matcher rejected every valid release.
  - `reconsider` moved 2 of the 3 flags one rejection sets.
  - `grab_token` read the ledger's hash over the queue item's `downloadId`,
    so a re-grab's blocked import was never forced.

Current cross-poll readers, and why each is safe:
  - `grab_token`      queue item first, ledger only when there is no item.
  - `grab_history_id` `derive` clears it on an import whose grab it cannot
                      confirm, so `_verify` never blocklists blind.
  - `imported_path` / `file_id`  written on the same line that sets
                      `verifying`, so they always describe that import.
  - `content` / `content_detail`  consumed by book_events keyed on
                      `imported_at`, so a re-import re-alerts.
  - `grabbed_at`      deliberately keeps the FIRST grab's date; display only.
  - `alerted_stuck`   written once and never read — book_events tracks this in
                      its own state file (`stuck_done`). Vestigial.
"""
from __future__ import annotations

import asyncio
import math
import os
import re
import struct
import traceback
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from . import ledger

CHAPTARR_URL = os.environ.get("CHAPTARR_URL", "http://localhost:8789")
CHAPTARR_API_KEY = os.environ.get("CHAPTARR_API_KEY", "")
QBT_MAM_URL = os.environ.get("QBT_MAM_URL", "http://localhost:8081")
QBT_MAM_USER = os.environ.get("QBT_MAM_USER", "admin")
QBT_MAM_PASSWORD = os.environ.get("QBT_MAM_PASSWORD", "")
ABS_URL = os.environ.get("ABS_URL", "http://localhost:13378")
ABS_API_KEY = os.environ.get("ABS_API_KEY", "")

# MAM's own limit on torrents not yet seeded 72 h — `unsat_limit` in
# landible_mam_stats — is 20 for new members and 50 at User class. Ours is a
# local estimate of their count, so it stays below whichever limit is live:
# raise this in mcp/.env when `unsat_limit` actually rises. A date used to be
# written here instead, which was wrong — class promotion is earned (4 weeks,
# 25 GiB up, ratio 2.0), not scheduled, so the date can pass with the limit
# still at 20.
MAM_UNSATISFIED_CAP = int(os.environ.get("MAM_UNSATISFIED_CAP", "15"))
SATISFIED_SEED_S = 72 * 3600

BOOK_LEDGER = str(Path(__file__).resolve().parents[2] / "systemd" / "books.json")

# Written hourly by systemd/mam_stats.py, which alone holds the mam_id
# cookie. This server only reads the allow-listed stats; it never calls MAM.
MAM_STATS_FILE = str(Path(__file__).resolve().parents[2] / "systemd" / "mam-stats.json")
MAM_STATS_FRESH_S = 2 * 3600   # older than this, MAM's unsat count isn't used by the guard
# jsonLoad.php has no join date. Power User: 4 weeks in, 25 GiB up, ratio 2.0;
# VIP needs Power User + 5,000 bonus points (MAM bonus store, 2026-09-19).
# Set MAM_JOINED (YYYY-MM-DD) to get a Power User eligibility date; unset, the
# time criterion is reported as unknown rather than guessed.
MAM_JOINED = os.environ.get("MAM_JOINED", "")
POWER_USER_DAYS, POWER_USER_GIB, POWER_USER_RATIO = 28, 25, 2.0
VIP_POINTS = 5000
_BELOW_POWER_USER = {"mouse", "user"}
GIB = 1024 ** 3

# This query is already per-book (`bookId=`), so the page only has to cover ONE
# book's events — and a book barely has any. Measured on a live install,
# every entry in the ledger: the most-churned book (9247, several grabs, imports,
# deletions and failures) has 7 records, and `totalRecords` equals page 1 for all
# of them. 20 was small enough to make "the grab aged out of the window" a real
# shape, and five bugs were attributed to it;
# a page this size makes that structurally impossible instead of guarding each
# reader against it one at a time. The per-book filter is what keeps it cheap.
HISTORY_PAGE = 200

# Chaptarr's library root and stock profiles; scripts/chaptarr_setup.py sets them up.
# Ebooks are a parallel set: a book record is format-specific (`mediaType`), and
# the same title has SEPARATE records for audio and ebook sharing a `baseBookId`
# (East of Eden: 9250 audiobook, 20670 ebook). So `ebookMonitored` on an
# audiobook record does nothing at all — the ebook record has to be found.
EBOOK_ROOT_FOLDER = "/music/books/ebooks"
EBOOK_QUALITY_PROFILE = 1     # "E-Book": PDF/MOBI/EPUB/AZW3
EBOOK_METADATA_PROFILE = 2    # "Ebook Default"
ROOT_FOLDER = "/music/books/audiobooks"
ABS_ROOT = "/audiobooks"   # the same dir inside the audiobookshelf container
# Audiobookshelf serves BOTH book libraries as `mediaType: book`, so the folder
# each one points at is the only structural thing telling them apart. compose
# binds /music/books/ebooks to /ebooks inside the container; verified against
# a live /api/libraries (Audiobooks -> /audiobooks, Ebooks ->
# /ebooks). The library's NAME is not used: that is typed in the ABS UI and can
# be renamed there, while the folder is the bind mount. The trailing
# slash is stripped because "/ebooks/" would match no library at all and quietly
# report every ebook as absent — the very failure this is here to fix.
ABS_EBOOK_FOLDER = os.environ.get("ABS_EBOOK_FOLDER", "/ebooks").rstrip("/")
AUDIOBOOK_QUALITY_PROFILE = 2
AUDIOBOOK_METADATA_PROFILE = 1

# The release search runs Prowlarr -> MAM, which can take a while.
_chaptarr = httpx.AsyncClient(base_url=CHAPTARR_URL, headers={"X-Api-Key": CHAPTARR_API_KEY}, timeout=120.0)
_abs = httpx.AsyncClient(base_url=ABS_URL, headers={"Authorization": f"Bearer {ABS_API_KEY}"}, timeout=15.0)
_qbt_mam = httpx.AsyncClient(base_url=QBT_MAM_URL, timeout=15.0)   # cookie session; logs in per call


def _now_iso() -> str:
    # Seconds precision + Z, the same shape as Chaptarr's history dates, so the
    # two compare correctly as strings.
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ============================================================================
# Pure helpers (unit-tested, no I/O)
# ============================================================================

def unsatisfied_count(torrents: list[dict]) -> int:
    """Torrents MAM would still call unsatisfied: incomplete, or seeded < 72 h.

    Counts every torrent in qbittorrent-mam (it holds nothing but MAM), which
    over-counts rather than under-counts.
    """
    return sum(
        1 for t in torrents
        if (t.get("progress") or 0) < 1 or (t.get("seeding_time") or 0) < SATISFIED_SEED_S
    )


def _is_vip(release: dict) -> bool:
    return "[VIP]" in (release.get("title") or "")


def _grabbable(r: dict) -> bool:
    """Would we actually take this release?

    `downloadAllowed` defaults CLOSED. It is a permission field, and defaulting
    it permissive is what let the matcher fix go inert in silence: `reconsider` cleared two
    of the three flags Chaptarr derives from a rejection, the missing one read
    as True in every fixture, and twelve green tests described a response shape
    the API never sends. A permission that goes missing must block, and say so
    by failing, not quietly re-enable grabbing.
    """
    return (
        bool(r.get("approved")) and not r.get("rejected")
        and r.get("downloadAllowed", False) and not _is_vip(r)
    )


# Chaptarr resolves a release to a book by title WITHIN THE AUTHOR, so a title
# several of that author's records share leaves it unable to choose, and it says
# so. It is the one rejection we second-guess: see `vouched_for`. It
# groups its reasons under a category, which is what to read — the sentence is
# free text it can reword. The string is the fallback for a response without
# `rejectionDetails`.
_MATCHING_CATEGORY = "matching"
_ADVISORY_REJECTION = "match a different book by this author"


def _only_advisory(r: dict) -> bool:
    """Chaptarr's sole objection is its own title matcher having given up."""
    details = r.get("rejectionDetails")
    if details:
        return all((d.get("category") or "").lower() == _MATCHING_CATEGORY for d in details)
    rejections = [str(x) for x in r.get("rejections") or []]
    return bool(rejections) and all(_ADVISORY_REJECTION in x.lower() for x in rejections)


def release_main_title(release_title: str, author: str | None) -> str:
    """A MAM release title reduced to the work: no "[ENG / M4B]" tag, no "by <author>"."""
    got = _main_title(release_title)
    if author:
        got = re.sub(rf"\bby\s+{re.escape(author)}\b", " ", got, flags=re.IGNORECASE)
    return got


def release_is_this_book(release_title: str, title: str, author: str | None) -> bool:
    """Does OUR matcher say this release is the requested book?

    Both ways round, as `in_abs` does: one way alone lets "East of Eden" match
    the five-novel collection that merely contains it. `_main_title` drops the
    "[ENG / M4B]" tag; the "by <author>" MAM appends has to go too, or the
    reverse direction fails on the very releases this is meant to rescue.
    """
    got = release_main_title(release_title, author)
    return (
        title_matches(title, got) and title_matches(got, title)
        and (author_matches(author, release_title) if author else True)
    )


def vouched_for(r: dict, title: str, author: str | None) -> bool:
    """Would we grab this release although Chaptarr wouldn't?

    Only when its single objection is the title matcher above AND our own
    matcher agrees. [VIP] is never overridden — MAM 406s it whoever asks.

    Two independent layers, and the second is doing the real work. `Matching`
    is a whole category, not one reason, so it also covers a rejection that
    means "this is not the book" rather than "I cannot choose between this
    author's records" — The Pearl's 1048 MB mystery anthology is the case to
    keep in mind. `release_is_this_book` is what holds those out, which
    is why it is tested against that anthology directly.
    """
    return (
        not _is_vip(r) and not r.get("approved") and _only_advisory(r)
        and release_is_this_book(r.get("title") or "", title, author)
    )


def reconsider(releases: list[dict], title: str, author: str | None) -> list[dict]:
    """Mark the releases our own matcher vouches for; Chaptarr's list is unchanged otherwise.

    All three flags have to move together. Chaptarr derives `rejected` and
    `downloadAllowed: False` from the same rejection we are overriding, and
    `_grabbable` reads every one of them — flipping `approved` alone changes
    nothing at all, which is how this shipped broken the first time.
    """
    return [
        {**r, "approved": True, "rejected": False, "downloadAllowed": True, "own_match": True}
        if vouched_for(r, title, author) else r
        for r in releases
    ]


def would_take(r: dict) -> bool:
    """The whole auto-pick gate, in one place.

    `_grabbable` is the approval half. The other half is that a dramatization,
    stage adaptation or abridgement is a different work from the book and has
    been the wrong answer 3 for 3 (9247, 9249, the 42 MB Pearl candidate), so
    it is never the automatic answer — though it stays *nameable*, since the
    override path skips `pick_release` entirely.

    `_slim_releases` reports this, not `_grabbable`: reporting the approval
    half alone put `grabbable: true` on the very release a `dramatization_only`
    reply had just refused to take, which is the contradiction this removed.
    """
    return _grabbable(r) and not _NOT_THE_BOOK.search(r.get("title") or "")


def pick_release(releases: list[dict], title: str = "",
                 author: str | None = None) -> tuple[dict | None, str]:
    """The first release Chaptarr approved that isn't [VIP], in Chaptarr's ranking.

    Returns (release, "ok") or (None, "none_found" | "vip_only" |
    "dramatization_only"). No size vs
    runtime check: Chaptarr only learns a book's runtime from the imported file,
    so it's never known before a grab (checked live). `release_concerns` and the
    content check after import are the safeguards.

    A different-language edition is ranked last rather than excluded. MAM
    files translations under the same work, so Chaptarr ranks them like any
    other release; preferring an edition we can read means an English copy
    lower down gets grabbed instead of the caller being asked about a Greek one
    that happened to rank first. If every candidate is a translation the best
    of them is still returned, so `release_concerns` raises it as a `choose`
    rather than the request dead-ending in `none_found`.

    Releases only *we* approved (`own_match`) come last and biggest first:
    Chaptarr's own approval is the stronger signal. Its `rank` is NOT — it is
    populated on rejected releases too, and on East of Eden it puts a 166 MB BBC
    radio play second, above every full M4B reading. Size is the blunt but
    honest proxy for the fullest reading.
    """
    approved = [r for r in releases if _grabbable(r)]
    the_book = [r for r in releases if would_take(r)]
    ranked = [r for r in the_book if not r.get("own_match")]
    ours = sorted((r for r in the_book if r.get("own_match")), key=lambda r: -(r.get("size") or 0))
    ordered = ranked + ours
    if title:
        readable = [r for r in ordered if not different_edition(r.get("title") or "", title, author)]
        ordered = readable + [r for r in ordered if r not in readable]
    if ordered:
        return ordered[0], "ok"
    if approved:
        return None, "dramatization_only"
    if releases and all(_is_vip(r) for r in releases):
        return None, "vip_only"
    return None, "none_found"


# A radio play or a stage adaptation is a different work from the book, and MAM
# lists them right next to it. "unabridged" must not match the abridged branch.
_NOT_THE_BOOK = re.compile(
    r"\b(?:bbc|classic serial|radio (?:4|play|drama)|latw|l\.?a\.? theatre works"
    r"|dramati[sz]ed|dramati[sz]ation|full cast)\b|(?<!un)abridged",
    re.IGNORECASE,
)
# Below this share of the largest release that could be this book, the pick is
# too small to be the full reading. The Grapes of Wrath: 157 MB against 1126 MB.
SMALL_RELEASE_RATIO = 0.5
# Part of the book, or the book plus others. Size catches a fragment only when
# there is something bigger to measure it against, and an omnibus is BIGGER than
# the book, so neither is reachable by the size rule alone.
_NOT_JUST_THIS_BOOK = re.compile(
    r"\b(?:part\s+(?:one|two|three|four|\d+)|vol(?:ume)?\.?\s*\d+"
    r"|omnibus|collection|complete works|box\s?set)\b|[&/+]",
    re.IGNORECASE,
)


def bundled_or_partial(release_title: str, title: str, author: str | None) -> bool:
    """Does this release carry a part marker, or this book AND another?

    The marker alone means nothing — "Pride & Prejudice" and "Crime &
    Punishment" are one book each, and a "Volume 1" the user asked for by name
    is the book they asked for. What makes it a fragment or a bundle is the
    marker PLUS words the requested title doesn't have: "East of Eden & Grapes
    Of Wrath" brings {grapes, wrath}, "East of Eden, Part One" brings
    {part, one}, and an exact match brings nothing.
    """
    got = release_main_title(release_title, author)
    if not _NOT_JUST_THIS_BOOK.search(got):
        return False
    return bool(_words(got) - _words(_main_title(title)))


def _could_be_this_book(r: dict) -> bool:
    """Is this release a plausible yardstick for the book's size?

    Only [VIP] disqualifies a release from being the book itself — every other
    rejection is Chaptarr saying "this is something else", and measuring against
    something else invents doubt: The Pearl (a novella) was flagged as too small
    against a 1048 MB mystery anthology that listed a "Pearl S. Buck" story.

    A release our own matcher vouched for counts, or this check stays dead
    exactly when it is needed: with every release advisory-rejected there is no
    yardstick at all, so a fragment would sail through unmeasured.
    """
    if r.get("own_match"):
        return True
    return not [x for x in r.get("rejections") or [] if "[VIP]" not in str(x)]


def _without_author(text: str, author: str | None) -> str:
    """`text` with the author's name words removed, so only the title is judged."""
    if not author:
        return text
    for word in re.findall(r"[^\W\d_]+", author, flags=re.UNICODE):
        text = re.sub(rf"\b{re.escape(word)}\b", " ", text, flags=re.IGNORECASE)
    return text


def different_edition(release_title: str, title: str, author: str | None) -> str | None:
    """Why this release looks like a different-language edition, or None.

    MAM files translations under the same work, so Chaptarr APPROVES them as
    real releases of the requested book and `would_take` takes them on
    `approved` alone — no title check ever runs. That is how the Greek edition
    of The Martian ("Άνθρωπος στον Άρη - Andy Weir") was grabbed and imported.
    It matters more for ebooks than audiobooks: the embedded-tag
    content check that catches a wrong audiobook afterwards reads AUDIO tags,
    so it is skipped for ebooks entirely and nothing downstream notices.

    `release_is_this_book` cannot be used as the veto here. It matches both
    ways, so an ordinary release carrying extra words fails the reverse test —
    "The Martian - Andy Weir (2011) Retail EPUB" needs 4 of its 6 tokens to
    appear in "The Martian" and only 2 do. That is exactly why it only ever
    vouches FOR a rejected release and never rejects an approved one. These
    two checks are deliberately narrower, and both raise a concern (a
    `choose`) rather than rejecting: a false positive costs one question, a
    wrong grab costs 72 h of seeding.
    """
    got = _without_author(release_main_title(release_title, author), author)
    want = _main_title(title)

    # 1. Different script. Only meaningful when the wanted title is Latin;
    #    a legitimately non-Latin request must not flag every real release.
    if any(c.isalpha() and c.isascii() for c in want):
        foreign = sum(1 for c in got if c.isalpha() and not c.isascii())
        latin = sum(1 for c in got if c.isalpha() and c.isascii())
        if foreign > latin:
            return "its title is written in a different script, so it is probably a translation"

    # 2. Same script, different language — "El Marciano" for "The Martian" —
    #    and releases carrying no title at all. Zero shared words after the
    #    author is removed from BOTH sides is the signal. Stripping the wanted
    #    title too is what keeps a book named after its own author ("Ozzy" by
    #    Ozzy Osbourne) from reducing to nothing and flagging itself.
    want_words = _words(_without_author(want, author))
    if want_words and not (want_words & _words(got)):
        return "its title shares no words with the book that was asked for"
    return None


def release_concerns(release: dict, releases: list[dict],
                     title: str = "", author: str | None = None) -> list[str]:
    """Why the top pick may not be the book that was asked for; empty = just grab it.

    A grab has to seed for 72 h and counts against the MAM unsatisfied cap, so a
    doubtful pick is worth one question to the user. The size rule is
    deliberately blunt: a box set in the list makes every single title look
    small, so it asks. Asking costs a message; a wrong grab costs 72 h of seeding.

    `title`/`author` are what was requested, needed to tell a bundle from a book
    whose own title has an "&" in it; without them that check is skipped.
    """
    concerns = []
    # No dramatization check here: `pick_release` refuses to pick one at all
    # now, which is strictly stronger than asking about it.
    if title:
        wrong_edition = different_edition(release.get("title") or "", title, author)
        if wrong_edition:
            concerns.append(wrong_edition)
        if bundled_or_partial(release.get("title") or "", title, author):
            concerns.append("its title reads like part of the book, or the book bundled with others")
    size = release.get("size") or 0
    comparable = [r for r in releases if _could_be_this_book(r)]
    biggest = max((r.get("size") or 0) for r in comparable) if comparable else 0
    if size and biggest and size < SMALL_RELEASE_RATIO * biggest:
        concerns.append(
            f"it is {round(size / 1e6)} MB against {round(biggest / 1e6)} MB "
            "for the largest release that could be this book"
        )
    return concerns


def slim_release(r: dict) -> dict:
    """Allow-list only: a release also carries downloadUrl (Prowlarr + apikey)."""
    return {
        "title": r.get("title"),
        "size_mb": round((r.get("size") or 0) / 1e6),
        "freeleech": bool((r.get("indexerFlags") or 0) & 1),
        "seeders": r.get("seeders"),
        "rejections": [str(x) for x in r.get("rejections") or []][:3],
    }


def book_format(value: str | None) -> str:
    """"ebook", or "audiobook" for everything else.

    Every format argument goes through here on the way in. An unrecognised one
    must not reach `_abs_book_libraries`, where it would match no library and
    so report every book as absent — a confident wrong answer, which is the
    whole shape of the format bug.
    """
    return "ebook" if (value or "").strip().lower() == "ebook" else "audiobook"


def entry_format(entry: dict) -> str:
    """A ledger entry's format. Entries written before ebook support carry none, and
    every one of them is an audiobook — ebook requests didn't exist yet.

    Normalised, not read raw: `request` passed the format through unchecked
    at first, so an entry could have been written "Ebook". That would match
    neither format's filter in `ledger_entry` and drop the entry out of both.
    """
    return book_format(entry.get("format"))


def library_format(lib: dict) -> str:
    """"ebook" or "audiobook" for an ABS book library, by the folder it serves.

    A library with no folders at all reads as "audiobook": that is the older
    single-library shape, which only ever held audiobooks.
    """
    return "ebook" if any(
        (f.get("fullPath") or "") == ABS_EBOOK_FOLDER
        or (f.get("fullPath") or "").startswith(ABS_EBOOK_FOLDER + "/")
        for f in lib.get("folders") or []
    ) else "audiobook"


def _local_id(hit: dict) -> int:
    """Chaptarr's own id for a lookup hit, 0 if it isn't in Chaptarr.

    `localBookId` comes back as the string "0" even when `localAudiobookBooks`
    lists the local copy of the same work.
    """
    local = int(hit.get("localBookId") or 0)
    if not local and hit.get("localAudiobookBooks"):
        local = int(hit["localAudiobookBooks"][0].get("id") or 0)
    return local


def _author_name(hit: dict) -> str | None:
    return (hit.get("author") or {}).get("authorName")


def ledger_entry(hit: dict, book_ledger: dict, fmt: str = "audiobook") -> dict | None:
    """This lookup hit's ledger entry for `fmt`, by Chaptarr's local id or the foreign id.

    `_local_id` returns 0 for a book Chaptarr already holds under another
    provider id, which used to report a requested book as never requested,
    so fall back to the foreign id the entry was written with.

    Both of those keys are audiobook-shaped: `_local_id` reads
    `localAudiobookBooks`, and an ebook request is filed under the AUDIOBOOK
    hit's foreign id, because that is the id the request carried. So a title
    held in both formats matches on either key twice over, and narrowing to one
    format first is what picks the right entry — without it the answer was
    whichever entry happened to come first.
    """
    same_format = {k: e for k, e in book_ledger.items() if entry_format(e) == fmt}
    local_id = _local_id(hit)
    entry = same_format.get(str(local_id)) if local_id else None
    if entry is not None:
        return entry
    foreign = hit.get("foreignBookId")
    return next((e for e in same_format.values() if foreign and e.get("foreign_book_id") == foreign), None)


def slim_lookup(hit: dict, book_ledger: dict, abs_books: list[dict], fmt: str = "audiobook") -> dict:
    """One lookup hit, answered for `fmt`. `abs_books` must already be scoped to it."""
    entry = ledger_entry(hit, book_ledger, fmt)
    return {
        "title": hit.get("title"),
        "author": _author_name(hit),
        "foreign_book_id": hit.get("foreignBookId"),
        # Which format this answer is about. The id above is the audiobook
        # record's either way — Chaptarr's lookup returns audiobook hits only,
        # and an ebook request goes through that same id.
        "format": fmt,
        # The ledger settles it when ABS's own title disagrees with the request
        # (a mislabeled upload): those items never match `in_abs`.
        "in_library": (
            # `hasFiles` counts the AUDIOBOOK record's files, so it says nothing
            # about whether the ebook is held.
            (fmt == "audiobook" and bool(hit.get("hasFiles")))
            or in_abs(hit.get("title") or "", _author_name(hit), abs_books)
            or (entry or {}).get("state") in ("verifying", "imported")
        ),
        "request_state": entry.get("state") if entry else None,
    }


def _words(text: str | None) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _main_title(title: str) -> str:
    """"Dune (Dune, #1)" -> "Dune"; "The Hobbit, or There and Back Again" -> "The Hobbit"."""
    title = re.sub(r"[(\[].*?[)\]]", " ", title)
    return re.split(r":|;|, or ", title, maxsplit=1)[0]


def title_matches(want: str, *got: str | None) -> bool:
    """Most (60%) of the wanted main title's words are in one of `got`."""
    words = _words(_main_title(want))
    need = max(1, math.ceil(0.6 * len(words)))
    return bool(words) and any(len(words & _words(g)) >= need for g in got if g)


_NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "phd", "md"}


def author_matches(want: str | None, *got: str | None) -> bool:
    """The wanted author's surname is a word of one of `got` ("Hemingway, Ernest" is fine)."""
    names = [w for w in re.findall(r"[a-z0-9]+", (want or "").lower()) if w not in _NAME_SUFFIXES]
    return bool(names) and any(names[-1] in _words(g) for g in got if g)


def content_verdict(title: str, author: str | None, tags: dict) -> tuple[str, str]:
    """Do the imported file's embedded tags describe the requested book?

    `tags` is ABS's audioFile.metaTags. Returns (verdict, detail):
      - `mismatch`: a title tag AND an author tag are present and BOTH disagree.
        Only this one triggers the blocklist + delete, so it needs both.
      - `suspect`: one of them disagrees (e.g. the artist tag holds the
        narrator, or a mislabeled upload with the right title): a human checks.
      - `ok`, or `unverified` when the file has neither tag.
    """
    title_tags = (tags.get("tagAlbum"), tags.get("tagTitle"))
    author_tags = (tags.get("tagArtist"), tags.get("tagAlbumArtist"), tags.get("tagComposer"))
    title_ok = title_matches(title, *title_tags) if any(title_tags) else None
    author_ok = author_matches(author, *author_tags) if any(author_tags) and author else None
    got_author = " / ".join(dict.fromkeys(a for a in author_tags if a))
    detail = f"got '{title_tags[0] or title_tags[1] or '?'}' by '{got_author or '?'}'"
    if title_ok is None and author_ok is None:
        return "unverified", "the file has no title/author tags"
    if title_ok is False and author_ok is False:
        return "mismatch", detail
    if False in (title_ok, author_ok):
        return "suspect", detail
    return "ok", detail


def library_mismatch(item_title: str | None, want_title: str) -> str | None:
    """Why the library entry isn't the book that was requested, or None.

    Audiobookshelf does not always delete an item whose folder disappears — it
    can re-point that item at the NEXT folder to appear under the same author.
    East of Eden imported correctly and then sat in the library titled
    "Classic Serial - John Steinbeck - Of Mice and Men", the dead radio play
    whose folder had just been removed. The file was right; the library entry
    was not, and the obvious cleanup ("both of these are empty, delete them")
    would have deleted a 1.5 GB book.

    Nothing else notices: the content check reads the FILE's embedded tags and
    finds them correct, and `in_library` comes from Chaptarr's file count. So
    the ABS item's own title is compared here, both ways round, and only when
    ABS actually has one — a blank title is missing metadata, not a mislabel.
    """
    if not (item_title or "").strip():
        return None
    if title_matches(want_title, item_title) and title_matches(item_title, want_title):
        return None
    return f"the library lists it as {item_title!r}"


def in_abs(title: str, author: str | None, abs_books: list[dict]) -> bool:
    """Is this book among ABS search results (slimmed with _slim_abs_item)?

    The title must match both ways, so a short title ("It") doesn't match every
    longer title by the same author that happens to contain the word.
    """
    return any(
        title_matches(title, b.get("title")) and title_matches(b.get("title") or "", title)
        and author_matches(author, b.get("author"))
        for b in abs_books
    )


def derive(entry: dict, records: list[dict]) -> dict:
    """Ledger updates implied by Chaptarr's history for this book.

    Only events since the request count (the book may have older history). The
    newest import/failure/grab sets the state; the grab's hash + history id are
    kept whatever came after it (marking a wrong import failed needs the id).
    """
    since = entry.get("requested_at") or ""
    recent = sorted(
        (r for r in records if (r.get("date") or "") >= since),
        key=lambda r: r.get("date") or "", reverse=True,
    )
    updates: dict = {}
    grab = next((r for r in recent if r.get("eventType") == "grabbed"), None)
    if grab:
        updates = {
            "state": "downloading",
            "torrent_hash": (grab.get("downloadId") or "").lower() or None,
            "grab_history_id": grab.get("id"),
            "grabbed_at": entry.get("grabbed_at") or grab.get("date"),
        }
    for r in recent:
        data = r.get("data") or {}
        if r.get("eventType") == "bookFileImported":
            return {**updates, "state": "verifying", "imported_path": data.get("importedPath"),
                    "file_id": int(data["fileId"]) if data.get("fileId") else None,
                    "imported_at": r.get("date"),
                    # The grab this import came from, or None: a grab ages out of
                    # the 20-event window before its import does, and the id the
                    # ledger kept is then a PREVIOUS grab. Blocklisting that one
                    # bans a release that was never the problem.
                    "grab_history_id": grab.get("id") if grab else None}
        if r.get("eventType") == "downloadFailed":
            return {**updates, "state": "failed", "reason": data.get("message") or "download failed"}
        if r.get("eventType") == "grabbed":
            break
    return updates


def queue_progress(item: dict | None) -> dict | None:
    if not item:
        return None
    size = item.get("size") or 0
    left = item.get("sizeleft") or 0
    # Chaptarr's import rejections are long and put the useful part (which book
    # it matched instead) at the end, so keep enough of it to be readable.
    warnings = [str(m)[:400] for s in item.get("statusMessages") or [] for m in s.get("messages") or []]
    return {
        "percent": round(100 * (1 - left / size)) if size else None,
        "status": item.get("status"),
        "eta": item.get("estimatedCompletionTime"),
        "warnings": warnings[:3],
    }


def abs_item_path(imported_path: str) -> str:
    """Chaptarr's file path -> the ABS library item folder for it."""
    return str(Path(ABS_ROOT) / Path(imported_path).relative_to(ROOT_FOLDER).parent)


# Chaptarr resolves a finished download by parsing the file name and matching it
# to a book within the author — it does not trust the book it grabbed for. The
# author metadata sync pulls whole bibliographies (Steinbeck: 600 records, mostly
# near-duplicate editions, omnibuses and study guides), so "Of Mice and Men"
# matched "Oxford Literature Companions: Of Mice and Men" and the import was
# refused. We know which book the grab was made for, so say it outright: import
# against that bookId and its monitored edition.

def import_blocked(item: dict | None) -> bool:
    return bool(item) and item.get("trackedDownloadState") == "importBlocked"


def forced_once(entry: dict, item: dict | None = None) -> bool:
    """Has this grab already had its import forced? One import per grab.

    Keyed on the grab, so a re-grab of the same book tries again, but an import
    that stays blocked after we forced it isn't re-fired on every status() poll.
    Only a ManualImport we actually sent counts: when we skip for want of an
    edition, nothing was imported, and the next poll should try again — that
    skip is a state a human can fix in Chaptarr's UI.
    """
    token = grab_token(entry, item)
    return bool(token) and entry.get("import_forced_for") == token


def grab_token(entry: dict, item: dict | None = None) -> str | None:
    """Stable id for the grab a blocked import belongs to.

    **The queue item first.** It IS the download in front of us, so it always
    names the right grab. The ledger's `torrent_hash` only matches while
    `derive` has seen the current `grabbed` record — and that record ages out
    of the 20-event history window before the download leaves the queue, which
    is the defect behind two earlier bugs. Reading the ledger first meant a
    re-grab could be measured against the PREVIOUS grab's hash: `forced_once`
    then matched the old `import_forced_for`, and the new blocked import was
    never forced at all.

    Chaptarr's queue spells the hash upper case and `derive` lower-cases it,
    hence the fold. The history id is the last resort: an entry written by
    `_request` has no hash until `derive` finds the grab, and a token of None
    makes `forced_once` read False for ever — which re-sent the ManualImport
    on every poll.
    """
    token = (item or {}).get("downloadId") or entry.get("torrent_hash") or entry.get("grab_history_id")
    return str(token).lower() if token else None


def monitored_edition(editions: list[dict]) -> int | None:
    """Local integer id of the book's one monitored audiobook edition, else None.

    `GET /api/v1/book/{id}` returns `editions: []` for these records, and the
    edition list holds every translation (109 for Of Mice and Men), so it has to
    come from `/api/v1/edition?bookId=` and be filtered. Without an editionId
    ManualImport answers "Edition must be selected"; the ebook editions are the
    wrong ones to hand it (Chaptarr's own pick was an ebook).

    None unless exactly one matches. Zero happens for real — 9247 and 17684 both
    monitor an *ebook* edition now, after importing — and with two there is no
    way to tell which the file belongs to. Guessing files an audiobook against
    the wrong edition silently; skipping says so and leaves it to a human.
    """
    audiobooks = [int(e["id"]) for e in editions if e.get("monitored") and not e.get("isEbook")]
    return audiobooks[0] if len(audiobooks) == 1 else None


def import_files(candidates: list[dict], book_id: str, author_id: int | None, edition_id: int) -> list[dict]:
    """ManualImport entries: our book and edition, Chaptarr's own path and quality.

    Extras (cue, jpg) are `additionalFile` and ManualImport handles them itself.

    `disableReleaseSwitching` is Chaptarr's "re-decide which edition this file
    belongs to on import", and re-deciding is the whole thing we are overriding,
    so it's off. Its own default is on, and both books that have imported so far
    (9247, 17684) now monitor an *ebook* edition — which is what switching would
    look like. That's inference, not proof: this is the line to revert first if
    a forced import misbehaves.
    """
    return [
        {
            "path": c["path"],
            "authorId": author_id,
            "bookId": int(book_id),
            "editionId": edition_id,
            "quality": c.get("quality"),
            "indexerFlags": c.get("indexerFlags") or 0,
            "disableReleaseSwitching": True,
        }
        for c in candidates
        if c.get("path") and not c.get("additionalFile")
    ]


_SUMMARY = {
    "requested": "In Chaptarr but nothing grabbed yet ({reason}).",
    "downloading": "Downloading from MAM.",
    "verifying": "Imported; waiting for Audiobookshelf to scan it so the content can be checked.",
    "imported": "In the library (Audiobookshelf).",
    "failed": "Failed: {reason}.",
    "cancelled": "Cancelled: {reason}. The torrent is still seeding, as MAM requires.",
    "retracted": "Retracted (not in the library): {reason}.",
}


def summarize(entry: dict, progress: dict | None) -> str:
    text = _SUMMARY.get(entry.get("state"), "").format(reason=entry.get("reason") or "no release")
    if entry.get("state") == "downloading" and progress and progress.get("percent") is not None:
        text = f"Downloading from MAM: {progress['percent']}%."
    if entry.get("state") == "downloading" and entry.get("import_forced_at"):
        text += (
            " Chaptarr matched the finished file to a different book of this author, so it was"
            " re-imported against this one; check again shortly."
        )
    # A grab clears `reason`, so one set while downloading is a blocked import we
    # could not redo. Without this the book sits stuck and only Chaptarr's own
    # rejection shows, never why we left it.
    elif entry.get("state") == "downloading" and entry.get("reason"):
        text += f" {entry['reason']}, so it needs a hand."
    if entry.get("state") == "imported" and entry.get("content") == "unverified":
        # "Check it's the right book" is advice for an audiobook whose tags were
        # missing. For an ebook the check simply does not apply — it reads AUDIO
        # tags — so saying that would send the user looking for nothing.
        text += (
            " The tag check reads audio tags, so it says nothing about an ebook; send it to a"
            " Kindle with landible_book_kindle when you want it."
            if entry.get("format") == "ebook"
            else " The file has no tags, so check it's the right book."
        )
    if entry.get("state") == "imported" and entry.get("content") == "suspect":
        text += f" But its tags don't fully match ({entry.get('content_detail')}): check it's the right book."
    if entry.get("state") == "imported" and entry.get("library_mismatch"):
        # The file is fine; Audiobookshelf is showing it as something else.
        text += (
            f" The file is right but {entry['library_mismatch']} — Audiobookshelf re-used an old"
            " entry, so fix the title there. Do NOT delete it; that removes the book."
        )
    if progress and progress.get("warnings"):
        text += " Chaptarr says: " + "; ".join(progress["warnings"])
    return text


def slim_entry(book_id: str, entry: dict, progress: dict | None) -> dict:
    return {
        "book_id": int(book_id),
        "title": entry.get("title"),
        "author": entry.get("author"),
        # The same title can be here twice, once per format, and without this
        # the two entries were indistinguishable in the output.
        "format": entry_format(entry),
        "state": entry.get("state"),
        "summary": summarize(entry, progress),
        "content": entry.get("content"),
        "requested_at": entry.get("requested_at"),
        "grabbed_at": entry.get("grabbed_at"),
        "imported_at": entry.get("imported_at"),
        "torrent_hash": entry.get("torrent_hash"),
        "progress": progress,
    }


# Amazon takes EPUB and PDF and silently bins the rest, so a grab that
# can never reach the Kindle is worse than no grab. MAM's East of Eden had two
# AZW3 and one MOBI against a single multi-format release — picking the biggest,
# as the audiobook path does, would have taken an unsendable one.
KINDLE_READY = re.compile(r"\b(epub|pdf)\b", re.IGNORECASE)


def kindle_ready(release: dict) -> bool:
    """Does this release contain a format Amazon will actually accept?

    MAM puts the formats in the title ("[ENG / AZW3 EPUB]"), and Chaptarr's
    `quality` names only the first one — so the title is the honest source.
    """
    quality = ((release.get("quality") or {}).get("quality") or {}).get("name") or ""
    return bool(KINDLE_READY.search(release.get("title") or "") or KINDLE_READY.search(quality))


def author_ebook_fields() -> dict:
    """What makes an author ebook-capable — and what CREATES its ebook records.

    Enabling Steinbeck took him from 600 books to 1188 (600 audiobook + 588
    ebook); before it, no ebook record existed to monitor or search. The API
    omits these fields when unset, which is why an audiobook-only author looks
    like it has no ebook support at all.

    `ebookMonitorNewItems: none` is not optional: without it, enabling an author
    pulls their whole bibliography into "wanted" — 588 books for one request.
    """
    return {
        "ebookRootFolderPath": EBOOK_ROOT_FOLDER,
        "ebookQualityProfileId": EBOOK_QUALITY_PROFILE,
        "ebookMetadataProfileId": EBOOK_METADATA_PROFILE,
        "ebookMonitored": True,
        "ebookMonitorNewItems": "none",
    }


def new_ebook_author_payload(hit: dict) -> dict:
    """POST /api/v1/author body for an author Chaptarr does not hold yet.

    Enabling an author is what CREATES its ebook records, so an author
    missing locally has nothing to find — which surfaced as a dead end
    ("Chaptarr has never seen Gene Kim") even though the metadata source
    resolves them instantly: Gene Kim, Steve Davies and Christopher Scotton all
    came back with a foreignAuthorId and bookCount 0.

    Request-only (never monitor the author's other books) is the thing to get right here. Both
    `*MonitorNewItems: none` and `addOptions.monitor: none` are load-bearing:
    without them, enabling an author pulls the whole bibliography into
    "wanted" — 588 books for one request on Steinbeck. The audiobook
    side is left unmonitored on purpose; this author is here for an ebook.
    """
    author = dict(hit)
    author.update({
        "id": 0,
        "monitored": True,
        "tags": [],
        "audiobookMonitored": False,
        "audiobookMonitorNewItems": "none",
        "audiobookRootFolderPath": ROOT_FOLDER,
        "audiobookQualityProfileId": AUDIOBOOK_QUALITY_PROFILE,
        "audiobookMetadataProfileId": AUDIOBOOK_METADATA_PROFILE,
        "metadataProfileId": AUDIOBOOK_METADATA_PROFILE,
        "addOptions": {"monitor": "none", "searchForMissingBooks": False},
    })
    author.update(author_ebook_fields())
    return author


def ebook_record(books: list[dict], want: dict) -> dict | None:
    """The ebook book record matching an audiobook lookup hit, or None.

    `baseBookId` is the work both formats share, so it is the reliable join —
    but a lookup hit often has neither that nor a matching `foreignBookId` (the
    audiobook carries `gr:2574991`, the ebook `hc:338117`), so the title has to
    carry it.

    The title alone is not enough. Steinbeck has 588 ebook records, and a
    both-ways match on "East of Eden" returns FIVE (measured live):

        East of Eden                     <- the book
        East of Eden & Grapes Of Wrath   <- omnibus
        East of Eden: Curriculum Unit    <- study guide
        East of Eden: Dramatisation      <- a different work
        East of Den                      <- a typo record

    `_main_title` splits on ":", which is what lets the last three in. So an
    exact title match comes first, and the fuzzy pass is a fallback with the
    junk filtered out by the same rules the release matcher uses.
    """
    base = want.get("baseBookId") or want.get("foreignBookId")
    title = want.get("title") or ""
    ebooks = [b for b in books if b.get("mediaType") == "ebook"]

    by_work = [b for b in ebooks if base and b.get("foreignBookId") == base]
    if by_work:
        return by_work[0]

    exact = [b for b in ebooks if same_title(b.get("title"), title)]
    if len(exact) == 1:
        return exact[0]

    named = [b for b in ebooks if _same_work(b.get("title"), title)]
    return named[0] if len(named) == 1 else None


def _same_work(record_title: str | None, want: str) -> bool:
    """Is this record the same work, allowing only an edition's bracketed noise?

    Not `_main_title`: that splits on ":", which is exactly how "East of Eden:
    Curriculum Unit" and "East of Eden: Dramatisation" pass for the novel. A
    subtitle is a different work; "(Penguin Classics)" is the same one. So
    brackets are dropped and any remaining extra word disqualifies — which also
    rejects the omnibus ("& Grapes Of Wrath") and the typo record ("East of
    Den") without needing a rule per kind of junk.
    """
    got = re.sub(r"[(\[].*?[)\]]", " ", record_title or "")
    return bool(_words(got)) and not (_words(got) - _words(want))


def new_book_payload(hit: dict) -> dict:
    """POST /api/v1/book body, built the way Chaptarr's own UI does (getNewBook).

    The book is monitored as an audiobook; a new author gets `monitor: none`
    and `audiobookMonitorNewItems: none` so nothing else is ever searched
    (request-only).
    """
    book = dict(hit)
    book.update({
        "id": 0,
        "localBookId": None,
        "monitored": True,
        "audiobookMonitored": True,
        "ebookMonitored": False,
        "mediaType": "audiobook",
        "addOptions": {"searchForNewBook": False},
    })
    editions = [dict(e) for e in book.get("editions") or []]
    if editions and not any(e.get("monitored") for e in editions):
        editions[0]["monitored"] = True
    book["editions"] = editions
    author = dict(book.get("author") or {})
    if not author.get("id"):
        author.update({
            "metadataProfileId": AUDIOBOOK_METADATA_PROFILE,
            "audiobookMetadataProfileId": AUDIOBOOK_METADATA_PROFILE,
            "audiobookQualityProfileId": AUDIOBOOK_QUALITY_PROFILE,
            "audiobookRootFolderPath": ROOT_FOLDER,
            "audiobookMonitored": True,
            "ebookMonitored": False,
            "audiobookMonitorNewItems": "none",
            "ebookMonitorNewItems": "none",
            "monitored": True,
            "tags": [],
            "addOptions": {"monitor": "none", "searchForMissingBooks": False},
        })
    book["author"] = author
    return book


# ============================================================================
# Backend calls
# ============================================================================

# Where qbittorrent-mam saves, and where the library file is hardlinked FROM.
MAM_ROOT = "/music/books/mam"
_COVER_NAMES = ("cover.jpg", "cover.jpeg", "cover.png")
_OPF_NS = "http://www.idpf.org/2007/opf"


# Open Library, keyed on the ISBN the EPUB names itself with. Chosen over
# Google Books, which answered HTTP 429 on all three lookups tried, first
# attempt, with no auth — not something to put in an import path.
OPENLIBRARY_COVER = "https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg"
COVER_TIMEOUT_S = 12
# It answers 200 with a 43-byte placeholder for an ISBN it does not know, and a
# Kindle tile wants something bigger than a thumbnail either way.
MIN_COVER_BYTES = 2000
MIN_COVER_PX = (200, 300)
# Line art compresses far better than a photograph, which is how a publisher's
# colophon is told from cover art without an image library. Measured: Penguin's
# logo 590x750 in 34 KB = 0.077 B/px; the real cover 308x475 in 28 KB = 0.192.
MIN_COVER_BYTES_PER_PX = 0.12


def _image_size(data: bytes) -> tuple[int, int] | None:
    """(width, height) of a JPEG or PNG, or None if it isn't one."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return struct.unpack(">II", data[16:24])
    if data[:2] != b"\xff\xd8":
        return None
    i = 2
    while i < len(data) - 9:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            height, width = struct.unpack(">HH", data[i + 5:i + 9])
            return width, height
        if marker in (0xD8, 0xD9):
            i += 2
            continue
        i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    return None


def cover_quality(data: bytes) -> int:
    """How good a Kindle cover is this? 0 = not usable.

    Rejects the three things actually seen: Open Library's 43-byte placeholder,
    Goodreads-sized thumbnails (125x193), and the publisher colophon a MAM
    uploader shipped as `cover.jpg`. Score is pixel count, so the
    largest usable candidate wins.
    """
    if len(data) < MIN_COVER_BYTES:
        return 0
    size = _image_size(data)
    if size is None:
        return 0
    width, height = size
    if width < MIN_COVER_PX[0] or height < MIN_COVER_PX[1]:
        return 0
    if len(data) / (width * height) < MIN_COVER_BYTES_PER_PX:
        return 0      # line art, not a photograph — a logo
    return width * height


def epub_isbns(epub_path: str) -> list[str]:
    """ISBN-13s the EPUB names itself with, best first.

    The OPF is the authority; the file names are the fallback, and are what
    identified the Penguin Modern Classics printing we actually downloaded
    (`9780141185064.opf`). An ISBN names the EDITION, so it finds the cover of
    the book in hand rather than of some other printing of the work.
    """
    with zipfile.ZipFile(epub_path) as z:
        names = z.namelist()
        opf = next((n for n in names if n.lower().endswith(".opf")), None)
        xml = z.read(opf).decode("utf-8", "replace") if opf else ""
    found = re.findall(r"97[89][0-9]{10}", xml) + re.findall(r"97[89][0-9]{10}", " ".join(names))
    return list(dict.fromkeys(found))


def _fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "landible/1.0"})
    with urllib.request.urlopen(req, timeout=COVER_TIMEOUT_S) as r:
        return r.read()


def best_cover(epub_path: str, shipped: str | None, fetch=_fetch) -> bytes | None:
    """The best usable cover for this book, or None.

    Open Library first (real artwork for the exact edition), the file the
    uploader shipped second. Best-effort throughout: a slow or unreachable
    cover service must never delay or fail a book landing in the library, so
    every failure is swallowed and the next candidate tried.

    Among usable candidates the largest wins. Only ISBNs the EPUB names itself
    with are looked up, so they are printings of the book in hand rather than
    of some other edition — the distinction that matters, since a different
    printing's cover would be wrong however large it is.
    """
    candidates: list[bytes] = []
    for isbn in epub_isbns(epub_path):
        try:
            candidates.append(fetch(OPENLIBRARY_COVER.format(isbn=isbn)))
        except Exception:
            continue
    if shipped and os.path.isfile(shipped):
        try:
            with open(shipped, "rb") as fh:
                candidates.append(fh.read())
        except OSError:
            pass
    usable = [(cover_quality(c), c) for c in candidates]
    usable = [(score, c) for score, c in usable if score]
    return max(usable, key=lambda pair: pair[0])[1] if usable else None


def epub_cover_state(epub_path: str) -> tuple[str | None, bool]:
    """(the OPF's path inside the zip, does it declare a cover?).

    Kindle shows a generic DOC tile for a personal document with no declared
    cover — which is what The Grapes of Wrath arrived as: 46 entries, one 10 KB
    inline figure, and no `<meta name="cover">` anywhere.
    """
    with zipfile.ZipFile(epub_path) as z:
        opf = next((n for n in z.namelist() if n.lower().endswith(".opf")), None)
        if opf is None:
            return None, False
        xml = z.read(opf).decode("utf-8", "replace")
    declared = 'name="cover"' in xml or 'properties="cover-image"' in xml
    return opf, declared


def source_folder(imported_path: str) -> str | None:
    """The download folder this library file is hardlinked from, or None.

    Found by inode rather than by remembering a path: the library copy and the
    seeding copy ARE the same file, so the link is the reliable join and it
    cannot go stale. Scoped to MAM_ROOT, which holds a handful of folders.
    """
    try:
        want = os.stat(imported_path).st_ino
    except OSError:
        return None
    for root, _dirs, files in os.walk(MAM_ROOT):
        for name in files:
            try:
                if os.stat(os.path.join(root, name)).st_ino == want:
                    return root
            except OSError:
                continue
    return None


def find_cover(folder: str | None) -> str | None:
    """A cover image the uploader shipped beside the book, if any."""
    if not folder or not os.path.isdir(folder):
        return None
    for name in os.listdir(folder):
        if name.lower() in _COVER_NAMES:
            return os.path.join(folder, name)
    return None


def _opf_with_cover(xml: str, href: str, media_type: str) -> str:
    """The OPF, declaring `href` as the cover. Both EPUB 2 and 3 spellings."""
    ET.register_namespace("", _OPF_NS)
    root = ET.fromstring(xml)
    manifest = root.find(f"{{{_OPF_NS}}}manifest")
    metadata = root.find(f"{{{_OPF_NS}}}metadata")
    if manifest is None or metadata is None:
        raise ValueError("OPF has no manifest/metadata")
    ET.SubElement(manifest, f"{{{_OPF_NS}}}item", {
        "id": "landible-cover", "href": href,
        "media-type": media_type, "properties": "cover-image",
    })
    # THE one that works. The Grapes of Wrath EPUB from MAM is package
    # version="2.0", where `properties="cover-image"` has no meaning — writing
    # only the EPUB 3 form passes every test and still shows a blank DOC tile
    # on the Kindle, which is exactly what happened.
    ET.SubElement(metadata, f"{{{_OPF_NS}}}meta", {"name": "cover", "content": "landible-cover"})
    return ET.tostring(root, encoding="unicode", xml_declaration=True)


def embed_cover(epub_path: str, cover: bytes | None, suffix: str = ".jpg") -> bool:
    """Rewrite the EPUB with the cover embedded. Returns True if it changed.

    **Breaks the hardlink, deliberately.** The library copy and the seeding
    torrent are one file with two names (same inode), so editing in place would
    rewrite the torrent's data, break its piece hashes and turn a healthy seed
    into a hit & run — the thing every invariant here exists to prevent. Writing
    a new file and `os.replace`-ing the library NAME leaves the other name
    pointing at the untouched original. It costs one real copy of the book.
    """
    if not cover:
        return False
    opf, declared = epub_cover_state(epub_path)
    if opf is None or declared:
        return False
    href = "landible-cover" + suffix
    media = "image/png" if cover[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
    # Beside the OPF, so its relative href resolves whatever folder it lives in.
    inner = os.path.join(os.path.dirname(opf), href) if os.path.dirname(opf) else href

    st = os.stat(epub_path)
    tmp = epub_path + ".landible-tmp"
    with zipfile.ZipFile(epub_path) as src, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as out:
        # `mimetype` must be the first entry and stored, or it is not an EPUB.
        if "mimetype" in src.namelist():
            out.writestr(zipfile.ZipInfo("mimetype"), src.read("mimetype"), zipfile.ZIP_STORED)
        for item in src.infolist():
            if item.filename in ("mimetype", inner):
                continue
            data = src.read(item.filename)
            if item.filename == opf:
                data = _opf_with_cover(data.decode("utf-8", "replace"), href, media).encode()
            out.writestr(item, data)
        out.writestr(inner, cover)
    os.chmod(tmp, st.st_mode & 0o7777)
    try:
        os.chown(tmp, st.st_uid, st.st_gid)   # ABS reads these as uid 1000
    except PermissionError:
        pass
    os.replace(tmp, epub_path)   # replaces THIS name only; the seeding one keeps the old inode
    return True


async def cancel(book_id: str) -> dict:
    """Stop tracking a grab that will never import. Never touches the torrent.

    The case this exists for: a download completes and Chaptarr refuses to file
    it, so the entry sits in `downloading` for ever and `book_stuck` fires (9249
    was exactly this). Nothing in the MCP could clear it.

    Removing the queue item uses `removeFromClient=false` — qbittorrent-mam
    keeps the torrent and keeps seeding. Removing or pausing a MAM torrent is a
    hit & run, which is an invariant of this whole file, and cancelling is a
    bookkeeping decision, not a reason to break it.
    """
    book_ledger = ledger.load(BOOK_LEDGER)
    entry = book_ledger.get(str(book_id))
    if entry is None:
        return {"success": False, "status": "not_found",
                "message": f"No request in the ledger for book {book_id}. landible_book_status lists them."}
    if entry.get("state") in ("imported", "cancelled"):
        return {
            "success": False, "status": "not_cancellable", "book_id": int(book_id),
            "message": (
                f"{entry.get('title')} is {entry['state']}, so there is nothing to cancel. "
                "A wrong imported book is taken out with landible_book_retract."
            ),
        }

    # Best-effort: the queue item may already be gone, and the ledger is what
    # actually stops status() and book_stuck acting on this entry.
    removed = False
    try:
        body = await _chaptarr_call("GET", "/api/v1/queue", params={"pageSize": 200}) or {}
        item = next((r for r in body.get("records") or [] if str(r.get("bookId")) == str(book_id)), None)
        if item:
            await _chaptarr_call(
                "DELETE", f"/api/v1/queue/{item['id']}",
                params={"removeFromClient": "false", "blocklist": "false"},
            )
            removed = True
    except httpx.HTTPError:
        traceback.print_exc()

    _record(book_id, {"state": "cancelled", "reason": "cancelled by request"})
    seeding = " Chaptarr has stopped tracking it." if removed else " It was not in Chaptarr's queue."
    return {
        "success": True, "status": "cancelled", "book_id": int(book_id),
        "message": (
            f"Cancelled {entry.get('title')}.{seeding} The torrent keeps seeding — removing it "
            "would be a hit & run. This does NOT give the MAM slot back: the grab already "
            "counted against the unsatisfied cap and only 72 h of seeding clears it."
        ),
    }


async def retract(book_id: str, reason: str) -> dict:
    """Take a wrong import back out of the library. Never touches the torrent.

    The manual version of what `_verify` does on a `mismatch` — needed because
    that check reads audio tags, so a wrong EBOOK (a Greek edition, say) is
    never caught and the ledger goes on calling it `imported`. Order matters:
    blocklist first so the same release is not grabbed again, then record, then
    delete the library copy — a hardlink, so the seeding copy is untouched.
    Deleting the torrent instead is the obvious manual cleanup, and it is a
    hit & run; that is why this is a tool and not advice.
    """
    book_ledger = ledger.load(BOOK_LEDGER)
    entry = book_ledger.get(str(book_id))
    if entry is None:
        return {"success": False, "status": "not_found",
                "message": f"No request in the ledger for book {book_id}. landible_book_status lists them."}
    if entry.get("state") != "imported":
        return {
            "success": False, "status": "not_retractable", "book_id": int(book_id),
            "message": (
                f"{entry.get('title')} is {entry.get('state')}, not imported. Retract is for a "
                "wrong book that reached the library; use landible_book_cancel for a request "
                "that never finished."
            ),
        }

    # The grab may have aged out of Chaptarr's history (404): the release then
    # can't be blocklisted, which is worth saying, not worth failing over.
    blocklisted = False
    if entry.get("grab_history_id"):
        try:
            await _chaptarr_call("POST", f"/api/v1/history/failed/{entry['grab_history_id']}")
            blocklisted = True
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 404:
                raise
    # Unmonitored, so nothing re-grabs it on its own; a new request monitors it again.
    await _chaptarr_call("PUT", "/api/v1/book/monitor",
                         json={"bookIds": [int(book_id)], "monitored": False})

    blocklist = ("release blocklisted" if blocklisted else
                 "release NOT blocklisted (its grab has aged out of Chaptarr's history)")
    _record(book_id, {"state": "retracted",
                      "reason": f"{reason}; {blocklist}, library copy removed, torrent still seeding"})
    removed = False
    if entry.get("file_id"):
        r = await _chaptarr.delete(f"/api/v1/bookfile/{entry['file_id']}")
        if r.status_code != 404:   # 404: already gone
            r.raise_for_status()
        removed = r.status_code != 404
    copy = ("Removed the library copy." if removed else
            "Chaptarr had no library file on record for it, so nothing was deleted; "
            "check the library by hand.")
    return {
        "success": True, "status": "retracted", "book_id": int(book_id),
        "message": (
            f"Retracted {entry.get('title')} ({entry.get('format') or 'audiobook'}): "
            f"{blocklist}. {copy} The torrent keeps seeding — removing it would be a "
            "hit & run. Request it again to look for a better release."
        ),
    }


async def _ebook_book_record(hit: dict) -> tuple[dict | None, str | None]:
    """(the monitored ebook record, why not) — enabling the author first.

    Enabling is what CREATES the ebook records — before it there is
    nothing to find — so it runs on every ebook request rather than being a
    manual setup step per author. It is idempotent: a PUT of fields already set.

    Two different failures, reported as two different reasons. "Chaptarr has no
    ebook edition" and "Chaptarr has never heard of this author" need different
    things from the user, and one message for both would be false half the time.
    """
    author_id = (hit.get("author") or {}).get("id")
    if not author_id:
        local = await _local_author(_author_name(hit))
        author_id = (local or {}).get("id")
    if not author_id:
        # Absent locally is not a dead end: the metadata source knows these
        # authors, and adding one is what creates its ebook records.
        added = await _add_ebook_author(_author_name(hit))
        author_id = (added or {}).get("id")
    if not author_id:
        return None, "no_author"

    author = await _chaptarr_call("GET", f"/api/v1/author/{author_id}")
    if not all(author.get(k) == v for k, v in author_ebook_fields().items()):
        author.update(author_ebook_fields())
        await _chaptarr_call("PUT", f"/api/v1/author/{author_id}", json=author)

    books = await _chaptarr_call("GET", "/api/v1/book", params={"authorId": author_id}) or []
    book = ebook_record(books, hit)
    if book is None:
        return None, "no_record"
    if not (book.get("monitored") and book.get("ebookMonitored")):
        book.update({"monitored": True, "ebookMonitored": True})
        book = await _chaptarr_call("PUT", f"/api/v1/book/{book['id']}", json=book) or book
    return book, None


async def _chaptarr_call(method: str, path: str, **kwargs: Any) -> Any:
    r = await _chaptarr.request(method, path, **kwargs)
    r.raise_for_status()
    return r.json() if r.content else None


async def mam_torrents() -> list[dict]:
    """qbittorrent-mam's torrent list. Read-only: login + torrents/info only."""
    r = await _qbt_mam.post(
        "/api/v2/auth/login",
        data={"username": QBT_MAM_USER, "password": QBT_MAM_PASSWORD},
        headers={"Referer": QBT_MAM_URL},
    )
    r.raise_for_status()
    if not (r.status_code == 204 or r.text.strip() == "Ok."):
        raise RuntimeError("qbittorrent-mam login failed")
    r = await _qbt_mam.get("/api/v2/torrents/info")
    r.raise_for_status()
    return r.json()


def load_mam_stats() -> dict | None:
    """mam-stats.json, or None if the poller hasn't written it yet."""
    try:
        return ledger.load(MAM_STATS_FILE) or None
    except ValueError:   # half-written can't happen (atomic), but don't break the guard
        return None


def stats_age_s(file: dict | None, now: datetime) -> float | None:
    fetched = (file or {}).get("fetched_at")
    if not fetched or not (file or {}).get("stats"):
        return None
    return (now - datetime.fromisoformat(fetched)).total_seconds()


def fresh_mam_unsat(file: dict | None, now: datetime) -> int | None:
    """MAM's own unsatisfied count, if the stats are fresh enough to trust."""
    age = stats_age_s(file, now)
    if age is None or age > MAM_STATS_FRESH_S:
        return None
    return file["stats"].get("unsat_count")


def ratio_value(ratio: Any) -> float:
    """MAM's ratio as a number: it sends "∞" while nothing has been downloaded."""
    if ratio == "∞":
        return math.inf
    try:
        return float(ratio)
    except (TypeError, ValueError):
        return 0.0


def power_user_time(now: datetime) -> dict:
    """The 4-weeks-in criterion. MAM's API has no join date, so it comes from
    MAM_JOINED; without a valid one the answer is unknown, not a guess."""
    try:
        joined = datetime.fromisoformat(MAM_JOINED).replace(tzinfo=timezone.utc)
    except ValueError:
        return {"eligible_on": None, "met": None,
                "note": "join date unknown: set MAM_JOINED (YYYY-MM-DD) in mcp/.env"}
    eligible = joined + timedelta(days=POWER_USER_DAYS)
    return {"eligible_on": eligible.date().isoformat(), "met": now >= eligible}


def class_progress(stats: dict, now: datetime) -> dict:
    """Progress toward Power User and VIP from the allow-listed stats."""
    time = power_user_time(now)
    up_gib = round((stats.get("uploaded_bytes") or 0) / GIB, 1)
    ratio = stats.get("ratio")
    is_pu = (stats.get("classname") or "").lower() not in _BELOW_POWER_USER
    points = stats.get("seedbonus") or 0
    power_user = {
        "reached": is_pu,
        "time": time,
        "upload": {"gib": up_gib, "need_gib": POWER_USER_GIB, "met": up_gib >= POWER_USER_GIB},
        "ratio": {"now": ratio, "need": POWER_USER_RATIO, "met": ratio_value(ratio) >= POWER_USER_RATIO},
    }
    vip = {
        # classname only: vip_until's non-VIP value isn't known yet (could be a placeholder)
        "reached": (stats.get("classname") or "").lower() in ("vip", "elite vip"),
        "needs_power_user_first": not is_pu,
        "points": points, "points_needed": VIP_POINTS, "points_short": max(0, VIP_POINTS - points),
    }
    return {"power_user": power_user, "vip": vip}


async def guard() -> dict:
    """The unsatisfied guard: our local count, raised to MAM's own count when fresh."""
    local = unsatisfied_count(await mam_torrents())
    mam = fresh_mam_unsat(load_mam_stats(), datetime.now(timezone.utc))
    count = max(local, mam or 0)
    return {"unsatisfied": count, "local": local, "mam": mam,
            "cap": MAM_UNSATISFIED_CAP, "ok": count < MAM_UNSATISFIED_CAP}


async def _lookup(title: str) -> list[dict]:
    # Title only: title + author ranks study guides first.
    return await _chaptarr_call("GET", "/api/v1/book/lookup", params={"term": title}) or []


async def _abs_book_libraries(fmt: str | None = None) -> list[dict]:
    """ABS's book libraries, or only the one holding `fmt`.

    `fmt=None` means both, and is right for the two callers that mean "find
    this file wherever it lives": the path walk, and the Kindle search (an
    audiobook can ship a PDF supplement). It is wrong for "do we already
    have this?", which has to be asked of one library or the other.
    """
    return [
        lib for lib in (await _abs_get("/api/libraries")).get("libraries") or []
        if lib.get("mediaType") == "book" and (fmt is None or library_format(lib) == fmt)
    ]


async def _abs_search_items(title: str, fmt: str | None = None) -> list[dict]:
    """Raw ABS library items matching `title` (they carry `path` and `audioFiles`)."""
    found = []
    for lib in await _abs_book_libraries(fmt):
        body = await _abs_get(f"/api/libraries/{lib['id']}/search", params={"q": title, "limit": 10})
        found += [b.get("libraryItem") or {} for b in body.get("book") or []]
    return found


async def _abs_item_at(item_path: str) -> dict | None:
    """The ABS item whose folder is `item_path`, or None if ABS hasn't scanned it.

    ABS's search matches embedded metadata, not the path, so a mislabeled file
    is unfindable by the title that was requested — which is exactly the
    case the content check exists for. Walk the library instead and match the
    path, then fetch the item for its `audioFiles`.
    """
    for lib in await _abs_book_libraries():
        page = await _abs_get(
            f"/api/libraries/{lib['id']}/items", params={"limit": 0, "minified": 1},
        )
        for item in page.get("results") or []:
            if item.get("path") == item_path:
                return await _abs_get(f"/api/items/{item['id']}")
    return None


async def _abs_search(title: str, fmt: str | None = None) -> list[dict]:
    """ABS books matching `title`, slimmed. The library truth (Libation + MAM)."""
    return [_slim_abs_item(i) for i in await _abs_search_items(title, fmt)]


def _meta_tags(item: dict) -> dict:
    files = (item.get("media") or {}).get("audioFiles") or []
    return (files[0].get("metaTags") or {}) if files else {}


async def _abs_item_by_path(title: str, item_path: str) -> dict | None:
    """The ABS item at `item_path`, or None if ABS hasn't scanned it yet.

    The title search is only the fast path (one cheap call, and it is right
    whenever the file is tagged correctly); `_abs_item_at` is the answer that
    doesn't depend on the tags being right. Neither uses "recently added": a
    Libation scan can add dozens of books at once.
    """
    for item in await _abs_search_items(_main_title(title)):
        if item.get("path") == item_path:
            return item
    return await _abs_item_at(item_path)


async def _abs_item_tags(title: str, item_path: str) -> dict | None:
    """Embedded tags of the ABS item at `item_path`, or None if unscanned."""
    item = await _abs_item_by_path(title, item_path)
    return None if item is None else _meta_tags(item)


# Amazon's Send-to-Kindle limits. It drops what it won't take with no bounce,
# so these are checked BEFORE sending — a refusal the user can read beats a book
# that silently never arrives.
KINDLE_FORMATS = ("epub", "pdf")
KINDLE_MAX_BYTES = 50 * 1024 * 1024
# ABS's e-reader device to send to by default. Set up under Settings -> Email
# there, with the `mcp` user ticked under the device's availability.
# Empty means "no default": kindle() then needs an explicit `device`.
KINDLE_DEVICE = os.environ.get("ABS_KINDLE_DEVICE", "")


def _ebook_file(item: dict) -> dict | None:
    return ((item.get("media") or {}).get("ebookFile")) or None


def kindle_blocker(ebook: dict | None) -> str | None:
    """Why Amazon would refuse this file, or None if it should go through."""
    if not ebook:
        return "it has no ebook file — only the audio"
    fmt = (ebook.get("ebookFormat") or "").lower()
    size = (ebook.get("metadata") or {}).get("size") or 0
    if fmt not in KINDLE_FORMATS:
        allowed = " or ".join(f.upper() for f in KINDLE_FORMATS)
        return f"Amazon only takes {allowed} and this is {fmt.upper() or 'unknown'}"
    if size > KINDLE_MAX_BYTES:
        return f"it is {round(size / 1e6)} MB and Amazon's limit is {KINDLE_MAX_BYTES // 1024 // 1024} MB"
    return None


async def _abs_items_with_ebooks(query: str) -> list[dict]:
    """Full ABS items matching `query` that actually carry an ebook file.

    Searched across BOTH book libraries on purpose: an Audible audiobook can
    ship a PDF supplement, which is a real thing to send. That is also why the
    caller has to report which library and which file it picked — a 600-page
    novel and a 12-page supplement look identical from the title alone.
    """
    out = []
    for hit in await _abs_search_items(_main_title(query) or query):
        item = await _abs_get(f"/api/items/{hit['id']}") if hit.get("id") else {}
        if _ebook_file(item):
            out.append(item)
    return out


def _slim_kindle_item(item: dict) -> dict:
    ebook = _ebook_file(item) or {}
    meta = (item.get("media") or {}).get("metadata") or {}
    return {
        "title": meta.get("title"),
        "author": meta.get("authorName"),
        "format": (ebook.get("ebookFormat") or "").lower() or None,
        "size_mb": round(((ebook.get("metadata") or {}).get("size") or 0) / 1e6, 1),
        "filename": (ebook.get("metadata") or {}).get("filename"),
        "library_item_id": item.get("id"),
    }


async def _local_author(name: str | None) -> dict | None:
    """Chaptarr's existing author by name. Lookup hits carry a different
    provider id (gr: vs hc:) and no local id, so adding the book as-is could
    create a second copy of the author."""
    want = _words(name)
    authors = await _chaptarr_call("GET", "/api/v1/author") or []
    return next((a for a in authors if want and _words(a.get("authorName")) == want), None)


async def _add_ebook_author(name: str | None) -> dict | None:
    """Add `name` to Chaptarr enabled for ebooks, or None if it can't be done.

    Only reached when the author is genuinely absent locally. A Chaptarr or
    network failure falls back to the existing `no_author` message rather than
    raising, and prints the cause so the journal says why. Deliberately narrow:
    a bare `except Exception` would report a TypeError here as "the metadata
    source doesn't resolve that name", which is a lie the user cannot debug.

    Verified against a real Chaptarr: this payload
    POSTs 201 and created 30 records for Gene Kim (15 audiobook + 15 ebook)
    with 0 monitored — the ebook records now exist to search, and the
    request-only invariant holds.
    """
    if not name:
        return None
    try:
        hits = await _chaptarr_call("GET", "/api/v1/author/lookup", params={"term": name}) or []
        want = _words(name)
        hit = next((h for h in hits if _words(h.get("authorName")) == want), None)
        if hit is None:
            return None
        return await _chaptarr_call("POST", "/api/v1/author", json=new_ebook_author_payload(hit))
    except httpx.HTTPError:
        traceback.print_exc()
        return None


async def _abs_get(path: str, **kwargs: Any) -> dict:
    r = await _abs.get(path, **kwargs)
    r.raise_for_status()
    return r.json()


def _slim_abs_item(item: dict) -> dict:
    media = item.get("media") or {}
    meta = media.get("metadata") or {}
    added = item.get("addedAt")
    return {
        "title": meta.get("title"),
        "author": meta.get("authorName"),
        "narrator": meta.get("narratorName"),
        "series": meta.get("seriesName") or None,
        "hours": round((media.get("duration") or 0) / 3600, 1),
        "added": datetime.fromtimestamp(added / 1000, timezone.utc).date().isoformat() if added else None,
    }


# ============================================================================
# Flows (the MCP tools in server.py are thin wrappers over these)
# ============================================================================

async def search(title: str, author: str | None, limit: int, format: str = "audiobook") -> list[dict]:
    fmt = book_format(format)
    hits = await _lookup(title)
    if author:
        want = _words(author)
        hits = [h for h in hits if want & _words(_author_name(h))]
    abs_books = await _abs_search(title, fmt)
    book_ledger = ledger.load(BOOK_LEDGER)
    return [slim_lookup(h, book_ledger, abs_books, fmt) for h in hits[:limit]]


def _record(book_id: str, fields: dict) -> None:
    """Merge `fields` into one ledger entry. No await between load and save."""
    book_ledger = ledger.load(BOOK_LEDGER)
    book_ledger.setdefault(book_id, {}).update(fields)
    ledger.save(BOOK_LEDGER, book_ledger)


_NO_GRAB = {
    "none_found": "MAM has no approved release for it",
    "vip_only": "only VIP copies exist on MAM, and the account can't download VIP torrents yet",
    # NOT "the only releases on MAM": for The Grapes of Wrath MAM has seven
    # unabridged copies, every one of them [VIP]. Saying MAM had nothing but
    # radio plays is false, and false in the reply whose job is to explain.
    "dramatization_only": "the only releases this account can take are dramatizations "
                          "or abridgements, not the full reading",
    "no_kindle_format": "every ebook release is AZW3 or MOBI, and Amazon's Send to Kindle "
                        "accepts neither — only EPUB and PDF",
}


# One request at a time, guard to grab: two concurrent requests could otherwise
# both see cap-1 and both grab. Single process, so an asyncio lock covers all callers.
_request_lock = asyncio.Lock()


async def request(title: str, foreign_book_id: str, release_title: str | None = None,
                  fmt: str = "audiobook") -> dict:
    async with _request_lock:
        return await _request(title, foreign_book_id, release_title, book_format(fmt))


def same_title(a: str | None, b: str | None) -> bool:
    """Release titles as a user relays them: spacing and case are noise.

    The title makes a round trip through a chat model before coming back in
    `release_title`, and a collapsed double space must not read as "MAM has
    dropped that release".
    """
    return " ".join((a or "").split()).casefold() == " ".join((b or "").split()).casefold()


def _slim_releases(releases: list[dict], limit: int = 12) -> list[dict]:
    """Everything MAM had, biggest first, each marked with what can be done to it.

    Two different questions, and one boolean used to answer both — which made
    every release in a `none_found` reply read as available, in the one state
    whose whole point is that nothing was taken:

      `grabbable` — this release passes the whole auto-pick gate (`would_take`):
                    we would take it without an override. A dramatization is
                    false here even when Chaptarr approved it. It is **not** "this is the one being taken" —
                    several can pass at once, and in a `choose` reply several
                    do while nothing is grabbed. `best_pick` names the one the
                    reply is actually about.
      `nameable`  — the user can override and have it by name. True for anything
                    that isn't [VIP]; MAM 406s those for this account whoever
                    asks, so a VIP release is only ever "this exists, but needs
                    VIP". This is the field to offer an override from.
    """
    ordered = sorted(releases, key=lambda r: -(r.get("size") or 0))
    return [
        {**slim_release(r), "grabbable": would_take(r), "nameable": not _is_vip(r)}
        for r in ordered[:limit]
    ]


async def _request(title: str, foreign_book_id: str, release_title: str | None = None,
                   fmt: str = "audiobook") -> dict:
    g = await guard()
    if not g["ok"]:
        return {
            "success": False, "status": "guard", "guard": g,
            "message": (
                f"Refused: {g['unsatisfied']} MAM torrents haven't seeded 72 h yet (cap {g['cap']}). "
                "Try again once some have."
            ),
        }
    hit = next((h for h in await _lookup(title) if h.get("foreignBookId") == foreign_book_id), None)
    if hit is None:
        return {
            "success": False, "status": "rejected",
            "message": "No lookup result has that foreign_book_id; re-run landible_book_search.",
        }
    name = f"{hit.get('title')} by {_author_name(hit)}"
    # Chaptarr's lookup doesn't link a hit to the copy we already have (a
    # different provider id), so ask the library itself — the ONE library that
    # holds this format. Asking both refused the Grapes of Wrath audiobook
    # because the ebook was there.
    if in_abs(hit.get("title") or "", _author_name(hit),
              await _abs_search(hit.get("title") or title, fmt)):
        return {"success": False, "status": "in_library",
                "message": f"{name} is already in the {fmt} library."}

    # Chaptarr often can't link a hit to its own copy, so the ledger is checked
    # on the foreign id too: without this a book already downloading looked new
    # and was grabbed twice.
    # Only the in-flight states: "imported" is history, and the library itself
    # (checked above and below) is the authority on what is still there.
    known = ledger_entry(hit, ledger.load(BOOK_LEDGER), fmt) or {}
    if known.get("state") in ("downloading", "verifying"):
        return {"success": False, "status": "already_requested",
                "message": f"{name} is already {known['state']}."}

    local_id = _local_id(hit)
    if fmt == "ebook":
        book, why = await _ebook_book_record(hit)
        if book is None:
            return {
                "success": False, "status": "no_ebook_record",
                "message": (
                    f"Chaptarr could not add {_author_name(hit)} — its metadata source "
                    "doesn't resolve that name, so there are no ebook records to search. "
                    "Check the spelling against the search result, or request the "
                    "audiobook instead."
                    if why == "no_author" else
                    f"Chaptarr has no ebook record for {name} — its metadata source lists "
                    "the audiobook only, so there is nothing to search for. The audiobook "
                    "may still be available."
                ),
            }
        if (book.get("statistics") or {}).get("bookFileCount"):
            return {"success": False, "status": "in_library",
                    "message": f"{name} is already in the ebook library."}
    elif local_id:
        book = await _chaptarr_call("GET", f"/api/v1/book/{local_id}")
        if (book.get("statistics") or {}).get("bookFileCount"):
            return {"success": False, "status": "in_library",
                    "message": f"{name} is already in the audiobook library."}
        entry = ledger.load(BOOK_LEDGER).get(str(local_id)) or {}
        if entry.get("state") in ("downloading", "verifying"):
            return {"success": False, "status": "already_requested", "message": f"{name} is already {entry['state']}."}
        if not (book.get("monitored") and book.get("audiobookMonitored")):
            book.update({"monitored": True, "audiobookMonitored": True})
            book = await _chaptarr_call("PUT", f"/api/v1/book/{local_id}", json=book)
    else:
        payload = new_book_payload(hit)
        if not (hit.get("author") or {}).get("id"):
            payload["author"] = await _local_author(_author_name(hit)) or payload["author"]
        book = await _chaptarr_call("POST", "/api/v1/book", json=payload)
    book_id = str(book["id"])
    # A fresh entry, so a re-request after a failure starts clean.
    book_ledger = ledger.load(BOOK_LEDGER)
    book_ledger[book_id] = {
        "title": hit.get("title"),
        "author": _author_name(hit),
        "foreign_book_id": foreign_book_id,
        # A book has a separate Chaptarr record per format, so the ledger key
        # already differs between the two — this is for the reader, and for the
        # content check, which has no audio tags to look at on an ebook.
        "format": fmt,
        "state": "requested",
        "requested_at": _now_iso(),
        # Vestigial: nothing reads this. book_events tracks stuck alerts in its
        # own state file (`stuck_done`), not here. Left in place so existing
        # entries keep their shape; remove with a ledger migration.
        "alerted_stuck": False,
    }
    ledger.save(BOOK_LEDGER, book_ledger)

    found = await _chaptarr_call("GET", "/api/v1/release", params={"bookId": book_id}) or {}
    # Filtered-out releases can't be grabbed, but they tell "VIP only" apart from "nothing".
    releases = (found.get("releases") or []) + [
        {**r, "approved": False} for r in found.get("hiddenReleases") or []
    ]
    # Chaptarr can't tell apart the records sharing this title within the author,
    # so it rejects every release of a crowded bibliography. Where our own
    # matcher is sure, that rejection doesn't stand.
    releases = reconsider(releases, hit.get("title") or "", _author_name(hit))
    overridden: list[str] = []
    if release_title is not None:
        # The user picked this one by name, so no second-guessing it: Chaptarr's
        # own rejections are advisory (its title matcher calls a radio play "a
        # different book by this author"), and this is the same override its
        # interactive search offers. [VIP] is not advisory: MAM 406s it.
        release = next((r for r in releases if same_title(r.get("title"), release_title)), None)
        if release is None:
            _record(book_id, {"reason": "the chosen release is no longer on MAM"})
            return {
                "success": False, "status": "stale", "book_id": int(book_id),
                "releases": _slim_releases(releases),
                "message": (
                    f"MAM is no longer listing a release of {name} with that exact title. "
                    "Show `releases` and ask again."
                ),
            }
        if _is_vip(release):
            _record(book_id, {"reason": _NO_GRAB["vip_only"]})
            return {
                "success": False, "status": "vip_only", "book_id": int(book_id),
                "releases": _slim_releases(releases),
                "message": (
                    f"\"{release_title}\" is a [VIP] release and MAM refuses it for this account, "
                    "whoever asks. Pick a release without [VIP], or wait for VIP."
                ),
            }
        overridden = [str(x) for x in release.get("rejections") or []][:3]
    else:
        if fmt == "ebook":
            # Amazon takes EPUB/PDF only. MAM's East of Eden offered two AZW3 and
            # a MOBI against one multi-format release — biggest-first would have
            # taken a book that can never reach the Kindle.
            sendable = [r for r in releases if kindle_ready(r)]
            if releases and not sendable:
                _record(book_id, {"reason": _NO_GRAB["no_kindle_format"]})
                return {
                    "success": False, "status": "no_kindle_format", "book_id": int(book_id),
                    "releases": _slim_releases(releases),
                    "message": (
                        f"Nothing was grabbed for {name}: {_NO_GRAB['no_kindle_format']}. "
                        "Naming one by hand still grabs it, but Amazon will not deliver it "
                        "to a Kindle — it would sit in the library only."
                    ),
                }
            releases = sendable
        want_title, want_author = hit.get("title") or "", _author_name(hit)
        release, reason = pick_release(releases, want_title, want_author)
        if release is None:
            _record(book_id, {"reason": _NO_GRAB[reason]})
            # Nothing auto-grabbable is not the end of it: the user can still
            # name one, so say so rather than dead-ending. Say it
            # accurately too — on `dramatization_only` Chaptarr *approved*
            # these and we skipped them, so "Chaptarr rejected the rest" would
            # be a false explanation in the reply that exists to explain.
            nameable = [r for r in releases if not _is_vip(r)]
            why_not = (
                "they are not the book" if reason == "dramatization_only"
                else "Chaptarr rejected them (see each release's `rejections`)"
            )
            offer = (
                f" Nothing was taken automatically because {why_not}; the user can still "
                "pick one by name and it will be grabbed." if nameable else ""
            )
            # The fact the user actually needs: the real book IS on MAM, it just
            # needs VIP. Without this the reply reads as "this book isn't here".
            # Our own matcher, not just the dramatization regex: MAM lists a
            # 47 MB "CliffsNotes: The Grapes of Wrath" as [VIP], and counting a
            # study guide as an unabridged copy overstates what waiting for VIP
            # actually buys. `_main_title` splits on ":", so CliffsNotes fails
            # the both-ways match while the Penguin Modern Classics ones pass.
            vip_full = [
                r for r in releases
                if _is_vip(r) and not _NOT_THE_BOOK.search(r.get("title") or "")
                and release_is_this_book(r.get("title") or "", want_title, want_author)
            ]
            if vip_full:
                biggest = max(r.get("size") or 0 for r in vip_full)
                offer += (
                    f" An unabridged copy does exist on MAM ({len(vip_full)} of them, "
                    f"largest {round(biggest / 1e6)} MB) but every one is [VIP], which this "
                    "account cannot take yet — landible_mam_stats shows how far off VIP is."
                )
            return {
                "success": False, "status": reason, "book_id": int(book_id),
                "releases": _slim_releases(releases),
                "message": f"Added {name} to Chaptarr, but nothing was grabbed: {_NO_GRAB[reason]}.{offer}",
            }
        concerns = release_concerns(release, releases, hit.get("title") or "", _author_name(hit))
        if concerns:
            _record(book_id, {"reason": "waiting for a release choice"})
            return {
                "success": False, "status": "choose", "book_id": int(book_id),
                "best_pick": slim_release(release), "concerns": concerns,
                "releases": _slim_releases(releases),
                "message": (
                    f"Not grabbing {name} yet: the best release the account can take is "
                    f"\"{release.get('title')}\", and {'; '.join(concerns)}. Show the user `releases` "
                    "(`nameable: false` means [VIP], which MAM refuses whoever asks) and call "
                    "landible_book_request again with release_title set to the exact title they choose."
                ),
            }
    await _chaptarr_call(
        "POST", "/api/v1/release",
        json={"guid": release["guid"], "indexerId": release["indexerId"], "bookId": int(book_id)},
    )
    _record(book_id, {"state": "downloading", "grabbed_at": _now_iso(), "reason": None})
    slim = slim_release(release)
    if overridden:
        note = f" Chaptarr had rejected it ({'; '.join(overridden)}), overridden because you named it."
    elif release.get("own_match"):
        # An override nobody asked for still costs 72 h of seeding, so say it happened.
        note = (" Chaptarr couldn't tell which of this author's books the release was, "
                "so the title was matched against the request instead.")
    else:
        note = ""
    return {
        "success": True, "status": "grabbed", "book_id": int(book_id), "release": slim,
        "guard": {**g, "unsatisfied": g["unsatisfied"] + 1},
        "message": f"Grabbed {name} ({slim['size_mb']} MB).{note} Follow it with landible_book_status.",
    }


async def _force_import(book_id: str, entry: dict, item: dict) -> dict:
    """Import a blocked download against the book it was grabbed for.

    Returns ledger updates. `importMode: copy` hardlinks, so the MAM torrent
    keeps seeding — `move` would break the hit & run rule.
    """
    blocked = "Chaptarr refused the import as a different book"

    def skipped(why: str) -> dict:
        """Nothing was imported for THIS grab, so `import_forced_at` must not stand.

        It survives a re-grab that didn't come through `_request` (a hand grab in
        Chaptarr's UI: `derive` puts the book back to downloading without
        replacing the entry), and a stale one made the summary claim an import
        that never happened for this grab, hiding `why`.
        """
        return {"reason": f"{blocked}, {why}", "import_forced_at": None}

    # No stable id for this grab means no way to remember we already imported it,
    # and an unremembered import repeats on every poll. Refuse instead.
    if not grab_token(entry, item):
        return skipped("and the grab has no torrent hash to import it against just once")
    editions = await _chaptarr_call("GET", "/api/v1/edition", params={"bookId": book_id}) or []
    edition_id = monitored_edition(editions)
    folder = item.get("outputPath")
    if not (edition_id and folder):
        missing = "no single monitored audiobook edition" if not edition_id else "no download folder"
        return skipped(f"and there is {missing} to import it against")
    candidates = await _chaptarr_call("GET", "/api/v1/manualimport", params={"folder": folder}) or []
    files = import_files(candidates, book_id, item.get("authorId"), edition_id)
    if not files:
        return skipped("and the download folder holds no importable file")
    await _chaptarr_call("POST", "/api/v1/command", json={
        "name": "ManualImport", "importMode": "copy", "files": files,
    })
    # Only a ManualImport actually sent spends the one attempt (see forced_once).
    return {"import_forced_for": grab_token(entry, item), "import_forced_at": _now_iso(), "reason": None}


async def _verify(book_id: str, entry: dict) -> dict:
    """Content check once imported. Returns ledger updates ({} = ABS hasn't scanned it yet)."""
    if entry.get("format") == "ebook":
        # The check reads embedded AUDIO tags; an ebook has none, so it would
        # read as "ABS hasn't scanned it yet" for ever. Settle it instead, and
        # say why — the file is still verifiable by a human, and the Kindle
        # send checks format and size before it goes anywhere.
        detail = "ebook: the tag check reads audio tags, which an ebook has none of"
        if (entry.get("imported_path") or "").lower().endswith(".epub"):
            # Kindle shows a generic DOC tile for a book with no declared cover,
            # and MAM uploaders routinely ship cover.jpg beside the file instead
            # of inside it. Best-effort: a cover is not worth failing an
            # import over, and `check_error` would be the wrong noise.
            try:
                path = entry["imported_path"]
                cover = best_cover(path, find_cover(source_folder(path)))
                if embed_cover(path, cover):
                    detail += "; cover embedded"
            except (OSError, ValueError, zipfile.BadZipFile) as e:
                traceback.print_exc()
                detail += f"; could not embed a cover ({type(e).__name__})"
        return {"state": "imported", "content": "unverified", "content_detail": detail}
    if not entry.get("imported_path"):
        # Nothing to look at in ABS, and no path means no way to get one. Settle
        # it here: raising would escape status() and take down every book.
        # One-way door — "imported" drops the book out of `live`, so a path a
        # later poll might have produced is never read. That beats the infinite
        # crash loop it replaces, and a human can request the book again.
        return {"state": "imported", "content": "unverified",
                "content_detail": "Chaptarr's history did not record where the file was imported"}
    want = entry.get("title") or ""
    item = await _abs_item_by_path(want, abs_item_path(entry["imported_path"]))
    if item is None:
        return {}
    tags = _meta_tags(item)
    # A separate question from the content check, and invisible to it: the FILE
    # can be right while the library ENTRY is another book.
    mislabel = library_mismatch(((item.get("media") or {}).get("metadata") or {}).get("title"), want)
    verdict, detail = content_verdict(want, entry.get("author"), tags)
    if verdict != "mismatch":
        # `content` stays honest about the tags; the library problem is its own
        # field, so neither claim has to stand in for the other.
        return {"state": "imported", "content": verdict, "content_detail": detail,
                "library_mismatch": mislabel}
    # Only a grab id `derive` confirmed against the current history: the ledger's
    # can belong to an earlier grab once this one ages out of the window, and
    # blocklisting that release bans a file that was never the problem.
    grab_history_id = entry.get("grab_history_id")
    blocklist = ("release blocklisted" if grab_history_id else
                 "release NOT blocklisted (its grab has aged out of Chaptarr's history)")
    failed = {
        "state": "failed", "content": "mismatch", "content_detail": detail,
        "reason": f"wrong content ({detail}); {blocklist}, library copy removed, torrent still seeding",
    }
    # Blocklist the release (removeFailedDownloads is off, so the torrent stays),
    # and record it at once: if the delete below fails, a later status must not
    # re-derive "failed" from the downloadFailed event with a generic reason.
    if grab_history_id:
        await _chaptarr_call("POST", f"/api/v1/history/failed/{grab_history_id}")
    _record(book_id, failed)
    # Drop the library hardlink; the seeding copy is a separate link.
    if entry.get("file_id"):
        r = await _chaptarr.delete(f"/api/v1/bookfile/{entry['file_id']}")
        if r.status_code != 404:   # 404: already gone
            r.raise_for_status()
    return failed


async def status(query: str | None) -> dict:
    book_ledger = ledger.load(BOOK_LEDGER)
    q = (query or "").lower()
    items = {
        k: v for k, v in book_ledger.items()
        if q in f"{v.get('title') or ''} {v.get('author') or ''}".lower()
    }
    live = [k for k, v in items.items() if v.get("state") in ("requested", "downloading", "verifying")]

    queue: dict[int, dict] = {}
    if live:
        body = await _chaptarr_call("GET", "/api/v1/queue", params={"pageSize": 200}) or {}
        queue = {r.get("bookId"): r for r in body.get("records") or []}

    updates: dict[str, dict] = {}
    errors: dict[str, str] = {}
    for k in live:
        # One book's error is reported on that book; the rest still show. Any
        # exception, not just HTTP: a malformed entry used to raise past this
        # and take down the whole response, for every book at once.
        # Contained, not swallowed — a bug still prints its stack trace to the
        # journal, because `check_error` alone is not enough to debug from.
        try:
            body = await _chaptarr_call("GET", "/api/v1/history", params={
                "bookId": k, "pageSize": HISTORY_PAGE, "sortKey": "date",
                "sortDirection": "descending",
            }) or {}
            u = derive(items[k], body.get("records") or [])
            merged = {**items[k], **u}
            # A finished download Chaptarr won't file under the book it was
            # grabbed for: tell it which book, once per grab.
            item = queue.get(int(k))
            if merged.get("state") == "downloading" and import_blocked(item) and not forced_once(merged, item):
                u.update(await _force_import(k, merged, item))
                merged = {**merged, **u}
            if merged.get("state") == "verifying":
                u.update(await _verify(k, merged))
        except Exception as e:
            traceback.print_exc()
            errors[k] = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
            continue
        if u:
            u["updated_at"] = _now_iso()
            updates[k] = u
    if updates:
        # Reload right before saving so a request made while we awaited survives.
        book_ledger = ledger.load(BOOK_LEDGER)
        for k, u in updates.items():
            if k in book_ledger:
                book_ledger[k].update(u)
        ledger.save(BOOK_LEDGER, book_ledger)

    books = [
        slim_entry(k, {**v, **updates.get(k, {})}, queue_progress(queue.get(int(k))))
        for k, v in items.items()
    ]
    for b in books:
        if str(b["book_id"]) in errors:
            b["check_error"] = errors[str(b["book_id"])]
    books.sort(key=lambda b: b.get("requested_at") or "", reverse=True)
    result: dict[str, Any] = {"books": books}
    try:
        result["guard"] = await guard()
    except (httpx.HTTPError, RuntimeError) as e:
        result["guard_unavailable"] = str(e) or type(e).__name__
    return result


async def kindle(query: str, device: str | None = None) -> dict:
    """Email a book already in the library to a Kindle. Deliberately a separate
    step from requesting one — arriving in the library
    and going to the device are different decisions.

    ABS does the sending, so the MCP needs no mail credentials and stays a
    non-admin ABS user; `/api/emails/send-ebook-to-device` carries no admin
    middleware, unlike the settings route beside it.
    """
    items = await _abs_items_with_ebooks(query)
    if not items:
        return {
            "success": False, "status": "not_found",
            "message": (
                f"Nothing in the library matching \"{query}\" has an ebook file. "
                "landible_audiobooks lists what is there; an audiobook only has "
                "one if Audible shipped a PDF supplement with it."
            ),
        }
    if len(items) > 1:
        return {
            "success": False, "status": "ambiguous",
            "candidates": [_slim_kindle_item(i) for i in items[:8]],
            "message": (
                f"{len(items)} library items match \"{query}\". Show the user "
                "`candidates` — the size and filename tell a book from a PDF "
                "supplement — and call again with a more exact title."
            ),
        }

    item = items[0]
    slim = _slim_kindle_item(item)
    blocker = kindle_blocker(_ebook_file(item))
    if blocker:
        # Amazon drops these silently, so refusing here is the only way the user
        # ever learns why a book never arrived.
        return {
            "success": False, "status": "unsendable", "book": slim,
            "message": f"Not sending \"{slim['title']}\": {blocker}.",
        }

    # The device name is configuration, not discovery: ABS has no REST endpoint
    # that lists a non-admin's devices (both /*/ereader-devices routes are POST
    # updates, and /api/me does not carry them — it arrives over the socket the
    # web UI uses). One env value is stabler than guessing, and `device`
    # overrides it when there is more than one Kindle.
    target = device or KINDLE_DEVICE
    if not target:
        return {
            "success": False, "status": "no_device", "book": slim,
            "message": (
                "No Kindle to send to: pass `device` (the e-reader device name in "
                "Audiobookshelf) or set ABS_KINDLE_DEVICE in mcp/.env."
            ),
        }
    r = await _abs.post("/api/emails/send-ebook-to-device",
                        json={"libraryItemId": item["id"], "deviceName": target})
    if r.status_code in (403, 404):
        # ABS's own two refusals, and they mean different things to a human.
        why = (
            f"Audiobookshelf has no e-reader device named \"{target}\"."
            if r.status_code == 404 else
            f"The `mcp` user is not allowed to use \"{target}\"."
        )
        return {
            "success": False, "status": "no_device", "book": slim,
            "message": (
                f"{why} Fix it at Settings -> Email on the Audiobookshelf server: "
                "the device must exist and have the `mcp` user ticked under its "
                "availability."
            ),
        }
    r.raise_for_status()
    return {
        "success": True, "status": "sent", "book": slim, "device": target,
        "message": (
            f"Sent \"{slim['title']}\" ({slim['format'].upper()}, {slim['size_mb']} MB) to {target}. "
            "It usually appears within a few minutes; Amazon only accepts mail from "
            "approved senders, so if it never lands, check that Audiobookshelf's SMTP "
            "sender address is on that Kindle's approved list."
        ),
    }


async def audiobooks(limit: int) -> dict:
    out = []
    # Both libraries on purpose, reported one per entry with its own name: the
    # question is "what have we got", and the answer differs per format.
    for lib in await _abs_book_libraries():
        page = await _abs_get(
            f"/api/libraries/{lib['id']}/items",
            params={"sort": "addedAt", "desc": 1, "limit": limit, "minified": 1},
        )
        out.append({
            "library": lib.get("name"),
            "total_books": page.get("total"),
            "recently_added": [_slim_abs_item(i) for i in page.get("results") or []],
        })
    return {"libraries": out}


async def mam_stats() -> dict:
    file = load_mam_stats()
    if not file or not file.get("stats"):
        return {"available": False, "error": (file or {}).get("error"),
                "message": "No MAM stats yet: the landible-mam-stats timer hasn't fetched them "
                           "(not installed, or the mam_id file is missing)."}
    now = datetime.now(timezone.utc)
    age = stats_age_s(file, now)
    stats = file["stats"]
    result: dict[str, Any] = {
        "available": True,
        "fetched_at": file.get("fetched_at"),
        "stale": age > MAM_STATS_FRESH_S,
        "last_error": file.get("error"),
        "mam": stats,
        **class_progress(stats, now),
    }
    try:
        torrents = await mam_torrents()
        result["local"] = {
            "torrents": len(torrents),
            "seeding": sum(1 for t in torrents if (t.get("progress") or 0) >= 1),
            "unsatisfied": unsatisfied_count(torrents),
            "cap": MAM_UNSATISFIED_CAP,
        }
    except (httpx.HTTPError, RuntimeError) as e:
        result["local_unavailable"] = str(e) or type(e).__name__
    return result

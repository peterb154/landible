#!/usr/bin/env python3
"""Wire Chaptarr to Prowlarr's MAM, qbittorrent-mam and the ABS library.

Idempotent: re-running updates in place, never duplicates. Prints no secrets.
The why behind each choice lives in docs/runbook.md -> Chaptarr.

  RUN (on the box, after the chaptarr + prowlarr containers are up):
    python3 /opt/landible/scripts/chaptarr_setup.py

Reads (never prints): Chaptarr + Prowlarr API keys from their config.xml,
QBT_MAM_WEBUI_PASSWORD and CHAPTARR_PASSWORD from $LANDIBLE_DIR/.env.
"""

import json
import os
import re
import urllib.request

PROJECT = os.environ.get("LANDIBLE_DIR", "/opt/landible")
COMPOSE = f"{PROJECT}/compose"
CHAPTARR = "http://localhost:8789/api/v1"
PROWLARR = "http://localhost:9696/api/v1"

FOLDER_TYPE_AUDIOBOOK = 1  # Chaptarr RootFolders.FolderType
FOLDER_TYPE_EBOOK = 2
AUDIOBOOK_QUALITY_PROFILE = 2  # stock "Audiobook"
AUDIOBOOK_METADATA_PROFILE = 1  # stock "Audiobook Default"
EBOOK_QUALITY_PROFILE = 1  # stock "E-Book"
EBOOK_METADATA_PROFILE = 2  # stock "Ebook Default"
# Paths as CHAPTARR sees them (container-side; its DB stores these).
AUDIOBOOK_ROOT = "/music/books/audiobooks"
EBOOK_ROOT = "/music/books/ebooks"
# Torznab categories asked of Prowlarr's MAM. 3030 = Audio/Audiobook,
# 7020 = Books/Ebook. WITHOUT 7020 an ebook search still goes out as 3030,
# gets audiobooks back, discards them as wrong-format and reports "no releases"
# — which looks exactly like MAM not having the book.
INDEXER_CATEGORIES = [3030, 7020]
# Least preferred first; Chaptarr treats the LAST allowed item as best. Amazon's
# Send to Kindle takes EPUB and PDF and silently drops AZW3 and MOBI, so the two
# it accepts go on top. The stock profile ranks AZW3 highest, which imported an
# unsendable file from a release that also contained the EPUB.
EBOOK_QUALITY_ORDER = ["Unknown Text", "MOBI", "AZW3", "PDF", "EPUB"]


def api_key(config_xml):
    return re.search(r"<ApiKey>([^<]+)", open(config_xml).read()).group(1)


def env(name):
    for line in open(f"{PROJECT}/.env"):
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{name} missing from {PROJECT}/.env")


def call(base, key, method, path, body=None):
    req = urllib.request.Request(
        base + path,
        method=method,
        headers={"X-Api-Key": key, "Content-Type": "application/json"},
        data=json.dumps(body).encode() if body is not None else None,
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        text = resp.read()
    return json.loads(text) if text else None


def set_fields(resource, values):
    for field in resource["fields"]:
        if field["name"] in values:
            field["value"] = values[field["name"]]
    return resource


def upsert(ck, path, name, resource):
    """POST a new named resource, or PUT over the existing one with that name."""
    existing = next(
        (r for r in call(CHAPTARR, ck, "GET", path) if r["name"] == name), None
    )
    if existing:
        resource["id"] = existing["id"]
        call(CHAPTARR, ck, "PUT", f"{path}/{existing['id']}", resource)
        return "updated"
    call(CHAPTARR, ck, "POST", path, resource)
    return "created"


def main():
    ck = api_key(f"{COMPOSE}/chaptarr-config/config.xml")
    pk = api_key(f"{COMPOSE}/prowlarr-config/config.xml")

    # Auth defaults to none, and Chaptarr has RW on the whole books tree.
    host = call(CHAPTARR, ck, "GET", "/config/host")
    host.update(
        {
            "authenticationMethod": "forms",
            "authenticationRequired": "enabled",
            "username": "admin",
            "password": env("CHAPTARR_PASSWORD"),
            "passwordConfirmation": env("CHAPTARR_PASSWORD"),
        }
    )
    call(CHAPTARR, ck, "PUT", "/config/host", host)
    print("auth: forms, user admin")

    # Hardlink imports so the torrent keeps seeding with no second copy.
    mm = call(CHAPTARR, ck, "GET", "/config/mediamanagement")
    mm["copyUsingHardlinks"] = True
    call(CHAPTARR, ck, "PUT", "/config/mediamanagement", mm)
    print("media management: copyUsingHardlinks=true")

    # Library roots, one per format. monitored=False: adding a book must never
    # pull an author's backlist. Drop any other (stale) root — but keep BOTH
    # known ones:
    # deleting the ebook root here would silently un-build the ebook feature.
    roots = {
        AUDIOBOOK_ROOT: {
            "name": "Audiobooks",
            "path": AUDIOBOOK_ROOT,
            "folderType": FOLDER_TYPE_AUDIOBOOK,
            "audiobookQualityProfileId": AUDIOBOOK_QUALITY_PROFILE,
            "audiobookMetadataProfileId": AUDIOBOOK_METADATA_PROFILE,
            "audiobookMonitored": False,
            "defaultMonitorOption": "none",
            "defaultNewItemMonitorOption": "none",
            "isCalibreLibrary": False,
        },
        EBOOK_ROOT: {
            "name": "Ebooks",
            "path": EBOOK_ROOT,
            "folderType": FOLDER_TYPE_EBOOK,
            "ebookQualityProfileId": EBOOK_QUALITY_PROFILE,
            "ebookMetadataProfileId": EBOOK_METADATA_PROFILE,
            "ebookMonitored": False,
            "ebookMonitorExisting": 0,
            # ABS reads the two as separate libraries, and /ebooks is a
            # read-only bind there, so they must not share a folder.
            "placeEbooksWithAudiobooks": False,
            "isCalibreLibrary": False,
        },
    }
    for r in call(CHAPTARR, ck, "GET", "/rootfolder"):
        if r["path"] not in roots:
            call(CHAPTARR, ck, "DELETE", f"/rootfolder/{r['id']}")
            print("root folder: removed stale", r["path"])
    have = {r["path"] for r in call(CHAPTARR, ck, "GET", "/rootfolder")}
    for path, body in roots.items():
        if path in have:
            print("root folder: exists", path)
            continue
        call(CHAPTARR, ck, "POST", "/rootfolder", body)
        print("root folder: created", path)

    # Ebook quality order. The stock "E-Book" profile ranks AZW3 highest, so a
    # release carrying AZW3 + EPUB imports the AZW3 — which Amazon refuses, and
    # the book can never reach a Kindle.
    for prof in call(CHAPTARR, ck, "GET", "/qualityprofile"):
        if prof["id"] != EBOOK_QUALITY_PROFILE:
            continue
        by_name = {(i.get("quality") or {}).get("name"): i for i in prof["items"]}
        if set(EBOOK_QUALITY_ORDER) - set(by_name):
            print("quality profile: unexpected contents, left alone")
            break
        wanted = [by_name[n] for n in EBOOK_QUALITY_ORDER]
        cutoff = (by_name["EPUB"].get("quality") or {}).get("id")
        if prof["items"] == wanted and prof.get("cutoff") == cutoff:
            print("quality profile: ebook order already EPUB-first")
            break
        prof["items"], prof["cutoff"] = wanted, cutoff
        call(CHAPTARR, ck, "PUT", f"/qualityprofile/{prof['id']}", prof)
        print("quality profile: ebook order ->", " < ".join(EBOOK_QUALITY_ORDER))
        break

    # Download client. Imported categories stay EMPTY: a post-import category
    # change can make qBittorrent move the files and stop seeding.
    dc = next(
        s
        for s in call(CHAPTARR, ck, "GET", "/downloadclient/schema")
        if s["implementation"] == "QBittorrent"
    )
    set_fields(
        dc,
        {
            "host": "qbittorrent-mam",
            "port": 8081,
            "username": "admin",
            "password": env("QBT_MAM_WEBUI_PASSWORD"),
            "audiobookCategory": "audiobooks",
            "audiobookImportedCategory": "",
            "ebookImportedCategory": "",
            "musicImportedCategory": "",
        },
    )
    # Never let Chaptarr remove a MAM torrent (and its data). Both default to True:
    # "completed" fires if a torrent is ever paused/stopped, "failed" when a grab
    # is marked failed. Either could turn into a hit & run.
    dc.update(
        {
            "name": "qbittorrent-mam",
            "enable": True,
            "removeCompletedDownloads": False,
            "removeFailedDownloads": False,
        }
    )
    print("download client:", upsert(ck, "/downloadclient", "qbittorrent-mam", dc))

    # No automatic re-grab after a failure: every MAM grab goes through the MCP,
    # whose guard keeps the unsatisfied-torrent count under MAM's limit.
    dch = call(CHAPTARR, ck, "GET", "/config/downloadclient")
    dch["autoRedownloadFailed"] = False
    dch["autoRedownloadFailedFromInteractiveSearch"] = False
    call(CHAPTARR, ck, "PUT", "/config/downloadclient", dch)
    print("failed downloads: no automatic re-grab")

    # Indexer: Prowlarr's MAM as Torznab, not via Prowlarr app sync, so the MAM
    # indexer can never be pushed into another *arr (e.g. one behind a VPN).
    mam = next(
        i for i in call(PROWLARR, pk, "GET", "/indexer") if i["name"] == "MyAnonamouse"
    )
    ix = next(
        s
        for s in call(CHAPTARR, ck, "GET", "/indexer/schema")
        if s["implementation"] == "Torznab"
    )
    set_fields(
        ix,
        {
            "baseUrl": f"http://prowlarr:9696/{mam['id']}/",
            "apiPath": "/api",
            "apiKey": pk,
            "categories": INDEXER_CATEGORIES,
        },
    )
    ix.update(
        {
            "name": "MAM (via Prowlarr)",
            "enableRss": False,
            "enableAutomaticSearch": True,
            "enableInteractiveSearch": True,
        }
    )
    print("indexer:", upsert(ck, "/indexer", "MAM (via Prowlarr)", ix))

    # MAM refuses [VIP] torrents below VIP rank ("Download Rank Blocked", HTTP 406
    # via Prowlarr), so never pick them. Drop this if the account becomes VIP.
    if not any(
        "[VIP]" in (p.get("ignored") or [])
        for p in call(CHAPTARR, ck, "GET", "/releaseprofile")
    ):
        call(
            CHAPTARR,
            ck,
            "POST",
            "/releaseprofile",
            {
                "enabled": True,
                "required": [],
                "ignored": ["[VIP]"],
                "indexerId": 0,
                "tags": [],
            },
        )
        print("release profile: created (ignore [VIP])")
    else:
        print("release profile: exists (ignore [VIP])")


if __name__ == "__main__":
    main()

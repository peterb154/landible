# Landible runbook

Audiobooks + ebooks in one LXC container: Audiobookshelf serves them, Libation
pulls an Audible library, Chaptarr grabs from MyAnonamouse (MAM) through
Prowlarr into a dedicated qBittorrent, and `landible-mcp` lets an assistant
search, request and send books.

Conventions used below:

- Project dir `/opt/landible`; compose in `/opt/landible/compose` (project name
  `landible`); env file `/opt/landible/.env` (`compose/.env` is a symlink to it).
- Host data lives under `${DATA_ROOT}` (one bind mount into the LXC, one filesystem).
- Root-only secrets outside the repo live in `/etc/landible`.
- Snippets below use `${DATA_ROOT}` and friends from `.env`. Load them into
  your shell first: `set -a; . /opt/landible/.env; set +a`.
- `<books-host>` is the hostname you serve Audiobookshelf on; `<container-ip>`
  is the container's LAN IP.

## Layout

```
${DATA_ROOT}/books/audiobooks           the library (ABS /audiobooks, Libation /data, Chaptarr root)
${DATA_ROOT}/books/ebooks               the ebook library (ABS /ebooks, read-only)
${DATA_ROOT}/books/mam                  MAM seeding (qbittorrent-mam; Chaptarr hardlinks from here)
${DATA_ROOT}/appdata/chaptarr-mediacover  Chaptarr cover cache (regenerable, off the rootfs)
${DATA_ROOT}/libation-staging           Libation in-progress downloads (not backed up)
```

**Container-side paths are unchanged from the stack this was split out of**
(`/audiobooks`, `/ebooks`, `/music/books`, `/music/books/mam`). The apps'
databases store those paths, so only the host side of a bind ever changes.
Where this doc says `/music/books/...` it means the path *inside* a container;
on the host that is `${DATA_ROOT}/books/...`.

**Keep `DATA_ROOT=/music`.** `landible-mcp` and the pollers run natively on
the LXC, not in Docker, and open Chaptarr's paths (`/music/books/...`)
directly: EPUB cover fixes, the inode search for a book's seeding folder, the
MAM health check. With `DATA_ROOT=/music` one path means the same file
everywhere — LXC, containers and databases.

Chaptarr mounts `${DATA_ROOT}/books` as **one** bind at `/music/books`:
hardlinks need the seeding dir and the library in the same mount.

## Units

systemd units live in `mcp/systemd/`. Unit changes are not auto-deployed.
`scripts/install-units.sh` copies them to `/etc/systemd/system`, runs
`daemon-reload` and `enable --now`s the timers. The manual equivalent for any
one unit is:

```bash
cp /opt/landible/mcp/systemd/landible-<name>.{service,timer} /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now landible-<name>.timer
```

Units: `landible-book-events`, `landible-book-digest`, `landible-mam-health`,
`landible-mam-stats`, `landible-unit-health` (timers), plus `landible-mcp` and
`landible-deploy` (services).

## Audiobookshelf (ABS)

- **URL:** `https://<books-host>` (LAN or home VPN), direct `http://<container-ip>:13378`
- **Image:** `ghcr.io/advplyr/audiobookshelf`, pinned in `compose/docker-compose.yml`
- **Runs as:** `${PUID}:${PGID}`
- **Volumes:**
  | Container | Host | What |
  |---|---|---|
  | `/config` | `/opt/landible/compose/audiobookshelf-data/config` | DB + settings |
  | `/metadata` | `/opt/landible/compose/audiobookshelf-data/metadata` | covers, cache, ABS's own backups |
  | `/audiobooks` | `${DATA_ROOT}/books/audiobooks` | the library (`Author/Series/Title/`) |
  | `/ebooks` (**ro**) | `${DATA_ROOT}/books/ebooks` | the Ebooks library |
- **Health:** `GET /healthcheck` → 200 (Docker healthcheck; autoheal restarts it).

### First-time setup

Before the first `docker compose up`:

```bash
mkdir -p ${DATA_ROOT}/books/audiobooks /opt/landible/compose/audiobookshelf-data/{config,metadata}
chown 1000:1000 ${DATA_ROOT}/books/audiobooks /opt/landible/compose/audiobookshelf-data/{,config,metadata}
```

Then in the web UI:

1. Create the root (admin) user.
2. Settings → Users → add one user per household member (type: user). Check
   that **Can Download** is on for each (apps hide Download without it).
3. Settings → Libraries → add a library: Media Type *Books*, name `Audiobooks`,
   Metadata Provider **Audible.com** (better narrator/series/chapter data than
   Google Books), folder `/audiobooks`. Leave "Automatically watch libraries for
   changes" (Settings page) on, so new books appear without a manual scan.
4. **Backups** page (left sidebar, not the Settings page): enable automatic
   backups, daily, keep 7. They are clean DB copies in `/metadata/backups`, on
   the container rootfs, so whatever backs up the container covers them.

The compose binds use `create_host_path: false`: if a dir is missing, the
deploy fails with "bind source path does not exist" instead of starting a
container that can't write. Run the `mkdir`/`chown` and redeploy.

### Ebooks library + Send to Kindle

ABS holds ebooks in a second library and emails them to Kindles itself. The
`/ebooks` bind is **read-only**: those files are hardlinks of MAM seeding
torrents, so ABS must never write them. Covers and edits go to `/metadata` and
the DB, not next to the files.

1. Before the deploy that adds the bind:
   `mkdir -p ${DATA_ROOT}/books/ebooks && chown 1000:1000 ${DATA_ROOT}/books/ebooks`
2. Library: `POST /api/libraries` with name `Ebooks`, mediaType `book`, folder
   `/ebooks`. Leave "Store covers with item" and "Store metadata with item" off
   (Settings); both default off and would fail on the ro mount. ABS **Upload**
   into this library fails too, by design: ebooks arrive via Chaptarr or a
   host-side copy.
3. Email (Settings → E-mail), through an AWS SES SMTP user: host = the SES SMTP
   endpoint for your region, port 587, secure **off** (587 is STARTTLS), from
   `<sender@your-domain>`. `GET /api/emails/settings` returns the password in
   the clear, so don't log it.
4. E-reader devices: **one per person**, availability *Specific users* set to
   that person's ABS user, so each only sees their own Kindle. The address is
   in the Kindle app → Settings → Send-to-Kindle Email Address. Personal
   documents sync to every Kindle and app on that Amazon account.
5. Each person adds `<sender@your-domain>` to Amazon's **Approved Personal
   Document E-mail List** (Manage Your Content and Devices → Preferences →
   Personal Document Settings). Approval is **per address**: Amazon silently
   drops mail from any sender not on the list, with no bounce.

Kindle email accepts EPUB and PDF up to 50 MB, not MOBI/AZW3.

**The Chaptarr side is in `scripts/chaptarr_setup.py`** and is idempotent: the
ebook root folder, the EPUB-first quality order and the `[3030, 7020]` indexer
categories. Keep them there, not as UI-only config: an earlier version of the
script deleted any root folder that wasn't the audiobook one, so a re-run
silently un-built ebooks.

### Requesting ebooks

`landible_book_request(..., format="ebook")`. Three things make it work, and the
third is invisible from Chaptarr's side:

1. **An ebook root folder:** `/music/books/ebooks`, `folderType: 2` (2 = ebook;
   1 = audiobook). Created once by the setup script.
2. **The author must be ebook-enabled:** `ebookRootFolderPath`,
   `ebookQualityProfileId` (1, "E-Book"), `ebookMetadataProfileId` (2),
   `ebookMonitored`, and **`ebookMonitorNewItems: "none"`**. The API omits
   these when unset, so an audiobook-only author looks like it has no ebook
   support. **Enabling is what creates the ebook records** (an author with 600
   audiobook records gains a similar number of ebook ones). The request code
   does this per request and it is idempotent, so it is not a manual step.
   Without `ebookMonitorNewItems: none` every new ebook record would be "wanted".
3. **The indexer must ask for the ebook category.** With `categories: [3030]`
   (audiobooks only) Chaptarr sends that even for ebook records: it gets
   audiobooks back, discards them as wrong-format and reports 0 releases. It
   must be `[3030, 7020]` (7020 = Books/Ebook). This is only visible in
   **Prowlarr's** search history, not in Chaptarr.

**A book record is format-specific** (`mediaType`); the two formats are separate
records sharing a `baseBookId`. So `ebookMonitored` on an audiobook record does
nothing, the ledger key already differs per format, and asking for the ebook of
a book already in the audiobook library is a normal fresh request. Both draw on
the same MAM cap.

**The in-library check is scoped to one format.** ABS serves both libraries as
`mediaType: book`, so `_abs_book_libraries(fmt)` tells them apart by the folder
each points at: `/ebooks` (`ABS_EBOOK_FOLDER`) is the ebook one, anything else
is audio. Not by name: the folder is the compose bind, the name is typed in the
UI. Without this, a hit in either library marked a title as held in both.
`landible_book_search` takes the same `format` and answers `in_library` and
`request_state` for that format alone; `ledger_entry` filters on the entry's
own `format` first, because both its keys are audiobook-shaped and would match
a title's ebook entry too.

**Format selection differs from audio.** Amazon takes EPUB and PDF only, so
`kindle_ready` filters releases before picking; otherwise biggest-first can
grab an AZW3/MOBI that can never reach a Kindle. All-AZW3/MOBI gives
`no_kindle_format`; naming one by hand still grabs it, into the library only.

### Ebook covers

Kindle shows a generic DOC tile for a personal document that declares no cover,
and MAM uploaders routinely ship `cover.jpg` *beside* the book rather than in it.

- **Source:** Open Library, keyed on the ISBN the EPUB names itself with (e.g.
  a `<isbn>.opf`). An ISBN names the edition, so it finds the cover of the book
  in hand. The cover file the uploader shipped is the fallback. **Google Books
  is deliberately not used**: unauthenticated, it answers HTTP 429 immediately.
- **`cover_quality` rejects three real failure modes:** Open Library answers
  **200 with a 43-byte placeholder** for an unknown ISBN; Goodreads covers are
  ~125x193 thumbnails; uploaders ship a publisher colophon as `cover.jpg`,
  which looks like a real cover by size alone. Line art compresses far better
  than a photograph, so bytes-per-pixel separates them without an image
  library (logo ~0.077, real cover ~0.192; the floor is 0.12).
- **Both cover declarations are written.** MAM files are often EPUB 2, where
  `properties="cover-image"` means nothing; writing only the EPUB 3 form passes
  tests and still shows a blank tile. The EPUB 2 `<meta name="cover">` is the
  one Amazon's converter reads.
- **Embedding breaks the hardlink, deliberately.** The library copy and the
  seeding torrent are one inode, so editing in place would rewrite the
  torrent's data, break its piece hashes and turn a seed into a hit & run.
  `embed_cover` writes a new file and `os.replace`s the *library* name; the
  seeding name keeps the original. It costs one real copy of the book. A test
  asserts the seeding copy is byte-for-byte unchanged and the link was
  actually broken; don't remove it.
- The download folder is found **by inode**, not a remembered path: the link
  is the join and cannot go stale.
- Best-effort: a missing cover, odd zip or read-only file logs and moves on.
- **The content check does not apply** to ebooks: it reads embedded *audio*
  tags, so an ebook settles as `unverified` with a reason saying so.

### Sending to Kindle over MCP

`landible_book_kindle`. Sending is a separate step from requesting, by design:
a book arriving in the library and a book going to the device are different
choices. ABS does the sending, so the MCP needs no mail credentials and keeps
its non-admin ABS user: `POST /api/emails/send-ebook-to-device` has no admin
middleware, unlike `/api/emails/ereader-devices` beside it. It does check that
the user may use the device, so the **`mcp` user must be ticked under that
device's availability** (Settings → E-mail) or it gets 403.

The device name is config (`ABS_KINDLE_DEVICE=<device-name>`), not discovery:
ABS has no REST endpoint listing a non-admin's devices (both
`*/ereader-devices` routes are POST updates and `/api/me` doesn't carry them).

Format and size are checked **before** sending. Amazon bounces nothing, so an
unchecked send just never arrives.

**An Audible audiobook can carry a PDF supplement**, which counts as an
`ebookFile` and is legitimate to send, so a title search can match an
audiobook. The reply always names the file and its size, because a 600-page
novel and a 12-page supplement look identical by title.

## Libation (Audible → M4B)

Pulls the Audible library, strips DRM, writes M4B (chapters + cover) into the
audiobook library as `Author/[Series/]Title/`, then rescans every 6 h
(`SLEEP_TIME`) for new purchases.

- **Image:** `rmcrackan/libation`, pinned; runs as `${PUID}` (the image default is 1001).
- **Config:** `compose/libation-config/Settings.json` is **tracked**: the folder
  template + `TokenStorageMethod: Plaintext`. The container copies `/config`
  inward and doesn't write back, so git stays clean. `AccountsSettings.json`
  (Audible tokens) sits next to it and is gitignored.
- **DB:** `compose/libation-data/db/` (gitignored).
- **Staging:** the image forces its in-progress dir to `/tmp`, bound to
  `${DATA_ROOT}/libation-staging` so multi-GB downloads stay off the rootfs.
- All three dirs must exist, owned by 1000, before the first `up`:
  ```bash
  cd /opt/landible/compose
  mkdir -p libation-config libation-data/db ${DATA_ROOT}/libation-staging
  chown 1000:1000 libation-config libation-data libation-data/db ${DATA_ROOT}/libation-staging
  ```

### Audible login (once, and on token expiry)

Do this yourself from your own terminal; never hand Audible credentials or the
response URL to an agent. In the container:

```bash
cd /opt/landible/compose && docker compose run --rm libation \
  LibationCli login-external --libationFiles /config --locale us --account <your audible email>
```

Use `docker compose run`, not `docker exec`: with no valid account the scan
exits 3 and the service container restart-loops, so there's nothing to exec
into. The same happens when tokens expire, and this command is the fix.

Open the printed URL in a browser, sign in to Audible, then paste the **final
address-bar URL** back into the terminal. Then:

```bash
chmod 600 /opt/landible/compose/libation-config/AccountsSettings.json
chmod 600 /opt/landible/compose/libation-config/libation-master.key   # minted at login; unused with Plaintext
docker exec libation LibationCli list-accounts --libationFiles /config
docker restart libation   # scan now instead of waiting 6 h
```

**Signs of trouble:** libation not up / restarting in `docker compose ps` →
`docker logs libation`. `scan failed (exit 3)` = no valid account → re-login.
The weekly digest counts Libation errors (`BookStatus` 2 in its DB).

**Hung scan:** the healthcheck marks the container unhealthy when a
`LibationCli` process (scan or liberate) has run more than 6 h, and autoheal
restarts it. Between runs only `liberate.sh` + `sleep` are alive, which is healthy.

**Harmless log noise:** every run prints `LAST-RESORT: Token encryption is
enabled…`. Libation resolves a key store even with `TokenStorageMethod:
Plaintext`; the setting is still honoured. The tokens are as sensitive as the
other `.env` secrets.

## qbittorrent-mam (MAM's client)

A qBittorrent for MyAnonamouse only.

- **Home IP, not a VPN:** MAM rule 1.2. Plain compose network, no VPN.
- **Version:** MAM whitelists clients (5.0.1–5.2.x as of 2026-09-18). Check
  [allowed clients](https://www.myanonamouse.net/tor/allowed_clients.php) before any bump.
- **Ports:** WebUI `http://<container-ip>:8081` (LAN only). BitTorrent `58123`
  tcp+udp, the only thing port-forwarded on the router → `<container-ip>`.
- **Paths:** saves to `/music/books/mam` inside the container
  (`${DATA_ROOT}/books/mam` on the host). Books get **hardlinked** into the
  library by Chaptarr; never move or delete anything here, or seeding stops.
- **Config:** qBittorrent owns `compose/qbittorrent-mam-config/` (gitignored).
  The tracked seed `compose/qbittorrent-mam-seed/` is copied in once before the
  first `up`:
  ```bash
  cd /opt/landible/compose
  mkdir -p qbittorrent-mam-config/qBittorrent ${DATA_ROOT}/books/mam
  cp qbittorrent-mam-seed/{qBittorrent.conf,categories.json} qbittorrent-mam-config/qBittorrent/
  chown -R 1000:1000 qbittorrent-mam-config ${DATA_ROOT}/books/mam
  ```
  Then set a random WebUI password once (qBt stores only a PBKDF2 hash) and put
  the plaintext in `/opt/landible/.env` as `QBT_MAM_WEBUI_PASSWORD`: POST
  `/api/v2/app/setPreferences` with `json={"web_ui_username":"admin","web_ui_password":"…"}`
  from inside the container (`docker exec qbittorrent-mam curl … localhost:8081/…`).

Why the seed settings (qBittorrent strips comments, so the reasons live here):

| Setting | Why |
|---|---|
| `GlobalMaxRatio=-1`, `GlobalMaxSeedingMinutes=-1`, `GlobalMaxInactiveSeedingMinutes=-1` | Never stop seeding. MAM needs 72 h per torrent; we seed forever. (`GlobalMaxRatio=0` would mean *stop at completion*, the opposite.) |
| `QueueingSystemEnabled=false` | Queued torrents don't upload, earn no seed time and quietly become hit & runs. |
| `DHTEnabled/PeXEnabled/LSDEnabled=false` | Private tracker: peers come from MAM only. |
| category `audiobooks` → `/music/books/mam` | Chaptarr tags grabs with it; its limits are `-2` = inherit the global unlimited. |
| No subnet whitelist; password for everyone | A bridge whitelist is bypassable: a container reaching the published port via the host IP arrives from a docker gateway address (tested). So every caller, including Chaptarr and `landible-mcp`, logs in as `admin` with `QBT_MAM_WEBUI_PASSWORD`. `LocalHostAuth=false` only lets the in-container healthcheck skip auth. |

Verify with `/api/v2/app/preferences` after a restart: all of the above should
be applied.

## Prowlarr

Holds the MAM indexer, entered by hand (it wants the `mam_id` cookie). Uses
FlareSolverr at `http://flaresolverr:8191`. **No app sync**: Chaptarr reaches
Prowlarr's MAM as a plain Torznab indexer at `http://prowlarr:9696`, so the MAM
indexer can never be pushed into any other app. `Use Freeleech Wedges` is on.

## Chaptarr (the grabber)

Readarr fork, picked over LazyLibrarian because LazyLibrarian can't hardlink.

- **UI:** `http://<container-ip>:8789`, user `admin`, password
  `CHAPTARR_PASSWORD` in `/opt/landible/.env`. Its auth defaults to **none**,
  so the setup script turns on forms auth.
- **Image:** `chaptarr/chaptarr`, pinned. Every release is a beta pre-release;
  read the notes before bumping. Metadata comes from the central
  `api2.chaptarr.com` (a single point of failure; search text + file names go there).
- **Mounts:** `./chaptarr-config:/config`, the MediaCover cache
  (`${DATA_ROOT}/appdata/chaptarr-mediacover`), and **one**
  `${DATA_ROOT}/books:/music/books` bind. Don't split it: separate binds for
  the seeding dir and the library make `link()` fail (EXDEV) and Chaptarr
  silently copies instead, doubling disk.
- **First run on a fresh host:** before the first `up`,
  `mkdir -p compose/chaptarr-config ${DATA_ROOT}/appdata/chaptarr-mediacover`,
  `chown 1000:1000` both, and add `CHAPTARR_PASSWORD=$(openssl rand -hex 16)`
  to `/opt/landible/.env`. Run the setup script **right after** the first
  `up`: until then the UI has no login.
- **Wiring** (`scripts/chaptarr_setup.py`, idempotent, prints no secrets; run
  after the container is up): forms auth; `copyUsingHardlinks`; root
  `/music/books/audiobooks` (Audiobook type, **monitored false**) and the ebook
  root; download client `qbittorrent-mam` (category `audiobooks`, **imported
  category empty**: a post-import category change can make qBt move files and
  stop seeding); indexer = Prowlarr's MAM as **Torznab** with categories
  `[3030, 7020]`; a release profile ignoring `[VIP]`; `autoRedownloadFailed`
  off. The client has **`removeCompletedDownloads` and
  `removeFailedDownloads` false** (both default true): Chaptarr must never
  remove a MAM torrent or its data.

Using it (the MCP tools make the same calls):

- **Look up by title only.** `GET /api/v1/book/lookup?term=<title>`; title +
  author tends to rank study guides first. Filter by author afterwards.
- Add the book with `monitored`/`audiobookMonitored` true and the author's
  `addOptions.monitor: "none"`. The author then shows `monitored: true` (a
  gate), but `audiobookMonitorNewItems: none`: only requested books are searched.
- **Grab explicitly:** `GET /api/v1/release?bookId=` → `POST /api/v1/release
  {guid, indexerId, bookId}`. The automatic search on add didn't grab in testing.
- **VIP torrents:** MAM blocks `[VIP]` releases below VIP rank ("Download Rank
  Blocked", which Prowlarr logs as HTTP 406). The release profile ignores
  `[VIP]`; remove it if the account ever becomes VIP.
- **Importing a file that's already downloaded** (e.g. a hand grab):
  `GET /api/v1/manualimport?folder=/music/books/mam` → `POST /api/v1/command
  {name: ManualImport, importMode: "copy", files: [{path, authorId, bookId,
  editionId, foreignEditionId, quality}]}`. `editionId` is the **local
  integer** from `GET /api/v1/edition?bookId=` (the monitored non-ebook
  edition); without it: "Edition must be selected". **Never `importMode:
  move`** (it stops seeding).
- Check an import: `ls -li` the file in `${DATA_ROOT}/books/mam` and in
  `${DATA_ROOT}/books/audiobooks/...`: same inode, link count 2.

## Asking an assistant for a book (MCP)

`landible_book_search` → `landible_book_request` → `landible_book_status`, plus
`landible_audiobooks` for the library, `landible_book_cancel`,
`landible_book_kindle` and `landible_mam_stats`. Code:
`mcp/src/landible_mcp/books.py`. Requests are recorded in the `books.json`
ledger; the MCP is its only writer.

- **Guard:** a request is refused (before anything is added to Chaptarr) when
  `qbittorrent-mam` holds `MAM_UNSATISFIED_CAP` (15) torrents that are
  incomplete or seeded < 72 h. That's a local estimate of MAM's count, set
  below its real limit (`unsat_limit` in `landible_mam_stats`: 20 for new
  members, 50 at User class). **Raise the cap in `mcp/.env` when `unsat_limit`
  rises**, not on a date: promotion is earned (4 weeks, 25 GiB up, ratio 2.0).
  The MCP only reads qbittorrent-mam; it never removes or pauses a torrent.
- **One request at a time:** requests are serialized (guard → grab), so
  parallel asks can't all slip in under the cap.
- **What gets grabbed:** the first release Chaptarr approves that isn't
  `[VIP]`; otherwise nothing, and the reason is returned. (No size-vs-runtime
  check: Chaptarr only knows runtime after import.)
- **It asks before a doubtful grab:** if that release's title reads like a
  dramatization or abridgement (BBC, Classic Serial, LATW, "dramatized",
  "abridged" but not "unabridged"), or it is under half the size of the
  biggest release **that could be this book**, the tool returns `choose` and
  grabs nothing. The yardstick counts only releases Chaptarr approved plus
  ones rejected solely for `[VIP]`; measuring against releases it called a
  different book invents doubt (a novella vs. an unrelated anthology). The
  reply lists every release, `[VIP]` ones marked `nameable: false`, so the
  assistant can say "the unabridged exists but needs VIP". The user picks, and
  the assistant calls `landible_book_request` again with `release_title` set to
  the exact title. Every grab has to seed 72 h and counts against the cap,
  hence the question.
- **A named release overrides Chaptarr:** Chaptarr's rejections are advisory
  (its title matcher can call a radio play of a novel "a different book by
  this author"), so naming a release grabs it anyway, like its interactive
  search does, and the reply says what was overridden. That also rescues a
  `none_found`. The exception is `[VIP]`: MAM 406s those whoever asks, so a
  named VIP release comes back `vip_only`. In `releases`, `nameable: false`
  means exactly VIP. `grabbable` is the other question, "does this pass our
  gate", and is false on every release in a `none_found` reply. Several can be
  `grabbable` in a `choose` reply; `best_pick` names the one it is about.
- **We re-decide "a different book by this author":** that rejection means
  Chaptarr's by-title-within-the-author matcher couldn't choose between records
  sharing the title (a classic novel can match five records), so every release
  gets rejected, good ones included. When it is a release's **only**
  rejection, `release_is_this_book` decides instead: `title_matches` **both
  ways round** (one way alone takes a five-novel collection for the one novel)
  plus the author surname. Vouched releases are picked after any Chaptarr
  approved, and among themselves biggest first. Every other rejection still
  blocks, and `[VIP]` is never overridden. Two shapes match both ways but
  aren't the book alone, "Part One" and "X & Y", and size can't catch either,
  so those become a `concerns` question instead of a grab. The `&` marker
  alone isn't enough ("Pride & Prejudice" is one book): it takes the marker
  **plus words the requested title doesn't have**. A grab only we approved
  says so in its reply.
- **Dramatizations are never auto-picked:** a BBC Classic Serial, a stage play
  or an abridgement is a different work. `pick_release` skips them entirely.
  If they're all MAM has, the reply is `dramatization_only` rather than
  `none_found`. The wording is "the only releases **this account** can take":
  MAM may hold unabridged copies that are all `[VIP]`, and when it does the
  message says so, since "wait for VIP" is actionable. They stay
  **nameable**: `release_title` skips `pick_release`.
- **No automatic re-grab:** `autoRedownloadFailed` is off, so a failure can't
  take another unsatisfied slot behind the guard's back.
- **A blocked import is redone against the right book:** Chaptarr doesn't trust
  the book it grabbed for. It re-parses the finished file name and matches it
  to a book *within the author*, and the author sync pulls whole bibliographies
  (hundreds of near-duplicate editions, omnibuses, study guides). So a correct
  download can be refused as "grabbed for BookId X … but import matched
  'Study Guide: …' (BookId Y)". The ledger knows which book the grab was for,
  so on a queue item in `importBlocked`, `landible_book_status` runs
  `ManualImport` with **our** `bookId`, the monitored non-ebook `editionId`
  from `GET /api/v1/edition?bookId=` (`GET /api/v1/book/{id}` returns
  `editions: []`), `importMode: copy` and `disableReleaseSwitching`.
  **Once per grab**, counted only when a ManualImport was actually sent: if
  there isn't exactly one monitored audiobook edition it skips, says so in the
  book's `summary`, and retries next poll (fixable in Chaptarr's UI; two
  editions is a guess worth refusing). A re-grab gets a fresh attempt. The
  content check still runs afterwards.
- **The defect is title-within-author resolution, not a record cap.** It
  degrades as a bibliography grows; there is no limit to find.
  `/api/v1/book?authorId=` is unpaginated (`page`/`pageSize` ignored) and a
  prolific author like Dickens returns over 8,000 records. So the
  re-decide/naming overrides are **permanent**, not workarounds awaiting an
  upstream fix. `copy` only hardlinks **within one filesystem**: the seeding
  dir and library are both under `${DATA_ROOT}/books`; splitting them across
  mounts would silently double disk.
- **Content check:** once imported, the file's embedded tags (read through
  ABS) are compared with the request. The item is found by **path**, not by
  title: ABS's search reads embedded metadata, so a mislabeled upload (what the
  check exists for) is invisible to a title search and would hang in
  `verifying` forever. The comparison: most of the main title's words (series
  markers and subtitles dropped) and the author's surname (Jr./III dropped;
  artist, album-artist or composer tag). Only when **both** a title tag and an
  author tag disagree is it a `mismatch`: the grab is marked failed
  (blocklisting the release) and the **library** hardlink is deleted via
  Chaptarr. The seeding copy and torrent are untouched. One disagreeing signal
  is `suspect` (e.g. a rip whose artist tag is the narrator); an untagged file
  is `unverified`. Both stay in the library for a human. A `suspect` also
  pushes `book_suspect`.
- **A re-used library entry is caught at import:** ABS doesn't always delete an
  item whose folder disappears; it can re-point that item at the **next**
  folder to appear under the same author. So a correct import can show up under
  the title of a just-removed release. Nothing else notices: the content check
  reads the file's tags (correct) and `in_library` comes from Chaptarr's file
  count. So the ABS item's own title is compared with the request, both ways
  round, and a mismatch is recorded as `library_mismatch` (`content` stays
  about the tags). It rides the `book_suspect` push, and the summary says **do
  not delete the odd-looking entry**: that would remove the book, and it's the
  obvious instinct.
- **Status is gated:** `landible_book_status` needs the MCP secret like the
  write tools, because a mismatch deletes the library copy.
- **Cancel is bookkeeping only (`landible_book_cancel`):** it marks the ledger
  entry `cancelled` and stops Chaptarr tracking the queue item
  (`removeFromClient=false`), for a download Chaptarr refuses to file that
  would otherwise sit in `downloading` forever raising `book_stuck`. It
  **never touches the torrent**, which keeps seeding: removing or pausing a MAM
  torrent is a hit & run. So it does **not** free the MAM slot (only 72 h of
  seeding does), and the reply says so. `cancelled` has three consumers and all
  must handle it: the `live` filter in `status()`, `summarize()`, and
  `stuck_candidates` in `book_events.py`.
- **ABS user `mcp`:** non-admin, key in `mcp/.env` as `ABS_API_KEY`.

## Push notifications

Events go through the deploy shim's webhook fan-out (`POST /api/events/books`,
`deploy/`) to subscribed targets (e.g. Home Assistant → phone):

| Event | When | `data` |
|---|---|---|
| `landible.book_ready` | a new item appears in ABS (any source) | `title`, `author`, `source` (`mam` / `audible`) |
| `landible.book_failed` | Chaptarr history `downloadFailed` / `bookImportIncomplete` | `title`, `author`, `source`, `message` |
| `landible.book_stuck` | a `books.json` request not imported 24 h after it was made | `title`, `author`, `source`, `message` |
| `landible.book_suspect` | the content check flagged the imported file `suspect` | `title`, `author`, `source`, `message` (tags found) |
| `landible.mam_health` | the MAM account is at risk (MAM health, MAM stats below) | `title` (torrent, `qbittorrent-mam` or `MAM account`), `source`, `message` |
| `landible.book_digest` | Sunday 09:00, the weekly digest | `title`, `source` (`digest`), `message` (4 lines) |
| `landible.unit_failed` | a systemd unit in the container is failed | `title` (unit), `source` (`systemd`), `message` (last journal lines) |

"Ready" means ABS has it, so a book is announced once, when playable. A
content-check `mismatch` marks the grab failed, so it also sends `book_failed`.
`source` is `mam` when Chaptarr imported into that ABS folder (history of the
last 14 days), else `audible`: a best guess, only used for the message.

**Why a poller:** Chaptarr's Webhook can't fire on download or import failure
(`supportsOnDownloadFailure` is false), so `landible-book-events.timer` (every
5 min, `mcp/systemd/book_events.py`) reads ABS and Chaptarr's history and POSTs
each new event once. High-water marks live in
`mcp/systemd/book-events-state.json`. The first run only seeds them (no pushes
for the existing library). At most 5 `book_ready` per run, so a bulk Libation
sync doesn't send 50 pushes. A push that doesn't land (shim down, or every
target failed, which the shim returns as 502) keeps its mark and is retried next
run; the unit exits 1, so check `systemctl --failed`. A failure still unsent
after 14 days is dropped. Secrets: ABS + Chaptarr keys from `mcp/.env`,
`WEBHOOK_INBOUND_SECRET` from `deploy/.env`.

Install with `scripts/install-units.sh` (or the manual steps under Units), then:

```bash
journalctl -u landible-book-events -n 20   # first run: "seeded ... no pushes"
```

Subscribe a target with `landible_webhook_set` (list every event in its
`events`); targets match by exact event name.

### Adding a new event type: all four steps, or it fails silently

An event type lives in four places, and missing any of the last three loses or
corrupts the alert **without an error**:

1. **Emit it:** the timer (`mcp/systemd/*.py`).
2. **Map it:** `_BOOK_EVENT_MAP` in `deploy/main.py`. An unmapped event is
   acknowledged with 200 and not relayed.
3. **Subscribe it:** each target's `events` list (`landible_webhook_set`, or
   edit `deploy/state/webhooks.json` to add one event without re-sending the
   target's secret URL). `_targets_for_event` matches exact names, no wildcard.
   **The shim answers 200 even when zero targets received it.**
4. **Render it:** the title/message templates in the Home Assistant
   notification automation. An unhandled `book_*` event must not fall through
   to "audiobook ready": that is how books flagged as the wrong content get
   announced as good news. Make the fallback name the event and print its
   message, so a missed step reads as odd.

Step 3 is caught by the poller: it treats a relay to `targets: 0` as **not
delivered**, keeps its dedupe mark unburned and fails the unit, so the alert
retries every 5 min until something subscribes.

**Stuck requests:** the same timer reads `books.json` (read-only; the MCP stays
its only writer). A request older than 24 h pushes `book_stuck` once, unless
Chaptarr's `bookFileCount` says it was imported, or it already failed. The
message is the ledger's `reason` (e.g. VIP only) or "not imported after 24 h".
Handled requests are `stuck_done` in the state file, keyed
`<book id>@<requested_at>`, so a re-request can alert again.

**Suspect imports:** the same read pushes `book_suspect` once for every entry
the content check left at `content: "suspect"`. The message is the ledger's
`content_detail` (the tags found). Keyed `<book id>@<imported_at>` in
`suspect_done`, so a re-import alerts again while an unhandled suspect stays
quiet. `unverified` does **not** push: untagged files are common on MAM and say
nothing about content; read it from `landible_book_status`.

### Failed-unit alerts

`landible-unit-health.timer` (every 15 min, `mcp/systemd/unit_health.py`)
sweeps `systemctl --failed` and pushes `unit_failed` for each newly failed
unit, with the last few journal lines as the message.

Several timers deliberately fail their unit so a problem shows in `systemctl
--failed`; this makes sure someone actually hears about it. **A sweep, not
`OnFailure=` per unit:** it catches everything in the container, including
units nobody thought to annotate and units added later, at the cost of up to
one interval of delay.

Deduped by `failed_seen` in `unit-health-state.json`: a unit that stays failed
alerts once; one that recovers drops out so its next failure alerts again.

```bash
journalctl -u landible-unit-health -n 20
```

**Known and accepted:** alerts go through the shim, so a shim outage can't
report itself, and this unit failing reports nothing. It is a backstop for the
other units, not for itself.

### MAM health alerts

`landible-mam-health.timer` (every 5 min, `mcp/systemd/mam_health.py`) reads
qbittorrent-mam (login + `torrents/info`, `transfer/info`, `app/preferences`;
read-only, never pauses or deletes). Each condition pushes once, then re-arms
when it clears:

| Condition | Message |
|---|---|
| unsatisfied >= `MAM_UNSATISFIED_CAP` | "Guard full: N/15 unsatisfied; requests refused" |
| a torrent paused / stopped | "<name> paused (seeded Xh; <72 h = hit & run risk)" |
| a torrent `error` / `missingFiles` | same shape |
| `save_path` no longer `/music/books/mam` | same shape |
| a torrent gone from qBt | same shape (fires once) |
| login/HTTP fails, or `connection_status` disconnected | "qbittorrent-mam down / listener not bound" |
| a share ratio or seeding-time limit enabled | "Share limit enabled: qBt would stop seeding" |

A torrent already seeded 72 h says "(seeded, lower priority)" instead.
`firewalled` isn't an alert (it flaps while idle). The snapshot and raised
alerts live in `mcp/systemd/mam-torrents.json`; the first run seeds it and
alerts only on current conditions. Secrets: `QBT_MAM_*` from `mcp/.env`.

```bash
journalctl -u landible-mam-health -n 20   # first run: "seeding the snapshot"
```

### MAM stats

`landible-mam-stats.timer` (hourly, `mcp/systemd/mam_stats.py`) calls MAM's
approved **Load User Data** endpoint (`/jsonLoad.php?snatch_summary`) from the
home IP and writes `mcp/systemd/mam-stats.json` from an allow-list: class,
points, ratio, up/down bytes, wedges, VIP expiry, connectable, and the
snatch-summary counts (`unsat` + limit, hit & runs, seeding). No username, uid,
IP or cookie. `landible_mam_stats` (MCP) reads that file and adds Power User /
VIP progress; the book guard uses MAM's `unsat` count when it's higher than
ours and less than 2 h old.

**The cookie** is the only MAM credential outside Prowlarr. It's a
**separate** session, so the poller and Prowlarr never overwrite each other's
rotated cookie. It lives only in `/etc/landible/mam_id` (root, 0600, outside the
repo and any backup sync). MAM rotates `mam_id`: a new value in `Set-Cookie` is
written back to that file. **Never print, log or cat it**; set it up by hand,
and no agent or script should read it back:

1. MAM → Preferences → Security → create a session named `landible-stats`,
   **IP-locked** (ASN-locked if your home IP isn't static), from home. No
   seedbox/dynamic-IP permission.
2. In the container:
   ```bash
   install -d -m 700 /etc/landible
   ( umask 077; read -rs -p 'mam_id: ' v; echo; printf '%s\n' "$v" > /etc/landible/mam_id )
   cp /opt/landible/mcp/systemd/landible-mam-stats.{service,timer} /etc/systemd/system/
   systemctl daemon-reload && systemctl enable --now landible-mam-stats.timer
   systemctl start landible-mam-stats && journalctl -u landible-mam-stats -n 5
   ```

Alerts (`mam_health`, once each, re-armed when cleared):

| Condition | Message |
|---|---|
| MAM answers with HTML / 401 / 403 | "MAM session rejected …: create a new IP-locked session …" |
| `connectable` is `no` / `offline` (healthy value: `yes`) | "MAM sees qbittorrent-mam as not connectable …" |
| hit & runs > 0 | "MAM counts N hit & run(s) …" |

A network error is only recorded (`error` in the file; `stale: true` in the
tool after 2 h). One request per run, never a retry loop.

### Weekly digest

`landible-book-digest.timer` (Sunday 09:00, `mcp/systemd/digest.py`) pushes one
`book_digest`, shaped like:

```
Books: N added this week (newest: <title>, <date>)
Libation: N downloaded, N errors, N pending
MAM: N seeding, guard N/15 (MAM: N/20 unsatisfied)
Account: <class>, ratio R, X GiB up, N pts (N short of VIP); connectable yes, 0 H&R
```

A source that's down reads "unavailable"; the rest still go out. Libation is
read from its SQLite DB, read-only. MAM stats older than 2 h are marked
`[stale since …]`. A failed push retries every 30 min (`Restart=on-failure`).

```bash
systemctl start landible-book-digest   # test trigger: one push now
```

### Monthly MAM login reminder

MAM disables inactive accounts, and the login must be from home (never
cellular, public Wi-Fi or work). A Home Assistant automation, not landible
code, pushes on the 1st of each month: "Log into MAM from home". Connectable
status is already in the digest and alerted by `landible-mam-stats`, so the
reminder is only about logging in.

## Library backup

A nightly S3 sync (script in `scripts/`, run from host cron) copies
`${DATA_ROOT}/books/audiobooks` and `${DATA_ROOT}/books/ebooks`. It **excludes
`books/mam`**: that dir is hardlinks of the library, so syncing it would upload
every book twice (and it must never be a restore target that could disturb
seeding). Staging and cover caches are regenerable and not synced. The script
on the host is a copy; reinstall it after changing it in the repo.

## iPhone app: SoundLeaf

There's no official Audiobookshelf app in the App Store (TestFlight beta only,
usually full). SoundLeaf is free for everything needed (offline downloads,
CarPlay); the optional one-time purchase is only themes.

1. App Store → install **SoundLeaf**.
2. Server `https://<books-host>`. Off home Wi-Fi, turn the home VPN on first.
3. Sign in with your own ABS user.
4. Open a book → tap the download icon.

Verified: login over the VPN, download, playback of the downloaded book. Not
yet verified (SoundLeaf advertises them): airplane-mode playback and chapter
skip, CarPlay, progress sync back to the server.

Fallbacks if SoundLeaf goes away: AudioBooth, Fable Frog (both free with in-app
purchases).

# landible-mcp

A [FastMCP](https://gofastmcp.com) server that lets an MCP client (e.g. Claude)
find, request and follow audiobooks and ebooks, check the MAM account, and send
ebooks to a Kindle. Split out of the landora music stack.

It talks to Chaptarr (grabber), qbittorrent-mam (read-only guard on
unsatisfied torrents) and Audiobookshelf (library + send-to-Kindle). It never
calls MAM or Prowlarr directly, and never removes or pauses a MAM torrent.

## Tools

| Tool | What it does |
|---|---|
| `landible_book_search` | Look a title up in Chaptarr's metadata; returns `foreign_book_id`. |
| `landible_book_request` | Grab a book from MAM via Chaptarr (write; guarded by the unsatisfied cap). |
| `landible_book_status` | Every requested book's state + summary (write-gated: it can act). |
| `landible_book_cancel` | Stop tracking a request that will never finish (the torrent keeps seeding). |
| `landible_book_kindle` | Email a book already in the library to a Kindle via Audiobookshelf. |
| `landible_audiobooks` | Library totals + recently added. |
| `landible_mam_stats` | MAM ratio/points/class progress, read from the stats poller's file. |
| `landible_webhook_list` / `_set` / `_delete` / `_test` | Manage outbound event webhooks via the deploy shim. |

Write tools require the `X-MCP-Secret` header when `MCP_SHARED_SECRET` is set.
`GET /health` is a no-backend liveness probe.

## Run and test

```bash
cp .env.example .env   # fill in keys
uv sync
uv run python -m landible_mcp      # streamable HTTP on MCP_HOST:MCP_PORT (default 0.0.0.0:8087), path /mcp
uv run pytest -q
uv run ruff check src tests
```

`systemd/landible-mcp.service` runs it from a checkout at `/opt/landible/mcp`.
Runtime state (`systemd/books.json`, `systemd/mam-stats.json`) lives next to it
and is gitignored.

## Gateway federation

An MCP gateway mounts this server with FastMCP's `create_proxy` pointed at
`http://<host>:8087/mcp`, forwarding `X-MCP-Secret`. Mount it **without a
namespace**: the tools are already prefixed `landible_`.

## Configuration

See [`.env.example`](.env.example). Nothing has a personal default:
`ABS_KINDLE_DEVICE` blank means `landible_book_kindle` needs a `device`, and
`MAM_JOINED` blank means the Power User eligibility date is reported as unknown.

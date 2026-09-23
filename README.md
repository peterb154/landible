# landible

Self-hosted audiobooks and ebooks: an Audible library that downloads itself, a
request-only grabber for everything else, and a Claude (MCP) front door to ask
for books by name.

Runs as one Docker Compose project inside a Proxmox LXC. It was split out of a
music stack, which is why some container-side paths still read `/music/books/…`
(the apps' databases store them; see the header of `compose/docker-compose.yml`).

## What's in it

| Service | Job |
|---|---|
| [Audiobookshelf](https://www.audiobookshelf.org/) | Library + player (web, iPhone via SoundLeaf, Send to Kindle) |
| [Libation](https://github.com/rmcrackan/Libation) | Pulls the owner's Audible purchases into the library |
| [Chaptarr](https://github.com/Chaptarr/chaptarr) | Finds and grabs requested books |
| [Prowlarr](https://prowlarr.com/) + FlareSolverr | Indexer (MyAnonamouse) for Chaptarr |
| qbittorrent-mam | Seeds forever, on the home IP — never a VPN |
| autoheal | Restarts containers Docker marks unhealthy |
| `mcp/` | FastMCP server: `landible_book_search/request/status/cancel/kindle`, … |
| `deploy/` | Deploy shim: auto-deploy on merge, health, webhook fan-out for book events |

## Quick start

```bash
git clone https://github.com/peterb154/landible /opt/landible
cd /opt/landible
cp .env.example .env    # fill in; compose/.env is a symlink to it
docker compose -f compose/docker-compose.yml config   # sanity check
```

Then follow [docs/runbook.md](docs/runbook.md) — the data dirs must exist and be
owned by `PUID:PGID` before the first `up`, and several services have one-time
setup.

## Rules this stack keeps

1. **Request-only.** Nothing is monitored; a book is fetched because someone asked.
2. **Never stop seeding.** No share limits, no queueing, no torrent deletion.
3. **Home IP only** for the tracker client.
4. **Hardlink, never move** from the seeding dir into the library — one filesystem.

## Development

```bash
cd mcp && uv sync && uv run pytest
```

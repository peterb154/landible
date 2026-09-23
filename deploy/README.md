# landible-deploy

Small FastAPI service that runs natively on the host (not in Docker) at
`/opt/landible/deploy`, port 8090. It redeploys the stack on every push to
`main`, reports stack health, and relays events from the pollers in
`mcp/systemd/` to webhook targets (Home Assistant or anything that takes a POST).

## Endpoints

| Method | Path | Auth | What |
|---|---|---|---|
| POST | `/api/deploy` | `Bearer $DEPLOY_TOKEN` | converge to `origin/main` and roll the stack (below). 500 on the first failing step, 409 if a deploy is already running |
| GET | `/api/health` | none | `{status, containers[], commit, timestamp}`. `degraded` if any expected container is missing, not running, or `unhealthy`. Always 200, so a watchdog can tell "stack degraded" from "shim down" |
| GET | `/api/webhooks` | `Bearer $DEPLOY_TOKEN` | list targets (URLs redacted) |
| PUT | `/api/webhooks/{name}` | `Bearer $DEPLOY_TOKEN` | upsert `{url, events[], headers?, enabled?}` |
| DELETE | `/api/webhooks/{name}` | `Bearer $DEPLOY_TOKEN` | remove a target |
| POST | `/api/webhooks/{name}/test` | `Bearer $DEPLOY_TOKEN` | send `landible.test` to one target |
| POST | `/api/events/books` | `Authorization: $WEBHOOK_INBOUND_SECRET` | relay a poller event to subscribed targets |

Targets live in `deploy/state/webhooks.json` (gitignored, so `reset --hard`
never touches it).

## Env (`/opt/landible/deploy/.env`, chmod 600)

See [`.env.example`](.env.example).

- `DEPLOY_TOKEN` — bearer token for deploy + webhook CRUD.
- `WEBHOOK_INBOUND_SECRET` — the pollers send it on `/api/events/books`.
- `LANDIBLE_PROJECT_DIR` — optional, default `/opt/landible`.

## How a deploy happens

```
merge to main
  -> landible-autodeploy.timer (every 2 min): git fetch; if origin/main moved,
     POST /api/deploy with Authorization: Bearer DEPLOY_TOKEN
       1. git fetch origin --prune
       2. git reset --hard origin/main          (the box is a pure mirror)
       3. docker compose pull
       4. docker compose up -d --remove-orphans
       5. if mcp/src, mcp/pyproject.toml or mcp/uv.lock changed:
            uv sync --frozen + systemctl restart landible-mcp
       6. if deploy/main.py, pyproject.toml or uv.lock changed:
            deferred systemctl restart landible-deploy (~2 s)
```

Polling instead of a GitHub webhook: the repo is public, so the box can fetch
anonymously and nothing needs a relay, a signature check or a credential. The
price is up to two minutes of lag. Anything else that can send the POST (a
webhook relay, a human with curl) still works.

The deploy never touches systemd unit files. After a `.service`/`.timer`
change, run `scripts/install-units.sh` on the host.

## Events

| Poller sends (`event`) | Relayed as |
|---|---|
| `book_ready` | `landible.book_ready` |
| `book_failed` | `landible.book_failed` |
| `book_stuck` | `landible.book_stuck` |
| `book_suspect` | `landible.book_suspect` |
| `mam_health` | `landible.mam_health` |
| `book_digest` | `landible.book_digest` |
| `unit_failed` | `landible.unit_failed` |

The relayed body is `{event, data: {title, author, source, message}, timestamp}`.
Unknown events get 200 `ignored`. The relay returns 502 only when every
subscribed target failed, so the poller retries without double-pushing.

### Adding a new event type (the trap)

A new event reaches nobody until a target subscribes to it, and the shim still
answers **200 with `targets: 0`**. Do all of these, in this order:

1. The poller emits the short name (`{"event": "my_event", ...}`).
2. Add `"my_event": "landible.my_event"` to `_BOOK_EVENT_MAP` in `main.py`.
3. Add `landible.my_event` to each target's `events` list (PUT
   `/api/webhooks/{name}`) **before the poller's first run**.
4. Teach the consumer (e.g. the Home Assistant automation template) the new
   event.

The pollers treat `targets: 0` as "not delivered": they keep their dedupe mark
and fail their unit, so a missed step 3 shows up in `systemctl --failed`
instead of silently dropping the alert.

## Install

```bash
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
cd /opt/landible/deploy && /root/.local/bin/uv sync --frozen
cp .env.example .env && chmod 600 .env   # then fill it in
/opt/landible/scripts/install-units.sh
```

## Tests

```bash
uv sync && uv run pytest -q
```

subprocess and the network are mocked; no docker needed.

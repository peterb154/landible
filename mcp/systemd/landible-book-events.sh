#!/usr/bin/env bash
# Audiobook push events. Thin wrapper: load the env, then exec
# book_events.py (logic + unit tests live there). Secrets go in the child's
# environment, never argv. Run by the timer (every 5 min).
set -uo pipefail

ENV=/opt/landible/mcp/.env
ABS_URL=$(sed -n 's/^ABS_URL=//p' "$ENV"); export ABS_URL=${ABS_URL:-http://localhost:13378}
export ABS_API_KEY=$(sed -n 's/^ABS_API_KEY=//p' "$ENV")
CHAPTARR_URL=$(sed -n 's/^CHAPTARR_URL=//p' "$ENV"); export CHAPTARR_URL=${CHAPTARR_URL:-http://localhost:8789}
export CHAPTARR_API_KEY=$(sed -n 's/^CHAPTARR_API_KEY=//p' "$ENV")
DEPLOY_HEALTH_URL=$(sed -n 's/^DEPLOY_HEALTH_URL=//p' "$ENV"); export DEPLOY_HEALTH_URL=${DEPLOY_HEALTH_URL:-http://localhost:8090}
# The shim's inbound secret (deploy/.env), sent as the Authorization header.
export WEBHOOK_INBOUND_SECRET=$(sed -n 's/^WEBHOOK_INBOUND_SECRET=//p' /opt/landible/deploy/.env)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/book_events.py"

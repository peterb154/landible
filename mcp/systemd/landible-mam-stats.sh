#!/usr/bin/env bash
# MAM account stats. Thin wrapper: load the env, then exec mam_stats.py
# (logic + unit tests live there). The mam_id cookie is NOT loaded here: the
# script reads it from MAM_COOKIE_FILE itself, so it's never in the environment
# or argv. Run by the timer (hourly).
set -uo pipefail

ENV=/opt/landible/mcp/.env
DEPLOY_HEALTH_URL=$(sed -n 's/^DEPLOY_HEALTH_URL=//p' "$ENV"); export DEPLOY_HEALTH_URL=${DEPLOY_HEALTH_URL:-http://localhost:8090}
# The shim's inbound secret (deploy/.env), sent as the Authorization header.
export WEBHOOK_INBOUND_SECRET=$(sed -n 's/^WEBHOOK_INBOUND_SECRET=//p' /opt/landible/deploy/.env)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/mam_stats.py"

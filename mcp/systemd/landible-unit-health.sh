#!/usr/bin/env bash
# Failed systemd unit alerts. Thin wrapper: load the env, then exec
# unit_health.py (logic + unit tests live there). Secrets go in the child's
# environment, never argv. Run by the timer (every 15 min).
set -uo pipefail

DEPLOY_HEALTH_URL=$(sed -n 's/^DEPLOY_HEALTH_URL=//p' /opt/landible/mcp/.env)
export DEPLOY_HEALTH_URL=${DEPLOY_HEALTH_URL:-http://localhost:8090}
# The shim's inbound secret (deploy/.env), sent as the Authorization header.
export WEBHOOK_INBOUND_SECRET=$(sed -n 's/^WEBHOOK_INBOUND_SECRET=//p' /opt/landible/deploy/.env)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/unit_health.py"

#!/usr/bin/env bash
# MAM account health alerts. Thin wrapper: load the env, then exec
# mam_health.py (logic + unit tests live there). Secrets go in the child's
# environment, never argv. Run by the timer (every 5 min).
set -uo pipefail

ENV=/opt/landible/mcp/.env
QBT_MAM_URL=$(sed -n 's/^QBT_MAM_URL=//p' "$ENV"); export QBT_MAM_URL=${QBT_MAM_URL:-http://localhost:8081}
QBT_MAM_USER=$(sed -n 's/^QBT_MAM_USER=//p' "$ENV"); export QBT_MAM_USER=${QBT_MAM_USER:-admin}
export QBT_MAM_PASSWORD=$(sed -n 's/^QBT_MAM_PASSWORD=//p' "$ENV")
MAM_UNSATISFIED_CAP=$(sed -n 's/^MAM_UNSATISFIED_CAP=//p' "$ENV"); export MAM_UNSATISFIED_CAP=${MAM_UNSATISFIED_CAP:-15}
DEPLOY_HEALTH_URL=$(sed -n 's/^DEPLOY_HEALTH_URL=//p' "$ENV"); export DEPLOY_HEALTH_URL=${DEPLOY_HEALTH_URL:-http://localhost:8090}
# The shim's inbound secret (deploy/.env), sent as the Authorization header.
export WEBHOOK_INBOUND_SECRET=$(sed -n 's/^WEBHOOK_INBOUND_SECRET=//p' /opt/landible/deploy/.env)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/mam_health.py"

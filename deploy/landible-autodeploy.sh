#!/usr/bin/env bash
# Poll-based auto-deploy: if origin/main moved, ask the local shim to deploy.
# The repo is public, so this needs no GitHub webhook, relay or credential —
# the cost is up to one timer interval of lag. The shim does the actual work
# (and its lock turns an overlapping run into a harmless 409).
set -euo pipefail

REPO=/opt/landible
TOKEN=$(sed -n 's/^DEPLOY_TOKEN=//p' "$REPO/deploy/.env")

git -C "$REPO" fetch --quiet origin main
head=$(git -C "$REPO" rev-parse HEAD)
want=$(git -C "$REPO" rev-parse origin/main)
[ "$head" = "$want" ] && exit 0

echo "deploying ${head:0:7} -> ${want:0:7}"
# -f: a non-2xx (failed step, 409) fails this unit, which unit_health reports.
curl -fsS -X POST -H "Authorization: Bearer $TOKEN" --max-time 900 \
  http://localhost:8090/api/deploy
echo

#!/usr/bin/env bash
# Install or refresh landible's systemd units. Safe to re-run.
#
# Copies mcp/systemd/*.service, mcp/systemd/*.timer and
# deploy/landible-deploy.service into /etc/systemd/system, reloads systemd, then
# enables + starts landible-mcp, landible-deploy and every landible-*.timer.
# The deploy shim never touches units, so run this after a unit file changes.
# A changed .service for an already-running daemon applies on its next restart.
#
#   scripts/install-units.sh            # do it (as root)
#   scripts/install-units.sh --dry-run  # print what it would do
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST=/etc/systemd/system
DRY_RUN=0

case "${1:-}" in
  --dry-run) DRY_RUN=1 ;;
  "") ;;
  *) echo "usage: $0 [--dry-run]" >&2; exit 2 ;;
esac

run() {
  if [ "$DRY_RUN" = 1 ]; then
    echo "+ $*"
  else
    "$@"
  fi
}

if [ "$DRY_RUN" = 0 ] && [ "$(id -u)" != 0 ]; then
  echo "run as root (or use --dry-run)" >&2
  exit 1
fi

shopt -s nullglob
units=("$REPO"/mcp/systemd/*.service "$REPO"/mcp/systemd/*.timer "$REPO"/deploy/landible-deploy.service)
timers=()

for src in "${units[@]}"; do
  name="$(basename "$src")"
  # Skip unchanged files so a re-run is quiet.
  if [ -f "$DEST/$name" ] && cmp -s "$src" "$DEST/$name"; then
    echo "unchanged: $name"
  else
    run install -m 0644 "$src" "$DEST/$name"
  fi
  case "$name" in
    landible-*.timer) timers+=("$name") ;;
  esac
done

run systemctl daemon-reload

# A timer's .service is started by the timer, not enabled on its own.
# enable --now is a no-op for a unit that's already enabled and running.
for unit in landible-mcp.service landible-deploy.service "${timers[@]}"; do
  if [ ! -f "$DEST/$unit" ] && [ "$DRY_RUN" = 0 ]; then
    echo "skip: $unit is not installed" >&2
    continue
  fi
  run systemctl enable --now "$unit"
done

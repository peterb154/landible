#!/usr/bin/env bash
# =============================================================================
# landible-books-s3-sync.sh — nightly backup of the book libraries to S3.
# =============================================================================
# Runs on the Proxmox HOST (not inside the LXC), from root's crontab.
#   - COPY mode (no --delete): S3 keeps files even if removed locally.
#   - Syncs books/audiobooks + books/ebooks. Excludes books/mam (hardlinks of
#     the library — syncing it would upload every book twice), the cover cache
#     and Libation staging (both regenerable).
#   - On success, publishes a CloudWatch LastSuccess metric so a dead-man-switch
#     alarm can page when the backup silently stops.
#
# Site values come from an env file (default /root/scripts/landible-books-s3-sync.env):
#   SRC=/mnt/media/landible      host path of the LXC's data bind mount
#   MOUNT=/mnt/media             mountpoint SRC lives under (guard)
#   BUCKET=<bucket>  PREFIX=landible-books  REGION=us-east-1
#   NAMESPACE=<metric namespace>  JOB=landible-books-s3-sync
# Creds: the default AWS chain (root's ~/.aws under cron). The preflight below
# fails loud if it doesn't resolve, so a bad cron env can't skip the backup.
# =============================================================================
set -euo pipefail

ENV_FILE="${LANDIBLE_BACKUP_ENV:-/root/scripts/landible-books-s3-sync.env}"
# shellcheck source=/dev/null
. "$ENV_FILE"
: "${SRC:?} ${MOUNT:?} ${BUCKET:?} ${PREFIX:?} ${REGION:?} ${NAMESPACE:?} ${JOB:?}"

# Single-runner lock: a first sync can run long; don't start a second one.
exec 9>"/var/lock/${JOB}.lock"
flock -n 9 || { echo "[$(date -Is)] another ${JOB} run holds the lock — skipping"; exit 0; }

# Guards: never publish a green metric for a backup that didn't happen. If the
# pool isn't mounted, SRC is an empty stub; COPY-mode sync would do nothing,
# exit 0, and blind the dead-man-switch. Refuse loudly instead.
if ! mountpoint -q "${MOUNT}"; then
  echo "[$(date -Is)] ERROR: ${MOUNT} not mounted — aborting, no metric published" >&2
  exit 1
fi
if [ -z "$(ls -A "${SRC}/books/audiobooks" 2>/dev/null)" ]; then
  echo "[$(date -Is)] ERROR: ${SRC}/books/audiobooks empty — aborting to protect the dead-man-switch" >&2
  exit 1
fi

aws sts get-caller-identity --region "${REGION}" >/dev/null

echo "[$(date -Is)] ${JOB} starting: ${SRC}/books -> s3://${BUCKET}/${PREFIX}/books/"
aws s3 sync "${SRC}/books" "s3://${BUCKET}/${PREFIX}/books/" \
  --exclude "mam/*" \
  --region "${REGION}"

aws cloudwatch put-metric-data \
  --namespace "${NAMESPACE}" \
  --metric-name LastSuccess \
  --dimensions "Job=${JOB}" \
  --value 1 \
  --region "${REGION}"

echo "[$(date -Is)] ${JOB} complete; published ${NAMESPACE}/LastSuccess Job=${JOB}"

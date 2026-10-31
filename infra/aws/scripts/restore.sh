#!/usr/bin/env bash
# Restore the database from S3. Usage: sudo shop-support-restore [--yes] [db/app-YYYYMMDDTHHMMSSZ.dump]
# Without a key the newest dump is used. The application is stopped while the data is replaced.
#
# GDPR note: a restore brings back conversations that may have been deleted after the backup was taken.
# Re-run the erasure requests received since the backup date (DELETE /api/admin/gdpr/customer?email=...) and the retention purge.
set -euo pipefail

# shellcheck disable=SC1091
source /etc/shop-support/host.env
cd /opt/shop-support
COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.aws.yml)

yes=0
if [ "${1:-}" = "--yes" ]; then yes=1; shift; fi
key="${1:-$(aws s3api list-objects-v2 --region "$AWS_REGION" --bucket "$BACKUP_BUCKET" --prefix db/ \
  --query 'sort_by(Contents,&LastModified)[-1].Key' --output text)}"
[ -n "$key" ] && [ "$key" != "None" ] || { echo "no backups found in s3://$BACKUP_BUCKET/db/"; exit 1; }

echo "Restoring s3://$BACKUP_BUCKET/$key into the running database. Current data will be replaced."
if [ "$yes" -ne 1 ]; then
  read -r -p "Type YES to continue: " answer
  [ "$answer" = "YES" ] || { echo "aborted"; exit 1; }
fi

"${COMPOSE[@]}" stop api worker beat
# shellcheck disable=SC2016 # $POSTGRES_* must expand inside the container, not here
aws s3 cp "s3://$BACKUP_BUCKET/$key" - --region "$AWS_REGION" \
  | "${COMPOSE[@]}" exec -T db sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner'
"${COMPOSE[@]}" up -d
echo "restore finished; check: ${COMPOSE[*]} ps"

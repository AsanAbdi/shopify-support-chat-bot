#!/usr/bin/env bash
# Daily backup to S3: PostgreSQL dump (custom format) and the uploaded documents. Triggered by shop-support-backup.timer.
# Retention is enforced by the S3 lifecycle rule (Terraform: backup_retention_days). On failure an alert goes to SNS.
set -euo pipefail

# shellcheck disable=SC1091
source /etc/shop-support/host.env
cd /opt/shop-support
COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.aws.yml)
ts="$(date -u +%Y%m%dT%H%M%SZ)"

alert() {
  [ -n "${ALERTS_TOPIC:-}" ] && aws sns publish --region "$AWS_REGION" --topic-arn "$ALERTS_TOPIC" \
    --subject "shop-support: backup FAILED" --message "$1" >/dev/null || true
}
trap 'alert "Backup $ts failed on $(hostname). See: journalctl -u shop-support-backup"' ERR

echo "dumping database"
# shellcheck disable=SC2016 # $POSTGRES_* must expand inside the container, not here
"${COMPOSE[@]}" exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -Fc "$POSTGRES_DB"' \
  | aws s3 cp - "s3://$BACKUP_BUCKET/db/app-$ts.dump" --region "$AWS_REGION" --sse AES256

echo "archiving uploads"
tar -C /srv/data -czf - uploads | aws s3 cp - "s3://$BACKUP_BUCKET/uploads/uploads-$ts.tar.gz" --region "$AWS_REGION" --sse AES256

# a dump of a few bytes means that something went wrong silently
size="$(aws s3api head-object --region "$AWS_REGION" --bucket "$BACKUP_BUCKET" --key "db/app-$ts.dump" --query ContentLength --output text)"
[ "$size" -gt 10000 ] || { echo "dump too small: $size bytes"; false; }
echo "backup $ts ok ($size bytes)"

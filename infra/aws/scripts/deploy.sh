#!/usr/bin/env bash
# Deploy (or update) the application on the EC2 host: code from git, configuration and secrets from SSM Parameter Store,
# then `docker compose up`. Run as root: `sudo shop-support-deploy` (over SSM Session Manager, or `aws ssm send-command`).
set -euo pipefail

# shellcheck disable=SC1091
source /etc/shop-support/host.env
APP_DIR=/opt/shop-support
COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.aws.yml)
log() { echo "[$(date -u +%FT%TZ)] $*"; }

ssm_get() { aws ssm get-parameter --region "$AWS_REGION" --name "$1" --with-decryption --query Parameter.Value --output text 2>/dev/null || true; }

# ---- 1. code
if [ -n "${REPO_URL:-}" ]; then
  token="$(ssm_get "$SSM_PREFIX/_GIT_TOKEN")" # optional, only for private repositories
  url="$REPO_URL"
  [ -n "$token" ] && url="${REPO_URL/https:\/\//https://oauth2:${token}@}"
  if [ ! -d "$APP_DIR/.git" ]; then
    mkdir -p "$APP_DIR"
    git clone --quiet "$url" "$APP_DIR"
  fi
  git -C "$APP_DIR" fetch --quiet --prune "$url" "$REPO_REF"
  git -C "$APP_DIR" reset --quiet --hard FETCH_HEAD
  git -C "$APP_DIR" remote set-url origin "$REPO_URL" # never leave the token on disk
  log "code at $(git -C "$APP_DIR" rev-parse --short HEAD)"
fi
[ -f "$APP_DIR/docker-compose.yml" ] || { log "ERROR: $APP_DIR has no docker-compose.yml. Set REPO_URL or copy the repository there."; exit 1; }
cd "$APP_DIR"

# ---- 2. configuration: every SSM parameter under the prefix becomes an environment variable (names starting with _ are skipped)
umask 077
aws ssm get-parameters-by-path --region "$AWS_REGION" --path "$SSM_PREFIX" --with-decryption --recursive \
  --query 'Parameters[].[Name,Value]' --output json \
  | jq -r --arg p "$SSM_PREFIX/" '.[] | (.[0] | ltrimstr($p)) as $k | select($k | startswith("_") | not) | "\($k)=\(.[1])"' > .env.new

add_default() { grep -q "^$1=" .env.new || echo "$1=$2" >> .env.new; }
add_default APP_ENV prod
add_default AWS_REGION "$AWS_REGION"
add_default AWS_LOG_GROUP "$LOG_GROUP"
[ -n "${DOMAIN:-}" ] && add_default DOMAIN "$DOMAIN"
[ -n "${DOMAIN:-}" ] && add_default PUBLIC_BASE_URL "https://$DOMAIN"

missing=()
for key in JWT_SECRET ADMIN_PASSWORD POSTGRES_PASSWORD ACME_EMAIL DOMAIN; do
  grep -q "^$key=." .env.new || missing+=("$key")
done
if [ ${#missing[@]} -gt 0 ]; then
  rm -f .env.new
  log "ERROR: missing in SSM under $SSM_PREFIX: ${missing[*]}. Run infra/aws/scripts/put-secrets.sh first."
  exit 1
fi
mv .env.new .env
chmod 600 .env

# ---- 3. build and start
log "docker compose up (first build downloads the ML models, about 10 minutes)"
"${COMPOSE[@]}" up -d --build --remove-orphans

log "waiting for the API to become healthy"
cid="$("${COMPOSE[@]}" ps -q api)"
for _ in $(seq 1 120); do
  status="$(docker inspect -f '{{.State.Health.Status}}' "$cid" 2>/dev/null || echo starting)"
  [ "$status" = "healthy" ] && { log "API is healthy"; docker image prune -f >/dev/null; exit 0; }
  sleep 5
done
log "ERROR: API did not become healthy. Look at: cd $APP_DIR && ${COMPOSE[*]} logs --tail=100 api"
exit 1

#!/usr/bin/env bash
# Upload a production env file to SSM Parameter Store as SecureStrings (run on YOUR machine, not on the server).
#   ./put-secrets.sh .env.prod [--region eu-central-1] [--prefix /shop-support/prod] [--dry-run]
# Every KEY=VALUE line becomes <prefix>/KEY. Lines starting with # and empty lines are skipped. Re-running overwrites.
# Special parameter: <prefix>/_GIT_TOKEN (a read-only deploy token, only for private repositories) is not passed to the app.
set -euo pipefail

file="${1:?usage: put-secrets.sh <env-file> [--region R] [--prefix P] [--dry-run]}"
shift
region="eu-central-1"
prefix="/shop-support/prod"
dry=0
while [ $# -gt 0 ]; do
  case "$1" in
    --region) region="$2"; shift 2 ;;
    --prefix) prefix="$2"; shift 2 ;;
    --dry-run) dry=1; shift ;;
    *) echo "unknown option $1"; exit 1 ;;
  esac
done

count=0
while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in ''|\#*) continue ;; esac
  key="${line%%=*}"
  value="${line#*=}"
  value="${value%%  #*}" # strip trailing "  # comment"
  [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || { echo "skipping invalid line: ${key:0:20}"; continue; }
  [ -n "$value" ] || { echo "skipping empty value: $key"; continue; }
  if [ "$dry" -eq 1 ]; then
    echo "would put $prefix/$key (${#value} chars)"
  else
    aws ssm put-parameter --region "$region" --name "$prefix/$key" --type SecureString --overwrite --value "$value" >/dev/null
    echo "put $prefix/$key"
  fi
  count=$((count + 1))
done < "$file"
echo "$count parameters processed under $prefix"

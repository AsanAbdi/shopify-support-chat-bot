#!/usr/bin/env bash
# First-boot setup of the EC2 host (Ubuntu 24.04). Idempotent: safe to run again.
# Installs Docker + AWS CLI, mounts the data disk, starts the CloudWatch agent, installs the backup/prune timers,
# and (when REPO_URL is set) runs the first deployment.
set -euo pipefail

# shellcheck disable=SC1091
source /etc/shop-support/host.env
export DEBIAN_FRONTEND=noninteractive
log() { echo "[$(date -u +%FT%TZ)] $*"; }

log "installing packages"
apt-get update -y
apt-get install -y ca-certificates curl gnupg git unzip jq

if ! command -v docker >/dev/null 2>&1; then
  log "installing Docker"
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  # shellcheck disable=SC1091
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -y
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
mkdir -p /etc/docker
cat > /etc/docker/daemon.json <<'JSON'
{ "log-driver": "local", "log-opts": { "max-size": "20m", "max-file": "5" }, "live-restore": true }
JSON
systemctl enable --now docker
systemctl restart docker

if ! command -v aws >/dev/null 2>&1; then
  log "installing AWS CLI v2"
  curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscliv2.zip
  unzip -q -o /tmp/awscliv2.zip -d /tmp
  /tmp/aws/install --update
fi

log "preparing the data disk"
vol="${DATA_VOLUME_ID//-/}"
dev="/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_${vol}"
for _ in $(seq 1 60); do [ -e "$dev" ] && break; sleep 5; done
[ -e "$dev" ] || { log "ERROR: data volume $DATA_VOLUME_ID is not attached"; exit 1; }
if ! blkid "$dev" >/dev/null 2>&1; then
  mkfs.ext4 -L shopdata "$dev"
fi
mkdir -p /srv/data
grep -q 'LABEL=shopdata' /etc/fstab || echo 'LABEL=shopdata /srv/data ext4 defaults,nofail,discard 0 2' >> /etc/fstab
mountpoint -q /srv/data || mount /srv/data
mkdir -p /srv/data/postgres /srv/data/redis /srv/data/uploads /srv/data/caddy_data /srv/data/caddy_config
chown 10001:10001 /srv/data/uploads # uid of the `app` user inside the backend image

log "configuring the CloudWatch agent (memory and disk metrics)"
if [ ! -x /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl ]; then
  curl -fsSL https://amazoncloudwatch-agent.s3.amazonaws.com/ubuntu/amd64/latest/amazon-cloudwatch-agent.deb -o /tmp/cwa.deb
  dpkg -i /tmp/cwa.deb
fi
cat > /opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json <<'JSON'
{
  "agent": { "metrics_collection_interval": 60, "run_as_user": "root" },
  "metrics": {
    "namespace": "CWAgent",
    "append_dimensions": { "InstanceId": "${aws:InstanceId}" },
    "aggregation_dimensions": [["InstanceId"], ["InstanceId", "path"]],
    "metrics_collected": {
      "mem": { "measurement": ["mem_used_percent"] },
      "disk": {
        "measurement": ["used_percent"],
        "resources": ["/", "/srv/data"],
        "ignore_file_system_types": ["sysfs", "devtmpfs", "tmpfs", "overlay", "squashfs"]
      }
    }
  }
}
JSON
/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl -a fetch-config -m ec2 \
  -c file:/opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json -s

log "installing timers (daily backup 02:30 UTC, weekly image prune)"
cat > /etc/systemd/system/shop-support-backup.service <<'UNIT'
[Unit]
Description=Back up the shop-support database and uploads to S3
After=docker.service
[Service]
Type=oneshot
ExecStart=/usr/local/bin/shop-support-backup
UNIT
cat > /etc/systemd/system/shop-support-backup.timer <<'UNIT'
[Unit]
Description=Daily shop-support backup
[Timer]
OnCalendar=*-*-* 02:30:00 UTC
Persistent=true
RandomizedDelaySec=300
[Install]
WantedBy=timers.target
UNIT
cat > /etc/systemd/system/shop-support-prune.service <<'UNIT'
[Unit]
Description=Remove unused Docker images and build cache
[Service]
Type=oneshot
ExecStart=/usr/bin/docker system prune -af --filter until=168h
UNIT
cat > /etc/systemd/system/shop-support-prune.timer <<'UNIT'
[Unit]
Description=Weekly Docker prune
[Timer]
OnCalendar=Sun *-*-* 04:00:00 UTC
Persistent=true
[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now shop-support-backup.timer shop-support-prune.timer

if [ -n "${REPO_URL:-}" ]; then
  log "first deployment from ${REPO_URL} (${REPO_REF})"
  /usr/local/bin/shop-support-deploy || log "WARNING: deployment failed. Upload the secrets (infra/aws/scripts/put-secrets.sh) and run: sudo shop-support-deploy"
else
  log "REPO_URL is empty: host is ready. Clone the repository to /opt/shop-support and run: sudo shop-support-deploy"
fi
log "bootstrap finished"

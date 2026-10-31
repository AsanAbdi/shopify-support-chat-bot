# Deploying on AWS

One EC2 instance running the same Docker Compose stack that runs locally (Caddy for HTTPS, FastAPI, Celery worker and
beat, PostgreSQL + pgvector, Redis), with everything around it done the AWS way: Terraform, IAM role instead of keys,
secrets in SSM Parameter Store, encrypted disks with daily snapshots, backups in S3, logs and alarms in CloudWatch.
This is the cheapest way to run about 1 200 chats a month.

> **Status.** The Terraform code passes `terraform validate` and `terraform fmt`, the scripts pass `shellcheck`, and the
> application image, compose files and tests were exercised locally. The stack has **not been applied to a real AWS account** yet:
> expect to fix small things on the first `apply` (the usual suspects: IAM, the Bedrock model access screen, DNS).

## What gets created

| Resource | Purpose |
| --- | --- |
| VPC, public subnets in 2 AZs, internet gateway | network (a second AZ is there for a future load balancer or RDS) |
| Security group | inbound 80/443 only; no SSH unless you list CIDRs; the API, DB and Redis are not reachable from outside |
| EC2 (Ubuntu 24.04, `m7i.xlarge`), Elastic IP | the server; IMDSv2 only; encrypted root disk |
| EBS gp3 data disk (encrypted) | PostgreSQL, Redis, uploaded documents, certificates; `prevent_destroy`; daily snapshots (DLM, 14 kept) |
| S3 bucket | database dumps and uploads; versioned, encrypted, TLS only, expires after 35 days (GDPR: deleted data leaves backups) |
| IAM instance role | Session Manager, CloudWatch agent, read own SSM parameters, write backups, publish alerts, optional Bedrock |
| SSM Parameter Store | production configuration and secrets (`/shop-support/prod/*`), read by the deploy script |
| CloudWatch | container logs (30 days), memory and disk metrics, alarms (instance check, CPU, memory, disks) |
| SNS | alarm and backup-failure emails |
| Route 53 record (optional) | A record for your domain |

Rough cost in `eu-central-1` (check the AWS pricing page): `m7i.xlarge` about 175 USD a month on demand, disks and snapshots
about 15 USD, Elastic IP and S3 a few USD. With the lighter mpnet embedding model `t3.large` is enough (about 75 USD).
A 1-year Savings Plan cuts the instance price by roughly a third.

## Deploy

Prerequisites: an AWS account, the AWS CLI logged in (`aws sts get-caller-identity` works), Terraform 1.6+ (or the
`hashicorp/terraform` Docker image), a domain you control, this repository pushed to a Git host.

```bash
# 1. infrastructure
cd infra/aws/terraform
cp terraform.tfvars.example terraform.tfvars        # set alert_email, domain, repo_url, ...
terraform init
terraform apply                                       # review the plan first
```

Outputs give the Elastic IP, the SSM prefix and the Session Manager command. **Confirm the SNS subscription email**, otherwise alarms are silent.
Create the DNS A record of your domain pointing at the Elastic IP (automatic with `route53_zone_id`).

```bash
# 2. configuration and secrets (from your laptop)
cp ../env.prod.example ../.env.prod                  # fill in; generate secrets with: openssl rand -hex 32
../scripts/put-secrets.sh ../.env.prod --region eu-central-1 --prefix /shop-support/prod
# private repository? also store a read-only deploy token:
aws ssm put-parameter --name /shop-support/prod/_GIT_TOKEN --type SecureString --value "<token>" --region eu-central-1
```

```bash
# 3. deploy (the first boot already tried; this is also how every update is rolled out)
aws ssm start-session --region eu-central-1 --target <instance-id>      # shell without SSH keys
sudo shop-support-deploy                                                  # git pull, SSM -> .env, docker compose up -d --build
```

The first build downloads the embedding model (about 2.3 GB) and takes about ten minutes. Then open `https://<your domain>/admin/`
and embed the widget on the shop (`<script src="https://<your domain>/widget.js" data-color="#0b6e4f" defer></script>`).
Remember `CORS_ORIGINS` must list the shop's origin.

Bootstrap log: `/var/log/shop-support-bootstrap.log`. If the API does not become healthy:
`cd /opt/shop-support && sudo docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.aws.yml logs --tail=100 api`.

## LLM provider options

* **Amazon Bedrock (recommended on AWS).** Claude runs inside AWS in the EU, authenticated by the instance role (no API key to leak), and chat
  data stays in the EU, which simplifies GDPR. Set `enable_bedrock = true` in `terraform.tfvars`, request access to the Claude model in
  the Bedrock console (region of the deployment), then use `LLM_PROVIDER=bedrock` and an EU inference profile as `LLM_MODEL`,
  for example `eu.anthropic.claude-haiku-4-5-20251001-v1:0`. The code path (`init_chat_model(..., "bedrock_converse")`) is covered by a unit test but has not been run against Bedrock.
* **Anthropic API:** `LLM_PROVIDER=anthropic` and `ANTHROPIC_API_KEY` in SSM. The data processing agreement with Anthropic applies; processing may happen outside the EU.

## Operations

| Task | How |
| --- | --- |
| Update the application | push to the branch in `repo_ref`, then `sudo shop-support-deploy` (or the manual GitLab job `deploy:aws`) |
| Change configuration or a secret | `put-secrets.sh` (it overwrites), then `sudo shop-support-deploy` |
| Logs | CloudWatch Logs group `/shop-support/prod` (stream per container); `docker compose logs` works on the host too |
| Metrics | the app exposes `/metrics`; Caddy hides it from the internet. For Prometheus/Grafana run the `monitoring` profile on the host and reach it through an SSM port-forward |
| Backups | systemd timer `shop-support-backup.timer` at 02:30 UTC: dump + uploads to S3. A failure emails the SNS topic. Check: `systemctl list-timers`, `sudo systemctl start shop-support-backup` |
| Restore | `sudo shop-support-restore` (newest dump) or `sudo shop-support-restore db/app-<timestamp>.dump`. Then re-run GDPR erasures received since that backup |
| Disk full alarm | data disk: grow the volume in Terraform (`data_volume_gb`) and `sudo resize2fs` the device; root disk: `docker system prune -af` |
| OS patches | `sudo apt-get update && sudo apt-get -y upgrade` in a maintenance window; Docker restarts containers (`live-restore` and restart policies) |
| Teardown | the data disk and the S3 bucket are protected. Remove `prevent_destroy` from the data volume, empty the bucket, then `terraform destroy` |

## Security notes

* No SSH, no public ports besides 80/443; shell access only through Session Manager (audited in CloudTrail).
* The instance role is least-privilege: only its own SSM prefix, its own bucket and log group, optional Bedrock.
* IMDSv2 required (hop limit 2 so that containers can use the role), encrypted root, data disk and bucket, TLS-only bucket policy.
* Secrets never live in Terraform state or in the repository; the deploy script writes `.env` with mode 600 and removes any Git token from the remote URL.
* The production overlay refuses to start with default secrets or with the mock integrations (unless `ALLOW_MOCK_INTEGRATIONS=true` for a demo).

## What this setup does not give you

* **High availability.** One instance in one AZ: a failure means minutes of downtime until you recover it (an EC2 auto-recovery alarm
  action is a one-line addition). For real HA move PostgreSQL to RDS/Aurora (pgvector is supported), Redis to ElastiCache, the API and
  worker to ECS Fargate or an Auto Scaling group behind an ALB (`aws_subnet.public_b` is already there). The application keeps no state in the
  API process except WebSocket connections, and all configuration is environment variables, so this migration is mostly infrastructure work.
* **A WAF.** Put CloudFront with AWS WAF in front if the widget attracts abuse; the application already rate-limits per IP.
* **Uploads on S3.** Uploaded documents live on the EBS disk (backed up to S3 daily); a multi-instance setup would store them in S3.

## Demo deployment with the built-in mocks

Add to the SSM parameters: `COMPOSE_PROFILES=mock,demo`, `ALLOW_MOCK_INTEGRATIONS=true`, `SHOPIFY_BASE_URL=http://mock-services:9000`,
`FRESHDESK_BASE_URL=http://mock-services:9000/freshdesk` (see the bottom of `env.prod.example`). The `seed` service loads the demo knowledge base and catalog.

variable "region" {
  description = "AWS region. Keep it in the EU (GDPR): eu-central-1 Frankfurt, eu-west-1 Ireland, eu-north-1 Stockholm."
  type        = string
  default     = "eu-central-1"
}

variable "name" {
  description = "Name prefix for all resources."
  type        = string
  default     = "shop-support"
}

variable "environment" {
  type    = string
  default = "prod"
}

variable "instance_type" {
  description = "x86_64 instance. The default embedding model (multilingual-e5-large) needs about 2.5 GB per process, so use 16 GB RAM (m7i.xlarge). With the lighter mpnet model t3.large (8 GB) is enough."
  type        = string
  default     = "m7i.xlarge"
}

variable "root_volume_gb" {
  description = "Root disk: OS, Docker images (the backend image is about 4.5 GB) and build cache."
  type        = number
  default     = 50
}

variable "data_volume_gb" {
  description = "Separate encrypted data disk: PostgreSQL, Redis, uploaded documents, TLS certificates. Snapshotted daily."
  type        = number
  default     = 60
}

variable "vpc_cidr" {
  type    = string
  default = "10.42.0.0/16"
}

variable "domain" {
  description = "Public host name of the assistant, for example support.your-shop.com. Caddy requests a Let's Encrypt certificate for it. Point its A record to the Elastic IP (output) or set route53_zone_id."
  type        = string
  default     = ""
}

variable "route53_zone_id" {
  description = "Optional Route 53 hosted zone ID: Terraform then creates the A record for var.domain."
  type        = string
  default     = ""
}

variable "alert_email" {
  description = "Email address that receives CloudWatch alarms (instance down, disk or memory almost full, high CPU). Confirm the subscription email."
  type        = string
}

variable "repo_url" {
  description = "Git URL (HTTPS) of this repository. If it is private, store a token in SSM as <prefix>/_GIT_TOKEN (see infra/aws/README.md). Empty = prepare the host only and deploy by hand."
  type        = string
  default     = ""
}

variable "repo_ref" {
  type    = string
  default = "main"
}

variable "backup_retention_days" {
  description = "How long database dumps stay in S3. Keep it short: deleted customer data must disappear from backups too (GDPR; chats are deleted after 90 days)."
  type        = number
  default     = 35
}

variable "snapshot_retention_count" {
  description = "Number of daily EBS snapshots of the data volume to keep."
  type        = number
  default     = 14
}

variable "log_retention_days" {
  type    = number
  default = 30
}

variable "ssh_cidrs" {
  description = "CIDR blocks allowed to SSH. Empty (recommended): no SSH at all, use AWS Systems Manager Session Manager."
  type        = list(string)
  default     = []
}

variable "enable_bedrock" {
  description = "Allow the instance role to call Amazon Bedrock (LLM_PROVIDER=bedrock): Claude inside AWS in the EU, no API key. Model access must also be enabled in the Bedrock console."
  type        = bool
  default     = false
}

variable "detailed_monitoring" {
  description = "1-minute EC2 metrics (small extra cost)."
  type        = bool
  default     = false
}

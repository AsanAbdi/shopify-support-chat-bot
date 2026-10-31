data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

locals {
  host_env = join("\n", [
    "NAME=${var.name}",
    "ENVIRONMENT=${var.environment}",
    "AWS_REGION=${var.region}",
    "SSM_PREFIX=${local.ssm_prefix}",
    "BACKUP_BUCKET=${aws_s3_bucket.backups.id}",
    "LOG_GROUP=${local.log_group}",
    "DOMAIN=${var.domain}",
    "REPO_URL=${var.repo_url}",
    "REPO_REF=${var.repo_ref}",
    "DATA_VOLUME_ID=${aws_ebs_volume.data.id}",
    "ALERTS_TOPIC=${aws_sns_topic.alerts.arn}",
  ])
}

resource "aws_instance" "app" {
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = var.instance_type
  subnet_id              = aws_subnet.public.id
  vpc_security_group_ids = [aws_security_group.app.id]
  iam_instance_profile   = aws_iam_instance_profile.instance.name
  monitoring             = var.detailed_monitoring
  ebs_optimized          = true

  # IMDSv2 only. Hop limit 2 lets the containers (Bedrock, awslogs) reach the instance role credentials.
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = var.root_volume_gb
    encrypted   = true
  }

  user_data_base64 = base64gzip(templatefile("${path.module}/templates/cloud-config.yaml.tftpl", {
    host_env  = local.host_env
    bootstrap = base64encode(file("${path.module}/../scripts/bootstrap.sh"))
    deploy    = base64encode(file("${path.module}/../scripts/deploy.sh"))
    backup    = base64encode(file("${path.module}/../scripts/backup.sh"))
    restore   = base64encode(file("${path.module}/../scripts/restore.sh"))
  }))

  tags = { Name = var.name }

  lifecycle {
    # A newer AMI or a changed bootstrap script must not silently replace the server (and its data attachment).
    ignore_changes = [ami, user_data_base64]
  }
}

resource "aws_eip" "app" {
  domain = "vpc"
  tags   = { Name = var.name }
}

resource "aws_eip_association" "app" {
  instance_id   = aws_instance.app.id
  allocation_id = aws_eip.app.id
}

resource "aws_route53_record" "app" {
  count   = var.route53_zone_id != "" && var.domain != "" ? 1 : 0
  zone_id = var.route53_zone_id
  name    = var.domain
  type    = "A"
  ttl     = 300
  records = [aws_eip.app.public_ip]
}

output "public_ip" {
  description = "Elastic IP. Create the DNS A record for your domain pointing here (done automatically with route53_zone_id)."
  value       = aws_eip.app.public_ip
}

output "instance_id" {
  value = aws_instance.app.id
}

output "ssm_session_command" {
  description = "Shell on the server without SSH keys (needs the AWS CLI session-manager plugin)."
  value       = "aws ssm start-session --region ${var.region} --target ${aws_instance.app.id}"
}

output "ssm_parameter_prefix" {
  description = "Upload the production .env here with infra/aws/scripts/put-secrets.sh."
  value       = local.ssm_prefix
}

output "backup_bucket" {
  value = aws_s3_bucket.backups.id
}

output "log_group" {
  value = aws_cloudwatch_log_group.app.name
}

output "alerts_topic" {
  value = aws_sns_topic.alerts.arn
}

output "https_url" {
  value = var.domain != "" ? "https://${var.domain}" : "https://${aws_eip.app.public_ip} (set var.domain for a real certificate)"
}

data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "instance" {
  name_prefix        = "${var.name}-instance-"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

# Session Manager (a shell without SSH keys) and the CloudWatch agent (memory and disk metrics).
resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.instance.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy_attachment" "cloudwatch_agent" {
  role       = aws_iam_role.instance.name
  policy_arn = "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy"
}

data "aws_iam_policy_document" "app" {
  statement {
    sid     = "ReadOwnConfig"
    actions = ["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath"]
    resources = [
      "arn:aws:ssm:${var.region}:${local.account_id}:parameter${local.ssm_prefix}",
      "arn:aws:ssm:${var.region}:${local.account_id}:parameter${local.ssm_prefix}/*",
    ]
  }

  statement {
    sid       = "ListBackups"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = [aws_s3_bucket.backups.arn]
  }

  statement {
    sid       = "ReadWriteBackups"
    actions   = ["s3:PutObject", "s3:GetObject", "s3:AbortMultipartUpload"]
    resources = ["${aws_s3_bucket.backups.arn}/*"]
  }

  statement {
    sid       = "AlertOnBackupFailure"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.alerts.arn]
  }

  statement {
    sid       = "ShipDockerLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"]
    resources = ["${aws_cloudwatch_log_group.app.arn}:*"]
  }
}

resource "aws_iam_role_policy" "app" {
  name   = "${var.name}-app"
  role   = aws_iam_role.instance.id
  policy = data.aws_iam_policy_document.app.json
}

data "aws_iam_policy_document" "bedrock" {
  statement {
    sid     = "InvokeClaudeOnBedrock"
    actions = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    resources = [
      "arn:aws:bedrock:*::foundation-model/anthropic.*",
      "arn:aws:bedrock:*:${local.account_id}:inference-profile/*",
    ]
  }
}

resource "aws_iam_role_policy" "bedrock" {
  count  = var.enable_bedrock ? 1 : 0
  name   = "${var.name}-bedrock"
  role   = aws_iam_role.instance.id
  policy = data.aws_iam_policy_document.bedrock.json
}

resource "aws_iam_instance_profile" "instance" {
  name_prefix = "${var.name}-"
  role        = aws_iam_role.instance.name
}

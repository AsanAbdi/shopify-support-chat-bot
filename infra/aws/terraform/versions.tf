terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
  }

  # Local state is fine for a first try. For a team keep the state in S3 (versioned, encrypted) with locking:
  # backend "s3" {
  #   bucket       = "my-terraform-state"
  #   key          = "shop-support/terraform.tfstate"
  #   region       = "eu-central-1"
  #   encrypt      = true
  #   use_lockfile = true
  # }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project     = var.name
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}

data "aws_caller_identity" "current" {}

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  ssm_prefix = "/${var.name}/${var.environment}"
  az         = data.aws_availability_zones.available.names[0]
  account_id = data.aws_caller_identity.current.account_id
  log_group  = "/${var.name}/${var.environment}"
}

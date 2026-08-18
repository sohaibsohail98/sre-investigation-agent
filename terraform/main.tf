terraform {
  required_version = ">= 1.9"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.21, < 7.0"
    }
  }

  backend "s3" {
    bucket         = "sohaib-bedrock-agentcore-tfstate-901876312125"
    key            = "agentcore/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "sohaib-bedrock-agentcore-tf-locks"
    encrypt        = true
  }
}

provider "aws" {
  region = var.region
}

data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  image_uri  = "${aws_ecr_repository.agent.repository_url}:${var.image_tag}"

  # data/*.json stays the single source of truth for content — Terraform
  # reads it to seed DynamoDB rather than duplicating the values here.
  infra_services    = jsondecode(file("${path.module}/../data/services.json"))
  infra_deployments = jsondecode(file("${path.module}/../data/deployments.json"))
  infra_logs        = jsondecode(file("${path.module}/../data/logs.json"))
  infra_costs       = jsondecode(file("${path.module}/../data/costs.json"))
}

# --- Image registry -------------------------------------------------------
# force_delete so `terraform destroy` does not fail on a repo holding images.
# Same-day teardown is the point; nothing here is meant to outlive the demo.

resource "aws_ecr_repository" "agent" {
  name                 = var.project
  image_tag_mutability = "MUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }
}

# Keep only the most recent images so ECR storage stays at pennies.
resource "aws_ecr_lifecycle_policy" "agent" {
  repository = aws_ecr_repository.agent.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Retain only the 3 most recent images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 3
      }
      action = { type = "expire" }
    }]
  })
}

# --- Metrics storage -------------------------------------------------------
# Phase 5's mandatory SQLite->DynamoDB swap (docs/PROJECT.md Storage
# section) — on-demand billing, single-table design (session_id + sk).

resource "aws_dynamodb_table" "metrics" {
  name         = var.metrics_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "session_id"
  range_key    = "sk"

  attribute {
    name = "session_id"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }
}

# --- Live-queried SRE tool data --------------------------------------------
# Replaces local JSON reads with genuine network queries against a real,
# hosted table — architecture is live, content stays deterministic (see
# docs/PROJECT.md) so tests/eval_scenarios.json's exact-match scoring
# still holds. No new compute: reuses the same AgentCore container.

resource "aws_dynamodb_table" "infra" {
  name         = var.infra_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "service"
  range_key    = "category"

  attribute {
    name = "service"
    type = "S"
  }

  attribute {
    name = "category"
    type = "S"
  }
}

resource "aws_dynamodb_table_item" "metrics" {
  for_each   = { for s in local.infra_services : s.service => s }
  table_name = aws_dynamodb_table.infra.name
  hash_key   = aws_dynamodb_table.infra.hash_key
  range_key  = aws_dynamodb_table.infra.range_key

  item = jsonencode({
    service        = { S = each.value.service }
    category       = { S = "METRICS" }
    status         = { S = each.value.status }
    error_rate     = { N = tostring(each.value.error_rate) }
    latency_p95_ms = { N = tostring(each.value.latency_p95_ms) }
    cpu            = { N = tostring(each.value.cpu) }
  })
}

# Only services with an actual recent deployment get an item here — a
# service with none simply has no DEPLOYMENT row, which is itself the
# correct finding (see checkout-api in docs/PROJECT.md).
resource "aws_dynamodb_table_item" "deployments" {
  for_each   = { for d in local.infra_deployments : d.service => d }
  table_name = aws_dynamodb_table.infra.name
  hash_key   = aws_dynamodb_table.infra.hash_key
  range_key  = aws_dynamodb_table.infra.range_key

  item = jsonencode({
    service     = { S = each.value.service }
    category    = { S = "DEPLOYMENT" }
    version     = { S = each.value.version }
    deployed_at = { S = each.value.deployed_at }
  })
}

resource "aws_dynamodb_table_item" "logs" {
  for_each   = local.infra_logs
  table_name = aws_dynamodb_table.infra.name
  hash_key   = aws_dynamodb_table.infra.hash_key
  range_key  = aws_dynamodb_table.infra.range_key

  item = jsonencode({
    service  = { S = each.key }
    category = { S = "LOGS" }
    lines    = { L = [for line in each.value : { S = line }] }
  })
}

resource "aws_dynamodb_table_item" "costs" {
  for_each   = { for c in local.infra_costs : c.service => c }
  table_name = aws_dynamodb_table.infra.name
  hash_key   = aws_dynamodb_table.infra.hash_key
  range_key  = aws_dynamodb_table.infra.range_key

  item = jsonencode({
    service          = { S = each.value.service }
    category         = { S = "COSTS" }
    monthly_cost_usd = { N = tostring(each.value.monthly_cost_usd) }
    requests_per_day = { N = tostring(each.value.requests_per_day) }
  })
}

# --- Runtime execution role ----------------------------------------------
# The role AgentCore assumes to pull the image, call the model, and log.

resource "aws_iam_role" "runtime" {
  name = "${var.project}-runtime"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "bedrock-agentcore.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = local.account_id }
      }
    }]
  })
}

resource "aws_iam_role_policy" "runtime" {
  name = "runtime"
  role = aws_iam_role.runtime.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "ecr:GetAuthorizationToken",
          "ecr:BatchGetImage",
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchCheckLayerAvailability",
        ]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "dynamodb:PutItem",
          "dynamodb:GetItem",
          "dynamodb:Query",
          "dynamodb:Scan",
          "dynamodb:BatchWriteItem",
        ]
        Resource = aws_dynamodb_table.metrics.arn
      },
      {
        # Read-only — the SRE tools query this table, they never write to it.
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:Scan"]
        Resource = aws_dynamodb_table.infra.arn
      },
    ]
  })
}

# --- AgentCore runtime -----------------------------------------------------
# Depends on the image already being pushed to aws_ecr_repository.agent at
# var.image_tag — build/push happens outside Terraform (docker buildx).

resource "aws_bedrockagentcore_agent_runtime" "agent" {
  agent_runtime_name = replace(var.project, "-", "_")
  role_arn           = aws_iam_role.runtime.arn

  agent_runtime_artifact {
    container_configuration {
      container_uri = local.image_uri
    }
  }

  network_configuration {
    network_mode = "PUBLIC"
  }

  environment_variables = {
    MODEL_ID        = "us.anthropic.claude-sonnet-4-6"
    STORAGE_BACKEND = "dynamodb"
    METRICS_TABLE   = aws_dynamodb_table.metrics.name
    INFRA_TABLE     = aws_dynamodb_table.infra.name
  }

  depends_on = [aws_iam_role_policy.runtime]
}

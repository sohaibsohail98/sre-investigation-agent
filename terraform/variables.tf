variable "region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "project" {
  description = "Project/resource name prefix"
  type        = string
  default     = "sohaib-bedrock-agentcore"
}

variable "image_tag" {
  description = "ECR image tag to deploy"
  type        = string
  default     = "sre-agent"
}

variable "metrics_table_name" {
  description = "DynamoDB table for the execution recorder (Phase 5 storage backend)"
  type        = string
  default     = "sre-agent-metrics"
}

variable "infra_table_name" {
  description = "DynamoDB table the SRE tools live-query for service metrics/deployments/logs/costs"
  type        = string
  default     = "sre-agent-infra"
}

variable "github_repo" {
  description = "owner/repo for the GitHub Actions OIDC trust policy — CONFIRM this matches the actual repo before applying ci.tf"
  type        = string
  default     = "sohaibsohail98/sre-investigation-agent"
}

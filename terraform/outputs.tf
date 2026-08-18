output "ecr_repository_url" {
  value = aws_ecr_repository.agent.repository_url
}

output "runtime_role_arn" {
  value = aws_iam_role.runtime.arn
}

output "image_uri" {
  value = local.image_uri
}

output "agent_runtime_arn" {
  value = aws_bedrockagentcore_agent_runtime.agent.agent_runtime_arn
}

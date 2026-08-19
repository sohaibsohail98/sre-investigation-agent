# --- GitHub Actions OIDC — no long-lived AWS keys in repo secrets --------
#
# Applied. var.github_repo must match the real owner/repo this gets
# pushed to (placeholder default: sohaibsohail98/aws-bedrock-project) —
# re-apply if that changes. The repo secret AWS_GITHUB_ACTIONS_ROLE_ARN
# still needs setting manually (see README.md's CI section) before the
# workflow actually runs.
#
# The OIDC provider for token.actions.githubusercontent.com already exists
# in this AWS account (from another project) — referenced via data source,
# not recreated (an account can only have one per provider URL).

data "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"
}

resource "aws_iam_role" "github_actions" {
  name = "${var.project}-github-actions"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = data.aws_iam_openid_connect_provider.github.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        }
        # Scoped to main branch only — PRs/other branches can't deploy.
        # GitHub's actual OIDC `sub` claim (confirmed via CloudTrail after
        # this policy first rejected every request with "Not authorized"
        # despite the owner/repo names matching exactly) is NOT the plain
        # "repo:owner/repo:ref:..." format the old docs describe — it now
        # embeds immutable numeric owner/repo IDs, e.g.
        # "repo:sohaibsohail98@67048112/sre-investigation-agent@1338702610:ref:refs/heads/main".
        # Wildcard the ID segments rather than hardcoding them (fragile —
        # unclear whether they're stable across e.g. a repo transfer).
        StringLike = {
          "token.actions.githubusercontent.com:sub" = "repo:${split("/", var.github_repo)[0]}@*/${split("/", var.github_repo)[1]}@*:ref:refs/heads/main"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "github_actions" {
  name = "deploy"
  role = aws_iam_role.github_actions.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # The eval job (tests/run_eval.py) invokes the model directly,
        # same as the deployed agent does — no resource-level ARN scoping
        # exists for bedrock:InvokeModel.
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
        Resource = "*"
      },
      {
        # ECR auth is account-wide, no resource-level scoping exists for it.
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken"]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:PutImage",
          "ecr:InitiateLayerUpload",
          "ecr:UploadLayerPart",
          "ecr:CompleteLayerUpload",
          "ecr:BatchGetImage",
          "ecr:GetDownloadUrlForLayer",
        ]
        Resource = aws_ecr_repository.agent.arn
      },
      {
        # Terraform itself needs to manage these resources end to end.
        Effect = "Allow"
        Action = [
          "ecr:DescribeRepositories",
          "ecr:PutLifecyclePolicy",
          "ecr:GetLifecyclePolicy",
          "ecr:TagResource",
        ]
        Resource = aws_ecr_repository.agent.arn
      },
      {
        Effect = "Allow"
        Action = [
          "iam:GetRole",
          "iam:GetRolePolicy",
          "iam:CreateRole",
          "iam:DeleteRole",
          "iam:PutRolePolicy",
          "iam:DeleteRolePolicy",
          "iam:PassRole",
          "iam:TagRole",
        ]
        Resource = aws_iam_role.runtime.arn
      },
      {
        Effect = "Allow"
        Action = [
          "dynamodb:CreateTable",
          "dynamodb:DeleteTable",
          "dynamodb:DescribeTable",
          "dynamodb:TagResource",
        ]
        Resource = aws_dynamodb_table.metrics.arn
      },
      {
        # bedrock-agentcore-control has no resource-level ARN scoping for
        # most actions (same limitation the runtime execution role has).
        Effect   = "Allow"
        Action   = ["bedrock-agentcore:*"]
        Resource = "*"
      },
      {
        # Terraform state backend (the bucket/table Terraform itself uses,
        # not this project's own resources).
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
        Resource = [
          "arn:aws:s3:::sohaib-bedrock-agentcore-tfstate-901876312125",
          "arn:aws:s3:::sohaib-bedrock-agentcore-tfstate-901876312125/*",
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem"]
        Resource = "arn:aws:dynamodb:${var.region}:${local.account_id}:table/sohaib-bedrock-agentcore-tf-locks"
      },
    ]
  })
}

output "github_actions_role_arn" {
  value = aws_iam_role.github_actions.arn
}

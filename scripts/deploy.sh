#!/usr/bin/env bash
# Deploys the agent to Bedrock AgentCore Runtime. Ordered, fails clearly
# on the first broken prerequisite — infra must exist before the runtime
# does, since the runtime needs an image to already exist. Full commands
# and rationale: docs/DEPLOYMENT.md.
#
#   ./scripts/deploy.sh

set -euo pipefail
cd "$(dirname "$0")/.."

command -v terraform >/dev/null || { echo "terraform not found — see docs/DEPLOYMENT.md"; exit 1; }
command -v docker >/dev/null || { echo "docker not found — see docs/DEPLOYMENT.md"; exit 1; }
docker buildx version >/dev/null 2>&1 || { echo "docker buildx not available — see docs/DEPLOYMENT.md"; exit 1; }
aws sts get-caller-identity >/dev/null 2>&1 || { echo "AWS credentials not configured — see docs/DEPLOYMENT.md"; exit 1; }

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGION="${AWS_REGION:-us-east-1}"
ECR_REPO="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/sohaib-bedrock-agentcore"

echo "--- 1/3: infra (ECR + IAM + DynamoDB) ---"
(cd terraform && terraform init -input=false
 terraform plan -input=false \
   -target=aws_ecr_repository.agent \
   -target=aws_ecr_lifecycle_policy.agent \
   -target=aws_dynamodb_table.metrics \
   -target=aws_iam_role.runtime \
   -target=aws_iam_role_policy.runtime \
   -out=infra.tfplan
 terraform apply -input=false infra.tfplan)

echo "--- 2/3: build + push image (ARM64 — AgentCore requires it) ---"
aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"
docker buildx build --platform linux/arm64 -f agent/Dockerfile -t "${ECR_REPO}:sre-agent" --push .

echo "--- 3/3: runtime ---"
(cd terraform && terraform plan -input=false -out=runtime.tfplan
 terraform apply -input=false runtime.tfplan)

echo "Done. Invoke it: uv run python -m scripts.invoke \"Why is payments-api degraded?\""

# Deployment

Stub — full walkthrough not written yet. What's true today:

- `./scripts/deploy.sh` runs the ordered deploy (infra → build/push the
  ARM64 image → runtime), checking prerequisites first and failing
  clearly. See the script itself for the underlying `terraform`/`docker
  buildx` commands it runs.
- `uv run python -m scripts.invoke "<prompt>"` invokes the deployed
  runtime, reading the ARN from `terraform output agent_runtime_arn`.
- Deployed, the agent writes metrics to DynamoDB instead of SQLite
  (`STORAGE_BACKEND=dynamodb`, set automatically via Terraform) — this
  switch is mandatory, not optional, since AgentCore's container
  filesystem doesn't persist across invocations (see `docs/PROJECT.md`'s
  Storage section for why).
- An idle-but-registered AgentCore runtime costs $0 (consumption-based
  billing, verified against AWS's own pricing page) — no cost reason to
  tear it down between sessions. If you want to anyway:
  `cd terraform && terraform plan -destroy -out=destroy.tfplan && terraform apply destroy.tfplan`.
- CI (`.github/workflows/deploy-agentcore.yml`) runs this same deploy
  automatically on every push to `main`, via AWS OIDC — no stored keys.

GitHub Actions Cloud Run deploy workflows and a Cloud Run-specific
deployment doc live in the sibling `mcp-context-inspector` repo's own
`docs/DEPLOYMENT.md`.

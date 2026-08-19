"""Invoke the deployed AgentCore runtime with a prompt. Run from repo root:

    uv run python -m scripts.invoke "Why is payments-api degraded?"

Reads the runtime ARN from `terraform output agent_runtime_arn` (falls
back to $AGENT_RUNTIME_ARN if Terraform state isn't available locally,
e.g. running this from a machine that didn't run the deploy).
"""

import json
import os
import subprocess
import sys
import uuid

import boto3

from common.config import DEFAULT_REGION


def _runtime_arn():
    env_arn = os.environ.get("AGENT_RUNTIME_ARN")
    if env_arn:
        return env_arn
    result = subprocess.run(
        ["terraform", "output", "-raw", "agent_runtime_arn"],
        cwd="terraform",
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        print("Couldn't read agent_runtime_arn from Terraform output, and AGENT_RUNTIME_ARN isn't set.", file=sys.stderr)
        sys.exit(1)
    return result.stdout.strip()


def main():
    if len(sys.argv) != 2:
        print('Usage: uv run python -m scripts.invoke "<prompt>"', file=sys.stderr)
        sys.exit(1)
    prompt = sys.argv[1]

    client = boto3.client("bedrock-agentcore", region_name=os.environ.get("AWS_REGION", DEFAULT_REGION))
    response = client.invoke_agent_runtime(
        agentRuntimeArn=_runtime_arn(),
        runtimeSessionId=str(uuid.uuid4()).replace("-", "") + "abcdefgh",
        payload=json.dumps({"prompt": prompt}).encode(),
        contentType="application/json",
        accept="application/json",
    )
    print(response["response"].read().decode())


if __name__ == "__main__":
    main()

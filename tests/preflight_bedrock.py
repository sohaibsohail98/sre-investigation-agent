"""Live Bedrock connectivity check — confirms credentials actually work
against real AWS before the server starts, instead of only discovering
"Bearer Token has expired" (or any other auth failure) when a user
submits the first chat message. Deliberately cheap: GetFoundationModel
is a control-plane read, not a model invocation — no Bedrock token cost,
just an auth/connectivity check.

Not a pytest test (matches tests/run_eval.py's pattern — needs real AWS,
not run as part of the offline unit suite). Run directly, or via
scripts/dev_server.py which runs this automatically before launching:

    uv run python -m tests.preflight_bedrock
"""

import os
import sys

import boto3

from common.config import DEFAULT_MODEL_ID, DEFAULT_REGION


def main():
    # Same defensive clear as agent/runtime.py — this check needs to
    # exercise the exact credential path the real agent uses, including
    # being immune to a stray bearer-token env var.
    os.environ.pop("AWS_BEARER_TOKEN_BEDROCK", None)

    region = os.environ.get("AWS_REGION", DEFAULT_REGION)
    model_id = os.environ.get("MODEL_ID", DEFAULT_MODEL_ID)
    # Converse rejects the inference-profile-prefixed ID for control-plane
    # lookups; strip the "us." prefix to get the underlying model ID.
    bare_model_id = model_id.removeprefix("us.")

    print(f"Checking Bedrock connectivity in {region} for {bare_model_id}...")
    try:
        client = boto3.client("bedrock", region_name=region)
        client.get_foundation_model(modelIdentifier=bare_model_id)
    except Exception as e:
        print(f"❌ FAIL — Bedrock preflight check failed: {e}")
        print("   Common cause: a stray AWS_BEARER_TOKEN_BEDROCK env var in this")
        print("   shell overriding your normal AWS credentials — check with:")
        print("   env | grep AWS_BEARER_TOKEN_BEDROCK")
        return 1

    print("✅ PASS — Bedrock credentials and connectivity confirmed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

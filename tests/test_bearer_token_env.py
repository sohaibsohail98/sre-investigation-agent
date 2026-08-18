"""agent/runtime.py must clear AWS_BEARER_TOKEN_BEDROCK at import time
(see docs/PROJECT.md's AWS/Bedrock gotchas)."""

import importlib
import os


def test_runtime_clears_bearer_token_env_var(monkeypatch):
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "fake-short-lived-token")

    from agent import runtime

    importlib.reload(runtime)

    assert "AWS_BEARER_TOKEN_BEDROCK" not in os.environ

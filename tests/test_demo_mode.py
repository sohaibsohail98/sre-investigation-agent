"""Regression tests for the public demo deployment's DEMO_MODE gating —
in-process via Starlette's TestClient, never a real Bedrock call (see
tests/test_http_routes.py's docstring for why /api/chat isn't covered
there). verify_credential is monkeypatched at the web.server module
level, where auth_verify's route handler actually looks it up — same
pattern mcp-context-inspector's test_server_auth.py uses.
"""

import pytest
from starlette.testclient import TestClient

from web import demo_replay, server as server_module

# Mirrors chat.js's describeToolResult() switch — the exact keys it reads
# off `output` per tool. A demo scenario missing one of these crashes the
# deployed chat UI with "Cannot read properties of undefined" instead of
# rendering a tool card (this happened for real on the public deployment:
# every scenario's tool output was a generic {"status": ..., "demo": True}
# placeholder with none of these keys).
_REQUIRED_OUTPUT_KEYS = {
    "list_services": ["services"],
    "get_service_metrics": ["metrics"],
    "search_logs": ["matches"],
    "get_recent_deployments": ["deployments"],
    "get_cost_breakdown": ["cost"],
}


@pytest.mark.parametrize("prompt", list(demo_replay.SCENARIOS.keys()) + ["some arbitrary unrecognized prompt"])
def test_every_demo_scenario_tool_output_matches_chat_js_shape(prompt):
    scenario = demo_replay.scenario_for(prompt)
    for call in scenario["trace"]:
        required = _REQUIRED_OUTPUT_KEYS.get(call["tool"])
        if not required:
            continue
        for key in required:
            assert key in call["output"], f"{call['tool']} demo output missing {key!r}: {call['output']!r}"


@pytest.fixture
def demo_client(monkeypatch):
    monkeypatch.setattr(server_module, "DEMO_MODE", True)
    monkeypatch.setattr(server_module, "_LIVE_SESSIONS", {})
    monkeypatch.setattr(server_module, "_DAILY_REQUEST_COUNTS", {})
    with TestClient(server_module.app) as c:
        yield c


@pytest.fixture
def local_dev_client(monkeypatch):
    monkeypatch.setattr(server_module, "DEMO_MODE", False)
    with TestClient(server_module.app) as c:
        yield c


def test_healthz_is_unauthenticated_and_ok(local_dev_client):
    resp = local_dev_client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_api_config_local_dev_reports_no_demo_mode(local_dev_client):
    resp = local_dev_client.get("/api/config")
    assert resp.status_code == 200
    assert resp.json()["demo_mode"] is False


def test_api_config_demo_mode_reports_true(demo_client, monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    resp = demo_client.get("/api/config")
    assert resp.status_code == 200
    body = resp.json()
    assert body["demo_mode"] is True
    assert body["live_available"] is True
    assert body["google_client_id"] == "test-client-id"


def test_api_chat_replays_in_demo_mode_without_live_token(demo_client):
    """No Authorization header → demo replay path, never invoke_streaming
    (which would need real Bedrock creds)."""
    resp = demo_client.post("/api/chat", json={"prompt": "is auth-api healthy?"})
    assert resp.status_code == 200
    assert "[DEMO" in resp.text
    assert "data:" in resp.text


def test_auth_verify_disabled_outside_demo_mode(local_dev_client):
    resp = local_dev_client.post("/auth/verify", json={"credential": "irrelevant"})
    assert resp.status_code == 404


def test_auth_verify_mints_live_token_for_valid_credential(demo_client, monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    monkeypatch.setattr(
        server_module, "verify_credential", lambda credential, client_id: {"sub": "sub123", "email": "a@example.com"}
    )
    resp = demo_client.post("/auth/verify", json={"credential": "fake-jwt"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["email"] == "a@example.com"
    assert body["live_token"] in server_module._LIVE_SESSIONS


def test_auth_verify_rejects_invalid_credential(demo_client, monkeypatch):
    from mcp_server.google_auth import InvalidGoogleToken

    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")

    def _raise(credential, client_id):
        raise InvalidGoogleToken("bad token")

    monkeypatch.setattr(server_module, "verify_credential", _raise)
    resp = demo_client.post("/auth/verify", json={"credential": "bad-jwt"})
    assert resp.status_code == 401


def test_daily_cap_blocks_live_requests_once_exhausted(demo_client, monkeypatch):
    monkeypatch.setattr(server_module, "LIVE_DAILY_CAP", 1)
    live_token = "fixed-test-live-token"
    server_module._LIVE_SESSIONS[live_token] = "sub123"

    # Directly exhaust the cap rather than invoking Bedrock via the real
    # live path — the cap check itself is what's under test.
    server_module._record_live_request("sub123")

    resp = demo_client.post(
        "/api/chat",
        json={"prompt": "is auth-api healthy?"},
        headers={"Authorization": f"Bearer {live_token}"},
    )
    assert resp.status_code == 429

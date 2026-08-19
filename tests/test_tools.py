"""Unit tests for tools/ — against a fake in-memory DynamoDB table (see
conftest.py's autouse fake_infra_table fixture), no real AWS, no Bedrock
calls. Covers get_service_metrics/get_cost_breakdown's contract: a
service that passes valid_services() but has no matching item (a
data-drift scenario) must return an error dict, not fall through to an
implicit `return None` — `None.get("status", ...)` would crash
agent/runtime.py's loop outside its try/except.
"""

from tools import _common, get_cost_breakdown, get_recent_deployments, get_service_metrics, list_services, search_logs


def test_list_services_returns_known_services():
    result = list_services.list_services()
    assert result["status"] == "ok"
    assert "payments-api" in result["services"]
    assert "checkout-api" in result["services"]


def test_get_service_metrics_known_service():
    result = get_service_metrics.get_service_metrics("payments-api")
    assert result["status"] == "ok"
    assert result["metrics"]["status"] == "degraded"


def test_get_service_metrics_unknown_service_returns_error_not_none():
    result = get_service_metrics.get_service_metrics("does-not-exist")
    assert result == {
        "status": "error",
        "message": "unknown service: does-not-exist. Call list_services() for valid names.",
    }


def test_get_service_metrics_data_drift_returns_error_not_none(monkeypatch):
    """The actual bug: a service valid_services() knows about, but with
    no METRICS item, must degrade to an error dict — not None, which
    would crash the caller."""
    monkeypatch.setattr(_common, "valid_services", lambda: {"ghost-service"})
    monkeypatch.setattr(_common, "get_item", lambda service, category: None)
    result = get_service_metrics.get_service_metrics("ghost-service")
    assert result is not None
    assert result["status"] == "error"


def test_get_cost_breakdown_data_drift_returns_error_not_none(monkeypatch):
    monkeypatch.setattr(_common, "valid_services", lambda: {"ghost-service"})
    monkeypatch.setattr(_common, "get_item", lambda service, category: None)
    result = get_cost_breakdown.get_cost_breakdown("ghost-service")
    assert result is not None
    assert result["status"] == "error"


def test_search_logs_empty_match_is_ok_not_error():
    """No matches is a valid finding (or an empty log), not a failure."""
    result = search_logs.search_logs("auth-api", "this substring appears nowhere")
    assert result == {"status": "ok", "matches": []}


def test_search_logs_finds_expected_substring():
    result = search_logs.search_logs("payments-api", "connection pool")
    assert result["status"] == "ok"
    assert any("connection pool" in line for line in result["matches"])


def test_get_recent_deployments_empty_is_ok_not_error():
    """checkout-api deliberately has no deployment record — that's the
    finding the eval scenario tests for, not an error state."""
    result = get_recent_deployments.get_recent_deployments("checkout-api")
    assert result == {"status": "ok", "deployments": []}


def test_get_recent_deployments_unknown_service():
    result = get_recent_deployments.get_recent_deployments("does-not-exist")
    assert result["status"] == "error"


def test_get_cost_breakdown_known_service():
    result = get_cost_breakdown.get_cost_breakdown("checkout-api")
    assert result["status"] == "ok"
    assert result["cost"]["monthly_cost_usd"] == 310.00

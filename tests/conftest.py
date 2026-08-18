"""Shared pytest fixtures. These tests are pure unit tests — no live
Bedrock calls, no real AWS — except tests/run_eval.py (a separate,
deliberately live-cost eval harness, not run as part of `pytest`).
"""

import json
from pathlib import Path

import pytest

DATA_DIR = Path(__file__).parent.parent / "data"


class _FakeInfraTable:
    """Minimal in-memory stand-in for the real sre-agent-infra DynamoDB
    table — only implements get_item/scan, the only calls tools/_common.py
    makes. Seeded from the same data/*.json files Terraform uses to seed
    the real table, so tests exercise the same content without any
    network call."""

    def __init__(self, items):
        self.items = items

    def get_item(self, Key):
        for i in self.items:
            if i["service"] == Key["service"] and i["category"] == Key["category"]:
                return {"Item": i}
        return {}

    def scan(self, FilterExpression=None, ExpressionAttributeValues=None):
        category = ExpressionAttributeValues[":c"]
        return {"Items": [i for i in self.items if i["category"] == category]}


@pytest.fixture(autouse=True)
def fake_infra_table(monkeypatch):
    """Autouse — every test gets tools/_common.py backed by an in-memory
    table seeded from data/*.json, never real DynamoDB. Applies globally
    since any test importing tools/* would otherwise need real AWS
    credentials and network access just to run."""
    services = json.loads((DATA_DIR / "services.json").read_text())
    deployments = json.loads((DATA_DIR / "deployments.json").read_text())
    logs = json.loads((DATA_DIR / "logs.json").read_text())
    costs = json.loads((DATA_DIR / "costs.json").read_text())

    items = []
    for s in services:
        items.append({**s, "category": "METRICS"})
    for d in deployments:
        items.append({**d, "category": "DEPLOYMENT"})
    for service, lines in logs.items():
        items.append({"service": service, "category": "LOGS", "lines": lines})
    for c in costs:
        items.append({**c, "category": "COSTS"})

    from tools import _common

    monkeypatch.setattr(_common, "_table", _FakeInfraTable(items))

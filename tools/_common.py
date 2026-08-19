"""Shared data access for the synthetic SRE tools — queries a real
DynamoDB table (see docs/PROJECT.md's "Tool data is live-queried"
section). Table schema: partition key `service`, sort key `category`
("METRICS" | "DEPLOYMENT" | "LOGS" | "COSTS").
"""

import functools
import os

import boto3

from common.config import DEFAULT_REGION
from mci_common.dynamo import clean_decimal as _clean

TABLE_NAME = os.environ.get("INFRA_TABLE", "sre-agent-infra")
REGION = os.environ.get("AWS_REGION", DEFAULT_REGION)

_table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE_NAME)


def get_item(service, category):
    """Returns the item dict, or None if this service has no record in
    that category (e.g. no DEPLOYMENT item — a valid, meaningful absence,
    not an error)."""
    resp = _table.get_item(Key={"service": service, "category": category})
    item = resp.get("Item")
    return _clean(item) if item else None


def list_services():
    """Enumerates known services from the METRICS category — every
    service has exactly one METRICS item, so this is the canonical
    membership list. A full Scan is fine — this table is a handful of
    items (4 services x up to 4 categories), no GSI needed."""
    resp = _table.scan(
        FilterExpression="category = :c", ExpressionAttributeValues={":c": "METRICS"}
    )
    return sorted(i["service"] for i in resp.get("Items", []))


def valid_services():
    return set(list_services())


def unknown_service_error(service):
    return {
        "status": "error",
        "message": f"unknown service: {service}. Call list_services() for valid names.",
    }


def service_param():
    """The repeated {"service": ...} JSON-schema fragment every tool's
    get_tool_spec() needs — one definition instead of five copies."""
    return {"type": "string", "description": "Service name, from list_services()"}


def requires_valid_service(func):
    """Wraps a tool(service, ...) function with the unknown-service guard
    every tool needs, so the check lives in one place instead of five."""

    @functools.wraps(func)
    def wrapper(service, *args, **kwargs):
        if service not in valid_services():
            return unknown_service_error(service)
        return func(service, *args, **kwargs)

    return wrapper

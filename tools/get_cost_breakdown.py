from . import _common


def get_tool_spec():
    return {
        "toolSpec": {
            "name": "get_cost_breakdown",
            "description": "Get monthly operating cost and request volume for a service.",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {"service": _common.service_param()},
                    "required": ["service"],
                }
            },
        }
    }


@_common.requires_valid_service
def get_cost_breakdown(service):
    item = _common.get_item(service, "COSTS")
    if item is None:
        return {
            "status": "error",
            "message": f"{service} is a known service but has no cost data — data may be out of sync.",
        }
    return {
        "status": "ok",
        "cost": {
            "monthly_cost_usd": item["monthly_cost_usd"],
            "requests_per_day": item["requests_per_day"],
        },
    }

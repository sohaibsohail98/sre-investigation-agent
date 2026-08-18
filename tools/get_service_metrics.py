from . import _common


def get_tool_spec():
    return {
        "toolSpec": {
            "name": "get_service_metrics",
            "description": "Get latency, error rate, and CPU metrics for a specific service.",
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
def get_service_metrics(service):
    item = _common.get_item(service, "METRICS")
    if item is None:
        # Passed valid_services() but has no METRICS item — data drifted
        # out of sync. Error, not None: runtime.py's error handling
        # assumes every tool result is a dict.
        return {
            "status": "error",
            "message": f"{service} is a known service but has no metrics data — data may be out of sync.",
        }
    return {
        "status": "ok",
        "metrics": {
            "status": item["status"],
            "error_rate": item["error_rate"],
            "latency_p95_ms": item["latency_p95_ms"],
            "cpu": item["cpu"],
        },
    }

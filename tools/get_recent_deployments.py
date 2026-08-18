from . import _common


def get_tool_spec():
    return {
        "toolSpec": {
            "name": "get_recent_deployments",
            "description": (
                "Get recent deployment history for a service. An empty list "
                "means no recent deployment — that is itself a valid finding, "
                "not a missing-data error."
            ),
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
def get_recent_deployments(service):
    item = _common.get_item(service, "DEPLOYMENT")
    deployments = (
        [{"service": service, "version": item["version"], "deployed_at": item["deployed_at"]}]
        if item
        else []
    )
    return {"status": "ok", "deployments": deployments}

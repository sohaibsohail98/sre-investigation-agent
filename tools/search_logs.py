from . import _common


def get_tool_spec():
    return {
        "toolSpec": {
            "name": "search_logs",
            "description": (
                "Search a service's logs for lines matching a substring "
                "(case-insensitive). Empty matches is a valid result, not an error."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "service": _common.service_param(),
                        "query": {
                            "type": "string",
                            "description": "Substring to search for in log lines",
                        },
                    },
                    "required": ["service", "query"],
                }
            },
        }
    }


@_common.requires_valid_service
def search_logs(service, query):
    item = _common.get_item(service, "LOGS")
    lines = item["lines"] if item else []
    matches = [line for line in lines if query.lower() in line.lower()]
    return {"status": "ok", "matches": matches}

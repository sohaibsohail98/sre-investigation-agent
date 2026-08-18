from . import _common


def get_tool_spec():
    return {
        "toolSpec": {
            "name": "list_services",
            "description": "List the names of all services available to investigate.",
            "inputSchema": {"json": {"type": "object", "properties": {}}},
        }
    }


def list_services():
    return {"status": "ok", "services": _common.list_services()}

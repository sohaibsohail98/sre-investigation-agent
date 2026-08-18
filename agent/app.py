"""The SRE investigation agent — this use case's config, composed with the
boilerplate loop in runtime.py.

Same function (invoke) is called two different ways with zero rewrite:
locally, Gradio imports and calls it directly (see learn/local_chat.py);
deployed, BedrockAgentCoreApp's @app.entrypoint serves it over HTTP. This
is the file that would look different for a different use case —
runtime.py would not.
"""

import os
from pathlib import Path

from bedrock_agentcore import BedrockAgentCoreApp

from agent.runtime import run_agent_loop
from common.config import DEFAULT_MODEL_ID
from metrics.store import record_session
from tools import (
    get_cost_breakdown,
    get_recent_deployments,
    get_service_metrics,
    list_services,
    search_logs,
)

MODEL_ID = os.environ.get("MODEL_ID", DEFAULT_MODEL_ID)
SYSTEM_PROMPT = (Path(__file__).parent / "system_prompt.txt").read_text()

_TOOL_MODULES = [
    list_services,
    get_service_metrics,
    search_logs,
    get_recent_deployments,
    get_cost_breakdown,
]
TOOL_SPECS = [m.get_tool_spec() for m in _TOOL_MODULES]
TOOL_FUNCS = {
    spec["toolSpec"]["name"]: getattr(module, spec["toolSpec"]["name"])
    for module, spec in zip(_TOOL_MODULES, TOOL_SPECS)
}

app = BedrockAgentCoreApp()


@app.entrypoint
def invoke(payload):
    prompt = payload.get("prompt", "List the available services.")
    loop_result = run_agent_loop(
        prompt=prompt,
        model_id=MODEL_ID,
        system_prompt=SYSTEM_PROMPT,
        tool_specs=TOOL_SPECS,
        tool_funcs=TOOL_FUNCS,
    )
    session_id = record_session(prompt, MODEL_ID, loop_result)
    return {"result": loop_result["text"], "trace": loop_result["trace"], "session_id": session_id}


def invoke_streaming(prompt, on_event, model_id=None, thinking_budget=None):
    """Same agent, same recording — the only difference from invoke() is
    that on_event fires live as the loop progresses (see runtime.py),
    instead of the caller only getting the finished trace at the end.
    Used by web/server.py's /api/chat SSE endpoint; not used by
    AgentCore (its @app.entrypoint contract is request/response, not
    streaming) or Gradio (invoke() is enough for local dev).

    model_id and thinking_budget are optional per-call overrides — falls
    back to this module's fixed MODEL_ID / no-thinking default when unset,
    so existing callers are unaffected."""
    model_id = model_id or MODEL_ID
    loop_result = run_agent_loop(
        prompt=prompt,
        model_id=model_id,
        system_prompt=SYSTEM_PROMPT,
        tool_specs=TOOL_SPECS,
        tool_funcs=TOOL_FUNCS,
        on_event=on_event,
        thinking_budget=thinking_budget,
    )
    session_id = record_session(prompt, model_id, loop_result)
    return {"result": loop_result["text"], "trace": loop_result["trace"], "session_id": session_id}


if __name__ == "__main__":
    app.run()

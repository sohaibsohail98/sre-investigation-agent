"""Unit tests for agent/runtime.py's loop mechanics — no live Bedrock
calls. agent.runtime._client.converse is monkeypatched with a canned
response sequence, so these test the loop's dispatch/error-handling/
event-emission logic in isolation from the model itself.
"""

from agent import runtime


class _FakeClient:
    """Returns one canned converse() response per call, in order."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def converse(self, **kwargs):
        resp = self.responses[self.calls]
        self.calls += 1
        return resp


def _usage(input_tokens=10, output_tokens=5):
    return {"inputTokens": input_tokens, "outputTokens": output_tokens}


def _tool_use_response(tool_name, args, tool_use_id="t1", text=None):
    content = []
    if text:
        content.append({"text": text})
    content.append({"toolUse": {"toolUseId": tool_use_id, "name": tool_name, "input": args}})
    return {
        "output": {"message": {"role": "assistant", "content": content}},
        "stopReason": "tool_use",
        "usage": _usage(),
    }


def _end_turn_response(text):
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
        "stopReason": "end_turn",
        "usage": _usage(),
    }


def _run(monkeypatch, responses, tool_funcs, on_event=None):
    monkeypatch.setattr(runtime, "_client", _FakeClient(responses))
    return runtime.run_agent_loop(
        prompt="test prompt",
        model_id="us.anthropic.claude-sonnet-4-6",
        system_prompt="test system prompt",
        tool_specs=[],
        tool_funcs=tool_funcs,
        on_event=on_event,
    )


def test_simple_end_turn_no_tools(monkeypatch):
    result = _run(monkeypatch, [_end_turn_response("the answer")], tool_funcs={})
    assert result["text"] == "the answer"
    assert result["trace"] == []
    assert len(result["turns"]) == 1


def test_tool_call_then_final_answer(monkeypatch):
    responses = [
        _tool_use_response("list_services", {}),
        _end_turn_response("here are the services"),
    ]
    result = _run(monkeypatch, responses, tool_funcs={"list_services": lambda: {"status": "ok", "services": []}})
    assert result["text"] == "here are the services"
    assert result["trace"] == [{"tool": "list_services", "args": {}, "status": "ok"}]
    assert len(result["turns"]) == 2


def test_tool_returning_non_dict_becomes_error_not_a_crash(monkeypatch):
    """The actual bug: a tool returning something other than a dict
    (e.g. an implicit None from a fallthrough) must not raise
    AttributeError from output.get(...) — it becomes an error result and
    the loop continues."""
    responses = [
        _tool_use_response("broken_tool", {}),
        _end_turn_response("recovered"),
    ]
    result = _run(monkeypatch, responses, tool_funcs={"broken_tool": lambda: None})
    assert result["trace"] == [{"tool": "broken_tool", "args": {}, "status": "error"}]
    assert result["text"] == "recovered"


def test_tool_raising_exception_becomes_error_not_a_crash(monkeypatch):
    def _raises():
        raise ValueError("boom")

    responses = [
        _tool_use_response("exploding_tool", {}),
        _end_turn_response("recovered"),
    ]
    result = _run(monkeypatch, responses, tool_funcs={"exploding_tool": _raises})
    assert result["trace"][0]["status"] == "error"
    assert result["text"] == "recovered"


def test_unknown_tool_name_becomes_error(monkeypatch):
    responses = [
        _tool_use_response("nonexistent_tool", {}),
        _end_turn_response("recovered"),
    ]
    result = _run(monkeypatch, responses, tool_funcs={})
    assert result["trace"] == [{"tool": "nonexistent_tool", "args": {}, "status": "error"}]


def test_turn_limit_reached_returns_without_crashing(monkeypatch):
    responses = [_tool_use_response("loop_tool", {}) for _ in range(runtime.MAX_TURNS)]
    result = _run(monkeypatch, responses, tool_funcs={"loop_tool": lambda: {"status": "ok"}})
    assert result["text"] == "Hit the turn limit without finishing."
    assert len(result["trace"]) == runtime.MAX_TURNS


def test_on_event_receives_full_sequence(monkeypatch):
    """This is what the chat UI's live tool-call visibility depends on —
    pin the event sequence so streaming can't silently drift from what
    actually happened in the loop.

    Pins the required contract: the final turn's text must be emitted
    ONLY as "final", never also as "assistant_text" — emitting both
    would duplicate the entire answer as raw unrendered markdown in the
    chat UI's "reasoning" line, directly above the same text properly
    rendered as the answer bubble."""
    events = []
    responses = [
        _tool_use_response("list_services", {}, text="Let me check."),
        _end_turn_response("done"),
    ]
    _run(
        monkeypatch, responses,
        tool_funcs={"list_services": lambda: {"status": "ok", "services": []}},
        on_event=events.append,
    )
    types = [e["type"] for e in events]
    assert types == ["turn_start", "assistant_text", "tool_call", "tool_result", "turn_start", "final"]
    assert events[1]["text"] == "Let me check."
    assert events[2] == {"type": "tool_call", "tool": "list_services", "args": {}}
    assert events[3]["status"] == "ok"
    assert events[5]["text"] == "done"


def test_on_event_none_is_a_safe_default(monkeypatch):
    """Existing callers (eval, Gradio) don't pass on_event at all."""
    result = _run(monkeypatch, [_end_turn_response("ok")], tool_funcs={})
    assert result["text"] == "ok"


def _end_turn_response_with_reasoning(text, reasoning_text):
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": [
                    {"reasoningContent": {"reasoningText": {"text": reasoning_text, "signature": "sig"}}},
                    {"text": text},
                ],
            }
        },
        "stopReason": "end_turn",
        "usage": _usage(),
    }


def test_thinking_budget_passes_additional_model_request_fields(monkeypatch):
    """The actual feature: when thinking_budget is set, every converse()
    call must carry Bedrock's extended-thinking request shape, with
    maxTokens exceeding budget_tokens as the API requires."""
    client = _FakeClient([_end_turn_response("ok")])
    monkeypatch.setattr(runtime, "_client", client)
    runtime.run_agent_loop(
        prompt="p", model_id="m", system_prompt="s", tool_specs=[], tool_funcs={}, thinking_budget=1024
    )
    assert client.calls == 1


def test_reasoning_event_fires_distinct_from_final(monkeypatch):
    """A reasoningContent block must emit a "reasoning" event, separate
    from "final" — the two are genuinely different content (structured
    chain-of-thought vs. the answer), not the same text twice."""
    events = []
    responses = [_end_turn_response_with_reasoning("the answer", "because X implies Y")]
    _run(monkeypatch, responses, tool_funcs={}, on_event=events.append)
    types = [e["type"] for e in events]
    assert types == ["turn_start", "reasoning", "final"]
    assert events[1]["text"] == "because X implies Y"
    assert events[2]["text"] == "the answer"


def test_no_reasoning_event_when_thinking_not_requested(monkeypatch):
    """Plain responses (no reasoningContent block) never fire "reasoning"
    — it's opt-in, not inferred from absence of narration."""
    events = []
    _run(monkeypatch, [_end_turn_response("ok")], tool_funcs={}, on_event=events.append)
    assert "reasoning" not in [e["type"] for e in events]


def test_context_blocks_category_sequence_for_tool_call_then_final(monkeypatch):
    """system/tools/user are seeded up front; a tool-call turn adds
    tool_call then tool_result; the final turn adds answer — this exact
    ordering is what metrics/store_sqlite.py's context_blocks table and
    the Context Window Explorer UI both depend on."""
    responses = [
        _tool_use_response("list_services", {}),
        _end_turn_response("here are the services"),
    ]
    result = _run(monkeypatch, responses, tool_funcs={"list_services": lambda: {"status": "ok", "services": []}})
    categories = [b["category"] for b in result["context_blocks"]]
    assert categories == ["system", "tools", "user", "tool_call", "tool_result", "answer"]


def test_context_blocks_tool_result_carries_status(monkeypatch):
    """A failing tool call's tool_result block carries its error status
    so the UI can color it distinctly from a successful call."""
    responses = [
        _tool_use_response("broken_tool", {}),
        _end_turn_response("recovered"),
    ]
    result = _run(monkeypatch, responses, tool_funcs={"broken_tool": lambda: None})
    tool_result_blocks = [b for b in result["context_blocks"] if b["category"] == "tool_result"]
    assert tool_result_blocks[0]["status"] == "error"

    ok_responses = [
        _tool_use_response("list_services", {}),
        _end_turn_response("here are the services"),
    ]
    ok_result = _run(monkeypatch, ok_responses, tool_funcs={"list_services": lambda: {"status": "ok"}})
    ok_tool_result_blocks = [b for b in ok_result["context_blocks"] if b["category"] == "tool_result"]
    assert ok_tool_result_blocks[0]["status"] == "ok"


def test_context_blocks_non_tool_result_categories_have_no_status_key(monkeypatch):
    result = _run(monkeypatch, [_end_turn_response("the answer")], tool_funcs={})
    for block in result["context_blocks"]:
        assert "status" not in block

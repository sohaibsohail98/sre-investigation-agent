"""Boilerplate — the AgentCore/Bedrock loop mechanics. Rarely touched.

Model, system prompt, and tools are injected by the use case's app.py;
nothing use-case-specific lives here. See docs/PROJECT.md for the
boilerplate/config split this follows and why.
"""

import json
import os
import time

import boto3

from common.config import DEFAULT_REGION

# 1 token ≈ 4 characters — a standard rough estimate (the same order of
# magnitude Anthropic's own docs use for illustrative context-window
# breakdowns), used ONLY for the per-block context_blocks composition
# below. It is explicitly NOT the source of the exact input/output token
# counts in `turns` — those are real numbers straight from Bedrock's
# `usage` field and are never touched by this estimate.
_CHARS_PER_TOKEN = 4

REGION = os.environ.get("AWS_REGION", DEFAULT_REGION)
MAX_TURNS = int(os.environ.get("MAX_TURNS", "10"))

# botocore silently prefers AWS_BEARER_TOKEN_BEDROCK over IAM credentials
# for any bedrock-runtime call the moment this var is set anywhere in the
# process env (see botocore.handlers._should_prefer_bearer_auth) — clear
# it so the standard credential chain always wins. See docs/PROJECT.md's
# AWS/Bedrock gotchas for the incident this fixes.
os.environ.pop("AWS_BEARER_TOKEN_BEDROCK", None)

_client = boto3.client("bedrock-runtime", region_name=REGION)


def run_agent_loop(
    prompt, model_id, system_prompt, tool_specs, tool_funcs, on_event=None, thinking_budget=None
):
    """Runs the Converse API tool-calling loop to completion.

    Returns a dict:
      text          — final answer
      trace         — [{"tool", "args", "status"}, ...] every tool call, in order
      turns         — [{"input_tokens", "output_tokens", "latency_ms",
                        "cache_read_input_tokens", "cache_write_input_tokens"}, ...]
                       one per converse() call
      input_tokens, output_tokens, total_tokens — summed across turns
      latency_ms    — total wall-clock time across all turns
      context_blocks — [{"category", "label", "char_count", "token_estimate",
                        "turn_n"}, ...] one entry per distinct thing that
                       entered context during this session (system prompt,
                       tool specs, the user's prompt, each turn's reasoning/
                       tool-call/tool-result/final-answer content), in the
                       order it was added. token_estimate is a character-
                       based ESTIMATE (see _CHARS_PER_TOKEN) for composition
                       breakdown purposes only — it is not, and is never
                       reconciled against, the exact per-turn input/output
                       token counts above, which come straight from
                       Bedrock's usage field.

    A tool function that raises is caught here and turned into an error
    toolResult rather than crashing the invocation, so the model sees the
    failure and can adapt instead of the whole request dying on one bad
    call. Tools are expected to handle their own "expected" bad input
    (e.g. an unknown service name) by returning a {"status": "error", ...}
    dict themselves — this except is a backstop for genuinely unexpected
    exceptions, not the primary error path.

    on_event(event: dict), if given, is called synchronously as the loop
    progresses — "turn_start", "assistant_text" (any reasoning text the
    model wrote before/alongside a tool call), "reasoning" (Claude's
    structured extended-thinking output, only fired when thinking_budget
    is set and the model returns a reasoningContent block — distinct from
    assistant_text, which is plain narration), "tool_call", "tool_result",
    "final". This is what makes the investigation's actual tool-calling
    visible live (chat UI) instead of only after the fact (trace/turns in
    the return value) — same underlying loop, no duplicated logic, the
    callback just observes what's already happening. None (the default)
    costs nothing extra; existing callers (eval, Gradio) are unaffected.

    thinking_budget, if set, enables real extended thinking on every
    converse() call in this loop (additionalModelRequestFields) — a
    genuine Bedrock Converse feature returning a structured reasoningContent
    block, not the plain pre-tool-call narration. Off by default: it costs
    more output tokens and latency, so it's opt-in per invocation.
    """
    if on_event is None:
        on_event = lambda event: None  # noqa: E731

    messages = [{"role": "user", "content": [{"text": prompt}]}]
    trace = []
    turns = []
    context_blocks = []

    def _add_block(category, label, content, turn_n=None, status=None):
        chars = len(content) if isinstance(content, str) else len(json.dumps(content))
        if chars == 0:
            return
        block = {
            "category": category,
            "label": label,
            "char_count": chars,
            "token_estimate": max(1, round(chars / _CHARS_PER_TOKEN)),
            "turn_n": turn_n,
        }
        if status is not None:
            block["status"] = status
        context_blocks.append(block)

    _add_block("system", "System prompt", system_prompt)
    _add_block("tools", f"Tool specs ({len(tool_specs)} tools)", tool_specs)
    _add_block("user", "User prompt", prompt, turn_n=0)

    def _totals():
        return {
            "input_tokens": sum(t["input_tokens"] for t in turns),
            "output_tokens": sum(t["output_tokens"] for t in turns),
            "total_tokens": sum(t["input_tokens"] + t["output_tokens"] for t in turns),
            "latency_ms": sum(t["latency_ms"] for t in turns),
        }

    converse_kwargs = {}
    if thinking_budget:
        converse_kwargs["additionalModelRequestFields"] = {
            "thinking": {"type": "enabled", "budget_tokens": thinking_budget}
        }
        # maxTokens must exceed budget_tokens — leave headroom for the answer itself.
        converse_kwargs["inferenceConfig"] = {"maxTokens": thinking_budget + 2000}

    for turn_n in range(MAX_TURNS):
        on_event({"type": "turn_start", "turn": turn_n})

        start = time.perf_counter()
        response = _client.converse(
            modelId=model_id,
            system=[{"text": system_prompt}, {"cachePoint": {"type": "default"}}],
            messages=messages,
            toolConfig={"tools": tool_specs},
            **converse_kwargs,
        )
        latency_ms = round((time.perf_counter() - start) * 1000)
        usage = response["usage"]
        turns.append(
            {
                "input_tokens": usage["inputTokens"],
                "output_tokens": usage["outputTokens"],
                "latency_ms": latency_ms,
                "cache_read_input_tokens": usage.get("cacheReadInputTokens", 0),
                "cache_write_input_tokens": usage.get("cacheWriteInputTokens", 0),
            }
        )

        reply = response["output"]["message"]
        messages.append(reply)

        assistant_text = "".join(b["text"] for b in reply["content"] if "text" in b)
        reasoning_text = "".join(
            b["reasoningContent"]["reasoningText"]["text"]
            for b in reply["content"]
            if "reasoningContent" in b and "reasoningText" in b["reasoningContent"]
        )

        if response["stopReason"] != "tool_use":
            # This turn's text IS the final answer — emit it once, as
            # "final", not also as "assistant_text" (that duplicated the
            # whole answer as raw unrendered "reasoning" text above the
            # properly-rendered answer bubble).
            if reasoning_text:
                on_event({"type": "reasoning", "text": reasoning_text})
                _add_block("thinking", f"Extended thinking (turn {turn_n})", reasoning_text, turn_n)
            _add_block("answer", "Final answer", assistant_text, turn_n)
            result = {"text": assistant_text, "trace": trace, "turns": turns, "context_blocks": context_blocks, **_totals()}
            on_event({"type": "final", "text": assistant_text})
            return result

        if reasoning_text:
            on_event({"type": "reasoning", "text": reasoning_text})
            _add_block("thinking", f"Extended thinking (turn {turn_n})", reasoning_text, turn_n)

        # Only turns that precede a tool call are "reasoning" — commentary
        # explaining what the agent is about to do next.
        if assistant_text:
            on_event({"type": "assistant_text", "text": assistant_text})
            _add_block("reasoning", f"Reasoning (turn {turn_n})", assistant_text, turn_n)

        tool_results = []
        for block in reply["content"]:
            if "toolUse" not in block:
                continue
            call = block["toolUse"]
            name = call["name"]
            args = call.get("input", {})
            func = tool_funcs.get(name)

            on_event({"type": "tool_call", "tool": name, "args": args})
            _add_block("tool_call", f"Tool call: {name}", args, turn_n)

            if func is None:
                output = {"status": "error", "message": f"unknown tool: {name}"}
            else:
                try:
                    output = func(**args)
                    if not isinstance(output, dict):
                        output = {"status": "error", "message": f"tool {name} returned {output!r}, expected a dict"}
                except Exception as e:
                    output = {"status": "error", "message": str(e)}

            status = output.get("status", "ok")
            trace.append({"tool": name, "args": args, "status": status})
            on_event({"type": "tool_result", "tool": name, "status": status, "output": output})
            _add_block("tool_result", f"Tool result: {name}", output, turn_n, status=status)
            tool_results.append(
                {
                    "toolResult": {
                        "toolUseId": call["toolUseId"],
                        "content": [{"json": output}],
                    }
                }
            )
        messages.append({"role": "user", "content": tool_results})

    final_text = "Hit the turn limit without finishing."
    on_event({"type": "final", "text": final_text})
    _add_block("answer", "Final answer (turn limit)", final_text, MAX_TURNS - 1)
    return {
        "text": final_text,
        "trace": trace,
        "turns": turns,
        "context_blocks": context_blocks,
        **_totals(),
    }

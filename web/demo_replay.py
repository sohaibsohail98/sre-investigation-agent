"""Fixture-replay path for the public demo deployment (DEMO_MODE=1) —
same SSE event shape agent/runtime.py's run_agent_loop() emits via
on_event (see its docstring), so web/server.py's /api/chat streaming
code doesn't need to know whether it's replaying or calling Bedrock.
Zero model spend either way this is invoked; every replayed answer is
labelled [DEMO] so nobody mistakes a canned response for a live one.

Each tool call's "output" mirrors the real shape the matching tools/*.py
module returns (see e.g. tools/get_service_metrics.py) — chat.js's
describeToolResult() indexes into output.metrics/.services/.matches/
.deployments/.cost for its one-line summary, and a generic placeholder
output crashes it (undefined property read). Values are illustrative,
not read from data/*.json — demo mode never calls the real tools, so
there's no live data to stay in sync with.

Canned scenarios cover the 4 curated suggested prompts (see
tests/eval_scenarios.json, the same ids web/server.py's
/api/suggested-prompts already curates) plus one generic fallback for
any other prompt typed in.
"""

_SERVICES = ["checkout-api", "payments-api", "auth-api", "notifications"]

SCENARIOS = {
    "why is payments-api degraded?": {
        "trace": [
            {
                "tool": "list_services",
                "args": {},
                "status": "ok",
                "output": {"status": "ok", "services": _SERVICES},
            },
            {
                "tool": "get_service_metrics",
                "args": {"service": "payments-api"},
                "status": "ok",
                "output": {
                    "status": "ok",
                    "metrics": {"status": "degraded", "error_rate": 8.4, "latency_p95_ms": 1840, "cpu": 71},
                },
            },
            {
                "tool": "get_recent_deployments",
                "args": {"service": "payments-api"},
                "status": "ok",
                "output": {
                    "status": "ok",
                    "deployments": [{"service": "payments-api", "version": "v1.8.3", "deployed_at": "14:27"}],
                },
            },
        ],
        "reasoning": [
            "Checking payments-api's current metrics first, then whether a recent deployment lines up.",
            "Error rate and latency are both elevated — checking recent deployments for a likely cause.",
        ],
        "answer": (
            "[DEMO — replayed, no live model call] payments-api's p99 latency and error rate both rose "
            "starting around 14:27, which lines up with a v1.8.3 deployment. Recommend checking that "
            "deployment's connection-pool settings against the downstream dependency."
        ),
    },
    "investigate checkout-api and tell me whether there is evidence of a recent deployment causing the issue.": {
        "trace": [
            {
                "tool": "get_service_metrics",
                "args": {"service": "checkout-api"},
                "status": "ok",
                "output": {
                    "status": "ok",
                    "metrics": {"status": "degraded", "error_rate": 6.1, "latency_p95_ms": 1200, "cpu": 65},
                },
            },
            {
                "tool": "get_recent_deployments",
                "args": {"service": "checkout-api"},
                "status": "ok",
                "output": {"status": "ok", "deployments": []},
            },
            {
                "tool": "search_logs",
                "args": {"service": "checkout-api", "query": "timeout"},
                "status": "ok",
                "output": {
                    "status": "ok",
                    "matches": [
                        "11:05:12 upstream inventory-service timeout after 3000ms",
                        "11:05:20 circuit breaker opened for inventory-service",
                    ],
                },
            },
        ],
        "reasoning": [
            "Pulling checkout-api's current metrics and recent deployment history together.",
            "No recent deployment on record — checking logs for a more direct signal before concluding.",
        ],
        "answer": (
            "[DEMO — replayed, no live model call] No — there's no recent deployment on record for "
            "checkout-api. Logs point to a different cause: upstream inventory-service timeouts tripped "
            "its circuit breaker, which better explains the elevated latency and error rate."
        ),
    },
    "is auth-api healthy?": {
        "trace": [
            {
                "tool": "get_service_metrics",
                "args": {"service": "auth-api"},
                "status": "ok",
                "output": {
                    "status": "ok",
                    "metrics": {"status": "healthy", "error_rate": 0.3, "latency_p95_ms": 180, "cpu": 22},
                },
            },
        ],
        "reasoning": [],
        "answer": (
            "[DEMO — replayed, no live model call] Yes — auth-api is healthy. Error rate and p95 latency "
            "are both within baseline, no active incidents."
        ),
    },
    "compare the cheapest and most expensive services to operate and tell me the difference in monthly cost.": {
        "trace": [
            {
                "tool": "list_services",
                "args": {},
                "status": "ok",
                "output": {"status": "ok", "services": _SERVICES},
            },
            {
                "tool": "get_cost_breakdown",
                "args": {"service": "payments-api"},
                "status": "ok",
                "output": {"status": "ok", "cost": {"monthly_cost_usd": 420.5, "requests_per_day": 180000}},
            },
            {
                "tool": "get_cost_breakdown",
                "args": {"service": "notifications"},
                "status": "ok",
                "output": {"status": "ok", "cost": {"monthly_cost_usd": 40.0, "requests_per_day": 60000}},
            },
        ],
        "reasoning": [
            "Pulling the cost breakdown for each tracked service to compare.",
        ],
        "answer": (
            "[DEMO — replayed, no live model call] payments-api is the most expensive service to operate "
            "at $420.50/month; notifications is the cheapest at $40.00/month — a difference of $380.50. "
            "The gap is driven by request volume and dependency cost, not a pricing difference between "
            "services."
        ),
    },
}

_FALLBACK = {
    "trace": [
        {
            "tool": "list_services",
            "args": {},
            "status": "ok",
            "output": {"status": "ok", "services": _SERVICES},
        },
    ],
    "reasoning": [
        "This demo deployment replays a fixed set of canned investigations instead of calling a live model.",
    ],
    "answer": (
        "[DEMO — replayed, no live model call] This public deployment runs in demo mode: it replays a small "
        "set of pre-recorded investigations (see the suggested prompts) instead of making live, billed model "
        "calls. Try one of the suggested prompts above for a full replayed investigation, or sign in for live "
        "mode."
    ),
}


def scenario_for(prompt):
    return SCENARIOS.get(prompt.strip().lower(), _FALLBACK)


def replay_events(prompt, on_event):
    """Emits the same event sequence run_agent_loop() would for a real
    investigation, built from a canned scenario — see module docstring.
    Returns a loop_result-shaped dict good enough for the /api/chat
    'done' event (a synthetic session_id, since nothing is recorded to
    metrics.store in demo mode — see web/server.py)."""
    scenario = scenario_for(prompt)

    on_event({"type": "turn_start", "turn": 0})
    for text in scenario["reasoning"]:
        on_event({"type": "assistant_text", "text": text})
    for call in scenario["trace"]:
        on_event({"type": "tool_call", "tool": call["tool"], "args": call["args"]})
        on_event({"type": "tool_result", "tool": call["tool"], "status": call["status"], "output": call["output"]})
    on_event({"type": "final", "text": scenario["answer"]})

    return {"text": scenario["answer"], "trace": scenario["trace"]}

"""Deterministic eval runner — scores against tool-trace + fact presence,
not judge-based, so it costs one Bedrock invocation per scenario and
nothing else. Run from repo root:

    uv run python -m tests.run_eval
"""

import json
import sys
from pathlib import Path

from agent.app import invoke

SCENARIOS_PATH = Path(__file__).parent / "eval_scenarios.json"


def score(scenario, result):
    trace_tools = {t["tool"] for t in result["trace"]}
    text = result["result"].lower()

    missing_tools = [t for t in scenario["expected_tools"] if t not in trace_tools]
    missing_facts = [f for f in scenario["expected_facts"] if f.lower() not in text]

    return {
        "passed": not missing_tools and not missing_facts,
        "missing_tools": missing_tools,
        "missing_facts": missing_facts,
    }


def main():
    scenarios = json.loads(SCENARIOS_PATH.read_text())
    results = []

    for scenario in scenarios:
        result = invoke({"prompt": scenario["question"]})
        outcome = score(scenario, result)
        results.append((scenario, outcome))

        status = "PASS" if outcome["passed"] else "FAIL"
        print(f"[{status}] #{scenario['id']}: {scenario['question']}")
        if not outcome["passed"]:
            if outcome["missing_tools"]:
                print(f"         missing tool calls: {outcome['missing_tools']}")
            if outcome["missing_facts"]:
                print(f"         missing facts in answer: {outcome['missing_facts']}")

    passed = sum(1 for _, o in results if o["passed"])
    total = len(results)
    print(f"\n{passed}/{total} scenarios passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())

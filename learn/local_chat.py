"""Local dev loop for the SRE agent — same invoke() the deployed agent
uses, no Docker, no AgentCore round-trip. Run from the repo root:

    uv run python -m learn.local_chat
"""

import json
from pathlib import Path

import gradio as gr

from agent.app import invoke

SCENARIOS_PATH = Path(__file__).parent.parent / "tests" / "eval_scenarios.json"
_CURATED_IDS = {1, 5, 6, 8}  # same subset web/server.py's chat page suggests — one source of truth


def _suggested_prompts():
    scenarios = json.loads(SCENARIOS_PATH.read_text())
    return [s["question"] for s in scenarios if s["id"] in _CURATED_IDS]


def chat(message, history):
    return invoke({"prompt": message})["result"]


if __name__ == "__main__":
    gr.ChatInterface(
        chat,
        title="SRE investigation agent — local",
        examples=_suggested_prompts(),
    ).launch()

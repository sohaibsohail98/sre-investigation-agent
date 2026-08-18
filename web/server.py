"""The chat frontend — decoupled from mcp_server/, which is strictly the
MCP protocol server + metrics data access, reusable by any agent client.
This process owns only: GET /chat, GET /chat.js, GET /mcp-client.js,
POST /api/chat, GET /api/suggested-prompts. Imports agent/app.py's
invoke_streaming() as a plain client — same relationship any other caller
has to the agent, no special access.

Run from repo root (alongside mcp_server.server, on a different port):
    uv run python -m web.server
"""

import json
import queue
import threading
from pathlib import Path

import anyio
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from starlette.routing import Route

from agent.app import invoke_streaming

WEB_DIR = Path(__file__).parent
SCENARIOS_PATH = Path(__file__).parent.parent / "tests" / "eval_scenarios.json"

_DONE = object()  # sentinel — distinguishes "queue empty" from "loop finished"


def _no_cache_file(path, media_type):
    """FileResponse sets ETag/Last-Modified but no Cache-Control by
    default, which leaves the browser free to serve a stale copy on
    reload depending on its own heuristics. This is local dev tooling
    under active change — never let the browser cache it, full stop."""
    resp = FileResponse(path, media_type=media_type)
    resp.headers["Cache-Control"] = "no-store"
    return resp


async def root_redirect(request: Request):
    return RedirectResponse(url="/chat")


async def chat_index(request: Request):
    return _no_cache_file(WEB_DIR / "chat.html", "text/html")


async def chat_js(request: Request):
    return _no_cache_file(WEB_DIR / "chat.js", "application/javascript")


async def mcp_client_js(request: Request):
    return _no_cache_file(WEB_DIR / "mcp-client.js", "application/javascript")


async def api_chat(request: Request):
    """Server-Sent Events — the loop runs turn by turn, and each event
    (turn started, tool called, tool result, final answer) streams to
    the browser as it happens, instead of one silent wait then a single
    JSON blob. This is what makes the investigation's actual tool calls
    visible live, and it's also *why* it takes several seconds: multiple
    real Bedrock round-trips are genuinely happening, not simulated —
    streaming surfaces that truthfully rather than hiding it behind a
    spinner.

    invoke_streaming() is synchronous (boto3 is blocking), so it runs on
    a background thread; a Queue bridges its on_event() calls to this
    async generator via anyio.to_thread so the blocking queue.get() call
    doesn't block the server's event loop.

    A cancelled Event plus a polled (not blocking) queue read is what
    lets this detect a client disconnect (tab closed mid-investigation)
    and stop the worker thread instead of letting it run a real, billed
    Bedrock investigation to completion for nobody.
    """
    body = await request.json()
    prompt = body.get("prompt", "").strip()
    if not prompt:
        return JSONResponse({"error": "prompt is required"}, status_code=400)
    model_id = body.get("model_id") or None
    thinking_budget = body.get("thinking_budget") or None

    events: queue.Queue = queue.Queue()
    cancelled = threading.Event()

    def wrapped_on_event(event):
        if cancelled.is_set():
            raise RuntimeError("client disconnected")
        events.put(event)

    def _worker():
        try:
            result = invoke_streaming(
                prompt, on_event=wrapped_on_event, model_id=model_id, thinking_budget=thinking_budget
            )
            events.put({"type": "done", "session_id": result["session_id"]})
        except Exception as e:
            if not cancelled.is_set():
                events.put({"type": "error", "message": str(e)})
        finally:
            events.put(_DONE)

    threading.Thread(target=_worker, daemon=True).start()

    async def event_stream():
        try:
            while True:
                if await request.is_disconnected():
                    cancelled.set()
                    return
                try:
                    event = await anyio.to_thread.run_sync(lambda: events.get(timeout=0.5))
                except queue.Empty:
                    continue
                if event is _DONE:
                    return
                yield f"data: {json.dumps(event)}\n\n"
        finally:
            cancelled.set()

    return StreamingResponse(event_stream(), media_type="text/event-stream")


async def api_suggested_prompts(request: Request):
    """Curated subset of tests/eval_scenarios.json — one source of truth
    for "questions the plan recommends," not a second hardcoded list."""
    scenarios = json.loads(SCENARIOS_PATH.read_text())
    curated_ids = {1, 5, 6, 8}  # regression, negative case, healthy check, comparison
    questions = [s["question"] for s in scenarios if s["id"] in curated_ids]
    return JSONResponse(questions)


app = Starlette(
    routes=[
        Route("/", root_redirect, methods=["GET"]),
        Route("/chat", chat_index, methods=["GET"]),
        Route("/chat.js", chat_js, methods=["GET"]),
        Route("/mcp-client.js", mcp_client_js, methods=["GET"]),
        Route("/api/chat", api_chat, methods=["POST"]),
        Route("/api/suggested-prompts", api_suggested_prompts, methods=["GET"]),
    ]
)

if __name__ == "__main__":
    import os

    import uvicorn

    # Port overridable so tests/test_http_routes.py can boot a throwaway
    # instance without colliding with a real dev.server session already
    # running on the default port.
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("WEB_SERVER_PORT", "8788")))

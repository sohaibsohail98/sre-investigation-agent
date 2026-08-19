"""The chat frontend — decoupled from mcp_server/, which is strictly the
MCP protocol server + metrics data access, reusable by any agent client.
This process owns only: GET /chat, GET /chat.js, GET /mcp-client.js,
POST /api/chat, GET /api/suggested-prompts. Imports agent/app.py's
invoke_streaming() as a plain client — same relationship any other caller
has to the agent, no special access.

Run from repo root (alongside mcp_server.server, on a different port):
    uv run python -m web.server
"""

import datetime
import hashlib
import json
import os
import queue
import secrets
import threading
from pathlib import Path

import anyio
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route

from agent.app import invoke_streaming
from mcp_server.google_auth import InvalidGoogleToken, verify_credential
from web import demo_replay

WEB_DIR = Path(__file__).parent
SCENARIOS_PATH = Path(__file__).parent.parent / "tests" / "eval_scenarios.json"

# Overridable so a deployed chat UI can point at a deployed mcp_server
# instance instead of localhost — see mcp_client_js() below.
MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL", "http://127.0.0.1:8787")

# Public deployment default: replay canned investigations (web/demo_replay.py)
# instead of live, billed Bedrock calls. Unset (the local dev default) means
# every /api/chat request behaves exactly as before this feature existed —
# no gating, no sign-in, straight to invoke_streaming.
DEMO_MODE = bool(os.environ.get("DEMO_MODE"))
LIVE_DAILY_CAP = int(os.environ.get("LIVE_DAILY_CAP", "20"))

# In-memory, process-local — an abuse control on the public demo deployment,
# not a billing-grade system. Resets on cold start / across the up-to-3
# Cloud Run instances (--max-instances 3), which is an accepted looseness
# for "keep the count roughly sane," not a hard guarantee.
_LIVE_SESSIONS = {}  # live_token -> Google `sub`
_DAILY_REQUEST_COUNTS = {}  # (sub, "YYYY-MM-DD") -> count

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
    content = (WEB_DIR / "mcp-client.js").read_text()
    content = content.replace(
        'const MCP_URL = "http://127.0.0.1:8787/mcp";',
        f'const MCP_URL = "{MCP_SERVER_URL}/mcp";',
    )
    resp = Response(content, media_type="application/javascript")
    resp.headers["Cache-Control"] = "no-store"
    return resp


async def healthz(request: Request):
    return JSONResponse({"status": "ok"})


async def api_config(request: Request):
    """Tells chat.js whether to show the demo-mode banner and, if live
    mode is available (Google sign-in configured), a sign-in control. In
    local dev (DEMO_MODE unset) demo_mode/live_available are always
    False, so chat.js's default UI is unaffected. google_client_id is
    exposed unconditionally (not gated by DEMO_MODE) — it's also used by
    the MCP connect panel's "Sign in with Google" option, which is
    useful in local dev too, not just the public demo deployment. Not a
    secret (see docs/AUTH.md) — safe to expose client-side either way."""
    return JSONResponse(
        {
            "demo_mode": DEMO_MODE,
            "live_available": DEMO_MODE and bool(os.environ.get("GOOGLE_OAUTH_CLIENT_ID")),
            "google_client_id": os.environ.get("GOOGLE_OAUTH_CLIENT_ID"),
        }
    )


def _daily_cap_remaining(sub):
    key = (sub, datetime.date.today().isoformat())
    return LIVE_DAILY_CAP - _DAILY_REQUEST_COUNTS.get(key, 0)


def _record_live_request(sub):
    key = (sub, datetime.date.today().isoformat())
    _DAILY_REQUEST_COUNTS[key] = _DAILY_REQUEST_COUNTS.get(key, 0) + 1


async def auth_verify(request: Request):
    """Mints a short-lived live-mode session for a verified Google
    identity — mirrors mcp-context-inspector's /auth/verify, reusing its
    same verify_credential() (mcp_server is one of the pinned dependency's
    exposed packages). Not the MCP auth token — this only gates the demo
    deployment's live-Bedrock path, a separate concern from MCP access."""
    if not DEMO_MODE:
        return JSONResponse({"error": "live mode is only relevant in demo deployments"}, status_code=404)
    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    if not client_id:
        return JSONResponse({"error": "GOOGLE_OAUTH_CLIENT_ID not configured"}, status_code=503)

    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "malformed JSON body"}, status_code=400)
    credential = body.get("credential") if isinstance(body, dict) else None
    if not credential:
        return JSONResponse({"error": "missing credential"}, status_code=400)

    try:
        identity = verify_credential(credential, client_id)
    except InvalidGoogleToken as e:
        return JSONResponse({"error": f"invalid Google credential: {e}"}, status_code=401)

    live_token = secrets.token_urlsafe(24)
    _LIVE_SESSIONS[live_token] = identity["sub"]
    return JSONResponse({"live_token": live_token, "email": identity["email"], "daily_cap": LIVE_DAILY_CAP})


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

    # Demo deployment: replay a canned investigation unless the caller
    # presents a live-mode session minted by /auth/verify (Bearer
    # live_token) and hasn't hit today's per-account cap. Local dev
    # (DEMO_MODE unset) always takes the live path, unchanged from before
    # this feature existed.
    use_live = True
    live_sub = None
    if DEMO_MODE:
        auth_header = request.headers.get("authorization", "")
        live_token = auth_header.removeprefix("Bearer ") if auth_header.startswith("Bearer ") else None
        live_sub = _LIVE_SESSIONS.get(live_token) if live_token else None
        use_live = live_sub is not None
        if use_live and _daily_cap_remaining(live_sub) <= 0:
            return JSONResponse(
                {"error": "daily live-mode request limit reached — try again tomorrow, or use demo mode"},
                status_code=429,
            )

    events: queue.Queue = queue.Queue()
    cancelled = threading.Event()

    def wrapped_on_event(event):
        if cancelled.is_set():
            raise RuntimeError("client disconnected")
        events.put(event)

    def _worker():
        try:
            if use_live:
                result = invoke_streaming(
                    prompt, on_event=wrapped_on_event, model_id=model_id, thinking_budget=thinking_budget
                )
                session_id = result["session_id"]
            else:
                demo_replay.replay_events(prompt, wrapped_on_event)
                session_id = "demo-" + hashlib.sha1(prompt.encode()).hexdigest()[:8]
            if use_live and live_sub is not None:
                _record_live_request(live_sub)
            events.put({"type": "done", "session_id": session_id})
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
        Route("/api/config", api_config, methods=["GET"]),
        Route("/auth/verify", auth_verify, methods=["POST"]),
        Route("/health", healthz, methods=["GET"]),
    ]
)

if __name__ == "__main__":
    import uvicorn

    # HOST defaults to loopback-only for local dev; Cloud Run sets PORT
    # itself and requires binding 0.0.0.0, so HOST=0.0.0.0 is set in the
    # container's env there. Port overridable so tests/test_http_routes.py
    # can boot a throwaway instance without colliding with a real
    # dev.server session already running on the default port.
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", os.environ.get("WEB_SERVER_PORT", "8788")))
    uvicorn.run(app, host=host, port=port)

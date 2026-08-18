"""HTTP route smoke test — boots both real servers as subprocesses and
hits every route that doesn't require a live Bedrock call, asserting the
status code actually returned. Free and offline (no AWS calls): every
route checked here either serves a static file, reads metrics/store.py
(SQLite, isolated via a temp DB), or is a pure routing decision (the "/"
redirect).

This exists because of a real gap the automated pipeline had: pytest
tests Python functions in isolation and never makes an HTTP request;
tests/preflight_bedrock.py checks AWS credentials, not the local web
server's routes; tests/browser_test_chat.py does exercise real routes in
a real browser, but costs a live Bedrock call and isn't run
automatically by scripts/dev_server.py. A route rename (the /chat move
from :8787 to :8788 during the web/ split) or a route being dropped
entirely could ship with a fully green `pytest` run and nobody would
know until a human hit the URL. This test is the offline layer that
would have caught that class of bug.
"""

import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

MCP_PORT = 18797
WEB_PORT = 18798
STARTUP_TIMEOUT_S = 15
TEST_MCP_AUTH_TOKEN = "http-routes-test-fixed-token"  # noqa: S105 — test-only, not a real secret


def _wait_until_up(url, name):
    deadline = time.time() + STARTUP_TIMEOUT_S
    last_err = None
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except urllib.error.HTTPError:
            return  # server is up and answering, even if this URL 404s/405s
        except (urllib.error.URLError, ConnectionRefusedError) as e:
            last_err = e
            time.sleep(0.3)
    raise RuntimeError(f"{name} did not come up at {url} within {STARTUP_TIMEOUT_S}s: {last_err}")


@pytest.fixture(scope="module")
def running_servers():
    """Real subprocesses, real HTTP — not TestClient — because the thing
    under test is "did we register this route on the real running app,"
    which importing the ASGI app in-process would trivially still get
    right even if e.g. the __main__ block's port wiring were broken.

    Every route this file checks is a read or a static file (never
    /api/chat, which would need a live Bedrock call) — so this runs
    against the developer's real data/metrics.db read-only, same as any
    other local read, with no isolation needed."""
    import os

    env = {
        **os.environ,
        "MCP_SERVER_PORT": str(MCP_PORT),
        "WEB_SERVER_PORT": str(WEB_PORT),
        "MCP_AUTH_TOKEN": TEST_MCP_AUTH_TOKEN,
    }
    mcp_proc = subprocess.Popen(
        [sys.executable, "-m", "mcp_server.server"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    web_proc = subprocess.Popen(
        [sys.executable, "-m", "web.server"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        _wait_until_up(f"http://127.0.0.1:{MCP_PORT}/api/sessions", "mcp_server.server")
        _wait_until_up(f"http://127.0.0.1:{WEB_PORT}/chat", "web.server")
        yield
    finally:
        for proc in (mcp_proc, web_proc):
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def _status(url, method="GET", headers=None):
    req = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        return urllib.request.urlopen(req, timeout=5).status
    except urllib.error.HTTPError as e:
        return e.code


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/", 200),  # redirects to /chat — urlopen follows redirects by default
        ("/chat", 200),
        ("/chat.js", 200),
        ("/mcp-client.js", 200),
        ("/api/suggested-prompts", 200),
    ],
)
def test_web_server_routes(running_servers, path, expected):
    assert _status(f"http://127.0.0.1:{WEB_PORT}{path}") == expected


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/api/sessions", 200),
        ("/api/tool-metrics", 200),
        ("/api/cost", 200),
    ],
)
def test_mcp_server_rest_routes(running_servers, path, expected):
    """REST routes now require the same bearer token as /mcp (see
    mcp-context-inspector's MultiTokenAuthMiddleware) — they used to be
    open, which stopped being safe once this server is meant to be
    handed out to other people, not just used solo."""
    headers = {"Authorization": f"Bearer {TEST_MCP_AUTH_TOKEN}"}
    assert _status(f"http://127.0.0.1:{MCP_PORT}{path}", headers=headers) == expected


@pytest.mark.parametrize("path", ["/api/sessions", "/api/tool-metrics", "/api/cost"])
def test_mcp_server_rest_routes_require_auth(running_servers, path):
    """Pins the actual regression this exists to catch: these routes
    must reject an unauthenticated request, not silently stay open."""
    assert _status(f"http://127.0.0.1:{MCP_PORT}{path}") == 401


def test_old_pre_restructure_chat_url_is_gone(running_servers):
    """Pins the exact regression that prompted this test file: /chat used
    to live on mcp_server's port before the web/ split. If it ever comes
    back (e.g. a bad merge resurrects the old route), catch it — a
    visitor hitting the old bookmarked URL should 404, not silently serve
    stale content from two different apps."""
    assert _status(f"http://127.0.0.1:{MCP_PORT}/chat") == 404

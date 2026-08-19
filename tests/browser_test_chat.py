"""Real-browser regression test for the /chat page — the thing curl and
server-log inspection literally cannot catch: whether the DOM actually
renders the streamed response. Starts web.server as a real subprocess,
drives a real headless Chromium tab via Playwright, clicks a suggested
prompt, and asserts on what a human would actually see: tool-call cards
appearing live, then a final answer bubble. Also exercises the model
selector, thinking toggle, and MCP metrics panel toggle (which needs
mcp_server.server also running, since it does a real cross-process MCP
handshake).

Not a pytest test (deliberately, matching tests/run_eval.py's pattern —
this costs a real Bedrock invocation and needs a browser binary). Run
directly:

    uv run python -m tests.browser_test_chat

Requires: `uv pip install playwright && playwright install chromium`
(playwright itself is in requirements-dev.txt; the browser binary is a
separate one-time download, not a Python package).
"""

import os
import subprocess
import sys
import time

from playwright.sync_api import sync_playwright

CHAT_URL = "http://127.0.0.1:8788/chat"
SERVER_STARTUP_TIMEOUT_S = 15


def _wait_for_server(proc, url, name):
    """Poll until the server is actually accepting connections, or the
    process dies trying (e.g. port already in use) — fail fast and
    loud rather than a confusing timeout with no explanation."""
    import urllib.error
    import urllib.request

    deadline = time.time() + SERVER_STARTUP_TIMEOUT_S
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"{name} exited early (code {proc.returncode}).")
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except urllib.error.HTTPError:
            return  # server is up and answering, even if this URL 401s/404s
        except (urllib.error.URLError, ConnectionRefusedError):
            time.sleep(0.3)
    raise RuntimeError(f"{name} did not respond at {url} within {SERVER_STARTUP_TIMEOUT_S}s")


TEST_MCP_AUTH_TOKEN = "browser-test-fixed-token"  # noqa: S105 — test-only, not a real secret


def main():
    print("Starting mcp_server.server + web.server as subprocesses...")
    # A fixed token via env var, not the auto-generated one mcp_server.server
    # prints to its console on a real run — this test needs to type a known
    # value into the connect form, and parsing it back out of the
    # subprocess's stdout would be more fragile than just setting it.
    mcp_env = {**os.environ, "MCP_AUTH_TOKEN": TEST_MCP_AUTH_TOKEN}
    mcp_server = subprocess.Popen(
        [sys.executable, "-m", "mcp_server.server"],
        env=mcp_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    server = subprocess.Popen(
        [sys.executable, "-m", "web.server"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_for_server(mcp_server, "http://127.0.0.1:8787/api/sessions", "mcp_server.server")
        _wait_for_server(server, CHAT_URL, "web.server")
        print("Servers ready. Launching headless Chromium...")

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()

            console_errors = []
            page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
            page.on("pageerror", lambda exc: console_errors.append(str(exc)))

            page.goto(CHAT_URL)

            # Static capability picker: chips pre-fill the input for
            # editing, they must never auto-send like the suggestion
            # buttons below do.
            print("Checking static capability picker...")
            page.wait_for_selector('[data-role="capability-button"]', timeout=5_000)
            capability_buttons = page.locator('[data-role="capability-button"]')
            assert capability_buttons.count() > 0, "no capability picker buttons rendered"
            first_capability_query = capability_buttons.first.get_attribute("title")
            capability_buttons.first.click()
            assert page.locator("#prompt-input").input_value() == first_capability_query, (
                "capability chip did not pre-fill the input with its query"
            )
            assert page.locator('[data-role="user-message"]').count() == 0, (
                "capability chip must only pre-fill the input, not send it"
            )
            page.fill("#prompt-input", "")
            print("✅ Capability picker pre-fills the input without sending.")

            print("Waiting for suggested prompts to load...")
            page.wait_for_selector("#suggestions button", timeout=10_000)
            suggestion_buttons = page.locator("#suggestions button")
            assert suggestion_buttons.count() > 0, "no suggested-prompt buttons rendered"

            first_prompt_text = suggestion_buttons.first.get_attribute("title")
            print(f"Clicking suggested prompt: {first_prompt_text!r}")
            suggestion_buttons.first.click()

            # The user message should appear immediately (no network wait).
            page.wait_for_selector('[data-role="user-message"]', timeout=3_000)
            user_msg_text = page.locator('[data-role="user-message"]').inner_text()
            assert first_prompt_text in user_msg_text, "user's own prompt didn't render"
            print("✅ User message rendered.")

            # The loading indicator must appear right after the user
            # message — the "request sent" confirmation the user sees
            # between sending a message and the first tool card appearing.
            page.wait_for_selector('[data-role="loading-indicator"]', timeout=2_000)
            print("✅ Loading indicator appeared immediately after sending.")

            # This is the actual thing being tested: does AT LEAST ONE
            # live tool-call card appear WHILE the agent is still working,
            # before the final answer — i.e. is there genuine streaming
            # visibility, not just a final blob at the end.
            print("Waiting for a live tool-call card to appear (streaming)...")
            page.wait_for_selector('[data-role="tool-card"]', timeout=20_000)
            first_card_text = page.locator('[data-role="tool-card"]').first.inner_text()
            print(f"✅ Tool-call card rendered live: {first_card_text!r}")

            # Now wait for the final agent answer.
            print("Waiting for the final answer to render...")
            page.wait_for_selector('[data-role="final-answer"]', timeout=30_000)
            answer_locator = page.locator('[data-role="final-answer"]').first
            answer_text = answer_locator.inner_text()
            assert len(answer_text.strip()) > 0, "final answer is empty"
            print(f"✅ Final answer rendered ({len(answer_text)} chars).")

            # The answer is markdown (bold, tables) — it must render as
            # real HTML, not show raw "**" / "|" syntax to the user.
            answer_html = answer_locator.inner_html()
            assert "**" not in answer_html, "raw markdown syntax leaked into the rendered answer"
            print("✅ Markdown rendered as HTML, not raw syntax.")

            # At least one tool card should have resolved (not stuck
            # "pending") by the time the final answer is in — this is
            # the exact class of bug this test exists to catch.
            pending_count = page.locator('[data-role="tool-card"][data-status="pending"]').count()
            resolved_count = page.locator(
                '[data-role="tool-card"][data-status="ok"], [data-role="tool-card"][data-status="error"]'
            ).count()
            print(f"Tool cards: {resolved_count} resolved, {pending_count} still pending.")
            assert resolved_count > 0, "no tool-call card ever resolved — stuck in pending state"

            # The loading indicator must be gone by the time the final
            # answer has rendered — it should never linger alongside
            # real content.
            leftover_loading = page.locator('[data-role="loading-indicator"]').count()
            assert leftover_loading == 0, "loading indicator still present after final answer rendered"
            print("✅ Loading indicator cleared once real content arrived.")

            # Toggle the MCP metrics panel on — this only reveals the
            # connect form, it does not connect by itself. Confirm that
            # boundary, then actually connect — a real cross-process MCP
            # Streamable-HTTP handshake against mcp_server.server,
            # authenticated with the fixed test token, not a mock.
            print("Toggling MCP server panel on...")
            page.locator('[data-role="mcp-toggle"]').check()
            page.wait_for_selector('[data-role="mcp-panel"]:not(.hidden)', timeout=2_000)
            assert page.locator('[data-role="mcp-status"]').inner_text() == "disconnected", (
                "toggling on must not auto-connect"
            )
            print("✅ Toggle reveals the connect form without auto-connecting.")

            # Manual token entry no longer exists in the UI (chat.html's
            # mcp-connect-form is Google-sign-in-only now — see its
            # comment for why) — a real Google credential can't be
            # scripted here, so this calls the same connectMcp(token)
            # function the Google-authorize flow itself calls, directly,
            # to still exercise the real cross-process MCP handshake
            # mechanics end to end.
            page.evaluate("(token) => connectMcp(token)", TEST_MCP_AUTH_TOKEN)
            page.wait_for_function(
                "document.querySelector('[data-role=\"mcp-status\"]').textContent === 'connected'",
                timeout=10_000,
            )
            print("✅ Real, authenticated MCP handshake succeeded.")
            page.wait_for_selector('[data-role="mcp-data"]:not(.hidden)', timeout=2_000)
            print("✅ MCP metrics data visible after connecting.")

            # Model selector and thinking toggle are present and usable —
            # not asserting on their downstream effect here (that would
            # need a second, more expensive live invocation); this test's
            # job is confirming the controls exist and don't break the
            # page.
            page.locator('[data-role="model-select"]').select_option("us.anthropic.claude-haiku-4-5-20251001-v1:0")
            page.locator('[data-role="thinking-toggle"]').check()
            print("✅ Model selector and thinking toggle are interactive.")

            page.locator('[data-role="mcp-toggle"]').uncheck()
            page.wait_for_selector('[data-role="mcp-panel"].hidden', timeout=2_000)
            print("✅ MCP panel hides cleanly on toggle-off.")

            if console_errors:
                print("⚠️  Browser console errors captured during the run:")
                for err in console_errors:
                    print(f"    {err}")

            browser.close()

        print("\n✅ PASS — response rendering confirmed in a real browser.")
        return 0

    except Exception as e:
        print(f"\n❌ FAIL — {e}")
        return 1
    finally:
        for proc in (server, mcp_server):
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    sys.exit(main())

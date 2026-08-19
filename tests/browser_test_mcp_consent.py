"""Real-browser regression test for the MCP consent screen — the
Google-sign-in -> review -> Authorize/Cancel flow in web/chat.js
(onMcpGoogleSignIn / cancelMcpConsent / authorizeMcpConsent).

This was previously only verified manually (Playwright, driven by hand,
with a fake-but-well-formed JWT) and never turned into a committed,
repeatable test — this file is that test. It complements
browser_test_chat.py, which already covers a real authenticated
handshake by calling `connectMcp(token)` directly (see that file's
comment on why: chat.html's MCP connect form is Google-sign-in-only, so
a real Google credential can't be scripted here either). That existing
coverage is not duplicated below.

What this file adds, that browser_test_chat.py does not cover:
  - The consent screen actually renders (right identity text, connect
    form hidden) once a Google credential comes back — driven by
    calling `onMcpGoogleSignIn()` directly with a fake-but-well-formed
    JWT, the same technique connectMcp(token) uses to bypass the real
    Google Identity Services button (which can't be scripted).
  - Clicking Cancel resets to the disconnected state and mints nothing:
    no MCP client, no pending credential, connect form visible again —
    proving Authorize is the *only* point that ever calls /auth/verify,
    per authorizeMcpConsent()'s own comment in chat.js.

Does NOT hit mcp_server.server at all (no /auth/verify network call
happens on this path) — only web.server needs to be running.

Not a pytest test (deliberately, matching browser_test_chat.py's own
pattern — this needs a real browser binary). Run directly:

    uv run python -m tests.browser_test_mcp_consent

Requires: `uv pip install playwright && playwright install chromium`
(see browser_test_chat.py's own docstring — same dev dependency).
"""

import base64
import json
import subprocess
import sys
import time

from playwright.sync_api import sync_playwright

CHAT_URL = "http://127.0.0.1:8788/chat"
SERVER_STARTUP_TIMEOUT_S = 15


def _wait_for_server(proc, url, name):
    """Same polling helper as browser_test_chat.py's _wait_for_server —
    duplicated rather than imported, since this file is meant to be
    runnable standalone without depending on browser_test_chat.py's
    module-level side effects (it isn't a package-internal helper)."""
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


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _fake_but_well_formed_jwt(email: str) -> str:
    """A JWT-shaped string that satisfies chat.js's
    decodeJwtPayloadForDisplay(): three dot-separated segments, with the
    middle one base64url-encoded JSON carrying an `email` claim. The
    signature is never checked client-side (only server-side, by the
    inspector's real /auth/verify, which this test never calls) — see
    decodeJwtPayloadForDisplay's own comment in chat.js: it's DISPLAY
    only, not verification. Header and signature are plausible-looking
    but not cryptographically real, matching the technique used for the
    manual Playwright verification this test replaces."""
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    payload = _b64url(
        json.dumps(
            {
                "iss": "https://accounts.google.com",
                "sub": "1234567890",
                "email": email,
                "email_verified": True,
                "name": "Test User",
                "exp": int(time.time()) + 3600,
                "iat": int(time.time()),
            }
        ).encode()
    )
    signature = _b64url(b"not-a-real-signature-display-only")
    return f"{header}.{payload}.{signature}"


def main():
    print("Starting web.server as a subprocess...")
    server = subprocess.Popen(
        [sys.executable, "-m", "web.server"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_for_server(server, CHAT_URL, "web.server")
        print("Server ready. Launching headless Chromium...")

        fake_email = "consent-test@example.com"
        fake_jwt = _fake_but_well_formed_jwt(fake_email)

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()

            console_errors = []
            page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
            page.on("pageerror", lambda exc: console_errors.append(str(exc)))

            page.goto(CHAT_URL)

            print("Toggling MCP server panel on...")
            page.locator('[data-role="mcp-toggle"]').check()
            page.wait_for_selector('[data-role="mcp-panel"]:not(.hidden)', timeout=2_000)
            assert page.locator('[data-role="mcp-connect-form"]').is_visible(), (
                "connect form must be visible before any sign-in"
            )
            assert not page.locator('[data-role="mcp-consent"]').is_visible(), (
                "consent screen must not be visible before sign-in"
            )
            print("✅ Panel opens on the connect form, consent hidden.")

            # A real Google Identity Services button can't be scripted
            # (same reasoning as browser_test_chat.py's connectMcp(token)
            # call) — onMcpGoogleSignIn() is the exact function GSI's
            # callback invokes with the credential response, so calling
            # it directly with our fake-but-well-formed JWT drives the
            # identical code path a real sign-in would.
            print("Driving onMcpGoogleSignIn() with a fake-but-well-formed JWT...")
            page.evaluate("(jwt) => onMcpGoogleSignIn({ credential: jwt })", fake_jwt)

            page.wait_for_selector('[data-role="mcp-consent"]:not(.hidden)', timeout=2_000)
            assert not page.locator('[data-role="mcp-connect-form"]').is_visible(), (
                "connect form must hide once the consent screen appears"
            )
            identity_text = page.locator('[data-role="mcp-consent-identity"]').inner_text()
            assert fake_email in identity_text, (
                f"consent screen identity text {identity_text!r} does not mention {fake_email!r}"
            )
            print(f"✅ Consent screen appeared with correct identity: {identity_text!r}")

            # Nothing must have been minted or connected yet — Authorize
            # is the only point that calls /auth/verify.
            pending_before_authorize = page.evaluate("() => typeof pendingMcpCredential")
            assert pending_before_authorize != "undefined", "pendingMcpCredential should exist as a variable"
            mcp_client_before = page.evaluate("() => mcpClient")
            assert mcp_client_before is None, "mcpClient must still be null before Authorize is clicked"
            print("✅ Nothing minted or connected while consent is still pending.")

            print("Clicking Cancel...")
            page.locator('[data-role="mcp-consent-cancel"]').click()

            page.wait_for_function(
                "document.querySelector('[data-role=\"mcp-consent\"]').classList.contains('hidden')",
                timeout=2_000,
            )
            assert page.locator('[data-role="mcp-connect-form"]').is_visible(), (
                "Cancel must reveal the connect form again"
            )
            assert page.locator('[data-role="mcp-status"]').inner_text() == "disconnected", (
                "Cancel must reset status to disconnected"
            )
            pending_after_cancel = page.evaluate("() => pendingMcpCredential")
            assert pending_after_cancel is None, "Cancel must clear pendingMcpCredential — nothing left minted"
            mcp_client_after = page.evaluate("() => mcpClient")
            assert mcp_client_after is None, "Cancel must not have created an mcpClient"
            print("✅ Cancel reset to disconnected state with nothing minted.")

            if console_errors:
                print("⚠️  Browser console errors captured during the run:")
                for err in console_errors:
                    print(f"    {err}")

            browser.close()

        print("\n✅ PASS — MCP consent screen render + Cancel reset confirmed in a real browser.")
        return 0

    except Exception as e:
        print(f"\n❌ FAIL — {e}")
        return 1
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()


if __name__ == "__main__":
    sys.exit(main())

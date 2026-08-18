"""Runs pytest + a live Bedrock preflight check before starting the two
local servers (mcp_server on 8787, web on 8788); aborts without starting
either if any check fails. Use this instead of bare
`python -m mcp_server.server` / `python -m web.server`:

    uv run python -m scripts.dev_server
"""

import subprocess
import sys
import time


def run(description, cmd):
    print(f"\n─── {description} " + "─" * max(0, 40 - len(description)))
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"\n❌ {description} failed — not starting the servers.")
        sys.exit(result.returncode)


def main():
    run("Unit tests (pytest)", [sys.executable, "-m", "pytest", "-q"])
    run("Bedrock preflight check", [sys.executable, "-m", "tests.preflight_bedrock"])

    print("\n─── Starting mcp_server.server (:8787) + web.server (:8788) " + "─" * 5)
    # Two separate processes, not one execv — web.server needs
    # mcp_server.server already listening (the chat panel's MCP handshake
    # calls out to it), and neither should die silently if the other is
    # still logging. If either exits on its own, tear the other down too
    # rather than leaving one server running headless.
    procs = {
        "mcp_server.server": subprocess.Popen([sys.executable, "-m", "mcp_server.server"]),
        "web.server": subprocess.Popen([sys.executable, "-m", "web.server"]),
    }

    try:
        while True:
            for name, proc in procs.items():
                code = proc.poll()
                if code is not None:
                    print(f"\n❌ {name} exited (code {code}) — shutting down the other server.")
                    for other in procs.values():
                        other.terminate()
                    sys.exit(code)
            time.sleep(1)
    except KeyboardInterrupt:
        for proc in procs.values():
            proc.terminate()


if __name__ == "__main__":
    main()

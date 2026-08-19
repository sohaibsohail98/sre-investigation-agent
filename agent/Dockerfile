# AgentCore Runtime requires linux/arm64. Build from the repo root:
#   docker buildx build --platform linux/arm64 -f agent/Dockerfile -t <tag> --push .

FROM --platform=linux/arm64 python:3.13-slim

WORKDIR /app

# git is needed by pip to clone agent/requirements.txt's
# mcp-context-inspector dependency (a git+https pin, not a PyPI
# package) — python:3.13-slim doesn't include it. Not needed before
# this dependency existed, so this was never caught until now.
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY agent/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent/ ./agent/
COPY tools/ ./tools/
COPY data/ ./data/
# common/ is imported directly (common.config) by both agent/app.py and
# agent/runtime.py — was missing from this image entirely (a pre-existing
# gap, never caught until the deploy pipeline's CI wiring actually got
# fixed and this build ran for real for the first time). metrics/ used
# to be a local package copied in here too, before the repo split moved
# it into mcp-context-inspector — it's now installed via
# requirements.txt's git dependency instead, not a local directory.
COPY common/ ./common/

# AgentCore contract: HTTP on 8080, POST /invocations + GET /ping.
EXPOSE 8080
CMD ["python", "-m", "agent.app"]

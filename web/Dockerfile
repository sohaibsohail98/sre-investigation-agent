# Cloud Run deployment of the chat UI. Build from the repo root:
#   docker build -f web/Dockerfile -t <tag> .

FROM python:3.13-slim

WORKDIR /app

# git is needed by pip to clone web/requirements.txt's
# mcp-context-inspector dependency (a git+https pin, not a PyPI
# package) — python:3.13-slim doesn't include it.
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY web/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent/ ./agent/
COPY tools/ ./tools/
COPY data/ ./data/
COPY common/ ./common/
COPY web/ ./web/
COPY tests/eval_scenarios.json ./tests/eval_scenarios.json

ENV HOST=0.0.0.0
EXPOSE 8080
CMD ["python", "-m", "web.server"]

# AgentCore Runtime requires linux/arm64. Build from the repo root:
#   docker buildx build --platform linux/arm64 -f agent/Dockerfile -t <tag> --push .

FROM --platform=linux/arm64 python:3.13-slim

WORKDIR /app

COPY agent/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent/ ./agent/
COPY tools/ ./tools/
COPY data/ ./data/
COPY metrics/ ./metrics/

# AgentCore contract: HTTP on 8080, POST /invocations + GET /ping.
EXPOSE 8080
CMD ["python", "-m", "agent.app"]

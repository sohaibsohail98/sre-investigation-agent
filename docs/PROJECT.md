# Project — SRE Investigation Agent (AgentCore + MCP Observability)

See `README.md` for "how do I run it," `CLAUDE.md` for working
conventions, this file for the design reasoning and history behind them.

## What this is

An AI agent that investigates incidents in a mocked-up infrastructure
environment (4 services, deterministic metrics/logs/deployments/cost
data), deployed on **Amazon Bedrock AgentCore Runtime**, with its own
**MCP server** exposing per-investigation cost/token/tool-call metrics,
and a live-streaming chat UI that can open a real MCP handshake to show
those metrics in an embedded panel. The goal is demonstrating the
platform layer around agents — observability, evaluation, tool-calling,
deployment — not just "call an LLM with tools."

## Current state

**v1 (Phases 1–5) is built, tested, and deployed.** Concretely:

- Local dev loop (Gradio) and a web chat UI, both hitting the same
  `agent/app.py` function the deployed agent uses
- 8/8 deterministic eval scenarios passing (`tests/run_eval.py`, real
  Bedrock cost — run before committing a prompt/tool change)
- 37 unit tests passing (`pytest`, no live AWS calls) covering the
  bugs described below, so they can't silently regress
- MCP server (8 tools — 7 read-only plus `record_session`, a write tool
  so a per-user-authenticated caller can record its own sessions,
  including the Context Window Explorer's `get_context_timeline` — now
  the standalone `mcp-context-inspector` package, see "Repo layout"
  above) + chat UI (`web/`, separate process/port), the latter
  live-streaming tool calls as they happen (see "Chat streaming" below)
  and able to open a real MCP handshake for an embedded metrics panel
- Deployed to AgentCore Runtime, DynamoDB-backed, confirmed working via
  live invocation (check `terraform output agent_runtime_arn` for the
  current ARN — it changes on redeploy)
- CI (`.github/workflows/tests.yml` + `deploy-agentcore.yml`) fully
  wired up and passing end to end — OIDC trust policy, repo secret, and
  a batch of Terraform-refresh IAM permissions the original setup
  missed (see docs section below)

**Not built**: Phase 7 (an Azure AI Foundry port, for an AWS-vs-Azure
comparison) hasn't started — deliberately deferred until Phase 5 has
been used for real.

## Repo layout

```
agent/              runtime.py (boilerplate loop, rarely touched) +
                     app.py (this agent's config) + system_prompt.txt
tools/               5 SRE investigation tools — live-query DynamoDB
                     (tools/_common.py), not local files (see below)
data/                *.json — the single source of truth for content,
                     seeded into DynamoDB by Terraform; tools don't read
                     these directly, only Terraform does
web/                 chat UI (:8788) — server.py + chat.html/js +
                     mcp-client.js (SSE-streamed tool-call visibility,
                     real MCP handshake for the metrics panel)
tests/               eval_scenarios.json + run_eval.py (live) +
                     test_*.py (pytest, no live calls) + conftest.py
learn/               local_chat.py — Gradio dev loop
terraform/           ECR + IAM + DynamoDB + the AgentCore runtime +
                     ci.tf (GitHub Actions OIDC role)
.github/workflows/   deploy-agentcore.yml
docs/PROJECT.md       this file
```

`mcp_server/` and `metrics/` (the MCP server and its storage layer) used
to live here; they're now the standalone
[`mcp-context-inspector`](https://github.com/sohaibsohail98/mcp-context-inspector)
package (see that repo's own README for the reasoning) — a regular
dependency via `requirements-dev.txt` / `agent/requirements.txt`, pinned
to a commit SHA rather than floating on `@main`. Same import paths
(`from metrics.store import record_session`), same `python -m
mcp_server.server` command; only where the code physically lives
changed.

## Architecture and key decisions

### The boilerplate/config split

`agent/runtime.py` holds the generic Converse-API tool-calling loop
(dispatch by name, catch tool exceptions, record trace/tokens/latency,
emit progress events). `agent/app.py` + `tools/` + `system_prompt.txt`
are this specific agent's configuration. `runtime.py` should rarely
change; `app.py` is what would look different for a different use case.

This split was researched against AWS's own reference code (the
official "no-CLI" getting-started guide, `awslabs/agentcore-samples`,
the `bedrock-agentcore-starter-toolkit` scaffold, AWS's Converse-API
tool-use demo) before being built — **AWS has no established convention
for this split**; their own examples are single-file per agent. This is
being invented for reuse, not copying an AWS pattern. Two things that
*did* inform the split: AWS's own tool-use demo puts each tool in its
own module exposing a `get_tool_spec()` function (what `tools/` follows),
and Strands (AWS's higher-level agent framework, deliberately not used
here so the loop stays hand-understood) draws the same
boilerplate/config boundary via its `@tool` decorator and
`Agent(tools=, system_prompt=)` — independent confirmation this is the
right line to draw.

### System prompt — principled, not scripted

`agent/system_prompt.txt` states the agent's role and what good
investigation looks like; it doesn't mandate a fixed tool-call order.
This is deliberate: scripting the order (metrics → logs → deployments,
always) would make the eval suite's negative-case scenario trivial
rather than a real test of investigation vs. pattern-matching (see
`checkout-api` below). One consequence: after the first live eval run,
the model's habit of probing logs with many single-word `search_logs`
calls hit the turn limit on that scenario — fixed by nudging the prompt
toward efficient searching and raising `MAX_TURNS` 6→10, not by
scripting the order.

### The synthetic data is deliberately adversarial

Four services: `payments-api` is the "obvious regression" case (a real
deployment correlates with the incident). `checkout-api` is the
deliberate negative — it looks degraded but has **no** deployment
record; the actual cause (in its logs) is a downstream dependency
timeout. An agent that correctly says "no deployment, this isn't a
regression" for `checkout-api` is demonstrating real investigation, not
reusing `payments-api`'s answer. `auth-api` and `notifications` exist so
"highest error rate" / "cheapest to operate" questions have more than
one candidate to compare.

### Tool data is live-queried, not read from local files

The 5 SRE tools originally read `data/*.json` directly. They now query a
real DynamoDB table (`sre-agent-infra`, partition key `service`, sort key
`category`) over the network — genuinely live query architecture, not a
local file read. The *content* of that data stays fixed (still seeded
from `data/*.json`, which remains the single source of truth — Terraform
reads the JSON and creates one `aws_dynamodb_table_item` per row, so the
data isn't duplicated between the files and the table). This is a
deliberate split: live query mechanism, deterministic facts, so
`tests/eval_scenarios.json`'s exact-match scoring keeps working without a
judge-based rewrite. No new hosted compute — reuses the same AgentCore
container, same pattern already proven for the metrics store
(`metrics/store_dynamodb.py`). Considered and rejected: hosting a
separate data server on ECS Fargate — technically works, but would have
been the first always-on-billed piece of infrastructure in this whole
project, breaking the $0-idle cost model everything else here holds to.

pytest never touches the real table — `tests/conftest.py`'s
`fake_infra_table` fixture (autouse) swaps in an in-memory fake seeded
from the same JSON files, so the unit suite stays free and offline.

### Storage — SQLite locally, DynamoDB once deployed, and why the swap is mandatory

AgentCore Runtime's container filesystem doesn't persist across
invocations. A SQLite file written inside the deployed container would
silently lose data between calls rather than erroring — the kind of bug
that looks fine in a demo and is actually broken. So Phase 5 deployment
is the trigger to switch backends, not an optional upgrade. Both
backends implement the same function signatures behind
`metrics/store.py`'s dispatcher (chosen via `STORAGE_BACKEND` env var,
set automatically by Terraform on the deployed runtime) — callers never
know which is active. The DynamoDB backend paginates `Scan` calls (a
single call silently truncates once a table grows past one page) and
strips its internal partition-key field so its shape matches the
SQLite backend exactly — both pinned by
`tests/test_metrics_store_dynamodb.py`.

No mounted volume (EFS/S3 access point) was ever added for chat
history — DynamoDB already serves that purpose, and running two storage
layers for the same 200 bytes of JSON isn't worth it.

### MCP server — its own thing, and why it's not named `mcp/`

`mcp_server/` (now shipped as part of the standalone
[`mcp-context-inspector`](https://github.com/sohaibsohail98/mcp-context-inspector)
package — see "Repo layout" above) is strictly the MCP protocol server +
metrics data access — it does not import `agent/` at all, and hosts no
chat/UI code. It was originally planned as `mcp/`, but a directory with
that name at the repo root shadows the installed `mcp` PyPI package the
moment anything inside it does `from mcp.server import ...`. Exposes 8
MCP tools — 7 read-only (`get_session_metrics`, `get_token_breakdown`,
`get_tool_metrics`, `get_agent_trace`, `get_cost_estimate`,
`get_recent_sessions`, `get_context_timeline`) plus `record_session`
(write, authenticated) — plus matching REST routes (a curl-friendly
debugging alternative to a full MCP handshake) — both paths call the
same underlying `metrics/store.py` functions, so there's one
data-access layer, not two. Runs with `CORSMiddleware` attached (via
`streamable_http_app()`, not the simpler `server.run()`, which doesn't
expose the underlying Starlette app) so `web/mcp-client.js`, running
from `web/server.py`'s own origin/port, can open a cross-origin MCP
handshake against it.

### `web/` — the chat UI, decoupled from the MCP server

`web/server.py` is a separate process/port (`:8788`, vs. `mcp_server`'s
`:8787`) owning only the chat routes (`/chat`, `/chat.js`,
`/mcp-client.js`, `/api/chat`, `/api/suggested-prompts`). It imports
`agent/app.py`'s `invoke_streaming()` as a plain client — the same
relationship any other caller has to the agent, not a special
dependency. This was originally hosted inside `mcp_server/` (webserver
reuse, since a server was already running there for the dashboard) but
that coupled a reusable MCP protocol server to one specific chat
frontend; splitting it out means `mcp_server/` stays connectable by any
agent, not just this one's chat UI. The old standalone `/dashboard` page
was removed entirely in the same move — superseded by the embedded MCP
panel below, not kept alongside it.

### MCP metrics panel — an embedded toggle, with a real, authenticated protocol handshake

The chat UI has an "MCP server" toggle (off by default) instead of the
old separate `/dashboard` page. The toggle only reveals a connect
form — it deliberately does not auto-connect, since the panel's data
(`get_recent_sessions` et al.) is global history from `data/metrics.db`,
not scoped to "since you connected." The chat always calls
`agent/app.py` directly, MCP toggle on or off; MCP here is read-only
observability into what already happened, never how the agent
investigates. Making "Connect" an explicit, separate action (paste a
token, click Connect, watch it report "connected" before anything
renders) keeps that boundary visible, matching how adding an MCP server
feels in Claude Desktop — a deliberate action with a visible result,
not a checkbox side effect.

The handshake itself: `web/mcp-client.js` — vanilla JS, no SDK —
performs a genuine MCP Streamable-HTTP JSON-RPC handshake (`initialize`
→ `notifications/initialized` → `tools/list`) against `mcp_server`'s
`/mcp` endpoint, tracking the `Mcp-Session-Id` response header per the
spec, and an `Authorization: Bearer <token>` header on every request.

**Auth has since evolved past a single shared secret** — now that this
MCP server (part of `mcp-context-inspector`) is meant to be handed out
to other people connecting their own LLMs/agents, not just used solo.
`mcp_server/server.py`'s `MultiTokenAuthMiddleware` (that repo) gates
`/mcp` **and** `/api/*` (REST routes used to stay open — that stopped
being safe once other people can reach this server) behind either the
owner's `MCP_AUTH_TOKEN` (unchanged, still just a plain Starlette
middleware comparing against an env var, zero setup) OR a per-user token
minted after a real Google sign-in (`/auth/login` → Google Identity
Services' credential flow → `/auth/verify` verifies the signed ID token
server-side, mints/reuses a token per Google account). Still explicitly
**not** the MCP SDK's own OAuth Resource Server support
(`MCPServer(auth=...)`), which requires a real `issuer_url` — a full
OAuth/OIDC authorization server (Cognito, Auth0, self-hosted) — and
still not a hand-rolled OAuth 2.1 authorization server (PKCE, dynamic
client registration, a consent screen — real infrastructure
disproportionate to a personal-scale server); Google Identity Services'
one-tap credential flow gets "each person authenticates as themselves,
revocably" without either. **Per-owner data isolation** on top of that:
every session is tagged with whoever recorded it (a Google `sub`, or
`None` for the server owner), and every read filters to the caller's
own data unless they're the owner token — implemented via a contextvar
(`current_owner`) set in the auth middleware and propagated through
Starlette's `BaseHTTPMiddleware` into MCP tool dispatch, verified
against a real MCP `tools/call` dispatch, not just the REST layer.
`record_session` is also now an authenticated MCP tool (not just a
direct Python import), so a friend's own remote agent can push its own
sessions in, attributed to them. Full details: `mcp-context-inspector`'s
own README, "Auth" section.

Once connected, the panel calls the same 7 MCP tools directly
(`get_recent_sessions`, `get_tool_metrics`, `get_cost_estimate`,
`get_context_timeline`, and — after a chat completes —
`get_session_metrics`/`get_token_breakdown` for the session just run) —
real protocol calls end to end, not a REST fallback, since that's more
honest to "do a handshake" than a shortcut would be. The REST routes in
`mcp_server/server.py` are kept anyway as a documented curl-debugging
alternative — authenticated the same way as `/mcp` (see the Auth
section above). Toggling off calls the client's `close()`, an
explicit `DELETE` with the session header — best-effort, since a user
closing the tab without toggling off first shouldn't be treated as an
error anywhere.

### Context Window Explorer — the MCP server's actual USP

What's unique about this server, versus just re-reading data the chat
UI already shows inline: full transparency into exactly what enters
the model's context window, block by block, with honest token
estimates — inspired by
[`code.claude.com/docs/en/context-window`](https://code.claude.com/docs/en/context-window)'s
interactive timeline (categorized, colored, hoverable blocks; a running
token total). Explicitly not copied: its play/scrub transport control —
that page is static despite feeling dynamic through the play button, and
so is this.

`agent/runtime.py`'s `run_agent_loop()` builds `context_blocks` — an
ordered list of every distinct thing that entered context during a
session (system prompt, tool specs, the user's prompt, and per turn:
reasoning/thinking text, one block per tool call, one block per tool
result, the final answer). Each block's `token_estimate` is
`char_count / 4` (`_CHARS_PER_TOKEN` in `runtime.py`) — a rough,
character-based estimate used only for this composition breakdown, and
explicitly never reconciled against the exact per-turn
`input_tokens`/`output_tokens` in `turns`, which come straight from
Bedrock's `usage` field. Every place this estimate is surfaced (the MCP
tool's docstring, the UI's running total) says "estimated," mirroring
the reference page's own "~18.1K tokens / 200K · illustrative" framing.

Storage is a new `context_blocks` table (SQLite) / `CTXBLOCK#{seq:04d}`
sort-key prefix (DynamoDB) — same shape, same dispatcher pattern as
`turns`/`tool_calls`. `get_context_timeline(session_id)` reads the raw
rows and computes `cumulative_tokens`/`cumulative_pct` in Python before
returning, against `common.config.CONTEXT_WINDOW_TOKENS` (200_000,
confirmed against Sonnet 4.6/Haiku 4.5's current model cards) — a single
source of truth for context-window math shared with `web/chat.js`. A
`tool_result` block also carries the tool's `status` ("ok"/"error") so
a failed call's block can be colored distinctly in the UI, via an
optional `status` kwarg on `_add_block` — avoids needing a client-side
join against `get_agent_trace` by turn/sequence.

The UI (`web/chat.js`'s `renderContextTimeline()`, replacing the old
flat "Current session" list) is a proportional segmented bar (one slice
per block, width = `token_estimate / CONTEXT_WINDOW_TOKENS`, so the bar
reads as "how much of the 200K window this session actually used," not
"100% filled with this session's blocks"), a category color legend, and
a clickable (not hover-only, so it works on touch) block list — each row
expands to a detail panel with the exact char count, token estimate,
running cumulative total/%, and a category description including
whether that content is ever visible in the chat itself (system/tools/
tool_call/tool_result never are; user/reasoning/answer always are;
thinking only when the Thinking toggle was on for that turn). It's
static, not live-updating during the stream — same trigger
`refreshMcpPanel()` already uses (after a chat turn finishes) — matching
the reference page's own actual (non-live) behavior; wiring it to build
live via a new `on_event` SSE type was considered and deferred as it
would mean two code paths (live SSE + stored `get_context_timeline`)
rendering the same UI, more surface area for a feature that's already
complete and honest without it.

### Chat streaming — live tool-call visibility, not a spinner

Multi-tool questions can genuinely take several seconds (each tool call
is a real Bedrock round-trip; a question needing 3–4 tools means 3–4
real sequential model calls, not simulated delay), so the chat UI
streams live tool-call visibility rather than showing a single
"Investigating…" spinner for the whole loop. An `on_event` callback on
`agent/runtime.py`'s loop (fires on every turn start, any reasoning
text, every tool call, every tool result, and the final answer) feeds a
Server-Sent Events endpoint (`/api/chat` in `web/server.py`, bridging
the blocking Bedrock calls to the async web server via a background
thread + queue), which `chat.js` renders live as events stream in. The
`/api/chat` handler polls `request.is_disconnected()` (instead of a
plain blocking queue read) and sets a `cancelled` `threading.Event()` on
disconnect, so a user closing the tab mid-investigation doesn't leave
the worker thread running a real, billed Bedrock investigation to
completion for nobody. `chat.html` also has an intro section explaining
what the agent is investigating and why multi-step questions take a
few seconds.

### Terraform state — S3 + DynamoDB backend

State was originally local (`terraform.tfstate` on one machine, no
locking, no recovery path but manual `terraform import`). Now backed by
an S3 bucket (versioned, encrypted, public access blocked) with a
DynamoDB lock table, so a new machine just needs `terraform init` and
the right AWS credentials to pick up existing state, with locking
against concurrent applies. The AgentCore runtime resource itself is
Terraform-managed too — the AWS provider is pinned to `>= 6.21`, since
`aws_bedrockagentcore_agent_runtime` doesn't exist in the `5.x` line.

### GitHub Actions CI — OIDC, no stored keys

`.github/workflows/deploy-agentcore.yml` runs the eval suite then
deploys on push to `main`. Uses AWS OIDC (`terraform/ci.tf`, referencing
an OIDC provider already present in the AWS account) rather than a
long-lived AWS key in repo secrets. Wired up and running — see
`README.md`'s CI section for the one-time setup steps a fork would need
to redo.

Notable parts of the current setup:

- **The OIDC trust policy's `sub` condition wildcards GitHub's numeric
  owner/repo ID segments** (`repo:owner@*/repo@*:ref:refs/heads/main`
  in `terraform/ci.tf`) rather than matching the plain
  `repo:owner/repo:ref:...` name format, since GitHub's actual token
  subject embeds immutable numeric IDs alongside the names.
- **The CI role has explicit read access to the `sre-agent-infra`
  DynamoDB table** the SRE tools query, plus the read/describe/list
  permissions Terraform's refresh phase needs for every resource
  already in state (ECR tags, DynamoDB backups/TTL/tags, IAM role
  policies for both the runtime role and the CI role's own
  self-referential ARN, the OIDC provider data source).
- **`agent/Dockerfile`** installs `git` (needed for
  `agent/requirements.txt`'s `mcp-context-inspector` git dependency)
  and copies `common/` into the image (imported directly by both
  `agent/app.py` and `agent/runtime.py`); it no longer copies
  `metrics/`, which now ships as part of the `mcp-context-inspector`
  package instead of a local directory.

## AWS/Bedrock gotchas — read before touching model config

Two non-obvious constraints when configuring the model ID:

1. **The `us.` inference-profile prefix is mandatory.** `converse()` /
   `converse_stream()` reject the bare model ID
   (`anthropic.claude-sonnet-4-6`, what `list-foundation-models`
   returns) with a `ValidationException`. Use
   `us.anthropic.claude-sonnet-4-6`.
2. **Sonnet 4.6 has no date suffix**, unlike Sonnet 4/4.5
   (`anthropic.claude-sonnet-4-5-20250929-v1:0`). Don't pattern-match a
   date onto it.

Model access, verified by invocation against this account:

| Model | Works? |
|---|---|
| Claude Sonnet 4.6 (`us.anthropic.claude-sonnet-4-6`) | ✅ |
| Claude Haiku 4.5 (`us.anthropic.claude-haiku-4-5-20251001-v1:0`) | ✅ |
| Sonnet 5 / Opus 5 / Opus 4.8 / Fable 5 | ❌ account-gated |

The frontier-model block is account-level eligibility, not a form or
agreement issue (agreements were accepted via API and still refuse to
invoke — `"not available for this account... contact AWS Sales"`). No
self-service unlock exists; not worth spending more time on. Bedrock
Agents (the older managed service) is also a dead end for this account
— `"in Maintenance Mode... not available for accounts without prior
service usage."` AgentCore Runtime is the correct, current path.

## Cost model

**AgentCore Runtime is purely consumption-based, verified against AWS's
live pricing page** — billed per vCPU-hour/GB-hour only while a session
is actually running. An idle-but-registered runtime costs $0; there's no
charge just for the runtime existing. The only thing that ticks
regardless of use is ECR image storage (~5p/month, negligible).
DynamoDB on-demand billing is the same story — $0 when idle. Because of
this, there's no cost-driven need to tear down between sessions (an
earlier "same-day teardown" habit from before this was confirmed is no
longer necessary — leave it deployed if useful).

## Testing

`pytest` (37 tests, `tests/test_*.py`, no live calls) covers: empty/fresh-DB
reads not crashing, DynamoDB pagination, the DynamoDB/SQLite
shape-parity contract, tool functions never returning `None` on data
drift, the runtime loop's event-emission sequence, `AWS_BEARER_TOKEN_BEDROCK`
never being able to silently hijack Bedrock auth, a pre-caching
`data/metrics.db` migrating cleanly (`CREATE TABLE IF NOT EXISTS` never
alters an existing table), and — via `tests/test_http_routes.py`, which
boots both real servers as subprocesses and hits every non-Bedrock-calling
route — routes actually existing at the URLs the frontend/docs claim, not
just the Python functions behind them being individually correct.
`tests/run_eval.py` is a separate, deliberately live-cost eval harness (8
scenarios, real Bedrock calls) — run it before committing a prompt or
tool change, not on every trivial edit. See `CLAUDE.md` for the standing
rule: test after implementing a feature or fixing a bug, not as an
end-of-session cleanup pass.

### Local dev launch — tests run automatically first

`scripts/dev_server.py` (`uv run python -m scripts.dev_server`) is the
documented way to start the local MCP server — it runs the full pytest
suite, then a live Bedrock connectivity check
(`tests/preflight_bedrock.py`, a cheap `GetFoundationModel` control-plane
call, no token cost), and refuses to start the server if either fails.

`agent/runtime.py` clears `AWS_BEARER_TOKEN_BEDROCK` defensively at
import time, since botocore silently prefers that env var over IAM
credentials for any bedrock-runtime call if it's set anywhere in the
process environment, regardless of what credentials `boto3.client()`
was actually constructed with (see
`botocore.handlers._should_prefer_bearer_auth`) — a non-expiring IAM
credential chain can otherwise get silently overridden by a stale
bearer token with no error until a request is actually made. The
preflight check catches any other auth/connectivity problem before the
server is reachable, not mid-conversation.

## Deferred / explicitly not built

- **Multi-region / failover** for the AgentCore deploy — AgentCore
  Runtime is regional; there's no multi-region primitive to opt into
  short of standing up a second full runtime in a second region and
  routing between them. Real infrastructure this scale of project
  doesn't need; not worth building without a reason "up in one region"
  isn't already answering.
- **A generic multi-tenant MCP usage-tracker** decoupled from any one
  Bedrock agent — `mcp-context-inspector` already covers the
  per-owner-isolated, Google-authenticated multi-tenant case this
  project actually needs; a fully generic, agent-agnostic version isn't
  planned unless a second, genuinely different agent needs shared
  usage-tracking infrastructure later.
- **Phase 7 (Azure port)** — not started, deliberately deferred until
  this project has been used and reviewed hands-on.

## Project history

An SRE-style synthetic-incident-investigation agent: cheap to run
(synthetic data, no real third-party API calls), simple in
per-invocation scope, but genuinely testable (deterministic eval
scenarios with real negative cases). Started as a minimal AgentCore
hello-world deploy to prove the deployment mechanics (ARM64 Docker
image, Terraform-managed ECR/IAM), then grew into the current agent +
chat UI + MCP observability layer. `mcp_server/` and `metrics/` were
later extracted into the standalone `mcp-context-inspector` package
(see "Repo layout" above) so they're reusable by any tool-calling
agent, not just this one.

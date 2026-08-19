// No framework, no build step — reads mcp_server/server.py's /api/chat
// as a Server-Sent Events stream (POST, so plain fetch() + a
// ReadableStream reader, not EventSource — EventSource is GET-only).
//
// Layout: one "turn" per user message and one per agent response — each
// with an avatar + name + timestamp header, matching how Claude.ai/
// ChatGPT/Cursor structure a single-column agent feed. Everything the
// agent does for one prompt (loading state, reasoning, tool cards, final
// answer) lives inside that one agent turn's content column, appended
// live as SSE events arrive — not a separate anonymous box per event.

const messagesEl = document.getElementById("messages");
const formEl = document.getElementById("chat-form");
const inputEl = document.getElementById("prompt-input");
const suggestionsEl = document.getElementById("suggestions");
const modelSelectEl = document.getElementById("model-select");
const thinkingToggleEl = document.getElementById("thinking-toggle");
const mcpToggleEl = document.getElementById("mcp-toggle");
const mcpPanelEl = document.getElementById("mcp-panel");
const mcpStatusEl = document.getElementById("mcp-status");

const CONTEXT_WINDOW = 200_000; // Sonnet 4.6 and Haiku 4.5, both confirmed against current model cards

let lastSessionId = null;

function scrollToBottom(el) {
  el.scrollIntoView({ behavior: "smooth", block: "end" });
}

function timeNow() {
  return new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

// Creates one turn (avatar + name + timestamp + empty content column) and
// returns the content column for the caller to fill in. modelLabel (agent
// turns only) shows which model actually answered — without it, switching
// models mid-conversation left every past answer looking like it came
// from whichever model happens to be selected right now.
function addTurn(role, modelLabel) {
  const wrapper = document.createElement("div");
  wrapper.className = "flex gap-3 py-3";

  const avatar = document.createElement("div");
  avatar.className =
    role === "user"
      ? "h-8 w-8 shrink-0 rounded-full bg-white/10 text-slate-300 flex items-center justify-center text-sm font-semibold"
      : "h-8 w-8 shrink-0 rounded-full bg-accent/20 text-accent flex items-center justify-center text-sm font-semibold";
  avatar.textContent = role === "user" ? "Y" : "A";

  const col = document.createElement("div");
  col.className = "flex-1 min-w-0";

  const header = document.createElement("div");
  header.className = "flex items-baseline gap-2 mb-1.5";
  const speaker = document.createElement("span");
  speaker.className = "text-sm font-medium text-slate-200";
  speaker.textContent = role === "user" ? "You" : "Agent";
  header.appendChild(speaker);
  if (modelLabel) {
    const model = document.createElement("span");
    model.dataset.role = "turn-model";
    model.className = "text-xs text-accent/70";
    model.textContent = modelLabel;
    header.appendChild(model);
  }
  const time = document.createElement("span");
  time.className = "text-xs text-slate-500";
  time.textContent = timeNow();
  header.append(time);

  const content = document.createElement("div");
  content.className = "space-y-2.5";

  col.append(header, content);
  wrapper.append(avatar, col);
  messagesEl.appendChild(wrapper);
  scrollToBottom(wrapper);
  return content;
}

function addUserMessage(content, text) {
  const div = document.createElement("div");
  div.className = "text-[15px] text-slate-200";
  div.dataset.role = "user-message";
  div.textContent = text;
  content.appendChild(div);
}

// A persistent, updating status line — not just an initial "request
// sent" confirmation. Every gap in the investigation (waiting for the
// next tool call, the final round-trip to compose the answer) needs
// visible feedback, or it's indistinguishable from "stuck." This stays
// visible and its text updates through the whole request, then is
// explicitly removed on "final"/"error" — never left lingering.
// data-role is "loading-indicator", matched by browser_test_chat.py's
// assertions ("appears right after the user message," "gone once the
// final answer renders").
function addStatusIndicator(content) {
  const div = document.createElement("div");
  div.className = "flex items-center gap-2 py-1 text-xs text-slate-500";
  div.dataset.role = "loading-indicator";

  const spinner = document.createElement("span");
  spinner.className = "h-3 w-3 shrink-0 rounded-full border-2 border-accent/30 border-t-accent animate-spin";
  const label = document.createElement("span");
  label.dataset.role = "status-text";
  label.textContent = "Starting investigation…";
  div.append(spinner, label);

  content.appendChild(div);
  scrollToBottom(div);
  return {
    el: div,
    setText(text) {
      label.textContent = text;
    },
    // Every other render function appends new content to `content`,
    // which would otherwise leave this status line stranded above the
    // newest card instead of below it. appendChild on a node already in
    // the DOM moves it rather than duplicating it — cheap way to keep
    // this pinned at the bottom as new content streams in.
    pinToBottom() {
      content.appendChild(div);
      scrollToBottom(div);
    },
  };
}

// Reasoning renders full-weight, same size as the final answer — not
// muted/italic, which reads as unreadable filler text. A left accent
// bar signals "this is process," not shrunk text.
function addReasoning(content, text) {
  const div = document.createElement("div");
  div.className = "border-l-2 border-accent/50 pl-3 py-0.5 text-[15px] leading-relaxed text-slate-300";
  div.textContent = text;
  content.appendChild(div);
  scrollToBottom(div);
  return div;
}

// Claude's structured extended-thinking output (reasoningContent), fired
// only when the "Thinking" toggle is on — distinct from addReasoning()
// above (plain pre-tool-call narration text). Collapsible, since this is
// genuinely a different, denser kind of content than the narration.
function addReasoningBlock(content, text) {
  const details = document.createElement("details");
  details.className = "rounded-xl border border-white/10 bg-surface-raised";
  details.dataset.role = "thinking-block";

  const summary = document.createElement("summary");
  summary.className = "px-4 py-2 cursor-pointer select-none text-xs text-slate-500";
  summary.textContent = "Thinking…";
  details.appendChild(summary);

  const body = document.createElement("div");
  body.className = "border-t border-white/10 px-4 py-3 text-sm text-slate-400 whitespace-pre-wrap";
  body.textContent = text;
  details.appendChild(body);

  content.appendChild(details);
  scrollToBottom(details);
}

function addFinalAnswer(content, text) {
  const div = document.createElement("div");
  div.className =
    "prose prose-invert prose-sm sm:prose-base max-w-none " +
    "prose-headings:text-slate-100 prose-a:text-accent prose-code:text-accent " +
    "prose-pre:bg-black/40 prose-pre:border prose-pre:border-white/10 prose-strong:text-slate-100";
  div.dataset.role = "final-answer";
  // Agent answers are markdown (bold, tables, lists) — render as such.
  // Trusted content: our own agent's response, not third-party input.
  div.innerHTML = marked.parse(text);
  content.appendChild(div);
  scrollToBottom(div);
}

function addError(content, text) {
  const div = document.createElement("div");
  div.className = "rounded-xl border border-err/30 bg-err/10 px-4 py-2.5 text-sm text-err";
  div.dataset.role = "error-message";
  div.textContent = text;
  content.appendChild(div);
  scrollToBottom(div);
}

// Human-readable one-liners for what a tool call is doing/found — the
// primary thing a viewer reads. Raw JSON is still available (see the
// "Raw" toggle in addPendingToolCard/resolveToolCard below) for anyone
// who wants the exact args/result, but it's no longer the first thing
// shown — reading "{"service": "payments-api"}" told a viewer nothing a
// plain sentence couldn't. Unrecognized tools (a fork adding a 6th tool)
// fall back to the tool name itself rather than erroring.
function describeToolCall(tool, args) {
  switch (tool) {
    case "list_services":
      return "Listing available services";
    case "get_service_metrics":
      return `Checking metrics for ${args.service}`;
    case "search_logs":
      return `Searching ${args.service}'s logs for "${args.query}"`;
    case "get_recent_deployments":
      return `Checking deployment history for ${args.service}`;
    case "get_cost_breakdown":
      return `Checking cost breakdown for ${args.service}`;
    default:
      return tool.replace(/_/g, " ");
  }
}

function describeToolResult(tool, output) {
  if (output?.status === "error") return output.message || "Error";
  switch (tool) {
    case "list_services":
      return `${output.services.length} services: ${output.services.join(", ")}`;
    case "get_service_metrics": {
      const m = output.metrics;
      return `${m.status} — ${m.error_rate}% error rate, ${m.latency_p95_ms}ms p95 latency, ${m.cpu}% CPU`;
    }
    case "search_logs":
      return output.matches.length
        ? `${output.matches.length} matching log line${output.matches.length === 1 ? "" : "s"}`
        : "No matching log lines";
    case "get_recent_deployments":
      return output.deployments.length
        ? `${output.deployments[0].version} deployed at ${output.deployments[0].deployed_at}`
        : "No recent deployment on record";
    case "get_cost_breakdown":
      return `$${output.cost.monthly_cost_usd}/month, ${output.cost.requests_per_day.toLocaleString()} requests/day`;
    default:
      return "Done";
  }
}

// Returns the live card element — call resolveToolCard() on it once the
// matching tool_result event arrives.
function addPendingToolCard(content, tool, args) {
  const details = document.createElement("details");
  details.className = "rounded-xl border border-accent/40 bg-surface-raised";
  details.dataset.role = "tool-card";
  details.dataset.status = "pending";

  const summary = document.createElement("summary");
  summary.className = "flex items-center gap-3 px-4 py-2.5 cursor-pointer select-none";
  summary.innerHTML = `
    <span class="text-slate-500 transition-transform group-open:rotate-90">›</span>
    <span class="text-accent">⚙</span>
    <span class="flex-1 min-w-0 truncate text-sm text-slate-300 font-mono">${tool.replace(/_/g, " ")}</span>
  `;
  const status = document.createElement("span");
  status.className = "shrink-0 inline-flex items-center gap-1 rounded-full bg-warn/15 text-warn text-xs font-medium px-2.5 py-0.5";
  status.innerHTML = `<span class="h-1.5 w-1.5 rounded-full bg-warn animate-pulse"></span> running`;
  summary.appendChild(status);
  details.appendChild(summary);

  const body = document.createElement("div");
  body.className = "border-t border-white/10 mt-0 px-4 py-3 space-y-2";

  const summaryLine = document.createElement("div");
  summaryLine.dataset.role = "tool-summary";
  summaryLine.className = "text-sm text-slate-300";
  summaryLine.textContent = describeToolCall(tool, args);
  body.appendChild(summaryLine);

  const rawToggle = document.createElement("details");
  rawToggle.className = "text-xs";
  const rawSummary = document.createElement("summary");
  rawSummary.className = "text-slate-500 hover:text-slate-400 cursor-pointer select-none";
  rawSummary.textContent = "Raw";
  const rawBody = document.createElement("pre");
  rawBody.className = "mt-1.5 font-mono text-slate-500 whitespace-pre-wrap break-words";
  const argsStr = Object.keys(args).length ? JSON.stringify(args, null, 2) : "(no arguments)";
  rawBody.textContent = `args: ${argsStr}\n\nwaiting for result…`;
  rawToggle.append(rawSummary, rawBody);
  body.appendChild(rawToggle);

  details.appendChild(body);

  content.appendChild(details);
  scrollToBottom(details);
  return { details, summary, statusEl: status, summaryLine, rawBody, argsStr, tool };
}

function resolveToolCard(card, status, output) {
  const ok = status === "ok";
  card.details.className = `rounded-xl border ${ok ? "border-white/10" : "border-err/40"} bg-surface-raised`;
  card.details.dataset.status = ok ? "ok" : "error";
  card.statusEl.className = `shrink-0 inline-flex items-center gap-1 rounded-full ${
    ok ? "bg-ok/15 text-ok" : "bg-err/15 text-err"
  } text-xs font-medium px-2.5 py-0.5`;
  card.statusEl.innerHTML = ok
    ? `<span class="h-1.5 w-1.5 rounded-full bg-ok"></span> success`
    : `<span class="h-1.5 w-1.5 rounded-full bg-err"></span> error`;
  card.summaryLine.textContent = describeToolResult(card.tool, output);
  card.summaryLine.className = `text-sm ${ok ? "text-slate-300" : "text-err"}`;
  card.rawBody.textContent = `args: ${card.argsStr}\n\nresult:\n${JSON.stringify(output, null, 2)}`;
}

async function sendPrompt(prompt) {
  addUserMessage(addTurn("user"), prompt);

  // Captured now, not read later — the model selector could change
  // before this turn's response finishes streaming, and the label must
  // reflect what actually answered this turn, not whatever is selected
  // when it happens to finish.
  const modelLabel = modelSelectEl.selectedOptions[0].textContent;
  const content = addTurn("agent", modelLabel);
  const status = addStatusIndicator(content);

  // Tracks the most recently opened pending tool card, since tool_call
  // and its matching tool_result arrive as a pair before the next call.
  let pendingCard = null;

  const requestBody = { prompt, model_id: modelSelectEl.value };
  if (thinkingToggleEl.checked) requestBody.thinking_budget = 1024;

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(requestBody),
    });

    if (!res.ok || !res.body) {
      status.el.remove();
      addError(content, `Request failed (HTTP ${res.status}).`);
      return;
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      // SSE frames are separated by a blank line; each line we care about
      // starts with "data: ".
      const frames = buffer.split("\n\n");
      buffer = frames.pop(); // last chunk may be incomplete, keep it for next read
      for (const frame of frames) {
        const line = frame.split("\n").find((l) => l.startsWith("data: "));
        if (!line) continue;
        const event = JSON.parse(line.slice("data: ".length));

        if (event.type === "turn_start") {
          status.setText(event.turn === 0 ? "Investigating…" : `Continuing investigation (turn ${event.turn + 1})…`);
        } else if (event.type === "assistant_text") {
          addReasoning(content, event.text);
          status.pinToBottom();
        } else if (event.type === "reasoning") {
          addReasoningBlock(content, event.text);
          status.pinToBottom();
        } else if (event.type === "tool_call") {
          status.setText(`Calling ${event.tool.replace(/_/g, " ")}…`);
          pendingCard = addPendingToolCard(content, event.tool, event.args);
          status.pinToBottom();
        } else if (event.type === "tool_result") {
          if (pendingCard) resolveToolCard(pendingCard, event.status, event.output);
          pendingCard = null;
          status.setText("Composing answer…");
        } else if (event.type === "final") {
          status.el.remove();
          addFinalAnswer(content, event.text);
        } else if (event.type === "error") {
          status.el.remove();
          addError(content, event.message);
        } else if (event.type === "done") {
          lastSessionId = event.session_id;
          refreshMcpPanel();
        }
      }
    }
  } catch (err) {
    // Network failure, connection dropped mid-stream, etc. — never leave
    // the UI silently waiting with no explanation.
    status.el.remove();
    addError(content, err.message);
  }
}

formEl.addEventListener("submit", (e) => {
  e.preventDefault();
  const prompt = inputEl.value.trim();
  if (!prompt) return;
  inputEl.value = "";
  sendPrompt(prompt);
});

async function loadSuggestions() {
  let questions;
  try {
    const res = await fetch("/api/suggested-prompts");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    questions = await res.json();
  } catch (err) {
    // The suggestions row is a nice-to-have, not core functionality —
    // degrade to hiding it entirely rather than leaving a stuck
    // "Loading suggested questions…" message on a failed fetch.
    suggestionsEl.remove();
    return;
  }
  suggestionsEl.innerHTML = "";
  const label = document.createElement("span");
  label.className = "text-slate-500 self-center";
  label.textContent = "Try:";
  suggestionsEl.appendChild(label);
  for (const q of questions) {
    const btn = document.createElement("button");
    btn.textContent = q.length > 50 ? q.slice(0, 47) + "…" : q;
    btn.title = q;
    btn.type = "button";
    btn.className =
      "rounded-full border border-white/10 bg-surface-raised px-3 py-1.5 text-slate-300 " +
      "hover:border-accent/50 hover:text-slate-100 transition";
    btn.addEventListener("click", () => sendPrompt(q));
    suggestionsEl.appendChild(btn);
  }
}

// --- MCP metrics panel ------------------------------------------------
// Off by default. Turning the toggle on does a real MCP Streamable-HTTP
// handshake (web/mcp-client.js) against mcp_server's /mcp endpoint —
// genuine protocol framing, not a REST shortcut — then renders recent
// sessions, aggregate tool/cost stats, and (once a chat completes)
// per-prompt detail for the session just run.

let mcpClient = null;

// --- Context Window Explorer -------------------------------------------
// Styled after https://code.claude.com/docs/en/context-window: a
// proportional segmented bar + an ordered, colored, clickable block list
// — everything that entered this session's context, block by block, with
// a running "visible in your chat vs. invisible" distinction. Static: it
// only renders once a chat turn has finished and get_context_timeline has
// real data (same trigger refreshMcpPanel() already uses), not a live
// play/scrub transport control.

const CATEGORY_META = {
  system: { dot: "bg-slate-500", desc: "The agent's fixed instructions — role, investigation approach, field semantics.", visible: "No — never shown to you" },
  tools: { dot: "bg-slate-300", desc: "JSON schemas for the 5 SRE tools the model can call.", visible: "No" },
  user: { dot: "bg-white", desc: "The question you actually asked.", visible: "Yes — the user message bubble" },
  reasoning: { dot: "bg-accent", desc: "The model's narration before a tool call (“Let me check…”).", visible: "Yes — the left-accent-bar reasoning line" },
  thinking: { dot: "bg-fuchsia-400", desc: "Extended-thinking output (only present if the Thinking toggle was on for that turn).", visible: "Conditional — only when the Thinking toggle was on for that turn" },
  tool_call: { dot: "bg-warn", desc: "Arguments sent to a tool.", visible: "No — only via a tool card's “Raw” toggle" },
  tool_result: { dot: "bg-ok", errDot: "bg-err", desc: "Data returned from DynamoDB.", visible: "No — same “Raw” toggle caveat" },
  answer: { dot: "bg-sky-300", desc: "The final answer.", visible: "Yes — the rendered answer bubble" },
};

function dotClassFor(block) {
  const meta = CATEGORY_META[block.category];
  if (!meta) return "bg-slate-500";
  if (block.category === "tool_result" && block.status === "error") return meta.errDot;
  return meta.dot;
}

function renderContextTimeline(container, totalEl, blocks) {
  container.innerHTML = "";
  if (!blocks.length) {
    container.textContent = "No context data yet — send a message.";
    totalEl.textContent = "";
    return;
  }

  const totalTokens = blocks[blocks.length - 1].cumulative_tokens;
  totalEl.textContent = `~${totalTokens.toLocaleString()} tokens / ${CONTEXT_WINDOW.toLocaleString()} · estimated`;

  // Segmented proportional bar — one slice per block, in order.
  const bar = document.createElement("div");
  bar.className = "flex h-2 w-full overflow-hidden rounded-full border border-white/5 bg-surface-raised";
  for (const block of blocks) {
    const slice = document.createElement("div");
    const pct = Math.max((block.token_estimate / CONTEXT_WINDOW) * 100, 0.3);
    slice.className = dotClassFor(block);
    slice.style.width = `${pct}%`;
    bar.appendChild(slice);
  }
  container.appendChild(bar);

  // Category legend — only categories actually present in this session.
  const seen = [...new Set(blocks.map((b) => b.category))];
  const legend = document.createElement("div");
  legend.className = "flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-slate-500";
  for (const category of seen) {
    const item = document.createElement("span");
    item.className = "inline-flex items-center gap-1";
    const dot = document.createElement("span");
    dot.className = `inline-block h-2 w-2 rounded-full ${CATEGORY_META[category]?.dot || "bg-slate-500"}`;
    item.append(dot, document.createTextNode(category));
    legend.appendChild(item);
  }
  container.appendChild(legend);

  // Ordered block list — click (or tap) a row to expand its detail panel.
  const list = document.createElement("div");
  list.className = "space-y-0.5";
  for (const block of blocks) {
    const meta = CATEGORY_META[block.category] || {};

    const rowEl = document.createElement("button");
    rowEl.type = "button";
    rowEl.className =
      "w-full flex items-center gap-2 rounded-lg px-2 py-1.5 text-left hover:bg-white/5 transition";

    const dot = document.createElement("span");
    dot.className = `inline-block h-2 w-2 shrink-0 rounded-full ${dotClassFor(block)}`;

    const label = document.createElement("span");
    label.className = "flex-1 min-w-0 truncate text-slate-300";
    label.textContent = block.label;

    const tokens = document.createElement("span");
    tokens.className = "shrink-0 font-mono text-slate-500";
    tokens.textContent = `+${block.token_estimate}`;

    const badge = document.createElement("span");
    badge.className = "shrink-0 rounded-full border border-white/10 px-1.5 py-0.5 text-[10px] text-slate-500";
    badge.textContent = meta.visible?.startsWith("Yes") ? "visible" : meta.visible?.startsWith("Conditional") ? "conditional" : "invisible";

    rowEl.append(dot, label, tokens, badge);

    const detail = document.createElement("div");
    detail.className = "hidden mx-2 mb-1 rounded-lg bg-surface-raised px-3 py-2 text-[11px] text-slate-400 space-y-1";
    detail.appendChild(row("Category", block.category));
    detail.appendChild(row("Description", meta.desc || "—"));
    detail.appendChild(row("Visible in chat", meta.visible || "—"));
    detail.appendChild(row("Exact chars", block.char_count.toLocaleString()));
    detail.appendChild(row("Token estimate", `~${block.token_estimate}`));
    detail.appendChild(row("Running total", `~${block.cumulative_tokens.toLocaleString()} (${block.cumulative_pct}%)`));
    if (block.turn_n !== null && block.turn_n !== undefined) detail.appendChild(row("Turn", block.turn_n));
    if (block.status) detail.appendChild(row("Tool status", block.status));

    rowEl.addEventListener("click", () => detail.classList.toggle("hidden"));

    list.append(rowEl, detail);
  }
  container.appendChild(list);
}

function row(label, value) {
  const div = document.createElement("div");
  div.className = "flex justify-between gap-2";
  const l = document.createElement("span");
  l.textContent = label;
  const v = document.createElement("span");
  v.className = "text-slate-200 font-mono";
  v.textContent = value;
  div.append(l, v);
  return div;
}

async function renderMcpPanel() {
  const sessionsEl = document.getElementById("mcp-sessions");
  const toolMetricsEl = document.getElementById("mcp-tool-metrics");
  const aggregateEl = document.getElementById("mcp-aggregate");
  const currentEl = document.getElementById("mcp-current-session");

  const [sessions, toolMetrics, cost] = await Promise.all([
    mcpClient.callTool("get_recent_sessions", { limit: 5 }),
    mcpClient.callTool("get_tool_metrics", {}),
    mcpClient.callTool("get_cost_estimate", {}),
  ]);

  sessionsEl.innerHTML = "";
  if (!sessions.length) sessionsEl.textContent = "No sessions recorded yet.";
  for (const s of sessions) {
    sessionsEl.appendChild(row(s.prompt.length > 28 ? s.prompt.slice(0, 25) + "…" : s.prompt, `$${s.estimated_cost.toFixed(4)}`));
  }

  toolMetricsEl.innerHTML = "";
  if (!toolMetrics.length) toolMetricsEl.textContent = "No tool calls recorded yet.";
  for (const m of toolMetrics) {
    toolMetricsEl.appendChild(row(`${m.tool_name} (${m.status})`, m.calls));
  }

  // TPM computed client-side from already-fetched session data — no 7th
  // MCP tool needed for something derivable from get_recent_sessions.
  const now = Date.now() / 1000;
  const windowSeconds = 300;
  const inWindow = sessions.filter((s) => now - s.timestamp <= windowSeconds);
  const tokensInWindow = inWindow.reduce((sum, s) => sum + s.total_tokens, 0);
  const tpm = inWindow.length ? Math.round((tokensInWindow / windowSeconds) * 60) : 0;

  aggregateEl.innerHTML = "";
  aggregateEl.appendChild(row("Total cost", `$${cost.toFixed(4)}`));
  aggregateEl.appendChild(row("TPM (5min window)", tpm));

  const ctxTotalEl = document.getElementById("ctx-total");
  if (lastSessionId) {
    const timeline = await mcpClient.callTool("get_context_timeline", { session_id: lastSessionId });
    renderContextTimeline(currentEl, ctxTotalEl, timeline);
  } else {
    ctxTotalEl.textContent = "";
  }
}

async function refreshMcpPanel() {
  if (!mcpClient) return;
  try {
    await renderMcpPanel();
  } catch (err) {
    mcpStatusEl.textContent = `error: ${err.message}`;
  }
}

const mcpConnectFormEl = document.getElementById("mcp-connect-form");
const mcpDataEl = document.getElementById("mcp-data");
const mcpTokenInputEl = document.getElementById("mcp-token-input");
const mcpConnectBtnEl = document.getElementById("mcp-connect-btn");

// Toggling this only reveals/hides the panel shell — it does NOT
// connect anything by itself. Connecting is its own explicit action
// (the Connect button below), same as adding an MCP server in Claude
// Desktop: you configure it, then a separate "connect" step happens
// with a visible result, rather than a checkbox silently starting
// background work. The panel always opens showing the connect form
// first, never the data — nothing queries the metrics DB until a real
// handshake has actually succeeded.
mcpToggleEl.addEventListener("change", () => {
  if (mcpToggleEl.checked) {
    mcpPanelEl.classList.remove("hidden");
  } else {
    mcpPanelEl.classList.add("hidden");
    disconnectMcp();
  }
});

mcpConnectBtnEl.addEventListener("click", connectMcp);

async function connectMcp() {
  const token = mcpTokenInputEl.value.trim();
  if (!token) {
    mcpStatusEl.textContent = "enter a token first";
    return;
  }
  mcpStatusEl.textContent = "connecting…";
  mcpConnectBtnEl.disabled = true;
  try {
    mcpClient = new McpClient(token);
    await mcpClient.initialize();
    await mcpClient.listTools(); // confirms the handshake actually works end to end
    mcpStatusEl.textContent = "connected";
    mcpConnectFormEl.classList.add("hidden");
    mcpDataEl.classList.remove("hidden");
    await refreshMcpPanel();
  } catch (err) {
    mcpStatusEl.textContent = `failed: ${err.message}`;
    mcpClient = null;
  } finally {
    mcpConnectBtnEl.disabled = false;
  }
}

async function disconnectMcp() {
  mcpDataEl.classList.add("hidden");
  mcpConnectFormEl.classList.remove("hidden");
  mcpStatusEl.textContent = "disconnected";
  if (mcpClient) {
    await mcpClient.close();
    mcpClient = null;
  }
}

loadSuggestions();

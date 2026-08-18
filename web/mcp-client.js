// A real MCP client — genuine Streamable-HTTP JSON-RPC framing against
// mcp_server/server.py's /mcp endpoint, no SDK. Deliberately decoupled
// from chat.js: this only runs when the user flips the "MCP server"
// toggle on, so a user who never touches it never triggers a network
// call to the metrics server at all.
//
// Streamable HTTP transport, per the MCP spec: POST JSON-RPC requests to
// /mcp; the server may reply with either a single `application/json`
// body or a `text/event-stream` (one or more SSE frames, the last of
// which carries the JSON-RPC response) — this client handles both. A
// session is established by the `initialize` response's `Mcp-Session-Id`
// response header, which must be echoed on every subsequent request
// (including the `notifications/initialized` notification) and can be
// torn down with an explicit DELETE.

const MCP_URL = "http://127.0.0.1:8787/mcp";

class McpClient {
  constructor(token) {
    this.token = token;
    this.sessionId = null;
    this._nextId = 1;
  }

  async _send(body) {
    const headers = {
      "Content-Type": "application/json",
      Accept: "application/json, text/event-stream",
      Authorization: `Bearer ${this.token}`,
    };
    if (this.sessionId) headers["Mcp-Session-Id"] = this.sessionId;

    const res = await fetch(MCP_URL, { method: "POST", headers, body: JSON.stringify(body) });
    if (res.status === 401) throw new Error("Unauthorized — check the auth token");
    if (!res.ok) throw new Error(`MCP request failed (HTTP ${res.status})`);

    const returnedSessionId = res.headers.get("Mcp-Session-Id");
    if (returnedSessionId) this.sessionId = returnedSessionId;

    // Notifications get a 202 with no body — nothing to parse.
    if (res.status === 202) return null;

    const contentType = res.headers.get("Content-Type") || "";
    if (contentType.includes("text/event-stream")) {
      return this._parseSseResponse(res);
    }
    return res.json();
  }

  async _parseSseResponse(res) {
    // A streamed reply is one or more "data: {...}" frames; the request/
    // response JSON-RPC message we care about is the last one carrying a
    // matching "id". Simple case (this client never opens a long-lived
    // subscription): read the whole stream, return the last JSON-RPC
    // message seen.
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let lastMessage = null;

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      // The server's SSE frames are CRLF-terminated ("\r\n\r\n" between
      // frames) — confirmed by direct inspection of the raw stream bytes.
      // Splitting on a bare "\n\n" never matches "\r\n\r\n" (there's a \r
      // between the two \n's), so every frame silently fell into the
      // "incomplete, wait for more" leftover buffer forever and
      // lastMessage never got set — the actual cause of initialize()
      // crashing on a null response. Normalize line endings before
      // splitting so this works regardless of which the server uses.
      buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
      const frames = buffer.split("\n\n");
      buffer = frames.pop();
      for (const frame of frames) {
        const line = frame.split("\n").find((l) => l.startsWith("data: "));
        if (!line) continue;
        lastMessage = JSON.parse(line.slice("data: ".length));
      }
    }
    return lastMessage;
  }

  // Real handshake: initialize (request/response) then notifications/initialized
  // (a notification — no id, no response expected).
  async initialize() {
    const initResponse = await this._send({
      jsonrpc: "2.0",
      id: this._nextId++,
      method: "initialize",
      params: {
        protocolVersion: "2025-06-18",
        capabilities: {},
        clientInfo: { name: "sre-agent-chat-panel", version: "1.0.0" },
      },
    });
    if (initResponse?.error) {
      throw new Error(`MCP initialize failed: ${initResponse.error.message}`);
    }

    await this._send({
      jsonrpc: "2.0",
      method: "notifications/initialized",
      params: {},
    });

    return initResponse.result;
  }

  async listTools() {
    const response = await this._send({
      jsonrpc: "2.0",
      id: this._nextId++,
      method: "tools/list",
      params: {},
    });
    if (response?.error) throw new Error(`MCP tools/list failed: ${response.error.message}`);
    return response.result.tools;
  }

  async callTool(name, args = {}) {
    const response = await this._send({
      jsonrpc: "2.0",
      id: this._nextId++,
      method: "tools/call",
      params: { name, arguments: args },
    });
    if (response?.error) throw new Error(`MCP tools/call(${name}) failed: ${response.error.message}`);
    if (response.result?.isError) {
      const text = response.result.content?.map((c) => c.text).join(" ") || "unknown tool error";
      throw new Error(`MCP tool ${name} returned an error: ${text}`);
    }
    // Scalar-returning tools (get_cost_estimate) get a typed
    // structuredContent field alongside the legacy content blocks — a
    // {"result": <value>} wrapper the server auto-generates for a bare
    // scalar return type. Prefer it when present; it's already the
    // right type, no re-parsing a stringified number.
    const structured = response.result.structuredContent;
    if (structured && Object.keys(structured).length === 1 && "result" in structured) {
      return structured.result;
    }

    // Everything else (dict- or list-returning tools) has no
    // structuredContent — each item of a *list* return comes back as
    // its OWN content block (confirmed by direct inspection: a 3-item
    // get_recent_sessions reply is 3 separate {type:"text"} blocks, not
    // one block holding a JSON array). So this always returns an array
    // of parsed blocks; the caller — who already knows whether the tool
    // returns a single object or a list — unwraps with `[0]` for the
    // former. Returning "the bare object when there's exactly one
    // block" would be ambiguous: a single-item list is wire-identical to
    // a dict return, and there's no way to tell them apart here.
    const textBlocks = response.result.content?.filter((c) => c.type === "text") || [];
    return textBlocks.map((b) => JSON.parse(b.text));
  }

  // No persistent subscription was ever opened server-side beyond the
  // session itself, so teardown is just the spec's explicit session-end:
  // DELETE with the session header. Best-effort — a user closing the tab
  // without toggling off first shouldn't be treated as an error anywhere.
  async close() {
    if (!this.sessionId) return;
    try {
      await fetch(MCP_URL, {
        method: "DELETE",
        headers: { "Mcp-Session-Id": this.sessionId, Authorization: `Bearer ${this.token}` },
      });
    } catch {
      // best-effort teardown, nothing to recover from client-side
    }
    this.sessionId = null;
  }
}

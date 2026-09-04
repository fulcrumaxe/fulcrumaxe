/**
 * app.js — Autonomous Team Dashboard frontend
 *
 * Vanilla JS, no build step, no frameworks.
 * Connects to /api/feed via EventSource for live agent events.
 * Sends prompts via POST /api/prompt.
 * Triggers loop iterations via POST /api/loop/trigger.
 */

(function () {
  "use strict";

  // ---------------------------------------------------------------------------
  // DOM refs
  // ---------------------------------------------------------------------------
  const feed = document.getElementById("feed");
  const promptInput = document.getElementById("prompt-input");
  const sendBtn = document.getElementById("send-btn");
  const loopBtn = document.getElementById("loop-btn");
  const dotEl = document.getElementById("connection-dot");
  const connLabel = document.getElementById("connection-label");
  const modelLabel = document.getElementById("model-label");
  const uptimeLabel = document.getElementById("uptime-label");
  const clientLabel = document.getElementById("client-label");
  const lastEventLabel = document.getElementById("last-event-label");

  // ---------------------------------------------------------------------------
  // State
  // ---------------------------------------------------------------------------
  // Map of request-id → { groupEl, bodyEl, collapsed }
  const requestGroups = {};
  // Whether the user has scrolled up (pause auto-scroll).
  let userScrolledUp = false;
  let sseSource = null;
  let statusPollInterval = null;

  // ---------------------------------------------------------------------------
  // Helpers
  // ---------------------------------------------------------------------------

  function escapeHtml(str) {
    if (typeof str !== "string") str = String(str);
    return str
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function truncate(str, maxLen) {
    if (typeof str !== "string") str = JSON.stringify(str) || "";
    return str.length > maxLen ? str.slice(0, maxLen) + "…" : str;
  }

  function toDisplayJson(value) {
    if (typeof value === "object" && value !== null) {
      return JSON.stringify(value, null, 2);
    }
    return String(value);
  }

  function autoScroll() {
    if (!userScrolledUp) {
      const container = document.getElementById("feed-container");
      container.scrollTop = container.scrollHeight;
    }
  }

  function setConnectionState(state) {
    dotEl.className = "dot dot-" + state;
    connLabel.textContent = state;
  }

  // ---------------------------------------------------------------------------
  // Request groups
  // ---------------------------------------------------------------------------

  function getOrCreateGroup(reqId) {
    if (requestGroups[reqId]) return requestGroups[reqId];

    const groupEl = document.createElement("div");
    groupEl.className = "req-group";
    groupEl.dataset.reqId = reqId;

    const header = document.createElement("div");
    header.className = "req-header";

    const idSpan = document.createElement("span");
    idSpan.className = "req-id";
    idSpan.textContent = reqId;

    const toggle = document.createElement("span");
    toggle.className = "req-toggle";
    toggle.textContent = "▼";

    header.appendChild(idSpan);
    header.appendChild(toggle);

    const body = document.createElement("div");
    body.className = "req-body";

    groupEl.appendChild(header);
    groupEl.appendChild(body);
    feed.appendChild(groupEl);

    const entry = { groupEl, bodyEl: body, collapsed: false };
    requestGroups[reqId] = entry;

    header.addEventListener("click", () => {
      entry.collapsed = !entry.collapsed;
      body.classList.toggle("collapsed", entry.collapsed);
      toggle.textContent = entry.collapsed ? "▶" : "▼";
    });

    return entry;
  }

  function appendEventToGroup(reqId, el) {
    const { bodyEl } = getOrCreateGroup(reqId);
    bodyEl.appendChild(el);
    autoScroll();
  }

  // ---------------------------------------------------------------------------
  // Event renderers
  // ---------------------------------------------------------------------------

  function renderThinking(event) {
    const el = document.createElement("div");
    el.className = "event event-thinking";
    el.textContent = event.content || "";
    return el;
  }

  function renderContent(event) {
    const el = document.createElement("div");
    el.className = "event event-content";
    el.textContent = event.content || "";
    return el;
  }

  function renderToolUse(event) {
    const el = document.createElement("div");
    el.className = "event event-tool-use";

    const inputStr = toDisplayJson(event.input || {});
    const preview = truncate(inputStr.replace(/\s+/g, " "), 80);

    el.innerHTML =
      '<div class="tool-summary">' +
        '<span class="tool-name">' + escapeHtml(event.tool || "tool") + "</span>" +
        '<span class="tool-input-preview">' + escapeHtml(preview) + "</span>" +
      "</div>" +
      '<div class="tool-detail">' + escapeHtml(inputStr) + "</div>";

    const summary = el.querySelector(".tool-summary");
    const detail = el.querySelector(".tool-detail");
    summary.addEventListener("click", () => {
      detail.classList.toggle("expanded");
    });

    return el;
  }

  function renderToolResult(event) {
    const el = document.createElement("div");
    el.className = "event event-tool-result" + (event.is_error ? " is-error" : "");

    const resultStr = toDisplayJson(event.result || "");
    const preview = truncate(resultStr.replace(/\s+/g, " "), 80);

    el.innerHTML =
      '<div class="tool-summary">' +
        '<span class="tool-name">result' + (event.is_error ? " (error)" : "") + "</span>" +
        '<span class="tool-result-preview">' + escapeHtml(preview) + "</span>" +
      "</div>" +
      '<div class="tool-detail">' + escapeHtml(resultStr) + "</div>";

    const summary = el.querySelector(".tool-summary");
    const detail = el.querySelector(".tool-detail");
    summary.addEventListener("click", () => {
      detail.classList.toggle("expanded");
    });

    return el;
  }

  function renderUsage(event) {
    const el = document.createElement("div");
    el.className = "event event-usage";
    const u = event.usage || {};
    el.textContent =
      "tokens: in=" + (u.input_tokens || 0) + " out=" + (u.output_tokens || 0);
    return el;
  }

  function renderDone(_event) {
    const el = document.createElement("div");
    el.className = "event event-done";
    return el;
  }

  function renderError(event) {
    const el = document.createElement("div");
    el.className = "event event-error";
    el.textContent = event.error || "unknown error";
    return el;
  }

  // ---------------------------------------------------------------------------
  // Main event dispatcher
  // ---------------------------------------------------------------------------

  function appendToFeed(event) {
    const type = event.type;
    const reqId = event.id;

    // Events without a request id (e.g. ready, top-level error).
    if (!reqId) {
      const el = document.createElement("div");
      el.className = "event-standalone";
      el.textContent = "[" + (type || "?") + "] " + JSON.stringify(event);
      feed.appendChild(el);
      autoScroll();
      return;
    }

    let el = null;
    switch (type) {
      case "thinking":
        el = renderThinking(event);
        break;
      case "content":
        el = renderContent(event);
        break;
      case "tool_use":
        el = renderToolUse(event);
        break;
      case "tool_result":
        el = renderToolResult(event);
        break;
      case "usage":
        el = renderUsage(event);
        break;
      case "done":
        el = renderDone(event);
        break;
      case "error":
        el = renderError(event);
        break;
      default:
        el = document.createElement("div");
        el.className = "event";
        el.textContent = "[" + type + "] " + JSON.stringify(event);
    }

    if (el) appendEventToGroup(reqId, el);
  }

  // ---------------------------------------------------------------------------
  // SSE connection
  // ---------------------------------------------------------------------------

  function connectSSE() {
    if (sseSource) {
      sseSource.close();
    }

    setConnectionState("connecting");

    sseSource = new EventSource("/api/feed");

    sseSource.onopen = function () {
      setConnectionState("connected");
    };

    sseSource.onmessage = function (e) {
      let event;
      try {
        event = JSON.parse(e.data);
      } catch (err) {
        console.warn("SSE parse error:", e.data);
        return;
      }
      appendToFeed(event);

      // Update model label from ready event.
      if (event.type === "ready" && event.model) {
        modelLabel.textContent = event.model;
      }
    };

    sseSource.onerror = function () {
      setConnectionState("disconnected");
      // EventSource auto-reconnects; we update UI on re-open.
    };
  }

  // ---------------------------------------------------------------------------
  // Status polling (fills status bar with server-side data)
  // ---------------------------------------------------------------------------

  async function pollStatus() {
    try {
      const res = await fetch("/api/status");
      if (!res.ok) return;
      const data = await res.json();

      if (data.model) modelLabel.textContent = data.model;
      uptimeLabel.textContent = "up " + formatUptime(data.uptime_s);
      clientLabel.textContent = data.connected_clients + " tab" +
        (data.connected_clients !== 1 ? "s" : "");
      if (data.last_event_at) {
        const ago = Math.round(Date.now() / 1000 - data.last_event_at);
        lastEventLabel.textContent = "last event " + ago + "s ago";
      }
    } catch (_) {
      // Server not yet up; ignore.
    }
  }

  function formatUptime(seconds) {
    if (seconds < 60) return Math.round(seconds) + "s";
    if (seconds < 3600) return Math.round(seconds / 60) + "m";
    return (seconds / 3600).toFixed(1) + "h";
  }

  // ---------------------------------------------------------------------------
  // Prompt submission
  // ---------------------------------------------------------------------------

  async function sendPrompt(text) {
    text = text.trim();
    if (!text) return;

    promptInput.value = "";
    promptInput.disabled = true;
    sendBtn.disabled = true;

    try {
      const res = await fetch("/api/prompt", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt: text }),
      });
      if (!res.ok) {
        const body = await res.text();
        console.error("Prompt rejected:", res.status, body);
        const errEl = document.createElement("div");
        errEl.className = "event-standalone";
        errEl.style.color = "var(--red)";
        errEl.textContent = "Failed to send prompt: " + res.status + " " + body;
        feed.appendChild(errEl);
        autoScroll();
      }
      // Events will arrive via SSE.
    } catch (err) {
      console.error("Prompt fetch error:", err);
    } finally {
      promptInput.disabled = false;
      sendBtn.disabled = false;
      promptInput.focus();
    }
  }

  // ---------------------------------------------------------------------------
  // Loop trigger
  // ---------------------------------------------------------------------------

  async function triggerLoop() {
    loopBtn.disabled = true;
    try {
      const res = await fetch("/api/loop/trigger", { method: "POST" });
      if (!res.ok) {
        console.error("Loop trigger failed:", res.status);
      }
    } catch (err) {
      console.error("Loop trigger error:", err);
    } finally {
      // Brief visual feedback before re-enabling.
      setTimeout(() => {
        loopBtn.disabled = false;
      }, 1000);
    }
  }

  // ---------------------------------------------------------------------------
  // Scroll tracking
  // ---------------------------------------------------------------------------

  document.getElementById("feed-container").addEventListener("scroll", function () {
    const el = this;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    userScrolledUp = !atBottom;
  });

  // ---------------------------------------------------------------------------
  // Event listeners
  // ---------------------------------------------------------------------------

  sendBtn.addEventListener("click", () => {
    sendPrompt(promptInput.value);
  });

  promptInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendPrompt(promptInput.value);
    }
  });

  loopBtn.addEventListener("click", triggerLoop);

  // ---------------------------------------------------------------------------
  // Boot
  // ---------------------------------------------------------------------------

  connectSSE();
  pollStatus();
  statusPollInterval = setInterval(pollStatus, 5000);

  promptInput.focus();
})();

/* Gremlin frontend: sessions, streaming chat, settings. Vanilla JS, no CDNs. */
"use strict";

const state = {
  settings: null,
  sessions: [],
  activeId: null,
  streaming: false,
  sidebar: null,
  n_ctx: null,
  lastPrompt: null,
};

const $ = (id) => document.getElementById(id);

/* ---------------- helpers ---------------- */

function toast(msg) {
  $("toast-body").textContent = msg;
  bootstrap.Toast.getOrCreateInstance($("toast")).show();
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const j = await res.json();
      if (j && j.error) detail = j.error;
    } catch (_) {}
    throw new Error(detail);
  }
  if (res.status === 204) return null;
  return res.json();
}

function escapeNone() {} // all user/model text is inserted via textContent

/* ---------------- settings ---------------- */

function themeHref(slug) {
  return slug && slug !== "default"
    ? `/static/themes/${slug}.min.css`
    : "/static/css/bootstrap.min.css";
}

function applySettings(s) {
  state.settings = s;
  document.documentElement.setAttribute("data-bs-theme", s.appearance === "light" ? "light" : "dark");
  $("theme-css").href = themeHref(s.theme);
  $("set-thinking").checked = !!s.show_thinking;
  $("set-max-tool-calls").value = s.max_tool_calls ?? "20";
  $("set-max-context-tokens").value = s.max_context_tokens ?? 32768;
  $("set-compaction-threshold").value = s.compaction_threshold ?? 0.65;
  $("set-context-window-turns").value = s.context_window_turns ?? 10;
  $("set-tool-result-max-chars").value = s.tool_result_max_chars ?? 8000;
  $("set-base-url").value = s.base_url || "";
  $("set-model").value = s.model || "";
  $("set-identity").value = s.identity || "";
  $("set-shell-allowlist").value = (s.shell_allowlist || []).join(", ");
  $("set-discord").checked = !!s.discord_enabled;
  const tokStatus = $("set-discord-token-status");
  if (tokStatus) tokStatus.textContent = s.discord_token_set ? "Token is configured." : "No token configured yet.";
  setAppearanceActive(s.appearance);
  const sel = $("set-theme");
  if (sel.value !== s.theme) sel.value = s.theme;
}

function setAppearanceActive(app) {
  $("set-appearance-light").classList.toggle("active", app === "light");
  $("set-appearance-dark").classList.toggle("active", app === "dark");
}

function currentAppearance() {
  return $("set-appearance-dark").classList.contains("active") ? "dark" : "light";
}

async function loadThemes() {
  try {
    const themes = await api("/api/themes");
    const sel = $("set-theme");
    sel.innerHTML = "";
    for (const t of themes) {
      const opt = document.createElement("option");
      opt.value = t;
      opt.textContent = t === "default" ? "Default (Bootstrap)" : t;
      sel.appendChild(opt);
    }
  } catch (e) {
    console.error(e);
  }
}

function collectSettings() {
  const s = {
    show_thinking: $("set-thinking").checked,
    appearance: currentAppearance(),
    theme: $("set-theme").value,
    base_url: $("set-base-url").value.trim(),
    model: $("set-model").value.trim(),
    identity: $("set-identity").value.trim(),
    shell_allowlist: $("set-shell-allowlist").value.split(",").map((x) => x.trim()).filter(Boolean),
    discord_enabled: $("set-discord").checked,
    max_tool_calls: parseInt($("set-max-tool-calls").value, 10) || 20,
    max_context_tokens: parseInt($("set-max-context-tokens").value, 10) || 32768,
    compaction_threshold: parseFloat($("set-compaction-threshold").value) || 0.65,
    context_window_turns: parseInt($("set-context-window-turns").value, 10) || 10,
    tool_result_max_chars: parseInt($("set-tool-result-max-chars").value, 10) || 8000,
  };
  const tok = $("set-discord-token").value.trim();
  if (tok) s.GREMLIN_DISCORD_TOKEN = tok;
  return s;
}

async function saveSettings() {
  const s = collectSettings();
  const prev = state.settings || {};
  const contextChanged = s.model !== prev.model || s.base_url !== prev.base_url;
  try {
    const saved = await api("/api/settings", { method: "POST", body: JSON.stringify(s) });
    applySettings(saved);
    if (contextChanged) {
      // A different model makes any previous count meaningless.
      state.lastPrompt = null;
      state.n_ctx = null;
      loadContextLimit();
    }
    toast("Settings saved");
  } catch (e) {
    toast(`Could not save settings: ${e.message}`);
  }
}

/* ---------------- context indicator ---------------- */
async function loadContextLimit() {
  try {
    const res = await api("/api/model/context");
    if (res.ok && typeof res.n_ctx === "number" && res.n_ctx > 0) {
      state.n_ctx = res.n_ctx;
    } else {
      state.n_ctx = null;
    }
  } catch (e) {
    state.n_ctx = null;
  }
  renderContextIndicator();
}

function renderContextIndicator() {
  const ind = $("context-indicator");
  if (!ind) return;
  const p = state.lastPrompt;
  // Prefer the model-reported limit; fall back to the configured window.
  const limit =
    (typeof state.n_ctx === "number" && state.n_ctx > 0)
      ? state.n_ctx
      : Number(state.settings?.max_context_tokens) || 0;
  if (p != null && limit > 0) {
    const pct = (p / limit) * 100;
    ind.textContent = `${Math.round(pct)}%`;
    ind.title = `${p.toLocaleString()} / ${limit.toLocaleString()} prompt tokens — last model request for this session`;
    ind.classList.toggle("warn", pct >= 80);
  } else if (p != null) {
    ind.textContent = `${p.toLocaleString()} prompt tokens`;
    ind.title = `${p.toLocaleString()} prompt tokens — last model request for this session (limit unknown)`;
    ind.classList.remove("warn");
  } else {
    ind.textContent = "–";
    ind.title = "";
    ind.classList.remove("warn");
  }
}

/* ---------------- views ---------------- */

function showView(name) {
  $("view-chat").classList.toggle("d-none", name !== "chat");
  $("view-settings").classList.toggle("d-none", name !== "settings");
}

/* ---------------- sessions ---------------- */

function renderSessions() {
  const list = $("session-list");
  list.innerHTML = "";
  for (const s of state.sessions) {
    const item = document.createElement("div");
    item.className = "session-item" + (s.id === state.activeId ? " active" : "");
    item.dataset.id = s.id;

    const title = document.createElement("button");
    title.type = "button";
    title.className = "session-title btn";
    title.textContent = s.title || "Untitled";
    title.addEventListener("click", () => selectSession(s.id));

    const rename = document.createElement("button");
    rename.type = "button";
    rename.className = "session-icon btn";
    rename.title = "Rename";
    rename.innerHTML = '<i class="bi bi-pencil"></i>';
    rename.addEventListener("click", (ev) => {
      ev.stopPropagation();
      renameSession(item, s.id);
    });

    const del = document.createElement("button");
    del.type = "button";
    del.className = "session-icon btn text-danger-emoji";
    del.title = "Delete";
    del.innerHTML = '<i class="bi bi-trash"></i>';
    del.addEventListener("click", (ev) => {
      ev.stopPropagation();
      deleteSession(s.id, s.title);
    });

    const compress = document.createElement("button");
    compress.type = "button";
    compress.className = "session-icon btn";
    compress.title = "Compress history (summarize to save tokens)";
    compress.innerHTML = '<i class="bi bi-box-seam"></i>';
    compress.addEventListener("click", (ev) => {
      ev.stopPropagation();
      compressSession(s.id, compress);
    });

    item.append(title, rename, compress, del);
    list.appendChild(item);
  }
}

async function refreshSessions(selectId) {
  state.sessions = await api("/api/sessions");
  if (selectId != null) state.activeId = selectId;
  renderSessions();
}

async function createSession() {
  const s = await api("/api/sessions", { method: "POST", body: JSON.stringify({}) });
  await refreshSessions(s.id);
  showView("chat");
  if (state.sidebar && window.innerWidth < 992) state.sidebar.hide();
  $("input").focus();
}

async function selectSession(id) {
  if (state.streaming) return;
  state.activeId = id;
  renderSessions();
  let session;
  try {
    session = await api(`/api/sessions/${id}`);
  } catch (e) {
    toast(`Could not load session: ${e.message}`);
    return;
  }
  renderMessages(session.messages || []);
  state.lastPrompt = session.last_usage && typeof session.last_usage.prompt_tokens === "number"
    ? session.last_usage.prompt_tokens
    : null;
  renderContextIndicator();
  if (state.sidebar && window.innerWidth < 992) state.sidebar.hide();
}

function renameSession(item, id) {
  const titleBtn = item.querySelector(".session-title");
  const current = titleBtn.textContent;
  const input = document.createElement("input");
  input.className = "form-control form-control-sm session-rename";
  input.value = current;
  item.replaceChildren(input);
  input.focus();
  input.select();
  let done = false;
  const commit = async (ok) => {
    if (done) return;
    done = true;
    const v = input.value.trim();
    if (ok && v && v !== current) {
      try {
        await api(`/api/sessions/${id}`, { method: "PATCH", body: JSON.stringify({ title: v }) });
      } catch (e) {
        toast(`Rename failed: ${e.message}`);
      }
    }
    await refreshSessions();
  };
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") commit(true);
    if (ev.key === "Escape") commit(false);
  });
  input.addEventListener("blur", () => commit(true));
}

async function deleteSession(id, title) {
  if (!confirm(`Delete session "${title}"? This cannot be undone.`)) return;
  try {
    await api(`/api/sessions/${id}`, { method: "DELETE" });
  } catch (e) {
    toast(`Delete failed: ${e.message}`);
    return;
  }
  if (state.activeId === id) {
    state.activeId = null;
    $("messages").innerHTML = "";
  }
  await refreshSessions();
  if (state.sessions.length === 0) {
    createSession();
  } else if (state.activeId === null) {
    selectSession(state.sessions[0].id);
  }
}

async function compressSession(id, btn) {
  if (state.streaming) return;
  if (btn) {
    btn.disabled = true;
    btn.classList.add("disabled");
  }
  toast("Compressing…");
  try {
    const res = await api(`/api/sessions/${id}/compress`, { method: "POST" });
    toast(`Compressed: ${res.chars} chars of summary`);
    await refreshSessions();
    if (state.activeId === id) await selectSession(id);
  } catch (e) {
    toast(`Compress failed: ${e.message}`);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.classList.remove("disabled");
    }
  }
}

/* ---------------- messages ---------------- */

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}

function toolChip(name, status) {
  const chip = el("span", "tool-chip" + (status === "error" ? " tool-chip-error" : ""));
  const icon = el("i", "bi " + (status === "error" ? "bi-x-circle" : "bi-gear"));
  chip.append(icon, el("span", "", name));
  return chip;
}

function renderTimeline(bubble, timeline) {
  for (const part of timeline) {
    if (part.t === "thinking") {
      const det = el("details", "thinking");
      det.append(el("summary", "thinking-summary", "Thinking"), el("div", "thinking-body", part.text));
      bubble.appendChild(det);
    } else if (part.t === "text") {
      bubble.appendChild(el("div", "msg-text", part.text));
    } else if (part.t === "tool") {
      let row = bubble.lastElementChild;
      if (!row || !row.classList.contains("tool-chips")) {
        row = el("div", "tool-chips");
        bubble.appendChild(row);
      }
      row.appendChild(toolChip(part.name, part.status));
    }
  }
}

function appendMessage(msg, container) {
  const row = el("div", `msg ${msg.role === "user" ? "msg-user" : "msg-assistant"} d-flex`);
  const bubble = el("div", `bubble ${msg.role === "user" ? "bubble-user" : "bubble-assistant"}`);

  let text = null;
  if (msg.role === "assistant") {
    if (Array.isArray(msg.timeline) && msg.timeline.length) {
      renderTimeline(bubble, msg.timeline);
    } else {
      if (msg.thinking) {
        const det = el("details", "thinking");
        const sum = el("summary", "thinking-summary", "Thinking");
        const body = el("div", "thinking-body", msg.thinking);
        det.append(sum, body);
        bubble.appendChild(det);
      }
      if (Array.isArray(msg.tool_calls) && msg.tool_calls.length) {
        const wrap = el("div", "tool-chips");
        for (const tc of msg.tool_calls) wrap.appendChild(toolChip(tc.name, tc.status));
        bubble.appendChild(wrap);
      }
      text = el("div", "msg-text", msg.content || "");
      bubble.appendChild(text);
    }
  } else {
    text = el("div", "msg-text", msg.content || "");
    bubble.appendChild(text);
  }
  row.appendChild(bubble);
  container.appendChild(row);
  return { row, bubble, text };
}

function renderMessages(messages) {
  const container = $("messages");
  container.innerHTML = "";
  if (!messages.length) {
    const hint = el("div", "empty-hint text-body-secondary text-center mt-5");
    hint.textContent = "Start a conversation — Gremlin can read and edit files in this project.";
    container.appendChild(hint);
    return;
  }
  for (const m of messages) appendMessage(m, container);
  container.scrollTop = container.scrollHeight;
}

/* ---------------- streaming chat ---------------- */

async function sendMessage() {
  const input = $("input");
  const text = input.value.trim();
  if (!text || state.streaming) return;
  if (!state.activeId) return;

  const container = $("messages");
  const hint = container.querySelector(".empty-hint");
  if (hint) hint.remove();

  appendMessage({ role: "user", content: text }, container);
  input.value = "";
  container.scrollTop = container.scrollHeight;

  // optimistic auto-title for brand-new sessions
  const session = state.sessions.find((s) => s.id === state.activeId);
  if (session && session.title === "New Session" && !session._titled) {
    const t = text.slice(0, 40) + (text.length > 40 ? "…" : "");
    session._titled = true;
    api(`/api/sessions/${state.activeId}`, {
      method: "PATCH",
      body: JSON.stringify({ title: t }),
    }).catch(() => {});
    session.title = t;
    renderSessions();
  }

  state.streaming = true;
  $("btn-send").disabled = true;
  input.placeholder = "Gremlin is thinking…";

  const refs = appendMessage({ role: "assistant", content: "" }, container);
  let pendingText = refs.text; // empty blinking placeholder, kept last until filled
  let lastPart = null;
  let lastKind = null;

  const place = (node) => {
    if (pendingText && pendingText.textContent === "") {
      refs.bubble.insertBefore(node, pendingText);
    } else {
      refs.bubble.appendChild(node);
    }
  };

  try {
    const res = await fetch(`/api/sessions/${state.activeId}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text }),
    });
    if (!res.ok || !res.body) {
      let detail = `HTTP ${res.status}`;
      try {
        const j = await res.json();
        if (j && j.error) detail = j.error;
      } catch (_) {}
      throw new Error(detail);
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) !== -1) {
        const block = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        for (const line of block.split("\n")) {
          if (!line.startsWith("data:")) continue;
          let payload;
          try {
            payload = JSON.parse(line.slice(5).trim());
          } catch (_) {
            continue;
          }
          handleEvent(payload);
        }
      }
    }
  } catch (e) {
    const err = el("div", "msg-error text-danger small", e.message);
    refs.bubble.appendChild(err);
  }

  function handleEvent(ev) {
    if (ev.type === "thinking") {
      if (!state.settings || !state.settings.show_thinking) return;
      if (lastKind === "thinking") {
        lastPart.querySelector(".thinking-body").textContent += ev.text;
      } else {
        const det = el("details", "thinking");
        det.open = true;
        det.append(el("summary", "thinking-summary", "Thinking"), el("div", "thinking-body", ev.text));
        place(det);
        lastPart = det;
        lastKind = "thinking";
      }
    } else if (ev.type === "text") {
      if (lastKind === "text") {
        lastPart.textContent += ev.text;
      } else if (pendingText && pendingText.textContent === "") {
        lastPart = pendingText;
        pendingText = null;
        lastKind = "text";
        lastPart.textContent = ev.text;
      } else {
        lastPart = el("div", "msg-text", ev.text);
        place(lastPart);
        lastKind = "text";
      }
    } else if (ev.type === "tool_call") {
      if (lastKind === "tool") {
        lastPart.appendChild(toolChip(ev.name, ev.status));
      } else {
        const row = el("div", "tool-chips");
        row.appendChild(toolChip(ev.name, ev.status));
        place(row);
        lastPart = row;
        lastKind = "tool";
      }
    } else if (ev.type === "error") {
      const err = el("div", "msg-error text-danger small", ev.message);
      refs.bubble.appendChild(err);
      lastPart = null;
      lastKind = null;
    } else if (ev.type === "usage") {
      if (ev.session_id === state.activeId) {
        state.lastPrompt = ev.prompt_tokens;
        renderContextIndicator();
      }
    } else if (ev.type === "done") {
      refreshSessions().catch(() => {});
    }
    container.scrollTop = container.scrollHeight;
  }

  state.streaming = false;
  $("btn-send").disabled = false;
  input.placeholder = "Message Gremlin…  (Enter to send, Shift+Enter for newline)";
  input.focus();
}

/* ---------------- boot ---------------- */

document.addEventListener("DOMContentLoaded", async () => {
  state.sidebar = bootstrap.Offcanvas.getOrCreateInstance($("sidebar"));
  $("btn-menu").addEventListener("click", () => state.sidebar.show());
  $("btn-new-session").addEventListener("click", createSession);
  $("btn-open-settings").addEventListener("click", () => {
    showView("settings");
    if (state.sidebar && window.innerWidth < 992) state.sidebar.hide();
  });
  $("btn-back-to-chat").addEventListener("click", () => showView("chat"));
  $("btn-save-settings").addEventListener("click", saveSettings);
  $("set-appearance-light").addEventListener("click", () => setAppearanceActive("light"));
  $("set-appearance-dark").addEventListener("click", () => setAppearanceActive("dark"));
  $("btn-send").addEventListener("click", sendMessage);
  $("input").addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && !ev.shiftKey) {
      ev.preventDefault();
      sendMessage();
    }
  });

  try {
    const settings = await api("/api/settings"); // persisted settings stick across reloads
    applySettings(settings);
  } catch (e) {
    console.error("settings load failed", e);
  }
  await loadThemes();
  if (state.settings) applySettings(state.settings); // restore select value
  loadContextLimit();

  try {
    state.sessions = await api("/api/sessions");
  } catch (e) {
    state.sessions = [];
  }
  if (state.sessions.length === 0) {
    const s = await api("/api/sessions", { method: "POST", body: JSON.stringify({}) }).catch(() => null);
    state.sessions = s ? [s] : [];
    if (s) state.activeId = s.id;
  }
  renderSessions();
  if (state.activeId) {
    selectSession(state.activeId);
  } else if (state.sessions.length) {
    selectSession(state.sessions[0].id);
  }
});

/*! Shop support chat widget. Embed:
 *  <script src="https://SUPPORT_HOST/widget.js" data-color="#0b6e4f" data-logo="https://.../logo.svg" data-title="Support" defer></script>
 */
(function () {
  "use strict";
  var script = document.currentScript || document.querySelector('script[src*="widget.js"]');
  if (!script || window.__shopSupportWidget) return;
  window.__shopSupportWidget = true;

  var base = new URL(script.src).origin;
  var color = script.getAttribute("data-color") || "#0b6e4f";
  var logo = script.getAttribute("data-logo") || "";
  var langAttr = (script.getAttribute("data-lang") || "auto").toLowerCase();
  var lang = langAttr === "auto" ? (navigator.language || "en").slice(0, 2).toLowerCase() : langAttr;
  if (["de", "en", "nl"].indexOf(lang) < 0) lang = "en";

  var I18N = {
    en: { title: "Support", privacy: "Privacy policy", hello: "Hi! I can answer questions about shipping, returns and sizes, check your order or find products.", ph: "Write a message…", send: "Send", open: "Open chat", close: "Close chat", reset: "New chat", sources: "Sources", up: "Helpful", down: "Not helpful", err: "Connection problem. Please try again.", thanks: "Thanks for your feedback!", ai: "AI assistant. Answers may contain mistakes; you can ask for a human at any time." },
    de: { title: "Support", privacy: "Datenschutz", hello: "Hallo! Ich beantworte Fragen zu Versand, Rückgabe und Größen, prüfe Ihre Bestellung oder suche Produkte.", ph: "Nachricht schreiben…", send: "Senden", open: "Chat öffnen", close: "Chat schließen", reset: "Neuer Chat", sources: "Quellen", up: "Hilfreich", down: "Nicht hilfreich", err: "Verbindungsproblem. Bitte erneut versuchen.", thanks: "Danke für Ihr Feedback!", ai: "KI-Assistent. Antworten können Fehler enthalten; ein Mensch ist jederzeit erreichbar." },
    nl: { title: "Support", privacy: "Privacybeleid", hello: "Hoi! Ik beantwoord vragen over verzending, retouren en maten, check je bestelling of zoek producten.", ph: "Typ een bericht…", send: "Verstuur", open: "Chat openen", close: "Chat sluiten", reset: "Nieuwe chat", sources: "Bronnen", up: "Nuttig", down: "Niet nuttig", err: "Verbindingsprobleem. Probeer het opnieuw.", thanks: "Bedankt voor je feedback!", ai: "AI-assistent. Antwoorden kunnen fouten bevatten; je kunt altijd om een medewerker vragen." }
  };
  var T = I18N[lang];
  var title = script.getAttribute("data-title") || T.title;
  var privacyUrl = script.getAttribute("data-privacy-url") || "";

  var store = {
    get: function (k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set: function (k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private mode */ } }
  };
  var sessionId = store.get("shopsupport.session") || "";

  // ---- DOM (Shadow DOM isolates our CSS from the shop theme) ----
  var host = document.createElement("div");
  host.id = "shop-support-widget";
  var root = host.attachShadow({ mode: "open" });
  root.innerHTML =
    "<style>" +
    ":host{all:initial}*{box-sizing:border-box;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif}" +
    ".fab{position:fixed;right:20px;bottom:20px;width:56px;height:56px;border-radius:50%;border:0;background:" + color + ";color:#fff;cursor:pointer;box-shadow:0 4px 14px rgba(0,0,0,.25);z-index:2147483000;display:flex;align-items:center;justify-content:center}" +
    ".fab img{width:30px;height:30px;object-fit:contain}.fab svg{width:28px;height:28px}" +
    ".panel{position:fixed;right:20px;bottom:90px;width:380px;max-width:calc(100vw - 24px);height:560px;max-height:calc(100vh - 110px);background:#fff;border-radius:14px;box-shadow:0 8px 30px rgba(0,0,0,.28);display:none;flex-direction:column;overflow:hidden;z-index:2147483000;color:#1b1b1b}" +
    ".panel.open{display:flex}" +
    "header{background:" + color + ";color:#fff;padding:12px 14px;display:flex;align-items:center;gap:10px;font-weight:600}" +
    "header img{height:24px}header span{flex:1}header button{background:none;border:0;color:#fff;font-size:22px;cursor:pointer;line-height:1}header button.reset{font-size:19px;opacity:.85}header button.reset:hover{opacity:1}" +
    ".msgs{flex:1;overflow:auto;padding:12px;display:flex;flex-direction:column;gap:8px;background:#f6f7f8}" +
    ".m{max-width:88%;padding:9px 12px;border-radius:12px;font-size:14px;line-height:1.4;white-space:pre-wrap;word-wrap:break-word}" +
    ".m.bot{background:#fff;border:1px solid #e3e5e8;align-self:flex-start}.m.user{background:" + color + ";color:#fff;align-self:flex-end}" +
    ".m a{color:inherit;text-decoration:underline}.m.err{background:#fde8e8;color:#8a1c1c;border-color:#f5c2c2}" +
    ".src{font-size:12px;margin-top:6px;color:#555}.src a{margin-right:8px}" +
    ".fb{margin-top:6px;display:flex;gap:6px;align-items:center;font-size:12px;color:#666}" +
    ".fb button{border:1px solid #d5d8dc;background:#fff;border-radius:8px;padding:2px 8px;cursor:pointer;font-size:14px}.fb button[disabled]{opacity:.5;cursor:default}" +
    ".note{font-size:11px;color:#777;text-align:center;padding:4px 10px}" +
    ".typing{display:inline-block;width:24px;height:8px;background:radial-gradient(circle,#999 3px,transparent 3px) 0 0/8px 8px repeat-x;animation:t 1s steps(4) infinite}@keyframes t{50%{opacity:.3}}" +
    "form{display:flex;gap:8px;padding:10px;border-top:1px solid #e3e5e8;background:#fff}" +
    "textarea{flex:1;resize:none;border:1px solid #cfd3d8;border-radius:10px;padding:8px 10px;font-size:14px;height:40px;max-height:100px}" +
    "textarea:focus,button:focus-visible{outline:2px solid " + color + ";outline-offset:1px}" +
    ".send{background:" + color + ";color:#fff;border:0;border-radius:10px;padding:0 14px;cursor:pointer;font-size:14px}.send[disabled]{opacity:.5}" +
    "@media(max-width:480px){.panel{right:0;bottom:0;width:100vw;max-width:100vw;height:100vh;max-height:100vh;border-radius:0}.fab{right:14px;bottom:14px}}" +
    "</style>" +
    '<button class="fab" aria-label="' + T.open + '"></button>' +
    '<section class="panel" role="dialog" aria-label="' + title + '">' +
    '<header>' + '<span></span><button class="reset" type="button" title="' + T.reset + '" aria-label="' + T.reset + '">↻</button><button class="close" type="button" aria-label="' + T.close + '">×</button></header>' +
    '<div class="msgs" role="log" aria-live="polite"></div><div class="note"></div>' +
    '<form><textarea rows="1" maxlength="2000"></textarea><button class="send" type="submit"></button></form></section>';

  var $ = function (s) { return root.querySelector(s); };
  var fab = $(".fab"), panel = $(".panel"), msgs = $(".msgs"), input = $("textarea"), sendBtn = $(".send");
  fab.innerHTML = logo ? '<img alt="" src="' + encodeURI(logo) + '">' : '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M4 4h16a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H8l-5 4V6a2 2 0 0 1 1-2z"/></svg>';
  $("header span").textContent = title;
  if (logo) { var hi = document.createElement("img"); hi.alt = ""; hi.src = logo; $("header").insertBefore(hi, $("header span")); }
  input.placeholder = T.ph; sendBtn.textContent = T.send; $(".note").textContent = T.ai;
  if (/^https?:\/\//i.test(privacyUrl)) {
    var pa = document.createElement("a"); pa.href = privacyUrl; pa.target = "_blank"; pa.rel = "noopener noreferrer"; pa.textContent = " " + T.privacy;
    pa.style.color = "inherit"; $(".note").appendChild(pa);
  }
  (document.body || document.documentElement).appendChild(host);

  // ---- rendering (textContent only: model output is never trusted as HTML) ----
  var URL_RE = /(https?:\/\/[^\s)]+)/g;
  var BOLD_RE = /\*\*([^*\n]+)\*\*/g;
  function fillText(el, text) {
    el.textContent = "";
    // the model sometimes uses **bold** despite the prompt: render it as <strong> (DOM nodes only, never innerHTML)
    var segments = [], last = 0, m;
    BOLD_RE.lastIndex = 0;
    while ((m = BOLD_RE.exec(text)) !== null) {
      if (m.index > last) segments.push([text.slice(last, m.index), false]);
      segments.push([m[1], true]); last = m.index + m[0].length;
    }
    if (last < text.length) segments.push([text.slice(last), false]);
    segments.forEach(function (seg) {
      var parent = el;
      if (seg[1]) { parent = document.createElement("strong"); el.appendChild(parent); }
      seg[0].split(URL_RE).forEach(function (part, i) {
        if (i % 2 === 1) { var a = document.createElement("a"); a.href = part; a.target = "_blank"; a.rel = "noopener noreferrer"; a.textContent = part; parent.appendChild(a); }
        else if (part) parent.appendChild(document.createTextNode(part));
      });
    });
  }
  function addMsg(role, text) {
    var d = document.createElement("div"); d.className = "m " + (role === "user" ? "user" : role === "err" ? "bot err" : "bot");
    fillText(d, text); msgs.appendChild(d); msgs.scrollTop = msgs.scrollHeight; return d;
  }
  function addSources(el, items) {
    if (!items || !items.length) return;
    var s = document.createElement("div"); s.className = "src"; s.appendChild(document.createTextNode(T.sources + ": "));
    items.forEach(function (it) {
      var label = "[" + it.n + "] " + it.title, node;
      if (it.url && /^https?:/i.test(it.url)) { node = document.createElement("a"); node.href = it.url; node.target = "_blank"; node.rel = "noopener noreferrer"; node.textContent = label; }
      else { node = document.createElement("span"); node.textContent = label + " "; }
      s.appendChild(node);
    });
    el.appendChild(s);
  }
  function addFeedback(el, messageId) {
    if (!messageId) return;
    var f = document.createElement("div"); f.className = "fb";
    [["👍", 1, T.up], ["👎", -1, T.down]].forEach(function (b) {
      var btn = document.createElement("button"); btn.type = "button"; btn.textContent = b[0]; btn.title = b[2]; btn.setAttribute("aria-label", b[2]);
      btn.onclick = function () {
        f.querySelectorAll("button").forEach(function (x) { x.disabled = true; });
        fetch(base + "/api/chat/feedback", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ session_id: sessionId, message_id: messageId, value: b[1] }) })
          .then(function () { var t = document.createElement("span"); t.textContent = T.thanks; f.appendChild(t); }).catch(function () {});
      };
      f.appendChild(btn);
    });
    el.appendChild(f);
  }

  // ---- transport: WebSocket with SSE fallback ----
  var ws = null, wsFailures = 0, busy = false, current = null, sseAbort = null, epoch = 0;
  function setBusy(b) { busy = b; sendBtn.disabled = b; }

  function handle(ev) {
    // after "New chat" a late event of the abandoned answer must change nothing (not even the session id)
    if (!current && ev.type !== "error") return;
    if (ev.type === "session") { sessionId = ev.id; store.set("shopsupport.session", sessionId); }
    else if (ev.type === "token") { if (current.typing) { current.el.textContent = ""; current.typing = false; } current.text += ev.text; fillText(current.el, current.text); msgs.scrollTop = msgs.scrollHeight; }
    else if (ev.type === "reset") { current.text = ""; current.el.textContent = ""; }
    else if (ev.type === "replace") { current.text = ev.text; current.typing = false; fillText(current.el, ev.text); }
    else if (ev.type === "sources") { current.sources = ev.items; }
    else if (ev.type === "done") { addSources(current.el, current.sources); addFeedback(current.el, ev.message_id); setBusy(false); current = null; }
    else if (ev.type === "error") { if (current) { current.el.className = "m bot err"; current.el.textContent = ev.text || T.err; current = null; } setBusy(false); }
  }

  function connect() {
    return new Promise(function (resolve, reject) {
      var url = base.replace(/^http/, "ws") + "/ws/chat" + (sessionId ? "?session_id=" + encodeURIComponent(sessionId) : "");
      var s = new WebSocket(url), opened = false;
      s.onopen = function () { opened = true; wsFailures = 0; ws = s; resolve(s); };
      s.onmessage = function (m) { try { handle(JSON.parse(m.data)); } catch (e) { /* ignore malformed frame */ } };
      s.onclose = function () {
        var wasCurrent = ws === s; // a socket we dropped on purpose (New chat) must not touch the new conversation
        if (wasCurrent) ws = null;
        if (!opened) { wsFailures++; reject(new Error("ws")); } else if (wasCurrent && busy && current) { handle({ type: "error", text: T.err }); }
      };
      s.onerror = function () { /* handled by onclose */ };
    });
  }

  function sendSse(text) {
    sseAbort = typeof AbortController === "function" ? new AbortController() : null;
    return fetch(base + "/api/chat/stream", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ session_id: sessionId || null, text: text }), signal: sseAbort ? sseAbort.signal : undefined })
      .then(function (res) {
        if (!res.ok || !res.body) throw new Error("http " + res.status);
        var reader = res.body.getReader(), dec = new TextDecoder(), buf = "";
        function pump() {
          return reader.read().then(function (r) {
            if (r.done) return;
            buf += dec.decode(r.value, { stream: true });
            var parts = buf.split("\n\n"); buf = parts.pop();
            parts.forEach(function (p) { if (p.indexOf("data: ") === 0) { try { handle(JSON.parse(p.slice(6))); } catch (e) { /* skip */ } } });
            return pump();
          });
        }
        return pump();
      });
  }

  function send(text) {
    addMsg("user", text);
    current = { el: addMsg("bot", ""), text: "", sources: null, typing: true };
    current.el.innerHTML = '<span class="typing"></span>';
    setBusy(true);
    var payload = JSON.stringify({ type: "message", session_id: sessionId || undefined, text: text });
    var ep = epoch; // "New chat" pressed while we were still connecting: drop this send instead of reviving the old session
    var viaWs = ws && ws.readyState === 1 ? Promise.resolve(ws) : (wsFailures >= 2 ? Promise.reject(new Error("ws disabled")) : connect());
    viaWs.then(function (s) {
      if (ep !== epoch) { if (ws === s) ws = null; try { s.close(); } catch (e) { /* ignore */ } return; }
      s.send(payload);
    })
      .catch(function () { return ep === epoch ? sendSse(text) : undefined; })
      .catch(function () { handle({ type: "error", text: T.err }); });
  }

  // ---- UI events ----
  var loaded = false;

  // "New chat": forget the session and start from scratch, as a first-time visitor. The old conversation stays on the
  // server (retention rules apply) but is no longer reachable from this browser.
  function resetChat() {
    epoch++;
    if (sseAbort) { try { sseAbort.abort(); } catch (e) { /* already finished */ } sseAbort = null; }
    if (ws) { var old = ws; ws = null; try { old.close(); } catch (e) { /* already closed */ } } // its URL carries the old session id
    wsFailures = 0; current = null; setBusy(false);
    sessionId = ""; store.set("shopsupport.session", "");
    loaded = true; // nothing to restore
    msgs.textContent = ""; addMsg("bot", T.hello);
    input.value = ""; input.focus();
  }
  $(".reset").onclick = resetChat;
  function openPanel() {
    panel.classList.add("open"); input.focus();
    if (loaded) return; loaded = true;
    addMsg("bot", T.hello);
    if (!sessionId) return;
    fetch(base + "/api/chat/history?session_id=" + encodeURIComponent(sessionId)).then(function (r) { return r.ok ? r.json() : null; }).then(function (h) {
      if (!h || !h.messages.length) { if (!h) { sessionId = ""; store.set("shopsupport.session", ""); } return; }
      msgs.textContent = ""; addMsg("bot", T.hello);
      h.messages.forEach(function (m) { var el = addMsg(m.role, m.content); if (m.role === "assistant") { addSources(el, m.sources); if (!m.feedback) addFeedback(el, m.id); } });
    }).catch(function () {});
  }
  fab.onclick = function () { panel.classList.contains("open") ? panel.classList.remove("open") : openPanel(); };
  $(".close").onclick = function () { panel.classList.remove("open"); fab.focus(); };
  panel.addEventListener("keydown", function (e) { if (e.key === "Escape") { panel.classList.remove("open"); fab.focus(); } });
  $("form").onsubmit = function (e) { e.preventDefault(); var t = input.value.trim(); if (!t || busy) return; input.value = ""; send(t); };
  input.addEventListener("keydown", function (e) { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("form").requestSubmit(); } });
})();

/* The session renderer: a trace event stream in, a chat transcript out.
 *
 * One implementation serves both views. The live page feeds it Server-Sent
 * Events; the baked page feeds it a frozen array through the same `handle()`.
 * Nothing here knows which it is — that is the whole point of the split, and
 * why a published page cannot drift from what the operator watched.
 *
 * Shape of the output: the operator's task, then one *turn* per model
 * completion — its prose, then a step card holding the code it ran, the
 * actions that code triggered, and the output that came back — then the
 * answer. Prompt snapshots and thinking are present but folded away; a
 * refusal is not, because a refusal is the claim this project makes.
 *
 * Untrusted input, always: prose and output go through `renderMarkdown`
 * (DOM-built, never `innerHTML`), everything else through `textContent`.
 */

/* --------------------------------------------------------------- theme --- */

function isDark() {
  var set = document.documentElement.getAttribute("data-theme");
  if (set) return set === "dark";
  return !matchMedia("(prefers-color-scheme: light)").matches;
}
function wireTheme() {
  var btn = document.getElementById("theme");
  if (!btn) return;
  function paint() {
    btn.textContent = isDark() ? "☾" : "☀";
    btn.title = isDark() ? "Switch to light" : "Switch to dark";
  }
  btn.onclick = function () {
    var next = isDark() ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try { localStorage.setItem("ph-theme", next); } catch (e) { /* private mode */ }
    paint();
  };
  paint();
}

/* ---------------------------------------------------------------- utils -- */

function el(tag, cls, text) {
  var n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = text;
  return n;
}
function fmtUsd(x) {
  var v = Number(x) || 0;
  return v > 0 && v < 0.01 ? "$" + v.toFixed(4) : "$" + v.toFixed(2);
}
function fmtDur(s) {
  s = Math.max(0, Math.floor(s));
  return Math.floor(s / 60) + "m" + String(s % 60).padStart(2, "0") + "s";
}
function byId(id) { return document.getElementById(id); }

var rootLog, nowEl, bannerEl;
var active = new Map();     // laneKey -> {label, t0, lane}
var approvals = new Map();  // key -> {label, t0}
var lanes = new Map();      // session dir name -> lane
var rootName = null;
var followTail = true;
var sessionStartMs = 0;
var searchTerm = "";

addEventListener("scroll", function () {
  followTail = innerHeight + scrollY >= document.body.scrollHeight - 80;
  var jump = byId("jump");
  if (jump) jump.hidden = followTail;
});
function scrollTail() { if (followTail) scrollTo(0, document.body.scrollHeight); }

/* ---------------------------------------------------------------- cells -- */

function copyButton(getText) {
  var b = el("button", "mini", "copy");
  b.onclick = function (ev) {
    ev.preventDefault();
    if (navigator.clipboard) navigator.clipboard.writeText(getText() || "");
    b.textContent = "copied";
    setTimeout(function () { b.textContent = "copy"; }, 1200);
  };
  return b;
}

/** Clamp a tall body behind a "show more", so one 900-line output does not
 *  bury the rest of the session. */
function clamp(body, node) {
  requestAnimationFrame(function () {
    if (node.scrollHeight <= 320) return;
    node.classList.add("clamp");
    var more = el("button", "more", "show all (" + node.scrollHeight + "px)");
    more.onclick = function () {
      node.classList.toggle("clamp");
      more.textContent = node.classList.contains("clamp") ? "show all" : "show less";
    };
    body.appendChild(more);
  });
}

/** A code or output cell: a one-line bar that stays readable when collapsed,
 *  plus the body. Output that looks like markdown renders as markdown, with
 *  `raw` one click away — the record has to stay inspectable. */
function cell(kind, text) {
  text = text || "";
  var wrap = el("div", "cell k-" + kind);
  var bar = el("div", "cellbar");
  bar.appendChild(el("span", "lbl", kind));
  var first = text.split("\n", 1)[0].slice(0, 96);
  bar.appendChild(el("span", "peek", first));
  var tools = el("div", "tools");

  var body = el("div", "cellbody");
  var rendered = kind === "output" && window.looksLikeMarkdown && looksLikeMarkdown(text);

  function paint() {
    body.replaceChildren();
    if (rendered) {
      body.appendChild(renderMarkdown(text));
    } else {
      var pre = el("pre", kind === "code" ? "src" : "out", text);
      body.appendChild(pre);
      clamp(body, pre);
    }
  }

  if (kind === "output" && window.looksLikeMarkdown && looksLikeMarkdown(text)) {
    var raw = el("button", "mini", "raw");
    raw.onclick = function () {
      rendered = !rendered;
      raw.classList.toggle("on", !rendered);
      raw.textContent = rendered ? "raw" : "rendered";
      paint();
    };
    tools.appendChild(raw);
  }
  tools.appendChild(copyButton(function () { return text; }));
  var fold = el("button", "mini", "−");
  fold.onclick = function () {
    body.hidden = !body.hidden;
    fold.textContent = body.hidden ? "+" : "−";
  };
  tools.appendChild(fold);
  bar.appendChild(tools);
  wrap.appendChild(bar);
  wrap.appendChild(body);
  paint();
  return wrap;
}

/** The full prompt sent for one completion. Folded, and built only on first
 *  open — a session's prompt snapshots outweigh everything else on the page. */
function promptView(e) {
  var d = el("details", "fold prompt k-prompt");
  var sum = el("summary");
  sum.appendChild(el("span", null, "prompt" +
    (e.input_tokens ? " · " + e.input_tokens + " tok" : "") +
    (e.model ? " · " + e.model : "")));
  d.appendChild(sum);
  var body = el("div", "body");
  d.appendChild(body);
  var built = false;
  d.addEventListener("toggle", function () {
    if (!d.open || built) return;
    built = true;
    if (e.system) {
      body.appendChild(el("div", "role sys", "system"));
      body.appendChild(el("pre", "out", e.system));
    }
    (e.messages || []).forEach(function (m) {
      body.appendChild(el("div", "role", m.role));
      if (typeof m.text === "string") { body.appendChild(el("pre", "out", m.text)); return; }
      (m.content || []).forEach(function (b) {
        if (b.type === "text") body.appendChild(el("pre", "out", b.text || ""));
        else if (b.type === "tool_use") body.appendChild(el("pre", "out", "→ " + (b.name || "") +
          "\n" + ((b.input && b.input.code) || JSON.stringify(b.input || {}))));
        else if (b.type === "tool_result") body.appendChild(el("pre", "out", "⟵ result\n" +
          (typeof b.content === "string" ? b.content : JSON.stringify(b.content))));
        else if (b.type === "image") body.appendChild(el("pre", "out", "[image]"));
        else body.appendChild(el("pre", "out", JSON.stringify(b)));
      });
    });
  });
  return d;
}

/* ---------------------------------------------------------------- lanes -- */

function makeLane(name, logEl, isRoot, head) {
  var lane = { name: name, logEl: logEl, isRoot: isRoot, head: head, spend: 0, steps: 0,
    step: null, think: null, refused: new Set() };
  lanes.set(name, lane);
  return lane;
}
function laneFor(name) {
  if (!name || name === rootName) return lanes.get(rootName);
  return lanes.get(name) || openSubLane(name, null, lanes.get(rootName));
}
function shortName(name) { return name.replace(/^.*-spawn-/, "spawn-"); }

function openSubLane(name, spawn, parent) {
  if (lanes.get(name)) return lanes.get(name);
  var panel = el("details", "subagent");
  panel.open = true;
  var sum = el("summary");
  var dot = el("span", "dot");
  sum.appendChild(dot);
  sum.appendChild(el("span", "sub-name", shortName(name)));
  if (spawn) {
    if (spawn.tier) sum.appendChild(el("span", "chip", spawn.tier));
    sum.appendChild(el("span", "sub-task", spawn.text || ""));
  }
  var stat = el("span", "sub-stat", "running…");
  sum.appendChild(stat);
  panel.appendChild(sum);
  var body = el("div", "sub-body");
  panel.appendChild(body);
  (parent ? parent.logEl : rootLog).appendChild(panel);
  scrollTail();
  return makeLane(name, body, false, { dot: dot, stat: stat, budget: spawn && spawn.budget_usd });
}

function laneAdd(lane, node, kindClass) {
  node.classList.add("item-row");
  if (kindClass) node.classList.add(kindClass);
  lane.logEl.appendChild(node);
  applyRowVisibility(node);
  scrollTail();
  return node;
}

/** The step card: one code cell and everything that code caused. Opened by a
 *  `code` event, closed by the next completion, answer, or task. */
function stepCard(lane) {
  if (lane.step) return lane.step;
  var card = el("div", "step");
  var head = el("div", "step-head");
  lane.steps++;
  head.appendChild(el("span", "idx", "step " + lane.steps));
  var chips = el("div", "chips k-action");
  head.appendChild(chips);
  card.appendChild(head);
  laneAdd(lane, card);
  lane.step = { card: card, chips: chips };
  return lane.step;
}
function closeStep(lane) { lane.step = null; }

function into(lane, node, kindClass) {
  // Output and media belong to the step that produced them; anything arriving
  // without an open step still has to land somewhere visible.
  if (lane.step) {
    node.classList.add("item-row");
    if (kindClass) node.classList.add(kindClass);
    lane.step.card.appendChild(node);
    applyRowVisibility(node);
    scrollTail();
    return node;
  }
  return laneAdd(lane, node, kindClass);
}

/* ------------------------------------------------------------- rollups --- */

function updateStats() {
  var total = 0;
  lanes.forEach(function (l) { total += l.spend; });
  var c = byId("stat-cost");
  if (c) c.textContent = fmtUsd(total);
}
function laneSpent(lane, usd, cumulative) {
  if (usd == null) return;
  lane.spend = cumulative ? Math.max(lane.spend, usd) : lane.spend + usd;
  if (lane.head) {
    lane.head.stat.textContent = fmtUsd(lane.spend) +
      (lane.head.budget ? " / " + fmtUsd(lane.head.budget) : "");
  }
  updateStats();
}
function laneDone(lane, cls, label) {
  if (!lane.head) return;
  lane.head.dot.className = "dot " + cls;
  lane.head.stat.textContent = lane.head.stat.textContent.split(" — ")[0] + " — " + label;
}

/* --------------------------------------------------- in-flight + banner -- */

function renderNow() {
  if (!nowEl) return;
  nowEl.replaceChildren();
  active.forEach(function (a) {
    var item = el("div", "nowitem");
    item.appendChild(el("span", "spin"));
    item.appendChild(el("span", null,
      (a.lane && !a.lane.isRoot ? "[" + shortName(a.lane.name) + "] " : "") + a.label));
    item.appendChild(el("span", "elapsed", ""));
    item.dataset.t0 = a.t0;
    nowEl.appendChild(item);
  });
}
function renderBanner() {
  if (!bannerEl) return;
  bannerEl.replaceChildren();
  approvals.forEach(function (a) {
    var item = el("div", "approval");
    item.appendChild(el("span", null, "⚠"));
    item.appendChild(el("span", null, a.label));
    item.appendChild(el("span", "elapsed", ""));
    item.dataset.t0 = a.t0;
    bannerEl.appendChild(item);
  });
}
var ticker = setInterval(function () {
  if (!nowEl || !bannerEl) return;
  [].concat([].slice.call(nowEl.children), [].slice.call(bannerEl.children))
    .forEach(function (item) {
      var s = (Date.now() - Number(item.dataset.t0)) / 1000;
      item.querySelector(".elapsed").textContent = s.toFixed(0) + "s";
    });
  if (sessionStartMs) {
    var clock = byId("stat-clock");
    if (clock) clock.textContent = fmtDur((Date.now() - sessionStartMs) / 1000);
  }
}, 500);
function startActive(lane, sub, label) {
  active.set(lane.name + "\x00" + sub, { label: label, t0: Date.now(), lane: lane });
  renderNow();
}
function endActive(lane, sub) { active.delete(lane.name + "\x00" + sub); renderNow(); }

/* ------------------------------------------------- filters, search, nav -- */

function applyRowVisibility(node) {
  if (searchTerm && node.textContent.toLowerCase().indexOf(searchTerm) === -1) {
    node.classList.add("search-hide");
  } else {
    node.classList.remove("search-hide");
  }
}

function wireChrome() {
  document.querySelectorAll("#viewpop input[data-f]").forEach(function (cb) {
    cb.onchange = function () {
      document.body.classList.toggle("hide-" + cb.dataset.f, !cb.checked);
    };
  });
  var menu = byId("viewmenu"), pop = byId("viewpop");
  if (menu && pop) {
    menu.onclick = function (ev) { ev.stopPropagation(); pop.hidden = !pop.hidden; };
    pop.onclick = function (ev) { ev.stopPropagation(); };
    addEventListener("click", function () { pop.hidden = true; });
  }
  // Filtering is occasional, so the input is folded away behind an icon rather
  // than holding a permanent row of the topbar. Closing it also clears the
  // term — a hidden filter still hiding half the transcript is a trap.
  var search = byId("search"), bar = byId("searchbar");
  if (search && bar) {
    function applyTerm(term) {
      searchTerm = term.toLowerCase();
      var hits = 0;
      rootLog.querySelectorAll(".item-row").forEach(function (r) {
        applyRowVisibility(r);
        if (searchTerm && !r.classList.contains("search-hide")) hits++;
      });
      byId("searchcount").textContent = searchTerm ? hits + " match" : "";
    }
    function openSearch() {
      bar.hidden = false;
      byId("searchbtn").classList.add("on");
      search.focus();
    }
    function closeSearch() {
      bar.hidden = true;
      byId("searchbtn").classList.remove("on");
      search.value = "";
      applyTerm("");
    }
    search.oninput = function (ev) { applyTerm(ev.target.value); };
    byId("searchbtn").onclick = function () { bar.hidden ? openSearch() : closeSearch(); };
    byId("searchclose").onclick = closeSearch;
    addEventListener("keydown", function (ev) {
      if (ev.key === "/" && document.activeElement !== search) { ev.preventDefault(); openSearch(); }
      if (ev.key === "Escape" && document.activeElement === search) closeSearch();
    });
  }
  var jump = byId("jump");
  if (jump) {
    jump.onclick = function () { followTail = true; scrollTail(); jump.hidden = true; };
    jump.hidden = true;
  }
  renderSessionList();
}

/* -------------------------------------------------------------- sidebar -- */
/* Static pages carry their session list baked in (`window.SESSIONS`, and a
 * `currentSession` the feed assigns); the live viewer polls `/sessions` and
 * defines `switchTo`. Same markup either way, so the switcher looks and
 * behaves identically in the archive and in front of a running agent. */

var currentSession = null;
var sessionList = [];  // last list rendered — a re-render must not wipe the rail
var sessionSig = null;

function renderSessionList(list) {
  var host = byId("sessions");
  if (!host) return;
  var items = list || window.SESSIONS || sessionList;
  sessionList = items;
  // The live viewer polls every few seconds. Rebuilding the rail each time
  // would throw away scroll position and, worse, swap the node out from under
  // a click in flight — so repaint only when something actually moved.
  var sig = currentSession + "|" + items.map(function (s) {
    return s.name + ":" + s.outcome + ":" + s.steps + ":" + s.cost_usd;
  }).join(",");
  if (sig === sessionSig) return;
  sessionSig = sig;
  if (!items.length) {
    host.replaceChildren(el("div", "slist-empty", "no sessions yet"));
    return;
  }
  var frag = document.createDocumentFragment();
  items.forEach(function (s) {
    var node = s.href ? el("a", "sitem") : el("button", "sitem");
    if (s.href) node.href = s.href;
    if (s.name === currentSession) node.classList.add("on");
    node.appendChild(el("span", "n", s.name));
    if (s.outcome) {
      var m = el("span", "m");
      m.appendChild(el("span", "pill " + (s.outcome === "answered" ? "ok" : "warn"), s.outcome));
      node.appendChild(m);
    }
    // A baked page links; the live viewer switches the stream in place. Picking
    // a session by hand also stops following the newest — otherwise the next
    // poll drags you straight back to it, four seconds after you chose.
    if (!s.href) {
      node.onclick = function () {
        var follow = byId("follow");
        if (follow) follow.checked = false;
        window.switchTo(s.name);
      };
    }
    frag.appendChild(node);
  });
  host.replaceChildren(frag);
}

/** Site nav, for a baked page. The live viewer has nowhere to navigate to and
 *  supplies nothing, leaving the container empty. */
function renderNav(items) {
  var host = byId("nav");
  if (!host || !items || !items.length) return;
  var frag = document.createDocumentFragment();
  items.forEach(function (it) {
    var a = el("a");
    a.href = it.href;
    a.appendChild(el("span", "ico", it.icon || "\u25c6"));
    a.appendChild(document.createTextNode(it.label));
    frag.appendChild(a);
  });
  host.replaceChildren(frag);
}

function setStatus(text, cls) {
  var s = byId("status");
  if (!s) return;
  s.textContent = text;
  s.className = "dotstat " + (cls || "");
}

/* --------------------------------------------------------------- events -- */

function resetAll(name) {
  if (!rootLog) return;  // a docs/index page has no feed to reset
  rootLog.replaceChildren();
  nowEl.replaceChildren();
  bannerEl.replaceChildren();
  active.clear();
  approvals.clear();
  lanes.clear();
  rootName = name;
  currentSession = name;
  sessionStartMs = 0;
  makeLane(name, rootLog, true, null);
  var t = byId("session-title");
  if (t) t.textContent = name;
  document.title = "pyharness — " + name;
  updateStats();
}

function handle(e) {
  var k = e.kind;
  if (k === "watch_session") { resetAll(e.session); renderSessionList(); return; }
  if (rootName === null) resetAll(e.session || "session");
  if (!sessionStartMs) sessionStartMs = Date.now();
  var lane = laneFor(e.session);
  if (!lane) return;

  if (k === "spawn") {
    openSubLane(e.child, e, lane);
  } else if (k === "spawned_by") {
    // the child->parent link; the nested panel already carries the relationship
  } else if (k === "session_start") {
    laneAdd(lane, el("div", "note", "session started"));
  } else if (k === "task") {
    closeStep(lane);
    var task = el("div", "task");
    task.appendChild(el("div", "who", "task"));
    task.appendChild(renderMarkdown(e.text || ""));
    laneAdd(lane, task);
  } else if (k === "llm_start") {
    startActive(lane, "llm", "thinking (" + (e.tier || "?") + ")…");
  } else if (k === "llm_thinking") {
    if (!lane.think) {
      var d = el("details", "fold think k-think");
      var sum = el("summary");
      var meta = el("span", null, "thinking");
      sum.appendChild(meta);
      d.appendChild(sum);
      var body = el("div", "body");
      var pre = el("pre", "out", "");
      body.appendChild(pre);
      d.appendChild(body);
      lane.think = { pre: pre, meta: meta };
      laneAdd(lane, d);
    }
    lane.think.pre.textContent += e.text || "";
    lane.think.meta.textContent = "thinking";
  } else if (k === "llm_call") {
    endActive(lane, "llm");
    lane.think = null;
    closeStep(lane);
    if (e.text) {
      var msg = el("div", "msg");
      msg.appendChild(renderMarkdown(e.text));
      laneAdd(lane, msg);
    }
    laneAdd(lane, promptView(e));
    laneSpent(lane, e.cost_usd, false);
  } else if (k === "code") {
    closeStep(lane);
    stepCard(lane);
    into(lane, cell("code", e.text), "k-code");
    updateStats();
  } else if (k === "output") {
    into(lane, cell("output", e.text), "k-output");
  } else if (k === "media") {
    var img = el("img", "shot");
    img.src = e.src || "";
    img.alt = e.text || "screenshot";
    img.loading = "lazy";
    var holder = el("div", "cellbody");
    holder.appendChild(img);
    into(lane, holder, "k-output");
  } else if (k === "action_start") {
    startActive(lane, "a:" + e.text, e.text + (e.args ? "(" + e.args + ")" : ""));
  } else if (k === "action_end") {
    endActive(lane, "a:" + e.text);
    // A refusal is not a failure — it is the thing this harness claims to do,
    // and it reaches the trace three ways: the broker denying outright
    // (`decision: "deny"`), the call it gated raising PermissionDenied, and an
    // action the human refused at the gate, which records only `ok: false`
    // against an earlier `approval_resolved`. All read as refused; only a
    // genuine crash reads as an error.
    var denied = e.decision === "deny" || /PermissionDenied/.test(e.error || "");
    if (!denied && e.ok === false && lane.refused.has(e.text)) {
      lane.refused.delete(e.text);
      denied = true;
    }
    var ok = e.ok !== false && !denied;
    var chip = el("span", "chip " + (denied ? "deny" : ok ? "ok" : "err"));
    chip.appendChild(el("span", null, e.text));
    // Only when it is long enough to be worth knowing — every action carrying
    // "0.001s" is four characters of nothing on every row.
    if (e.elapsed_s >= 0.5) chip.appendChild(el("span", "t", e.elapsed_s + "s"));
    if (e.decision === "deny") chip.appendChild(el("span", "t", "refused by policy"));
    else if (e.error) chip.appendChild(el("span", "t", e.error));
    if (lane.step) {
      lane.step.chips.appendChild(chip);
    } else {
      var row = el("div", "note k-action");
      row.appendChild(chip);
      laneAdd(lane, row);
    }
  } else if (k === "approval_pending") {
    approvals.set(e.text, {
      label: (lane.isRoot ? "" : "[" + shortName(lane.name) + "] ") +
        "[" + (e.category || "") + "] " + e.text + " — " + (e.summary || "") +
        " (answer in the terminal)",
      t0: Date.now(),
    });
    renderBanner();
  } else if (k === "approval_resolved") {
    approvals.delete(e.text);
    renderBanner();
    var row = el("div", "note");
    if (e.outcome === "deny") lane.refused.add(e.text);
    var verdict = el("span", "chip " + (e.outcome === "deny" ? "deny" : "ok"));
    verdict.appendChild(el("span", null, e.text));
    verdict.appendChild(el("span", "t", "approval " + e.outcome));
    row.appendChild(verdict);
    into(lane, row);
  } else if (k === "notify") {
    var n = el("div", "note warn");
    n.appendChild(el("span", null, "note"));
    n.appendChild(renderMarkdown(e.text || ""));
    laneAdd(lane, n);
  } else if (k === "error") {
    endActive(lane, "llm");
    lane.think = null;
    closeStep(lane);
    laneAdd(lane, el("div", "note err", e.text));
    if (!lane.isRoot) laneDone(lane, "err", "error");
  } else if (k === "answer") {
    closeStep(lane);
    var ans = el("div", "answer");
    ans.appendChild(el("div", "who", "answer"));
    ans.appendChild(renderMarkdown(e.text || ""));
    laneAdd(lane, ans);
    if (!lane.isRoot) laneDone(lane, "done", "answered");
  } else if (k === "budget") {
    laneSpent(lane, e.spent_usd, true);
  } else if (k === "session_end") {
    endActive(lane, "llm");
    closeStep(lane);
    laneSpent(lane, e.spent_usd, true);
    laneAdd(lane, el("div", "note", "session ended"));
    if (!lane.isRoot && lane.head && lane.head.dot.className === "dot") {
      laneDone(lane, "done", "done");
    }
  } else if (k === "skill_use") {
    into(lane, el("div", "note", "skill " + (e.skill || "") + ": " + (e.outcome || "") +
      " " + (e.note || "")));
  } else if (k === "reflection") {
    laneAdd(lane, el("div", "note", "reflection: " + (e.text || "")));
  } else if (k === "worker") {
    into(lane, el("div", "note", e.text || ""));
  } else if (k === "llm_attempt") {
    laneAdd(lane, el("div", "note", "llm " + (e.text || "")));
  } else if (k === "note") {
    // preamble text, duplicated by the llm_call event — skip
  } else {
    // Unknown kind. A viewer that silently drops what it does not recognize is
    // worse than one that renders it plainly: a new trace kind (or an old
    // session replayed by a newer viewer) would simply not exist on screen,
    // with nothing to notice. Render the kind and whatever fields it carried.
    var rest = Object.keys(e)
      .filter(function (f) { return ["kind", "ts", "text", "session"].indexOf(f) === -1; })
      .map(function (f) {
        return f + "=" + (typeof e[f] === "object" ? JSON.stringify(e[f]) : e[f]);
      });
    laneAdd(lane, el("div", "note", k + (e.text ? ": " + e.text : "") +
      (rest.length ? " · " + rest.join(" ") : "")));
  }
}

/* ----------------------------------------------------------------- boot -- */
/* The scripts sit at the end of <body>, so the DOM is already there. The feed
 * runs next and starts calling `handle()` immediately — nothing may be waiting
 * on DOMContentLoaded, or a baked page would replay into a null log. */

rootLog = byId("log");
nowEl = byId("now");
bannerEl = byId("banner");
wireTheme();
wireChrome();

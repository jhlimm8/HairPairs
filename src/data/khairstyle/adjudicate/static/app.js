"use strict";

const state = {
  exp: localStorage.getItem("hairilr_exp") || "",
  kind: "split",
  filter: "todo",
  sharedOnly: localStorage.getItem("hairilr_shared") === "1",
  rater: localStorage.getItem("hairilr_rater") || "",
  tasks: [],
  current: null,      // current item_id
  item: null,         // loaded item payload
  clusterOf: [],      // merge: cluster id per display member (null = unplaced)
  groupOrder: [],     // merge: cluster ids in the order groups were created
  nextCid: 0,         // merge: monotonic cluster id allocator
  history: [],        // merge: member indices in the order they were placed (for undo)
  active: null,       // merge: a member explicitly pulled out for re-assignment
  selection: new Set(),
};

// Palette for group accents (mirrors --c0..--c7 in styles.css).
const CCOLORS = ["--c0", "--c1", "--c2", "--c3", "--c4", "--c5", "--c6", "--c7"];
const cvar = (i) => `var(${CCOLORS[i % 8]})`;

const $ = (s) => document.querySelector(s);
const el = (t, cls, txt) => { const e = document.createElement(t); if (cls) e.className = cls; if (txt != null) e.textContent = txt; return e; };

function rq(path) {
  const sep = path.includes("?") ? "&" : "?";
  const exp = state.exp ? `&exp=${encodeURIComponent(state.exp)}` : "";
  return `${path}${sep}rater=${encodeURIComponent(state.rater || "anon")}${exp}`;
}
async function getJSON(p) { const r = await fetch(rq(p)); return r.json(); }
async function postJSON(p, body) {
  const r = await fetch(rq(p), { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  return r.json();
}

let toastT;
function toast(msg, bad) {
  let t = $(".toast");
  if (!t) { t = el("div", "toast"); document.body.appendChild(t); }
  t.textContent = msg; t.style.borderColor = bad ? "var(--bad)" : "var(--line)";
  t.classList.add("show"); clearTimeout(toastT);
  toastT = setTimeout(() => t.classList.remove("show"), 1400);
}

async function refreshProgress() {
  const f = await getJSON(scoped("/api/frame"));
  // No top-bar pills; reflect live progress in the experiment dropdown label.
  const done = f.counts.split.done + f.counts.merge.done;
  const total = f.counts.split.total + f.counts.merge.total;
  const opt = [...expSel.options].find((o) => o.value === state.exp);
  if (opt && opt.dataset.label) opt.textContent = `${opt.dataset.label} — ${done}/${total}`;
  updateScopeUI(f);
}

// Append the shared-subset scope to task/frame requests when the toggle is on.
function scoped(p) {
  return state.sharedOnly ? `${p}${p.includes("?") ? "&" : "?"}scope=shared` : p;
}

function updateScopeUI(f) {
  const row = $("#scope-row");
  const btn = $("#f-shared");
  if (!f.has_shared) {
    row.classList.add("hidden");
    if (state.sharedOnly) { state.sharedOnly = false; localStorage.setItem("hairilr_shared", "0"); }
    return;
  }
  row.classList.remove("hidden");
  btn.textContent = `shared only · ${f.shared_total}`;
  btn.classList.toggle("active", state.sharedOnly);
}

function filteredTasks() {
  if (state.filter === "todo") return state.tasks.filter((t) => !t.done);
  if (state.filter === "done") return state.tasks.filter((t) => t.done);
  return state.tasks;
}

async function loadTasks() {
  const data = await getJSON(scoped(`/api/tasks?kind=${state.kind}`));
  state.tasks = data.tasks;
  renderList();
}

function renderList() {
  const ul = $("#tasklist"); ul.innerHTML = "";
  filteredTasks().forEach((t, i) => {
    const li = el("li"); li.dataset.id = t.item_id;
    if (t.done) li.classList.add("done");
    if (t.item_id === state.current) li.classList.add("active");
    const label = state.kind === "merge" ? `group · ${t.n_members}` : `pair`;
    const idspan = el("span", "id", `${i + 1}. ${label}`);
    li.appendChild(idspan);
    if (t.done) li.appendChild(el("span", "mark", "✓"));
    li.onclick = () => openItem(t.item_id);
    ul.appendChild(li);
  });
}

async function openItem(id) {
  if (!state.rater) { toast("set a rater id first", true); $("#rater").focus(); return; }
  state.current = id;
  const item = await getJSON(`/api/item/${encodeURIComponent(id)}`);
  if (item.error) { toast(item.error, true); return; }
  state.item = item;
  state.selection.clear();
  if (item.kind === "merge") {
    state.clusterOf = new Array(item.n_members).fill(null);
    state.groupOrder = [];
    state.nextCid = 0;
    state.history = [];
    state.active = null;
    if (item.existing && item.existing.clusters) {
      // Pre-load a saved verdict as fully-placed groups (review mode).
      item.existing.clusters.forEach((cl) => {
        const cid = state.nextCid++;
        state.groupOrder.push(cid);
        cl.forEach((m) => { state.clusterOf[m] = cid; });
      });
    }
  }
  renderList();
  renderStage();
}

function viewsHtml(card) {
  const wrap = el("div", "views");
  card.views.forEach((v) => {
    const col = el("div", "view");
    const img = el("img"); img.src = v.image_url; img.loading = "lazy";
    col.appendChild(img);
    col.appendChild(el("div", "slot", v.slot));
    wrap.appendChild(col);
  });
  return wrap;
}

function renderStage() {
  $("#empty").classList.add("hidden");
  $("#work").classList.remove("hidden");
  const it = state.item;
  $("#work-title").textContent = it.kind === "merge"
    ? `Group of ${it.n_members} — cluster same-hairstyle photos together`
    : `Pair — same hairstyle or not?`;
  $("#work-status").textContent = it.existing
    ? `saved ${it.existing.updated_at || ""}` : "unlabeled";
  it.kind === "merge" ? renderMerge() : renderSplit();
  renderComments();
}

// ---- comments ---------------------------------------------------------------
function renderComments() {
  const it = state.item;
  $("#comment-box").value = it.my_comment || "";
  $("#comment-status").textContent = "";
  const ul = $("#comment-list"); ul.innerHTML = "";
  (it.comments || []).filter((c) => !c.mine).forEach((c) => {
    const li = el("li", "comment-item");
    li.appendChild(el("span", "comment-rater", c.rater));
    li.appendChild(el("span", "comment-text", c.comment));
    li.appendChild(el("span", "comment-time", c.updated_at || ""));
    ul.appendChild(li);
  });
}

async function saveComment() {
  if (!state.item) return;
  if (!state.rater) { toast("set a rater id first", true); return; }
  const comment = $("#comment-box").value.trim();
  const r = await postJSON("/api/comment", { item_id: state.current, rater: state.rater, comment });
  if (r.error) return toast(r.error, true);
  if (state.item) state.item.my_comment = comment;
  $("#comment-status").textContent = comment ? `saved ${r.updated_at}` : "cleared";
  toast("note saved");
}

// ---- SPLIT ------------------------------------------------------------------
function renderSplit() {
  const it = state.item;
  const cards = $("#cards"); cards.innerHTML = ""; cards.className = "split-pair";
  it.cards.forEach((c) => {
    const card = el("div", "card");
    card.appendChild(viewsHtml(c));
    cards.appendChild(card);
  });
  const rel = it.existing && it.existing.relation;
  const ctr = $("#controls"); ctr.innerHTML = "";
  const same = el("button", "action same" + (rel === "same" ? " primary" : ""), "Same  (S)");
  const diff = el("button", "action diff" + (rel === "different" ? " primary" : ""), "Different  (D)");
  same.onclick = () => saveSplit("same");
  diff.onclick = () => saveSplit("different");
  ctr.append(same, diff);
  $("#help").innerHTML = `<kbd>S</kbd> same · <kbd>D</kbd> different · <kbd>←</kbd>/<kbd>→</kbd> prev/next`;
}

async function saveSplit(relation) {
  const r = await postJSON("/api/verdict", { item_id: state.current, rater: state.rater, verdict: { relation } });
  if (r.error) return toast(r.error, true);
  markDone(); toast(relation === "same" ? "→ same" : "→ different");
  await refreshProgress(); advance();
}

// ---- MERGE ------------------------------------------------------------------
// Model: each member is placed into a cluster id (state.clusterOf[i]); null =
// not yet placed. Members are introduced one at a time as a "candidate" while
// already-formed groups sit in a persistent rail at the top, so a rater only
// ever decides "does this one photo join an existing group, or start a new one".

// Drop clusters that no longer have members (e.g. after a re-assignment).
function pruneGroups() {
  state.groupOrder = state.groupOrder.filter((cid) => state.clusterOf.some((c) => c === cid));
}

// Ordered groups with their member indices; prunes empties as a side effect.
function mergeGroups() {
  pruneGroups();
  return state.groupOrder.map((cid) => ({
    cid,
    members: state.clusterOf.flatMap((c, i) => (c === cid ? [i] : [])),
  }));
}

// The member currently awaiting placement: an explicitly pulled-out one, else
// the first member with no cluster yet. Returns null when everything is placed.
function activeMember() {
  if (state.active != null && state.clusterOf[state.active] == null) return state.active;
  const i = state.clusterOf.findIndex((c) => c == null);
  return i === -1 ? null : i;
}

function assign(i, cid) {
  state.clusterOf[i] = cid;
  state.history.push(i);
  state.active = null;
  renderMerge();
}
function assignNew(i) {
  const cid = state.nextCid++;
  state.groupOrder.push(cid);
  assign(i, cid);
}
function undoLast() {
  const i = state.history.pop();
  if (i == null) return;
  state.clusterOf[i] = null;
  state.active = i;
  renderMerge();
}
function pullOut(m) {
  const hi = state.history.lastIndexOf(m);
  if (hi !== -1) state.history.splice(hi, 1);
  state.clusterOf[m] = null;
  state.active = m;
  renderMerge();
}
function resetMerge() {
  state.clusterOf = new Array(state.item.n_members).fill(null);
  state.groupOrder = []; state.nextCid = 0; state.history = []; state.active = null;
}

// Singleton floating preview: shows a member's full set of views at large size.
// Rendered on <body> (position:fixed) so the rail's overflow never clips it.
let hoverEl;
function showPreview(it, m, anchor) {
  if (!hoverEl) { hoverEl = el("div", "hover-preview"); document.body.appendChild(hoverEl); }
  hoverEl.innerHTML = "";
  it.cards[m].views.forEach((v) => {
    const col = el("div", "hp-view");
    const img = el("img"); img.src = v.image_url;
    col.append(img, el("div", "hp-slot", v.slot));
    hoverEl.appendChild(col);
  });
  hoverEl.classList.add("show");
  const r = anchor.getBoundingClientRect();
  const pw = hoverEl.offsetWidth, ph = hoverEl.offsetHeight;
  let top = r.bottom + 8;
  if (top + ph > window.innerHeight - 8) top = Math.max(8, r.top - ph - 8);
  let left = r.left + r.width / 2 - pw / 2;
  left = Math.max(8, Math.min(left, window.innerWidth - pw - 8));
  hoverEl.style.top = `${top}px`; hoverEl.style.left = `${left}px`;
}
function hidePreview() { if (hoverEl) hoverEl.classList.remove("show"); }

function memberThumb(it, m) {
  const thumb = el("div", "member-thumb");
  const v = it.cards[m].views[0];
  if (v) { const img = el("img"); img.src = v.image_url; img.loading = "lazy"; thumb.appendChild(img); }
  thumb.appendChild(el("span", "thumb-move", "move"));
  thumb.title = "hover to enlarge · click to move this photo to another group";
  thumb.onmouseenter = () => showPreview(it, m, thumb);
  thumb.onmouseleave = hidePreview;
  thumb.onclick = (e) => { e.stopPropagation(); hidePreview(); pullOut(m); };
  return thumb;
}

function renderMerge() {
  hidePreview();
  const it = state.item;
  const groups = mergeGroups();
  const active = activeMember();
  const placed = state.clusterOf.filter((c) => c != null).length;

  const cards = $("#cards"); cards.innerHTML = ""; cards.className = "merge-stage";

  // --- groups rail (always visible) ---
  const rail = el("div", "group-rail");
  if (!groups.length && active != null) {
    rail.appendChild(el("div", "rail-hint", "No groups yet — place the first photo to start one."));
  }
  groups.forEach((g, gi) => {
    const col = el("div", "group-col");
    col.style.borderTopColor = cvar(gi);
    if (active != null) {
      col.classList.add("droppable");
      col.title = `add candidate to Group ${gi + 1}`;
      col.onclick = () => assign(active, g.cid);
    }
    const head = el("div", "group-head");
    const dot = el("span", "group-dot"); dot.style.background = cvar(gi);
    const name = el("span", "group-name", `Group ${gi + 1}`);
    const left = el("div", "group-name-wrap"); left.append(dot, name);
    head.append(left, el("span", "group-count", String(g.members.length)));
    col.appendChild(head);
    const grid = el("div", "group-members");
    g.members.forEach((m) => grid.appendChild(memberThumb(it, m)));
    col.appendChild(grid);
    rail.appendChild(col);
  });
  if (active != null) {
    const tile = el("div", "group-col new-group");
    tile.title = "start a new group with the candidate";
    tile.onclick = () => assignNew(active);
    tile.appendChild(el("div", "new-group-plus", "+"));
    tile.appendChild(el("div", "new-group-label", `New group ${groups.length + 1}`));
    rail.appendChild(tile);
  }
  cards.appendChild(rail);

  // --- candidate (one at a time) or done summary ---
  if (active != null) {
    const panel = el("div", "candidate");
    const ch = el("div", "candidate-head");
    ch.appendChild(el("span", "candidate-label", "Up next"));
    ch.appendChild(el("span", "candidate-progress", `${placed + 1} of ${it.n_members}`));
    panel.appendChild(ch);
    panel.appendChild(el("div", "candidate-q", "Which group does this hairstyle belong to?"));
    panel.appendChild(viewsHtml(it.cards[active]));
    cards.appendChild(panel);
  } else {
    const done = el("div", "merge-done");
    done.appendChild(el("div", "merge-done-big",
      `${groups.length} group${groups.length > 1 ? "s" : ""} from ${it.n_members} photos`));
    done.appendChild(el("div", "merge-done-sub",
      "Review the groups above — click any photo to move it — then submit."));
    cards.appendChild(done);
  }

  // --- controls ---
  const ctr = $("#controls"); ctr.innerHTML = "";
  if (active != null) {
    groups.forEach((g, gi) => {
      const b = el("button", "action grp-btn", `${gi + 1} · Group ${gi + 1}`);
      b.style.borderColor = cvar(gi);
      b.onclick = () => assign(active, g.cid);
      ctr.appendChild(b);
    });
    const nb = el("button", "action grp-new", "N · New group");
    nb.onclick = () => assignNew(active);
    ctr.appendChild(nb);
    if (state.history.length) {
      const u = el("button", "action subtle", "Undo (U)");
      u.onclick = undoLast; ctr.appendChild(u);
    }
  } else {
    const sub = el("button", "action primary big",
      `Submit ${groups.length} group${groups.length > 1 ? "s" : ""} (Enter)`);
    sub.onclick = saveMerge;
    const reset = el("button", "action subtle", "Start over");
    reset.onclick = () => { resetMerge(); renderMerge(); };
    ctr.append(sub, reset);
  }

  $("#help").innerHTML = active != null
    ? `<kbd>1</kbd>–<kbd>9</kbd> add to a group · <kbd>N</kbd> new group · <kbd>U</kbd> undo · click a photo above to move it`
    : `<kbd>Enter</kbd> submit · click any photo above to move it · <kbd>←</kbd>/<kbd>→</kbd> prev/next task`;
}

async function saveMerge() {
  if (state.clusterOf.some((c) => c == null)) return toast("place all photos first", true);
  const groups = mergeGroups();
  const clusters = groups.map((g) => g.members);
  const k = clusters.length;
  const r = await postJSON("/api/verdict", { item_id: state.current, rater: state.rater, verdict: { clusters } });
  if (r.error) return toast(r.error, true);
  markDone(); toast(`saved · ${k} group${k > 1 ? "s" : ""}`);
  await refreshProgress(); advance();
}

// ---- shared -----------------------------------------------------------------
function markDone() {
  const t = state.tasks.find((t) => t.item_id === state.current);
  if (t) t.done = true;
}
async function advance() {
  if (state.filter === "todo") {
    // current item is now done -> it leaves the todo list; open the next remaining one
    const before = filteredTasks();
    const pos = before.findIndex((t) => t.item_id === state.current);
    await loadTasks();
    const remaining = filteredTasks();
    const next = remaining[pos] || remaining[remaining.length - 1];
    if (next) openItem(next.item_id);
    else { state.current = null; state.item = null; renderList(); $("#work").classList.add("hidden"); $("#empty").classList.remove("hidden"); }
    return;
  }
  const list = filteredTasks();
  const idx = list.findIndex((t) => t.item_id === state.current);
  if (list[idx + 1]) openItem(list[idx + 1].item_id); else renderList();
}
function step(d) {
  const list = filteredTasks();
  const idx = list.findIndex((t) => t.item_id === state.current);
  const nxt = list[idx + d]; if (nxt) openItem(nxt.item_id);
}

// ---- wiring -----------------------------------------------------------------
function setKind(kind) {
  state.kind = kind; state.current = null; state.item = null;
  document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b.dataset.kind === kind));
  $("#work").classList.add("hidden"); $("#empty").classList.remove("hidden");
  loadTasks();
}
function setFilter(f) {
  state.filter = f;
  document.querySelectorAll(".filter .chip").forEach((b) => b.classList.remove("active"));
  $("#f-" + f).classList.add("active");
  renderList();
}

document.querySelectorAll(".tab").forEach((b) => (b.onclick = () => setKind(b.dataset.kind)));
$("#f-todo").onclick = () => setFilter("todo");
$("#f-done").onclick = () => setFilter("done");
$("#f-all").onclick = () => setFilter("all");

$("#f-shared").onclick = () => {
  state.sharedOnly = !state.sharedOnly;
  localStorage.setItem("hairilr_shared", state.sharedOnly ? "1" : "0");
  $("#f-shared").classList.toggle("active", state.sharedOnly);
  state.current = null; state.item = null;
  $("#work").classList.add("hidden"); $("#empty").classList.remove("hidden");
  refreshProgress(); loadTasks();
};

const expSel = $("#exp");
expSel.onchange = async () => {
  state.exp = expSel.value;
  localStorage.setItem("hairilr_exp", state.exp);
  state.current = null; state.item = null;
  $("#work").classList.add("hidden"); $("#empty").classList.remove("hidden");
  await loadRaters();
  await refreshProgress();
  await loadTasks();
};

async function loadExperiments() {
  const data = await getJSON("/api/experiments");
  const valid = new Set(data.experiments.map((e) => e.id));
  if (!valid.has(state.exp)) state.exp = data.default;
  expSel.innerHTML = "";
  data.experiments.forEach((e) => {
    const o = el("option", null, `${e.label} — ${e.done}/${e.total}`);
    o.value = e.id;
    o.dataset.label = e.label;
    expSel.appendChild(o);
  });
  expSel.value = state.exp;
  localStorage.setItem("hairilr_exp", state.exp);
}

$("#comment-save").onclick = saveComment;

const raterSel = $("#rater");
const raterOther = $("#rater-other");
const OTHER = "__other__";

async function loadRaters() {
  const data = await getJSON("/api/raters");
  const labelers = data.raters || [];
  raterSel.innerHTML = "";
  const blank = el("option", null, "— select —"); blank.value = ""; raterSel.appendChild(blank);
  labelers.forEach((r) => { const o = el("option", null, r); o.value = r; raterSel.appendChild(o); });
  const other = el("option", null, "other / admin…"); other.value = OTHER; raterSel.appendChild(other);
  if (state.rater && labelers.includes(state.rater)) {
    raterSel.value = state.rater; raterOther.classList.add("hidden");
  } else if (state.rater) {
    raterSel.value = OTHER; raterOther.value = state.rater; raterOther.classList.remove("hidden");
  } else {
    raterSel.value = ""; raterOther.classList.add("hidden");
  }
}

function applyRater(r) {
  state.rater = (r || "").trim();
  localStorage.setItem("hairilr_rater", state.rater);
  loadExperiments().then(() => refreshProgress()).then(() => loadTasks());
}

raterSel.onchange = () => {
  if (raterSel.value === OTHER) {
    raterOther.classList.remove("hidden"); raterOther.focus();
    applyRater(raterOther.value);
  } else {
    raterOther.classList.add("hidden");
    applyRater(raterSel.value);
  }
};
raterOther.onchange = () => applyRater(raterOther.value);

document.addEventListener("keydown", (e) => {
  const ae = document.activeElement;
  if (ae && (ae.tagName === "INPUT" || ae.tagName === "TEXTAREA")) return;
  if (!state.item) {
    if (e.key === "ArrowRight") step(1); else if (e.key === "ArrowLeft") step(-1);
    return;
  }
  const k = e.key.toLowerCase();
  if (e.key === "ArrowRight") { step(1); return; }
  if (e.key === "ArrowLeft") { step(-1); return; }
  if (state.item.kind === "split") {
    if (k === "s") saveSplit("same");
    if (k === "d") saveSplit("different");
  } else {
    const active = activeMember();
    if (active == null) { if (e.key === "Enter") saveMerge(); return; }
    if (k === "n") { assignNew(active); return; }
    if (k === "u") { undoLast(); return; }
    const d = parseInt(e.key, 10);
    if (!isNaN(d) && d >= 1) {
      const groups = mergeGroups();
      if (d <= groups.length) assign(active, groups[d - 1].cid);
    }
  }
});

(async function init() {
  await loadExperiments();
  await loadRaters();
  await refreshProgress();
  await loadTasks();
})();

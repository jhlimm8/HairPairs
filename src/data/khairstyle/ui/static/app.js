"use strict";

const state = {
  split: "", sex: "", anno: "", category_id: "", q: "", page: 1,
  total: 0, page_size: 60, items: [], meta: null, facets: null,
  filters: {}, ranges: {}, // field -> Set(values) ; field -> {min,max}
  detail: null, detailIndex: -1,
};

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const api = (p) => fetch(p).then((r) => r.json());
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

let searchTimer = null;

async function boot() {
  [state.meta, state.facets] = await Promise.all([api("/api/meta"), api("/api/facets")]);
  renderStats();
  renderCategories();
  renderAttrFilters();
  $("#split-note").textContent = state.meta.leakage_free_split
    ? "leakage-free splits (images_clean)"
    : "⚠ raw author split — run make_splits.py";
  wireFilters();
  initFiltersFromURL();
  await loadImages();
  const m = location.hash.match(/gid=([^&]+)/);
  if (m) openDetailByGid(decodeURIComponent(m[1]));
}

// Read f_<field> / r_<field>_min|max (and split/sex/category) from the URL so
// filtered views are shareable; expand any group that ends up active.
function initFiltersFromURL() {
  const p = new URLSearchParams(location.search);
  const catFields = new Set(state.facets.categorical.map((f) => f.field));
  const numFields = new Set(state.facets.numeric.map((f) => f.field));
  for (const [key, val] of p.entries()) {
    if (key.startsWith("f_") && catFields.has(key.slice(2))) {
      (state.filters[key.slice(2)] ||= new Set()).add(val);
    } else if (key.startsWith("r_") && key.endsWith("_min") && numFields.has(key.slice(2, -4))) {
      (state.ranges[key.slice(2, -4)] ||= {}).min = val;
    } else if (key.startsWith("r_") && key.endsWith("_max") && numFields.has(key.slice(2, -4))) {
      (state.ranges[key.slice(2, -4)] ||= {}).max = val;
    } else if (key === "split") setSeg("#split-seg", "split", val);
    else if (key === "sex") setSeg("#sex-seg", "sex", val);
    else if (key === "category_id") selectCategory(val);
  }
  $$(".attr-group").forEach((g) => {
    const fld = g.dataset.field;
    if (state.filters[fld] || state.ranges[fld]) {
      g.classList.add("open");
      if (!g.dataset.numeric) fillOptions(fld);
    }
  });
  syncControls();
  updateBadges();
  renderActiveFilters();
}

function setSeg(sel, key, val) {
  const btn = $(`${sel} button[data-${key}="${val}"]`);
  if (!btn) return;
  $$(`${sel} button`).forEach((b) => b.classList.remove("active"));
  btn.classList.add("active");
  state[key] = val;
}

function selectCategory(id) {
  const el = $(`#cat-list .cat[data-cat="${id}"]`);
  if (!el) return;
  $$("#cat-list .cat").forEach((x) => x.classList.remove("active"));
  el.classList.add("active");
  state.category_id = id;
}

function renderStats() {
  const m = state.meta;
  const val = m.splits.find((s) => s.split === "mq-val") || { n: 0, s: 0 };
  const tr = m.splits.find((s) => s.split === "mq-train") || { n: 0, s: 0 };
  $("#stats").innerHTML = `
    <div class="stat"><b>${m.total_images.toLocaleString()}</b><span>images</span></div>
    <div class="stat"><b>${tr.n.toLocaleString()}</b><span>train</span></div>
    <div class="stat"><b>${val.n.toLocaleString()}</b><span>val</span></div>
    <div class="stat"><b class="acc" id="anno-count">${m.annotations.toLocaleString()}</b><span>labeled</span></div>`;
}

function bumpAnnoCount(delta) {
  state.meta.annotations += delta;
  const el = $("#anno-count");
  if (el) el.textContent = state.meta.annotations.toLocaleString();
}

function renderCategories() {
  const cats = state.meta.categories;
  $("#cat-count").textContent = `· ${cats.length}`;
  const list = $("#cat-list");
  list.innerHTML = `<div class="cat active" data-cat=""><div class="cat-names">
      <span class="cat-en">All styles</span><span class="cat-ko">전체</span></div>
      <span class="cat-n">${state.meta.total_images.toLocaleString()}</span></div>` +
    cats.map((c) => `
      <div class="cat" data-cat="${c.category_id}">
        <div class="cat-names">
          <span class="cat-en">${esc(c.category_en)}</span>
          <span class="cat-ko">${esc(c.category)}</span>
        </div>
        <span class="cat-n">${c.n.toLocaleString()}</span>
      </div>`).join("");
  $$("#cat-list .cat").forEach((el) => el.addEventListener("click", () => {
    $$("#cat-list .cat").forEach((x) => x.classList.remove("active"));
    el.classList.add("active");
    state.category_id = el.dataset.cat; state.page = 1; loadImages();
  }));
}

/* ---------------- attribute filters ---------------- */
function renderAttrFilters() {
  const wrap = $("#attr-filters");
  const cat = state.facets.categorical.map((f) => `
    <div class="attr-group" data-field="${f.field}">
      <button class="attr-head">
        <span>${esc(f.label)}</span>
        <span class="badge" data-badge="${f.field}"></span>
        <span class="chev">▸</span>
      </button>
      <div class="attr-options" data-opts="${f.field}"></div>
    </div>`).join("");
  const num = state.facets.numeric.map((f) => `
    <div class="attr-group" data-field="${f.field}" data-numeric="1">
      <button class="attr-head">
        <span>${esc(f.label)}</span>
        <span class="badge" data-badge="${f.field}"></span>
        <span class="chev">▸</span>
      </button>
      <div class="attr-options">
        <div class="range-row">
          <input type="number" data-rmin="${f.field}" placeholder="${f.min}" min="${f.min}" max="${f.max}" />
          <span>–</span>
          <input type="number" data-rmax="${f.field}" placeholder="${f.max}" min="${f.min}" max="${f.max}" />
        </div>
        <div class="range-hint">range ${f.min} – ${f.max}</div>
      </div>
    </div>`).join("");
  wrap.innerHTML = cat + num;

  $$(".attr-group .attr-head").forEach((h) => h.addEventListener("click", () => {
    const g = h.closest(".attr-group");
    g.classList.toggle("open");
    if (g.classList.contains("open") && !g.dataset.numeric) fillOptions(g.dataset.field);
  }));
  $$("[data-rmin], [data-rmax]").forEach((inp) => inp.addEventListener("change", onRangeChange));
}

function fillOptions(field) {
  const box = $(`[data-opts="${field}"]`);
  if (box.dataset.filled) return;
  const f = state.facets.categorical.find((x) => x.field === field);
  const sel = state.filters[field] || new Set();
  box.innerHTML = f.values.map((v) => {
    const vv = String(v.value);
    return `
    <label class="opt ${sel.has(vv) ? "on" : ""}">
      <input type="checkbox" value="${esc(vv)}" ${sel.has(vv) ? "checked" : ""} />
      <span>${esc(v.en ?? vv)}</span>
      ${v.en && String(v.en) !== vv ? `<span class="ko">${esc(vv)}</span>` : ""}
      <span class="n">${v.count.toLocaleString()}</span>
    </label>`;
  }).join("");
  $$("input", box).forEach((cb) => cb.addEventListener("change", () => onOptionToggle(field, cb)));
  box.dataset.filled = "1";
}

function onOptionToggle(field, cb) {
  const set = state.filters[field] || new Set();
  if (cb.checked) set.add(cb.value); else set.delete(cb.value);
  if (set.size) state.filters[field] = set; else delete state.filters[field];
  cb.closest(".opt").classList.toggle("on", cb.checked);
  afterFilterChange();
}

function onRangeChange(e) {
  const min = e.target.dataset.rmin, max = e.target.dataset.rmax;
  const field = min || max;
  const r = state.ranges[field] || {};
  const v = e.target.value.trim();
  if (min) r.min = v; else r.max = v;
  if ((r.min ?? "") === "" && (r.max ?? "") === "") delete state.ranges[field];
  else state.ranges[field] = r;
  afterFilterChange();
}

function afterFilterChange() {
  updateBadges();
  renderActiveFilters();
  refreshAttrRows();
  state.page = 1;
  loadImages();
}

function updateBadges() {
  $$(".attr-group").forEach((g) => {
    const field = g.dataset.field;
    const n = state.filters[field]?.size || (state.ranges[field] ? 1 : 0);
    g.classList.toggle("active", !!n);
    const b = g.querySelector("[data-badge]");
    if (b) b.textContent = state.filters[field]?.size || (state.ranges[field] ? "•" : "");
  });
  const total = activeFilterCount();
  $("#attr-active").textContent = total ? `· ${total}` : "";
  $("#attr-clear").hidden = total === 0;
}

function activeFilterCount() {
  return Object.values(state.filters).reduce((a, s) => a + s.size, 0)
    + Object.keys(state.ranges).length;
}

function renderActiveFilters() {
  const labelOf = (field) => (state.facets.categorical.find((f) => f.field === field)
    || state.facets.numeric.find((f) => f.field === field) || {}).label || field;
  const chips = [];
  for (const [field, set] of Object.entries(state.filters)) {
    const f = state.facets.categorical.find((x) => x.field === field);
    for (const val of set) {
      const en = f?.values.find((v) => String(v.value) === val)?.en ?? val;
      chips.push(`<span class="fchip"><span class="k">${esc(labelOf(field))}</span> ${esc(en)}<b data-rm-set="${esc(field)}|${esc(val)}">✕</b></span>`);
    }
  }
  for (const [field, r] of Object.entries(state.ranges)) {
    const txt = (r.min ?? "") !== "" && r.min === r.max
      ? `${r.min}` : `${r.min ?? "↓"}–${r.max ?? "↑"}`;
    chips.push(`<span class="fchip"><span class="k">${esc(labelOf(field))}</span> ${esc(txt)}<b data-rm-range="${esc(field)}">✕</b></span>`);
  }
  const el = $("#active-filters");
  el.innerHTML = chips.length
    ? chips.join("") + `<span class="fchip clear-all" id="chip-clear">clear all ✕</span>`
    : "";
  $$("[data-rm-set]", el).forEach((b) => b.addEventListener("click", () => {
    const [field, val] = b.dataset.rmSet.split("|");
    state.filters[field]?.delete(val);
    if (!state.filters[field]?.size) delete state.filters[field];
    syncControls(); afterFilterChange();
  }));
  $$("[data-rm-range]", el).forEach((b) => b.addEventListener("click", () => {
    delete state.ranges[b.dataset.rmRange];
    syncControls(); afterFilterChange();
  }));
  const ca = $("#chip-clear");
  if (ca) ca.addEventListener("click", clearAllFilters);
}

function clearAllFilters() {
  state.filters = {}; state.ranges = {};
  syncControls(); afterFilterChange();
}

// reflect state back into the DOM controls (after removing chips)
function syncControls() {
  $$(".attr-options input[type=checkbox]").forEach((cb) => {
    const field = cb.closest(".attr-group").dataset.field;
    const on = state.filters[field]?.has(cb.value) || false;
    cb.checked = on; cb.closest(".opt").classList.toggle("on", on);
  });
  $$("[data-rmin]").forEach((i) => { i.value = state.ranges[i.dataset.rmin]?.min ?? ""; });
  $$("[data-rmax]").forEach((i) => { i.value = state.ranges[i.dataset.rmax]?.max ?? ""; });
}

function wireFilters() {
  const seg = (sel, key) => $$(`${sel} button`).forEach((b) => b.addEventListener("click", () => {
    $$(`${sel} button`).forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    state[key] = b.dataset[key]; state.page = 1; loadImages();
  }));
  seg("#split-seg", "split");
  seg("#sex-seg", "sex");
  seg("#anno-seg", "anno");
  $("#search").addEventListener("input", (e) => {
    clearTimeout(searchTimer);
    state.q = e.target.value.trim();
    searchTimer = setTimeout(() => { state.page = 1; loadImages(); }, 280);
  });
  $("#scrim").addEventListener("click", closeDrawer);
  $("#attr-clear").addEventListener("click", clearAllFilters);
  document.addEventListener("keydown", onKey);
}

async function loadImages() {
  const grid = $("#grid");
  grid.innerHTML = `<div class="loading">developing…</div>`;
  const p = new URLSearchParams({
    split: state.split, sex: state.sex, annotated: state.anno,
    category_id: state.category_id, q: state.q,
    page: state.page, page_size: state.page_size,
  });
  for (const [field, set] of Object.entries(state.filters)) {
    for (const v of set) p.append(`f_${field}`, v);
  }
  for (const [field, r] of Object.entries(state.ranges)) {
    if ((r.min ?? "") !== "") p.append(`r_${field}_min`, r.min);
    if ((r.max ?? "") !== "") p.append(`r_${field}_max`, r.max);
  }
  const data = await api(`/api/images?${p}`);
  state.total = data.total; state.items = data.items;
  renderGrid();
  renderMeta();
  renderPager();
  syncURL();
}

// Mirror the active filters into the address bar (shareable), preserving #gid.
function syncURL() {
  const p = new URLSearchParams();
  if (state.split) p.set("split", state.split);
  if (state.sex) p.set("sex", state.sex);
  if (state.category_id) p.set("category_id", state.category_id);
  for (const [field, set] of Object.entries(state.filters)) {
    for (const v of set) p.append(`f_${field}`, v);
  }
  for (const [field, r] of Object.entries(state.ranges)) {
    if ((r.min ?? "") !== "") p.append(`r_${field}_min`, r.min);
    if ((r.max ?? "") !== "") p.append(`r_${field}_max`, r.max);
  }
  const qs = p.toString();
  history.replaceState(null, "", (qs ? "?" + qs : location.pathname) + location.hash);
}

function renderMeta() {
  const cat = state.meta.categories.find((c) => c.category_id === state.category_id);
  const label = cat ? `${cat.category_en}` : "All styles";
  $("#result-meta").innerHTML = `<b>${state.total.toLocaleString()}</b> in <b>${esc(label)}</b>`;
}

function renderGrid() {
  const grid = $("#grid");
  if (!state.items.length) { grid.innerHTML = `<div class="empty">No frames match these filters.</div>`; return; }
  grid.innerHTML = state.items.map((it, i) => `
    <div class="card ${it.annotated ? "annotated" : ""}" data-i="${i}" style="animation-delay:${Math.min(i * 14, 350)}ms">
      <img loading="lazy" src="${it.image_url}" alt="${esc(it.category_en)}" />
      <span class="tag-split ${it.split === "mq-val" ? "val" : ""}">${it.split === "mq-val" ? "val" : "train"}</span>
      <span class="dot"></span>
      <div class="ov">
        <span class="c-en">${esc(it.category_en)}</span>
        <span class="c-id">${esc(it.source)} · ${esc(it.view)}</span>
      </div>
    </div>`).join("");
  $$("#grid .card").forEach((el) => el.addEventListener("click", () => openDetail(+el.dataset.i)));
}

function renderPager() {
  const pages = Math.max(1, Math.ceil(state.total / state.page_size));
  const html = `
    <button ${state.page <= 1 ? "disabled" : ""} data-go="first">«</button>
    <button ${state.page <= 1 ? "disabled" : ""} data-go="prev">‹</button>
    <span class="page-info">${state.page} / ${pages}</span>
    <button ${state.page >= pages ? "disabled" : ""} data-go="next">›</button>
    <button ${state.page >= pages ? "disabled" : ""} data-go="last">»</button>`;
  ["#pager", "#pager-bottom"].forEach((sel) => {
    const el = $(sel); el.innerHTML = html;
    $$("button", el).forEach((b) => b.addEventListener("click", () => {
      const g = b.dataset.go;
      state.page = g === "first" ? 1 : g === "last" ? pages : g === "next" ? state.page + 1 : state.page - 1;
      document.querySelector(".main").scrollTop = 0;
      loadImages();
    }));
  });
}

/* ---------------- detail drawer ---------------- */
async function openDetail(i) {
  state.detailIndex = i;
  await openDetailByGid(state.items[i].gid);
}

async function openDetailByGid(gid) {
  state.detail = await api(`/api/detail/${encodeURIComponent(gid)}`);
  if (state.detailIndex < 0 || state.items[state.detailIndex]?.gid !== gid) {
    state.detailIndex = state.items.findIndex((x) => x.gid === gid);
  }
  renderDetail();
  $("#drawer").classList.add("open");
  $("#drawer").setAttribute("aria-hidden", "false");
  $("#scrim").classList.add("open");
  history.replaceState(null, "", `#gid=${encodeURIComponent(gid)}`);
}

function closeDrawer() {
  $("#drawer").classList.remove("open");
  $("#drawer").setAttribute("aria-hidden", "true");
  $("#scrim").classList.remove("open");
  state.detail = null;
  history.replaceState(null, "", location.pathname);
}

function renderDetail() {
  const d = state.detail;
  const anno = d.annotation ? JSON.parse(d.annotation.payload || "{}") : {};
  const status = anno.status || "";
  const tags = anno.tags || [];
  const annotator = localStorage.getItem("annotator") || (d.annotation?.annotator ?? "");

  const attrs = d.attrs.map((a) => {
    const kind = attrKind(a.field);
    const on = kind && attrActive(kind, a.field, a.ko);
    return `
    <div class="attr ${kind ? "filterable" : ""} ${on ? "f-active" : ""}" ${kind ? `data-kind="${kind}" data-field="${esc(a.field)}" data-val="${esc(a.ko)}"` : ""}>
      <div class="k">${esc(a.label)}${kind ? `<span class="plus">${on ? "remove −" : "filter +"}</span>` : ""}</div>
      <div class="v">${esc(a.en ?? a.ko)}${a.en && a.en !== a.ko ? `<span class="ko">${esc(a.ko)}</span>` : ""}</div>
    </div>`;
  }).join("");

  const sibs = d.siblings.map((s) => `
    <div class="sib ${s.gid === d.gid ? "cur" : ""}" data-gid="${s.gid}">
      <img loading="lazy" src="${s.image_url}" /><span>${esc(s.view)}</span>
    </div>`).join("");

  const rgb = d.rgb && d.rgb[0] != null
    ? `rgb(${d.rgb.map((x) => Math.round(x)).join(",")})` : null;

  $("#drawer-inner").innerHTML = `
    <div class="d-hero" id="hero">
      <img id="hero-img" src="${d.image_url}" />
      <div class="bbox" id="bbox"></div>
      <button class="d-close" id="d-close">✕</button>
      <div class="d-hero-tools">
        ${d.hair_bbox ? `<span class="chip" id="bbox-toggle">⬚ hair bbox</span>` : ""}
        ${rgb ? `<span class="chip" style="color:${rgb}">● mean color</span>` : ""}
      </div>
    </div>
    <div class="d-body">
      <div class="d-title">${esc(d.category_en)} <span class="ko">${esc(d.category)}</span></div>
      <div class="d-meta">
        <span><b>session</b> ${esc(d.source)}</span>
        <span><b>view</b> ${esc(d.view)}</span>
        <span><b>split</b> ${esc(d.split)}</span>
        <span><b>crop</b> ${d.crop.join("×")}</span>
        <span><b>original</b> ${d.orig.filter(Boolean).join("×") || "—"}</span>
        <span><b>views in session</b> ${d.n_session_views}</span>
        ${d.dup_label_count ? `<span><b>dup labels</b> ${d.dup_label_count}</span>` : ""}
      </div>

      <div class="section-label">Manual label · ground truth</div>
      <div class="anno" id="anno">
        <div class="anno-status">
          <button data-status="keep" class="${status === "keep" ? "sel" : ""}">Keep</button>
          <button data-status="review" class="${status === "review" ? "sel" : ""}">Review</button>
          <button data-status="discard" class="${status === "discard" ? "sel" : ""}">Discard</button>
        </div>
        <div class="tags-input" id="tags-input">
          ${tags.map((t) => `<span class="tag">${esc(t)}<b data-tag="${esc(t)}">✕</b></span>`).join("")}
          <input id="tag-entry" placeholder="add tag + Enter" />
        </div>
        <textarea id="note" placeholder="notes (free text)…">${esc(anno.note || "")}</textarea>
        <div class="anno-row">
          <input class="annotator" id="annotator" placeholder="annotator id" value="${esc(annotator)}" />
          <button class="btn" id="save-btn">Save</button>
          ${d.annotation ? `<button class="btn ghost" id="del-btn">Clear</button>` : ""}
          <span class="saved-flash" id="flash">saved ✓</span>
        </div>
        <div class="kbd-hint">
          <span><kbd>K</kbd> keep</span><span><kbd>R</kbd> review</span><span><kbd>D</kbd> discard</span>
          <span><kbd>←</kbd><kbd>→</kbd> prev/next</span><span><kbd>Esc</kbd> close</span>
        </div>
      </div>

      <div class="section-label plain">
        <span>Salon attributes</span>
        <span class="match-count" id="detail-matches"></span>
        <span class="rule"></span>
        <button class="mini-btn" id="use-all-attrs">use all as filter</button>
      </div>
      <div class="attr-grid">${attrs}</div>
      <div class="mining-note">Click any attribute to filter the gallery by it · mining-only signal, never benchmark ground truth.</div>

      <div class="section-label">Session views · ${d.n_session_views}</div>
      <div class="sib-strip">${sibs}</div>
    </div>`;

  wireDetail();
}

function wireDetail() {
  $("#d-close").addEventListener("click", closeDrawer);
  const bt = $("#bbox-toggle");
  if (bt) bt.addEventListener("click", () => toggleBbox(bt));
  $$("#anno .anno-status button").forEach((b) => b.addEventListener("click", () => {
    $$("#anno .anno-status button").forEach((x) => x.classList.remove("sel"));
    b.classList.add("sel");
  }));
  $("#tag-entry").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && e.target.value.trim()) {
      addTag(e.target.value.trim()); e.target.value = "";
    }
  });
  $$("#tags-input .tag b").forEach((b) => b.addEventListener("click", () => b.parentElement.remove()));
  $("#save-btn").addEventListener("click", saveAnnotation);
  const del = $("#del-btn");
  if (del) del.addEventListener("click", deleteAnnotation);
  $$(".sib").forEach((el) => el.addEventListener("click", () => jumpSibling(el.dataset.gid)));

  $$(".attr.filterable").forEach((el) => el.addEventListener("click", () =>
    onAttrClick(el.dataset.kind, el.dataset.field, el.dataset.val)));
  $("#use-all-attrs").addEventListener("click", useAllAttrsAsFilter);
}

// Which filter mechanism backs a given attribute field.
function attrKind(field) {
  if (state.facets.categorical.some((f) => f.field === field)) return "cat";  // f_ value-set
  if (state.facets.numeric.some((f) => f.field === field)) return "num";      // r_ range (exact)
  if (field === "sex") return "sex";                                          // dedicated control
  return null;  // basestyle (=category), front, device: not filterable
}

function attrActive(kind, field, val) {
  if (kind === "cat") return !!state.filters[field]?.has(String(val));
  if (kind === "num") {
    const r = state.ranges[field], v = String(val);
    return !!r && String(r.min) === v && String(r.max) === v;
  }
  if (kind === "sex") return state.sex === val;
  return false;
}

function onAttrClick(kind, field, val) {
  if (kind === "cat") {
    const set = state.filters[field] || new Set();
    set.has(val) ? set.delete(val) : set.add(val);
    if (set.size) state.filters[field] = set; else delete state.filters[field];
  } else if (kind === "num") {
    attrActive("num", field, val)
      ? delete state.ranges[field]
      : (state.ranges[field] = { min: val, max: val });
  } else if (kind === "sex") {
    state.sex = state.sex === val ? "" : val;
    setSeg("#sex-seg", "sex", state.sex || "");
  }
  refreshAttrRows();
  applyFiltersLive();
}

function refreshAttrRows() {
  $$(".attr.filterable").forEach((el) => {
    const { kind, field, val } = el.dataset;
    const on = attrActive(kind, field, val);
    el.classList.toggle("f-active", on);
    const p = el.querySelector(".plus");
    if (p) p.textContent = on ? "remove −" : "filter +";
  });
}

function useAllAttrsAsFilter() {
  state.filters = {}; state.ranges = {};
  for (const a of state.detail.attrs) {
    const kind = attrKind(a.field);
    if (kind === "cat") state.filters[a.field] = new Set([String(a.ko)]);
    else if (kind === "num") state.ranges[a.field] = { min: a.ko, max: a.ko };
    else if (kind === "sex") { state.sex = a.ko; setSeg("#sex-seg", "sex", a.ko); }
  }
  if (state.detail.category_id) selectCategory(state.detail.category_id);
  refreshAttrRows();
  applyFiltersLive();
}

async function applyFiltersLive() {
  updateBadges();
  renderActiveFilters();
  syncControls();
  state.page = 1;
  await loadImages();
  const m = $("#detail-matches");
  if (m) m.textContent = `${state.total.toLocaleString()} matches →`;
}

function addTag(t) {
  const span = document.createElement("span");
  span.className = "tag";
  span.innerHTML = `${esc(t)}<b>✕</b>`;
  span.querySelector("b").addEventListener("click", () => span.remove());
  $("#tag-entry").before(span);
}

function toggleBbox(btn) {
  const hero = $("#hero"); const box = $("#bbox"); const img = $("#hero-img");
  const d = state.detail;
  if (!d.hair_bbox) return;
  hero.classList.toggle("show-bbox");
  btn.classList.toggle("on");
  if (hero.classList.contains("show-bbox")) {
    const [w, h] = d.crop;
    const sx = img.clientWidth / w, sy = img.clientHeight / h;
    const [x0, y0, x1, y1] = d.hair_bbox;
    box.style.left = `${x0 * sx}px`; box.style.top = `${y0 * sy}px`;
    box.style.width = `${(x1 - x0) * sx}px`; box.style.height = `${(y1 - y0) * sy}px`;
  }
}

async function jumpSibling(gid) {
  state.detail = await api(`/api/detail/${encodeURIComponent(gid)}`);
  renderDetail();
}

function collectPayload() {
  const status = $("#anno .anno-status button.sel")?.dataset.status || "";
  const tags = $$("#tags-input .tag").map((t) => t.childNodes[0].textContent);
  const note = $("#note").value.trim();
  return { status, tags, note };
}

async function saveAnnotation() {
  const d = state.detail;
  const annotator = $("#annotator").value.trim() || "anon";
  localStorage.setItem("annotator", annotator);
  const wasNew = !d.annotation;
  const res = await fetch("/api/annotate", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ gid: d.gid, annotator, payload: collectPayload() }),
  }).then((r) => r.json());
  if (res.ok) {
    if (wasNew) bumpAnnoCount(1);
    d.annotation = { annotator, payload: JSON.stringify(collectPayload()) };
    flash();
    markAnnotatedInGrid(d.gid, true);
  }
}

async function deleteAnnotation() {
  const d = state.detail;
  await fetch(`/api/annotate/${encodeURIComponent(d.gid)}`, { method: "DELETE" });
  bumpAnnoCount(-1);
  d.annotation = null;
  markAnnotatedInGrid(d.gid, false);
  renderDetail();
}

function markAnnotatedInGrid(gid, on) {
  const idx = state.items.findIndex((x) => x.gid === gid);
  if (idx >= 0) {
    state.items[idx].annotated = on ? 1 : 0;
    const card = $(`#grid .card[data-i="${idx}"]`);
    if (card) card.classList.toggle("annotated", on);
  }
}

function flash() {
  const f = $("#flash"); if (!f) return;
  f.classList.add("show"); setTimeout(() => f.classList.remove("show"), 1100);
}

function onKey(e) {
  if (!state.detail) return;
  const typing = ["INPUT", "TEXTAREA"].includes(document.activeElement.tagName);
  if (e.key === "Escape") { closeDrawer(); return; }
  if (typing) return;
  if (e.key === "ArrowRight") { e.preventDefault(); navDetail(1); }
  else if (e.key === "ArrowLeft") { e.preventDefault(); navDetail(-1); }
  else if (["k", "r", "d"].includes(e.key.toLowerCase())) {
    const map = { k: "keep", r: "review", d: "discard" };
    const btn = $(`#anno .anno-status button[data-status="${map[e.key.toLowerCase()]}"]`);
    if (btn) { btn.click(); saveAnnotation(); }
  }
}

function navDetail(dir) {
  const ni = state.detailIndex + dir;
  if (ni < 0 || ni >= state.items.length) return;
  openDetail(ni);
}

boot();

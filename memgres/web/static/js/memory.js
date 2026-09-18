// Memory: a space drawn as a graph or a tree, search, the local view and a record's card.
// Read-only in this release.

import { get, post, ApiError } from "./api.js";
import { ago, fmtDate, nf, t } from "./i18n.js";
import { renderMarkdown } from "./md.js";
import { VISUALIZERS } from "./viz.js";
import { $, $$, autoSlots, esc, fnv, hexIcon, PALETTE, PALETTE_KEYS, reducedMotion, store, toast } from "./ui.js";

const LARGE = 60;          // above this many records the whole graph gets busy: suggest the local view
const m = {
  spaces: [], visiting: [], space: null, graph: null,
  T: new Map(), links: [], deg: new Map(), rootAuto: {}, spaceAuto: {}, byRecord: new Map(),
  selected: null, query: "", hits: null, scope: "all", depth: 1, vizId: store.get("memgres.viz") || "graph",
  ctl: null, ctxIds: new Set(), dismissed: new Set(), root: null, navigate: null, searchSeq: 0,
};

// ─── the area ────────────────────────────────────────────────────────────────
export async function renderMemory(root, ctx) {
  m.root = root;
  m.navigate = ctx.navigate;
  m.session = ctx.session;
  if (!root.dataset.built) buildArea(root);
  applyAreaText();
  const reload = ctx.forceReload;
  if (!m.loaded || reload) { await loadSpaces(); if (reload) m.graph = null; }
  loadVisiting();
  const asked = new URLSearchParams(location.search).get("space");
  if (asked && !known(asked) && isSuper()) await visit(asked);
  renderSpaceList();
  if (asked && !known(asked)) { showNoAccess(asked); return; }
  const wanted = asked || store.get("memgres.space");
  const target = known(wanted) || [...m.spaces].sort(byRecords)[0];
  if (!target) { showEmpty(); return; }
  if (!m.space || m.space.id !== target.id || !m.graph) await openSpace(target);
  else { mountViz(); inspect(); }
}

// The sidebar's list of spaces, on every page — not only while memory is open.
export async function sidebarSpaces(ctx) {
  m.navigate = ctx.navigate;
  m.session = ctx.session;
  loadVisiting();
  wireSidebar();
  if (!m.loaded || ctx.forceReload) { try { await loadSpaces(); } catch { return; } m.graph = null; }
  renderSpaceList();
}

const byRecords = (a, b) => b.records - a.records || a.name.localeCompare(b.name);

// ─── spaces a superadmin opened by its role ──────────────────────────────────
// Not a membership and nothing on the server: a list in this browser that
// belongs to this panel session, so the next sign-in starts with only your own.
const isSuper = () => m.session?.role === "superadmin";
const known = (id) => id && (m.spaces.find((s) => s.id === id) || m.visiting.find((s) => s.id === id));
const visitKey = () => `${m.session?.user?.id}|${m.session?.expires_at}`;

function loadVisiting() {
  let saved = null;
  try { saved = JSON.parse(store.get("memgres.visiting") || "null"); } catch { /* ignore */ }
  const ours = isSuper() && saved && saved.key === visitKey() && Array.isArray(saved.spaces);
  m.visiting = ours ? saved.spaces : [];
  // a list left by an earlier session (one that expired rather than signed out) goes now
  if (saved && !ours) store.set("memgres.visiting", "");
}

// Signing out forgets what this browser was showing: the spaces opened by role,
// and the cached sidebar, so the next person in this tab starts clean.
export function forgetSession() {
  store.set("memgres.visiting", "");
  Object.assign(m, { spaces: [], visiting: [], space: null, graph: null, loaded: false, session: null,
    selected: null, hits: null, results: null, query: "" });
  $("#spacelist").innerHTML = "";
  $("#mbar-space").innerHTML = "";
}

function saveVisiting() {
  store.set("memgres.visiting", JSON.stringify({ key: visitKey(), spaces: m.visiting }));
}

async function visit(id) {
  let meta;
  try { meta = await get(`/spaces/${encodeURIComponent(id)}`); } catch { return null; }
  if (meta.member) { m.loaded = false; await loadSpaces(); return known(id); }
  const entry = { ...meta, visiting: true };
  m.visiting = [entry, ...m.visiting.filter((s) => s.id !== id)].slice(0, 20);
  saveVisiting();
  return entry;
}

function forget(id) {
  m.visiting = m.visiting.filter((s) => s.id !== id);
  saveVisiting();
  if (m.space?.id === id) { m.space = null; m.graph = null; m.navigate("/memory", { replace: true }); }
  else renderSpaceList();
}

function openEverySpace() {
  const veil = document.createElement("div");
  veil.className = "veil";
  veil.innerHTML = `<div class="dialog" role="dialog" aria-modal="true" aria-labelledby="as-title">
    <h3 id="as-title">${esc(t("every.title"))}</h3>
    <div class="filter" style="max-width:none"><svg width="12" height="12" viewBox="0 0 16 16" aria-hidden="true"><circle cx="7" cy="7" r="4.6" fill="none" stroke="#707aa0" stroke-width="1.6"/><path d="m10.4 10.4 3.4 3.4" stroke="#707aa0" stroke-width="1.6" stroke-linecap="round"/></svg>
      <input id="as-q" type="search" autocomplete="off" placeholder="${esc(t("every.search"))}" aria-label="${esc(t("every.search"))}"></div>
    <div class="stack every-list" id="as-list"><p class="note">…</p></div>
    <p class="note">${esc(t("every.note"))}</p>
    <div class="row" style="justify-content:flex-end"><button type="button" class="btn quiet" data-x>${esc(t("common.cancel"))}</button></div>
  </div>`;
  document.body.append(veil);
  const close = () => veil.remove();
  veil.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
  veil.addEventListener("click", (e) => {
    if (e.target === veil || e.target.closest("[data-x]")) return close();
    const b = e.target.closest("[data-open]");
    if (!b) return;
    close();
    $("#shell").classList.remove("drawer");
    m.navigate(`/memory?space=${encodeURIComponent(b.dataset.open)}`);
  });
  let seq = 0, timer = null;
  const load = async () => {
    const my = ++seq, q = $("#as-q", veil).value.trim();
    let rows, truncated;
    try { ({ spaces: rows, truncated } = await get(`/admin/spaces?q=${encodeURIComponent(q)}`)); } catch {
      if (my === seq) $("#as-list", veil).innerHTML = `<p class="note">${esc(t("err.network"))}</p>`;
      return;
    }
    if (my !== seq) return;
    const mine = new Set(m.spaces.map((s) => s.id));
    $("#as-list", veil).innerHTML = rows.length ? rows.map((s) => {
      const c = PALETTE[fnv(s.name) % PALETTE.length];
      return `<button class="item pick" data-open="${esc(s.id)}">${hexIcon(c, !mine.has(s.id), 16)}
        <div class="grow"><b class="mono">${esc(s.name)}</b><small>${esc(t("every.row", { owner: s.owner.name || "—", members: s.members, records: t("mem.records", { n: s.records }) }))}</small></div>
        ${mine.has(s.id) ? `<span class="perm read">${esc(t("every.yours"))}</span>` : ""}</button>`;
    }).join("") + (truncated ? `<p class="note" style="color:var(--admin)">${esc(t("every.truncated", { n: rows.length }))}</p>` : "")
      : `<p class="note">${esc(t("every.none"))}</p>`;
  };
  $("#as-q", veil).addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(load, 220); });
  $("#as-q", veil).focus();
  load();
}

function buildArea(root) {
  root.dataset.built = "1";
  root.innerHTML = `<div class="mem">
    <div class="toolbar">
      <div class="spacetitle" id="spacetitle"></div>
      <div class="search">
        <svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true"><circle cx="7" cy="7" r="4.6" fill="none" stroke="#707aa0" stroke-width="1.5"/><path d="m10.4 10.4 3.4 3.4" stroke="#707aa0" stroke-width="1.5" stroke-linecap="round"/></svg>
        <input id="q" type="search" autocomplete="off">
      </div>
    </div>
    <div class="workspace">
      <div class="stage" id="stage">
        <div class="viz-host" id="viz-host"></div>
        <div class="stagebar">
          <div class="segm" id="viz-switch"></div>
          <div class="segm" id="scope-seg">
            <button id="scope-all" aria-pressed="true"></button>
            <button id="scope-local" aria-pressed="false"></button>
            <span class="lbl" id="depth-lbl"></span>
            <button data-depth="1" aria-pressed="true">1</button><button data-depth="2" aria-pressed="false">2</button><button data-depth="3" aria-pressed="false">3</button>
          </div>
          <span class="count" id="count"></span>
          <div class="crumbs" id="crumbs" hidden></div>
        </div>
        <div class="suggest" id="suggest" hidden></div>
        <div class="zoom">
          <button class="iconbtn" id="zin"><svg width="12" height="12" viewBox="0 0 12 12"><path d="M6 1v10M1 6h10" stroke="#c9d0e6" stroke-width="1.6" stroke-linecap="round"/></svg></button>
          <button class="iconbtn" id="zout"><svg width="12" height="12" viewBox="0 0 12 12"><path d="M1 6h10" stroke="#c9d0e6" stroke-width="1.6" stroke-linecap="round"/></svg></button>
          <button class="iconbtn" id="zfit"><svg width="12" height="12" viewBox="0 0 12 12"><path d="M1 4V1h3M8 1h3v3M11 8v3H8M4 11H1V8" fill="none" stroke="#c9d0e6" stroke-width="1.5" stroke-linecap="round"/></svg></button>
          <button class="iconbtn" id="keys-btn" aria-expanded="false"><span class="qmark">?</span></button>
        </div>
        <div class="keys-pop" id="keys-pop" hidden></div>
      </div>
      <aside class="results" id="results" aria-live="polite" hidden></aside>
      <div class="splitter" id="splitter" role="separator" aria-orientation="vertical" tabindex="0"></div>
      <aside class="inspector" id="inspector" aria-live="polite"></aside>
    </div>
  </div>`;

  let timer = null;
  $("#q", root).addEventListener("input", (e) => {
    clearTimeout(timer);
    const value = e.target.value.trim();
    timer = setTimeout(() => runSearch(value), 280);
  });
  $("#q", root).addEventListener("keydown", (e) => { if (e.key === "Escape") { e.target.value = ""; runSearch(""); e.target.blur(); } });
  $("#viz-switch", root).addEventListener("click", (e) => {
    const b = e.target.closest("[data-viz]");
    if (!b) return;
    m.vizId = b.dataset.viz; store.set("memgres.viz", m.vizId);
    renderVizSwitch(); mountViz();
  });
  $("#scope-all", root).onclick = () => setScope("all");
  $("#scope-local", root).onclick = () => setScope("local");
  for (const b of $$("[data-depth]", root)) b.onclick = () => { m.depth = Number(b.dataset.depth); sync("data"); };
  $("#zin", root).onclick = () => m.ctl?.zoomBy?.(1.3);
  $("#zout", root).onclick = () => m.ctl?.zoomBy?.(1 / 1.3);
  $("#zfit", root).onclick = () => m.ctl?.fit?.();
  $("#keys-btn", root).onclick = () => { const p = $("#keys-pop", root); p.hidden = !p.hidden; $("#keys-btn", root).setAttribute("aria-expanded", String(!p.hidden)); };
  $("#crumbs", root).addEventListener("click", (e) => {
    const b = e.target.closest("[data-crumb]");
    if (!b || b.getAttribute("aria-current")) return;
    if (b.dataset.crumb === "") { m.scope = "all"; select(""); m.ctl?.fit?.(); } else select(b.dataset.crumb, { centre: true });
  });
  $("#suggest", root).addEventListener("click", (e) => {
    const b = e.target.closest("[data-sg]");
    if (!b) return;
    if (b.dataset.sg === "x") { m.dismissed.add(m.space.id); updateSuggest(); return; }
    if (m.selected === null || m.selected === "") {
      const busiest = [...m.deg.entries()].sort((a, c) => c[1] - a[1])[0];
      if (!busiest) return;
      m.selected = busiest[0]; inspect();
    }
    setScope("local");
  });
  $("#inspector", root).addEventListener("click", (e) => {
    const go = e.target.closest("[data-go]");
    if (go) { select(go.dataset.go, { centre: true }); return; }
    const cp = e.target.closest("[data-copy]");
    if (cp) (navigator.clipboard?.writeText(cp.dataset.copy) || Promise.reject()).then(() => toast(t("toast.copied", { v: cp.dataset.copy })), () => toast(cp.dataset.copy));
  });

  $("#inspector", root).addEventListener("click", onInspectorAction);
  $("#results", root).addEventListener("click", (e) => {
    if (e.target.closest("[data-close]")) { const q = $("#q", root); q.value = ""; runSearch(""); q.focus(); return; }
    const go = e.target.closest("[data-go]");
    if (go) select(go.dataset.go, { centre: true });
  });
  wireSplitter(root);
  wireSidebar();
  document.addEventListener("keydown", onKey);
}

function wireSidebar() {
  if (m.sidebarWired) return;
  m.sidebarWired = true;
  $("#spacelist").addEventListener("click", (e) => {
    const b = e.target.closest("[data-space]");
    if (!b) return;
    e.preventDefault();
    $("#shell").classList.remove("drawer");
    m.navigate(`/memory?space=${encodeURIComponent(b.dataset.space)}`);
  });
  $("#spacelist").addEventListener("click", (e) => {
    const x = e.target.closest("[data-forget]");
    if (x) { e.preventDefault(); e.stopPropagation(); forget(x.dataset.forget); return; }
    if (e.target.closest("[data-every]")) openEverySpace();
  }, true);
  $("#space-filter").addEventListener("input", renderSpaceList);
}

function applyAreaText() {
  const r = m.root;
  $("#q", r).placeholder = t("mem.search");
  $("#q", r).setAttribute("aria-label", t("mem.search"));
  $("#scope-all", r).textContent = t("scope.all");
  $("#scope-local", r).textContent = t("scope.local");
  $("#depth-lbl", r).textContent = t("scope.depth");
  for (const [id, key] of [["zin", "zoom.in"], ["zout", "zoom.out"], ["zfit", "zoom.fit"], ["keys-btn", "zoom.keys"]]) $("#" + id, r).setAttribute("aria-label", t(key));
  $("#keys-pop", r).innerHTML = [["/", "keys.search"], ["L", "keys.local"], ["F", "keys.fit"], ["↑", "keys.parent"], ["C", "keys.copy"], ["Esc", "keys.clear"]]
    .map(([k, key]) => `<kbd>${esc(k)}</kbd><span>${esc(t(key))}</span>`).join("");
  renderVizSwitch();
}

function onKey(e) {
  if (!m.root || m.root.hidden || e.target.closest("input, select, textarea, .veil") || e.metaKey || e.ctrlKey || e.altKey) return;
  const k = e.key.toLowerCase();
  if (e.key === "/") { e.preventDefault(); $("#q", m.root).focus(); }
  else if (k === "f") m.ctl?.fit?.();
  else if (k === "l") setScope(m.scope === "local" ? "all" : "local");
  else if (e.key === "Escape") { select(null); $("#keys-pop", m.root).hidden = true; }
  else if (e.key === "ArrowUp" && m.selected) { const n = m.T.get(m.selected); if (n && n.parent !== undefined) select(n.parent, { centre: true }); }
  else if (k === "c" && m.selected && m.T.get(m.selected)?.path) {
    const p = m.T.get(m.selected).path;
    (navigator.clipboard?.writeText(p) || Promise.reject()).then(() => toast(t("toast.copied", { v: p })), () => toast(p));
  }
}

// ─── spaces ──────────────────────────────────────────────────────────────────
async function loadSpaces() {
  m.spaces = (await get("/spaces")).spaces;
  m.loaded = true;
  m.spaceAuto = autoSlots(m.spaces.map((s) => s.name));
}

const spaceColor = (s) => s.visiting ? PALETTE[fnv(s.name) % PALETTE.length] : PALETTE[m.spaceAuto[s.name] ?? 0];

function renderSpaceList() {
  const q = $("#space-filter").value.trim().toLowerCase();
  const list = [...m.spaces].sort(byRecords);
  $("#space-filter-wrap").hidden = list.length <= 8;
  $("#spaces-total").textContent = nf(list.length);
  const shown = list.filter((s) => !q || s.name.toLowerCase().includes(q));
  const own = shown.map((s) => `<a class="spaceitem" role="listitem" href="/memory?space=${encodeURIComponent(s.id)}" data-space="${esc(s.id)}" style="--c:${spaceColor(s)}" aria-current="${m.space?.id === s.id}" title="${esc(s.name)}">${hexIcon(spaceColor(s), false, 16)}<span class="nm">${esc(s.name)}</span>${s.waiting ? `<span class="wait-dot" title="${esc(t("sp.waitingN", { n: s.waiting }))}">${nf(s.waiting)}</span>` : `<span class="ct">${nf(s.records)}</span>`}</a>`).join("")
    || `<p class="side-empty">${esc(list.length ? t("side.noSpaces", { q }) : isSuper() ? t("every.noOwn") : t("mem.noSpaces"))}</p>`;
  const visiting = (m.visiting || []).filter((s) => !q || s.name.toLowerCase().includes(q));
  const role = visiting.length ? `<div class="sec-h visiting-h" title="${esc(t("every.groupWhy"))}"><span>${esc(t("every.group"))}</span></div>` + visiting.map((s) =>
    `<a class="spaceitem visiting" role="listitem" href="/memory?space=${encodeURIComponent(s.id)}" data-space="${esc(s.id)}" style="--c:${spaceColor(s)}" aria-current="${m.space?.id === s.id}" title="${esc(t("every.itemWhy", { name: s.name }))}">${hexIcon(spaceColor(s), true, 16)}<span class="nm">${esc(s.name)}</span><button class="forget" data-forget="${esc(s.id)}" aria-label="${esc(t("every.forget", { name: s.name }))}">×</button></a>`).join("") : "";
  const every = isSuper() ? `<button class="every-link" data-every>${esc(t("every.link"))}</button>` : "";
  $("#spacelist").innerHTML = own + role + every;
  if (m.space) $("#mbar-space").innerHTML = `${hexIcon(spaceColor(m.space), false, 14)}${esc(m.space.name)}`;
}

// A link to a space this person cannot open — or that does not exist; the two look
// the same. What they can do is ask the people who run it.
async function showNoAccess(id) {
  m.space = null; m.graph = null;
  for (const sel of [".stagebar", ".zoom", "#suggest"]) $(sel, m.root).hidden = true;
  $("#q", m.root).disabled = true;
  $("#spacetitle", m.root).innerHTML = "";
  $("#mbar-space").innerHTML = "";
  const host = $("#viz-host", m.root);
  $("#inspector", m.root).innerHTML = `<p class="note">${esc(t("noacc.side"))}</p>`;
  host.innerHTML = `<div class="noaccess"><div class="card">
    ${hexIcon("#707aa0", true, 34)}
    <h2>${esc(t("noacc.title"))}</h2>
    <p class="note">${esc(t("noacc.lead"))}</p>
    <div id="na-state"><p class="note">${esc(t("mem.loading"))}</p></div>
  </div></div>`;
  let state = { status: null };
  try { state = await get(`/spaces/${encodeURIComponent(id)}/access`); } catch { /* treat as none */ }
  const box = $("#na-state", host);
  if (!box) return;
  const form = (again) => `<form class="invite-row" id="na-form">
      <div class="field"><label for="na-perm">${esc(t("tok.access"))}</label><select id="na-perm" class="sel">
        ${["read", "write", "admin"].map((p) => `<option value="${p}">${esc(t("perm." + p))}</option>`).join("")}</select></div>
      <button class="btn primary" type="submit">${esc(t(again ? "noacc.askAgain" : "noacc.ask"))}</button></form>`;
  if (state.status === "pending") box.innerHTML = `<div class="auth-note wait">${esc(t("noacc.pending", { perm: t("perm." + state.permission), ago: ago(state.created_at) }))}</div>`;
  else box.innerHTML = (state.status === "denied" ? `<div class="auth-note bad">${esc(t("noacc.denied"))}</div>` : "") + form(state.status === "denied");
  $("#na-form", box)?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const b = $("button", e.target);
    b.disabled = true;
    try {
      const r = await post(`/spaces/${encodeURIComponent(id)}/access`, { permission: $("#na-perm", box).value });
      if (r.status === "already_reachable") { m.loaded = false; return m.navigate(`/memory?space=${encodeURIComponent(id)}`, { replace: true }); }
      toast(t("noacc.sent"));
      showNoAccess(id);
    } catch (ex) {
      b.disabled = false;
      toast(ex instanceof ApiError && ex.status === 409 ? String(ex.detail) : t("err.save"));
    }
  });
}

function showEmpty() {
  m.space = null; m.graph = null;
  for (const sel of [".stagebar", ".zoom", "#suggest"]) $(sel, m.root).hidden = true;
  $("#q", m.root).disabled = true;
  $("#spacetitle", m.root).innerHTML = "";
  $("#viz-host", m.root).innerHTML = `<p class="viz-empty">${esc(t("mem.noSpaces"))}</p>`;
  $("#inspector", m.root).innerHTML = `<p class="note">${esc(t(isSuper() ? "every.emptyHelp" : "mem.noSpacesHelp"))}</p>`;
}

async function openSpace(space) {
  for (const sel of [".stagebar", ".zoom"]) $(sel, m.root).hidden = false;
  $("#q", m.root).disabled = false;
  m.space = space;
  store.set("memgres.space", space.id);
  m.selected = null; m.query = ""; m.hits = null; m.results = null; m.scope = "all";
  setSearching(false); renderResults();
  $("#q", m.root).value = "";
  renderSpaceList();
  $("#spacetitle", m.root).innerHTML = `${hexIcon(spaceColor(space), false, 20)}<h1>${esc(space.name)}</h1><small>${esc(t("mem.records", { n: space.records }))}</small>${space.visiting ? `<span class="perm-badge role" title="${esc(t("every.groupWhy"))}">${esc(t("every.badge"))}</span>` : `<span class="perm-badge">${esc(t("perm." + space.permission))}</span>`}`;
  $("#viz-host", m.root).innerHTML = `<p class="viz-empty">${esc(t("mem.loading"))}</p>`;
  try {
    m.graph = await get(`/spaces/${encodeURIComponent(space.id)}/graph`);
  } catch (e) {
    m.graph = null;
    $("#viz-host", m.root).innerHTML = `<p class="viz-empty">${esc(e instanceof ApiError && e.status === 404 ? t("mem.gone") : t("err.network"))}</p>`;
    return;
  }
  index(m.graph);
  mountViz();
  inspect();
}

// ─── index ───────────────────────────────────────────────────────────────────
function index(graph) {
  const T = new Map([["", { id: "", label: m.space.name, depth: 0, children: [] }]]);
  const ensure = (path) => {
    if (T.has(path)) return T.get(path);
    const parts = path.split("."), parent = parts.slice(0, -1).join(".");
    const node = { id: path, path, label: parts.at(-1), depth: parts.length, parent, root: parts[0], children: [] };
    T.set(path, node); ensure(parent).children.push(node);
    return node;
  };
  m.byRecord = new Map();
  for (const r of graph.records) {
    let node;
    if (r.path) node = ensure(r.path);
    else {
      // a record with no place in the tree hangs off the space itself
      const id = "#" + r.id;
      node = { id, path: null, label: (r.title || r.id).slice(0, 24), depth: 1, parent: "", root: id, children: [] };
      T.set(id, node); T.get("").children.push(node);
    }
    node.record = r;
    m.byRecord.set(r.id, node.id);
  }
  const count = (n) => (n.record ? 1 : 0) + n.children.reduce((s, c) => s + count(c), 0);
  for (const n of T.values()) n.children.sort((a, b) => count(b) - count(a) || a.label.localeCompare(b.label));
  m.links = []; m.deg = new Map();
  for (const l of graph.links) {
    const a = m.byRecord.get(l.a), b = m.byRecord.get(l.b);
    if (!a || !b) continue;
    m.links.push({ a, b });
    m.deg.set(a, (m.deg.get(a) || 0) + 1);
    m.deg.set(b, (m.deg.get(b) || 0) + 1);
  }
  m.T = T;
  m.rootAuto = autoSlots(T.get("").children.map((c) => c.id));
}

const count = (n) => (n.record ? 1 : 0) + n.children.reduce((s, c) => s + count(c), 0);
const colorOf = (id) => id === "" ? spaceColor(m.space) : PALETTE[m.rootAuto[m.T.get(id)?.root ?? id] ?? 0];

function neighbours(id) {
  const s = new Set([id]), n = m.T.get(id);
  if (!n) return s;
  if (n.parent !== undefined) s.add(n.parent);
  n.children.forEach((c) => s.add(c.id));
  for (const l of m.links) { if (l.a === id) s.add(l.b); if (l.b === id) s.add(l.a); }
  return s;
}

const isLocal = () => m.scope === "local" && m.selected !== null && m.selected !== "" && m.T.has(m.selected);

function visibleIds() {
  m.ctxIds = new Set();
  if (!isLocal()) return [...m.T.keys()];
  const dist = new Map([[m.selected, 0]]), queue = [m.selected];
  while (queue.length) {
    const id = queue.shift(), d = dist.get(id);
    if (d >= m.depth) continue;
    for (const nb of neighbours(id)) if (!dist.has(nb)) { dist.set(nb, d + 1); queue.push(nb); }
  }
  let cur = m.T.get(m.T.get(m.selected).parent);
  while (cur) {
    if (!dist.has(cur.id)) { dist.set(cur.id, -1); m.ctxIds.add(cur.id); }
    if (cur.id === "") break;
    cur = m.T.get(cur.parent);
  }
  return [...dist.keys()];
}

const matches = (n) => !!(m.hits && n?.record && m.hits.has(n.record.id));

const api = {
  get space() { return m.space; }, get selected() { return m.selected; }, get query() { return m.hits ? m.query : ""; }, get local() { return isLocal(); },
  node: (id) => m.T.get(id), root: () => m.T.get(""), links: () => m.links, degree: (id) => m.deg.get(id) || 0,
  visibleIds, isContext: (id) => m.ctxIds.has(id), colorOf, neighbours, matches, count, t,
  select: (id) => select(id), tip, hideTip,
};

// ─── visualizer host & chrome ────────────────────────────────────────────────
function renderVizSwitch() {
  const box = $("#viz-switch", m.root);
  if (!VISUALIZERS.has(m.vizId)) m.vizId = "graph";
  box.innerHTML = [...VISUALIZERS.values()].map((v) => `<button data-viz="${esc(v.id)}" aria-pressed="${v.id === m.vizId}">${esc(v.nameKey ? t(v.nameKey) : v.name)}</button>`).join("");
}

function mountViz() {
  if (!m.graph) return;
  if (m.ctl?.destroy) m.ctl.destroy();
  const host = $("#viz-host", m.root);
  host.replaceChildren();
  hideTip();
  m.ctl = VISUALIZERS.get(m.vizId).mount(host, api);
  for (const id of ["zin", "zout", "zfit"]) $("#" + id, m.root).hidden = !m.ctl.zoomBy;
  updateChrome();
}

function sync(reason = "focus") {
  m.ctl?.update(reason);
  updateChrome();
}

function updateChrome() { updateCount(); updateScope(); updateCrumbs(); updateSuggest(); }

function updateCount() {
  const el = $("#count", m.root), g = m.graph;
  if (!g) { el.textContent = ""; return; }
  if (m.hits) { el.textContent = t("mem.matches", { n: m.hits.size }); return; }
  if (isLocal()) {
    const shown = visibleIds().filter((id) => m.T.get(id)?.record && !m.ctxIds.has(id)).length;
    el.innerHTML = t("count.local", { shown: nf(shown), total: esc(t("mem.records", { n: g.total })) });
    return;
  }
  el.textContent = g.truncated ? t("count.truncated", { shown: nf(g.records.length), total: t("mem.records", { n: g.total }) })
    : `${t("mem.records", { n: g.total })} · ${t("mem.links", { n: m.links.length })}`;
}

function updateScope() {
  const local = m.scope === "local";
  $("#scope-all", m.root).setAttribute("aria-pressed", String(!local));
  $("#scope-local", m.root).setAttribute("aria-pressed", String(local));
  for (const b of $$("[data-depth]", m.root)) { b.setAttribute("aria-pressed", String(Number(b.dataset.depth) === m.depth)); b.hidden = !local; }
  $("#depth-lbl", m.root).hidden = !local;
}

function updateCrumbs() {
  const el = $("#crumbs", m.root);
  if (!isLocal()) { el.hidden = true; return; }
  const node = m.T.get(m.selected);
  const chain = [];
  let cur = node;
  while (cur && cur.id !== "") { chain.unshift(cur); cur = m.T.get(cur.parent); }
  const sc = spaceColor(m.space);
  el.innerHTML = `<nav aria-label="${esc(t("crumbs.label"))}"><button data-crumb="" style="--c:${sc}">${hexIcon(sc, false, 13)}${esc(m.space.name)}</button>${chain.map((n, i) =>
    `<span class="sep">/</span><button data-crumb="${esc(n.id)}" style="--c:${colorOf(n.id)}"${i === chain.length - 1 ? ' aria-current="true"' : ""}>${esc(n.label)}</button>`).join("")}</nav>`;
  el.hidden = false;
}

function updateSuggest() {
  const el = $("#suggest", m.root), g = m.graph;
  const show = !!g && m.vizId === "graph" && m.scope === "all" && !m.hits && g.total > LARGE && !m.dismissed.has(m.space.id);
  el.hidden = !show;
  if (!show) return;
  const has = m.selected !== null && m.selected !== "";
  el.innerHTML = `<span>${esc(t("suggest.text", { records: t("mem.records", { n: g.total }) }))} ${esc(t(has ? "suggest.withSel" : "suggest.noSel"))}</span>
    <button class="btn small" data-sg="local">${esc(t(has ? "suggest.local" : "suggest.busiest"))}</button><button class="btn small quiet" data-sg="x">${esc(t("suggest.later"))}</button>`;
}

function setScope(scope) {
  if (scope === "local" && (m.selected === null || m.selected === "")) { toast(t("local.needSel")); return; }
  m.scope = scope;
  sync("data");
  if (scope === "local" && m.ctl?.focus) setTimeout(() => m.ctl.focus(m.selected), reducedMotion() ? 0 : 480);
}

function select(id, opts = {}) {
  if (id !== null && !m.T.has(id)) return;
  m.selected = id;
  if (opts.clearSearch && m.hits) { m.hits = null; m.query = ""; $("#q", m.root).value = ""; }
  if (m.scope === "local") { if (id === null || id === "") m.scope = "all"; sync("data"); } else sync("focus");
  inspect();
  if (opts.centre && id !== null && m.ctl?.focus) setTimeout(() => m.ctl.focus(id), m.scope === "local" && !reducedMotion() ? 480 : 0);
}

async function runSearch(q) {
  m.query = q;
  const seq = ++m.searchSeq;
  if (!q) { m.hits = null; m.results = null; setSearching(false); renderResults(); sync("focus"); return; }
  setSearching(true);
  renderResults();
  try {
    const { hits } = await get(`/spaces/${encodeURIComponent(m.space.id)}/search?q=${encodeURIComponent(q)}`);
    if (seq !== m.searchSeq) return;          // a newer query already answered
    m.results = hits;
    m.hits = new Set(hits.map((h) => h.id));
    if (m.scope === "local") { m.scope = "all"; sync("data"); } else sync("focus");
  } catch {
    if (seq === m.searchSeq) toast(t("err.network"));
  } finally {
    if (seq === m.searchSeq) { setSearching(false); renderResults(); }
  }
}

function setSearching(on) {
  m.searching = on;
  $(".search", m.root).classList.toggle("busy", on);
}

// Results sit in their own column beside the record, so opening one keeps the list.
function renderResults() {
  const box = $("#results", m.root);
  const on = !!m.query;
  const changed = box.hidden === on;
  box.hidden = !on;
  $(".workspace", m.root).classList.toggle("with-results", on);
  // the map just got narrower or wider: fit it to its new room once the grid has moved
  if (changed) requestAnimationFrame(() => m.ctl?.fit?.());
  if (!on) return;
  const results = m.results || [];
  const waiting = m.searching && !m.results;
  const head = `<div class="res-head"><div><div class="eyebrow">${esc(t("keys.search"))}</div>
      <b>${esc(waiting ? t("res.searching") : t("insp.search", { n: results.length, q: m.query }))}</b></div>
      <button class="ghost" data-close aria-label="${esc(t("res.close"))}" title="${esc(t("res.close"))}">
        <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true"><path d="M3 3l8 8M11 3l-8 8" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg></button></div>`;
  if (waiting) {
    box.innerHTML = head + `<div class="res-wait"><span class="spinner" aria-hidden="true"></span>${esc(t("res.searching"))}</div>`;
    return;
  }
  box.innerHTML = head + (results.length ? `<div class="list${m.searching ? " stale" : ""}">${results.map((h) => {
    const id = m.byRecord.get(h.id);
    return id ? `<button data-go="${esc(id)}" aria-current="${m.selected === id}">${hexIcon(colorOf(id), false, 12)}<span>${esc(h.title || h.path)}</span><small>${esc(h.path || "")}</small></button>
      ${h.snippet ? `<p class="snippet">${esc(plainSnippet(h.snippet))}</p>` : ""}` : "";
  }).join("")}</div>` : `<p class="note">${esc(t("insp.noHits"))}</p>`);
}

// a snippet is a slice of Markdown: show the words, not the marks
function plainSnippet(text) {
  return String(text)
    .replace(/\[\[([^\]|#\n]+)(?:#[^\]|\n]*)?(?:\|([^\]\n]*))?\]\]/g, (w, target, label) => (label || target).trim())
    .replace(/\[([^\]\n]*)\]\([^)\n]*\)/g, "$1")
    .replace(/\*{1,3}|`+|^#{1,6}\s+|^>\s?/gm, "")
    .replace(/\|/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 240);
}

// The record panel's width, dragged by the edge between it and the map.
function wireSplitter(root) {
  const bar = $("#splitter", root), ws = $(".workspace", root);
  const MIN = 280, KEY = "memgres.inspector";
  const apply = (w) => {
    const room = ws.getBoundingClientRect().width - 320 - ($("#results", root).hidden ? 0 : 310);
    const v = Math.round(Math.max(MIN, Math.min(w, Math.max(MIN, room))));
    ws.style.setProperty("--insp-w", v + "px");
    return v;
  };
  const saved = Number(store.get(KEY));
  if (saved) requestAnimationFrame(() => apply(saved));
  let drag = null;
  bar.addEventListener("pointerdown", (e) => {
    drag = { x: e.clientX, w: $("#inspector", root).getBoundingClientRect().width };
    bar.setPointerCapture(e.pointerId); bar.classList.add("on"); e.preventDefault();
  });
  bar.addEventListener("pointermove", (e) => { if (drag) apply(drag.w + (drag.x - e.clientX)); });
  const end = () => {
    if (!drag) return;
    drag = null; bar.classList.remove("on");
    store.set(KEY, String(Math.round($("#inspector", root).getBoundingClientRect().width)));
    m.ctl?.fit?.();
  };
  bar.addEventListener("pointerup", end);
  bar.addEventListener("pointercancel", end);
  bar.addEventListener("dblclick", () => { ws.style.removeProperty("--insp-w"); store.set(KEY, ""); });
  bar.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    e.preventDefault();
    const w = apply($("#inspector", root).getBoundingClientRect().width + (e.key === "ArrowLeft" ? 24 : -24));
    store.set(KEY, String(w));
  });
}

// ─── tooltip ─────────────────────────────────────────────────────────────────
function tip(id, e) {
  const n = m.T.get(id), el = $("#tip");
  if (!n) return;
  if (id === "") el.innerHTML = `<b>${esc(m.space.name)}</b><small>${esc(t("mem.records", { n: count(n) }))} · ${esc(t("mem.links", { n: m.links.length }))}</small>`;
  else if (!n.record) el.innerHTML = `<b>${esc(n.path)}</b><small>${esc(t("tip.below", { records: t("mem.records", { n: count(n) }) }))}</small>`;
  else el.innerHTML = `<b>${esc(n.record.title || n.path)}</b><small>${esc(t("tip.rec", { path: n.path || "—", links: t("mem.links", { n: m.deg.get(id) || 0 }), ago: ago(n.record.updated_at) }))}</small>${n.record.tags.length ? `<div class="tags">${n.record.tags.map((x) => `<span class="tag">${esc(x)}</span>`).join("")}</div>` : ""}`;
  el.hidden = false;
  const w = el.offsetWidth, h = el.offsetHeight;
  let x = e.clientX + 16, y = e.clientY + 16;
  if (x + w > innerWidth - 8) x = e.clientX - w - 16;
  if (y + h > innerHeight - 8) y = e.clientY - h - 16;
  el.style.left = Math.max(8, x) + "px";
  el.style.top = Math.max(8, y) + "px";
}
function hideTip() { $("#tip").hidden = true; }

// ─── inspector ───────────────────────────────────────────────────────────────
const pathNav = (node) => {
  if (!node.path) return "";
  const parts = node.path.split("."), acc = [];
  return `<nav class="path"><button data-go="">${esc(m.space.name)}</button>${parts.map((p) => { acc.push(p); const x = acc.join("."); return `<span>/</span><button data-go="${esc(x)}">${esc(p)}</button>`; }).join("")}</nav>`;
};
const listOf = (ids) => ids.length ? `<div class="list">${ids.map((id) => {
  const n = m.T.get(id);
  return `<button data-go="${esc(id)}">${hexIcon(colorOf(id), !n.record, 12)}<span>${esc(n.record?.title || n.path || n.label)}</span><small>${esc(n.path || "")}</small></button>`;
}).join("")}</div>` : `<p class="note">${esc(t("insp.none"))}</p>`;
const stats = (pairs) => `<div class="stats">${pairs.map(([k, v]) => `<div class="stat"><small>${esc(t(k))}</small><b>${nf(v)}</b></div>`).join("")}</div>`;
const colorName = (hex) => t("pal." + PALETTE_KEYS[PALETTE.indexOf(hex)]);

// a [[path]] inside a body: a button to the record if it exists, else a dimmed name
function wikiLink(target, label) {
  const path = target.trim();
  return m.T.has(path) ? `<button class="wl" data-go="${esc(path)}" style="color:${colorOf(path)}">${esc(label)}</button>`
    : `<span class="wl dangling" title="${esc(t("insp.dangling"))}">${esc(label)}</span>`;
}

let inspectSeq = 0;
async function inspect() {
  const box = $("#inspector", m.root);
  if (!m.graph) return;
  if (m.query) renderResults();          // keep the open result marked in the list
  const n = m.selected === null ? null : m.T.get(m.selected);
  const sc = spaceColor(m.space);
  if (!n || n.id === "") {
    const hubs = [...m.deg.entries()].sort((a, b) => b[1] - a[1]).slice(0, 8).map(([id]) => id);
    box.innerHTML = `<div class="eyebrow">${hexIcon(sc, false, 13)}${esc(t("insp.space"))}</div><h2 class="mono">${esc(m.space.name)}</h2>
      ${m.space.description ? `<p class="note" style="color:var(--text)">${esc(m.space.description)}</p>` : ""}
      ${stats([["insp.records", m.graph.total], ["insp.links", m.links.length]])}
      <div><p class="h3">${esc(t("insp.color"))}</p><div class="colorline"><span class="txt">${hexIcon(sc, false, 14)} ${t("color.spaceAuto", { name: esc(colorName(sc)) })}</span></div></div>
      <div><p class="h3">${esc(t("insp.mostLinked"))}</p>${listOf(hubs)}</div>
      ${m.graph.truncated ? `<p class="note" style="color:var(--admin)">${esc(t("mem.truncatedNote", { shown: nf(m.graph.records.length), total: nf(m.graph.total) }))}</p>` : ""}
      <div class="row">
        ${m.space.permission === "admin" ? `<a class="btn" href="/space?id=${encodeURIComponent(m.space.id)}">${esc(t("insp.members"))}${m.space.waiting ? ` <span class="badge">${nf(m.space.waiting)}</span>` : ""}</a>` : ""}
        <button class="btn quiet" data-sp="link">${esc(t("insp.copyLink"))}</button>
        ${m.space.mine || m.space.visiting ? "" : `<button class="btn quiet" data-sp="leave">${esc(t("sp.leave"))}</button>`}
      </div>
      <p class="note">${esc(t("insp.spaceNote"))}</p>`;
    return;
  }
  if (!n.record) {
    box.innerHTML = `${pathNav(n)}<div class="eyebrow">${hexIcon(colorOf(n.id), true, 13)}${esc(t("insp.bare"))}</div><h2 class="mono">${esc(n.path)}</h2>
      ${stats([["insp.below", count(n)], ["insp.direct", n.children.length]])}
      <div><p class="h3">${esc(t("insp.inside"))}</p>${listOf(n.children.slice(0, 12).map((k) => k.id))}${n.children.length > 12 ? `<p class="note">${esc(t("insp.andMore", { n: n.children.length - 12 }))}</p>` : ""}</div>
      <div class="row"><button class="btn quiet" data-copy="${esc(n.path)}">${esc(t("insp.copy"))}</button></div>`;
    return;
  }
  const c = colorOf(n.id), rec = n.record, seq = ++inspectSeq;
  const head = `${pathNav(n)}<div class="eyebrow">${hexIcon(c, false, 13)}${esc(t("insp.record"))}</div><h2>${esc(rec.title || n.path)}</h2>
    ${rec.tags.length ? `<div class="tags">${rec.tags.map((x) => `<span class="tag">${esc(x)}</span>`).join("")}</div>` : ""}
    <div class="updated">${t("insp.changed", { date: esc(fmtDate(rec.updated_at)), ago: esc(ago(rec.updated_at)) })}</div>`;
  box.innerHTML = head + `<p class="note">${esc(t("mem.loading"))}</p>`;
  let full;
  try {
    full = await get(`/spaces/${encodeURIComponent(m.space.id)}/records/${encodeURIComponent(rec.id)}`);
  } catch (e) {
    if (seq !== inspectSeq) return;
    box.innerHTML = head + `<p class="note">${esc(e instanceof ApiError && e.status === 404 ? t("mem.recordGone") : t("err.network"))}</p>`;
    return;
  }
  if (seq !== inspectSeq) return;
  const r = full.record, out = (full.links.out || []), inn = (full.links.in || []);
  const outIds = [...new Set(out.filter((l) => l.id && m.byRecord.has(l.id)).map((l) => m.byRecord.get(l.id)))];
  const inIds = [...new Set(inn.filter((l) => m.byRecord.has(l.id)).map((l) => m.byRecord.get(l.id)))];
  const dangling = out.filter((l) => !l.resolved && !l.scheme);
  box.innerHTML = head + `
    ${stats([["insp.links", outIds.length + inIds.length], ["insp.opened", r.usage?.gets ?? 0], ["insp.found", r.usage?.recalled ?? 0]])}
    <div class="body md">${renderMarkdown(r.body, { link: wikiLink })}</div>
    <div><p class="h3">${esc(t("insp.linksTo"))}</p>${listOf(outIds)}${dangling.length ? `<p class="note">${esc(t("insp.danglingList", { list: dangling.map((l) => l.target).join(", ") }))}</p>` : ""}</div>
    <div><p class="h3">${esc(t("insp.linkedFrom"))}</p>${listOf(inIds)}</div>
    <div><p class="h3">${esc(t("insp.color"))}</p><div class="colorline"><span class="txt">${hexIcon(c, false, 14)} ${t("color.auto", { name: esc(colorName(c)), root: esc(n.root) })}</span></div></div>
    <div><p class="h3">${esc(t("insp.history"))}</p><div class="hist" id="insp-hist"><p class="note">…</p></div></div>
    <div class="row">${n.path ? `<button class="btn quiet" data-copy="${esc(n.path)}">${esc(t("insp.copy"))}</button>` : ""}<button class="btn quiet" data-copy="${esc(r.id)}">${esc(t("insp.copyId"))}</button></div>`;
  let hist;
  try {
    hist = (await get(`/spaces/${encodeURIComponent(m.space.id)}/records/${encodeURIComponent(rec.id)}/history`)).history;
  } catch {
    hist = null;
  }
  const el = seq === inspectSeq && $("#insp-hist", box);
  if (!el) return;
  if (!hist) { el.innerHTML = `<p class="note">${esc(t("err.network"))}</p>`; return; }
  const shown = hist.slice(0, 8);
  el.innerHTML = shown.map((h) => `<div class="hrow"><span class="op">${esc(t("op." + h.op) === "op." + h.op ? h.op : t("op." + h.op))}</span>
      ${h.author_id ? `<a class="person" href="/people?id=${encodeURIComponent(h.author_id)}">${esc(h.author || t("people.unnamed"))}</a>` : `<span class="note">${esc(t("insp.noAuthor"))}</span>`}
      <time class="note" datetime="${esc(h.at)}" title="${esc(fmtDate(h.at))}">${esc(ago(h.at))}</time>
      ${h.reason ? `<small>${esc(h.reason)}</small>` : ""}</div>`).join("")
    + (hist.length > shown.length ? `<p class="note">${esc(t("insp.andMore", { n: hist.length - shown.length }))}</p>` : "");
}

async function onInspectorAction(e) {
  const b = e.target.closest("[data-sp]");
  if (!b || !m.space) return;
  const space = m.space;
  if (b.dataset.sp === "link") {
    const link = `${location.origin}/memory?space=${encodeURIComponent(space.id)}`;
    (navigator.clipboard?.writeText(link) || Promise.reject()).then(() => toast(t("insp.linkCopied")), () => toast(link));
    return;
  }
  if (b.dataset.sp === "leave" && m.session?.user && confirm(t("sp.leaveConfirm", { space: space.name }))) {
    try {
      const { del } = await import("./api.js");
      await del(`/spaces/${encodeURIComponent(space.id)}/members/${encodeURIComponent(m.session.user.id)}`);
      toast(t("sp.left", { space: space.name }));
      store.set("memgres.space", "");
      m.loaded = false; m.space = null; m.graph = null;
      m.navigate("/memory", { replace: true });
    } catch (ex) {
      toast(ex instanceof ApiError && ex.detail ? String(ex.detail) : t("err.save"));
    }
  }
}

// The panel: boot, routing, the sidebar. Each area renders itself from its module.

import { get, del, setCsrf, ApiError } from "./api.js";
import { applyStatic, setLanguage, t } from "./i18n.js";
import { showGate, hideGate } from "./gate.js";
import { renderAccount } from "./account.js";
import { $, $$, esc, store, toast } from "./ui.js";

const state = {
  session: null,
  options: { providers: [], admin_redirect: false, locales: ["en"] },
  modules: {},          // areas added by later modules register here (memory, tokens, admin)
};
window.memgresPanel = state;

// ─── routing ────────────────────────────────────────────────────────────────
function areaOf(path) {
  if (path.startsWith("/account")) return "account";
  if (path.startsWith("/admin")) return "admin";
  return "memory";
}

export function navigate(path, { replace = false } = {}) {
  if (replace) history.replaceState(null, "", path); else history.pushState(null, "", path);
  render();
}

document.addEventListener("click", (e) => {
  const a = e.target.closest("a[href]");
  if (!a || a.target || e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
  const url = new URL(a.href, location.href);
  if (url.origin !== location.origin || url.pathname.startsWith("/ui/")) return;
  e.preventDefault();
  $("#shell").classList.remove("drawer");
  navigate(url.pathname + url.search);
});
window.addEventListener("popstate", () => render());

async function render() {
  const path = location.pathname;
  const s = state.session;
  if (!s) {
    if (!path.startsWith("/signin")) return navigate("/signin", { replace: true });
    if (path === "/signin" && state.options.admin_redirect) return navigate("/signin/admin?from=signin", { replace: true });
    showGate({ path, options: state.options, onSignedIn });
    return;
  }
  if (path.startsWith("/signin")) return navigate(s.can.memory ? "/memory" : "/admin", { replace: true });

  hideGate();
  $("#shell").hidden = false;
  let area = areaOf(path);
  if (area === "admin" && !s.can.admin) return navigate("/memory", { replace: true });
  if (area === "memory" && !s.can.memory) return navigate(s.can.admin ? "/admin" : "/account", { replace: true });

  renderSidebar(area);
  for (const id of ["memory", "account", "admin"]) $("#area-" + id).hidden = id !== area;
  const root = $("#area-" + area);
  const ctx = { session: s, path, navigate, onLanguage, refreshSession, pane: path === "/account/tokens" ? "tokens" : "profile",
    renderTokens: state.modules.tokens };
  if (area === "account") renderAccount(root, ctx);
  else if (state.modules[area]) state.modules[area](root, ctx);
  else root.innerHTML = `<div class="page-in"><p class="note">${esc(t("app.notYet"))}</p></div>`;
}

// ─── sidebar ────────────────────────────────────────────────────────────────
function renderSidebar(area) {
  const s = state.session;
  for (const a of $$(".navbtn")) a.setAttribute("aria-current", String(a.dataset.area === area));
  $('.navbtn[data-area="admin"]').hidden = !s.can.admin;
  $('.navbtn[data-area="memory"]').hidden = !s.can.memory;
  $("#spaces-sec").hidden = !s.can.memory;
  const me = s.user;
  const name = me ? (me.full_name || me.name || me.email || "") : t("me.rootName");
  $("#me").textContent = initials(name);
  $("#me-name").textContent = name;
  $("#me-role").textContent = t("role." + s.role);
}

function initials(name) {
  const parts = String(name).trim().split(/\s+/).filter(Boolean);
  const letters = parts.length > 1 ? parts[0][0] + parts[1][0] : (parts[0] || "?").slice(0, 2);
  return letters.toUpperCase();
}

if (store.get("memgres.sidebar") === "collapsed") $("#shell").classList.add("collapsed");
$("#collapse").onclick = () => {
  const shell = $("#shell");
  shell.classList.toggle("collapsed");
  store.set("memgres.sidebar", shell.classList.contains("collapsed") ? "collapsed" : "open");
};
$("#drawer-open").onclick = () => $("#shell").classList.add("drawer");
$("#scrim").onclick = () => $("#shell").classList.remove("drawer");

// ─── account menu ───────────────────────────────────────────────────────────
let menu = null;
function closeMenu() { if (menu) { menu.remove(); menu = null; $("#me").setAttribute("aria-expanded", "false"); } }
$("#me").addEventListener("click", (e) => {
  e.stopPropagation();
  if (menu) return closeMenu();
  const s = state.session;
  menu = document.createElement("div");
  menu.className = "menu";
  menu.setAttribute("role", "menu");
  menu.innerHTML = `<div class="who"><b>${esc(s.user ? (s.user.full_name || s.user.name) : t("me.rootName"))}</b><small>${esc(s.user?.email || t("role." + s.role))}</small></div>
    <a role="menuitem" href="/account">${esc(t("me.profile"))}</a>
    ${s.can.tokens ? `<a role="menuitem" href="/account/tokens">${esc(t("me.tokens"))}</a>` : ""}
    <button role="menuitem" data-signout>${esc(t("me.signout"))}</button>`;
  $("#me").parentElement.append(menu);
  $("#me").setAttribute("aria-expanded", "true");
  menu.addEventListener("click", async (ev) => {
    if (ev.target.closest("a")) { closeMenu(); return; }
    if (!ev.target.closest("[data-signout]")) return;
    closeMenu();
    try { await del("/session"); } catch { /* already gone */ }
    state.session = null;
    setCsrf(null);
    navigate("/signin");
  });
});
document.addEventListener("click", (e) => { if (menu && !e.target.closest(".menu")) closeMenu(); });

// ─── session & language ─────────────────────────────────────────────────────
async function refreshSession() {
  try {
    state.session = await get("/session");
    setCsrf(state.session.csrf);
  } catch (e) {
    if (!(e instanceof ApiError && e.status === 401)) throw e;
    state.session = null;
    setCsrf(null);
  }
  return state.session;
}

async function onSignedIn(session) {
  state.session = session;
  setCsrf(session.csrf);
  await applyLanguage();
  navigate(session.can.memory ? "/memory" : "/admin", { replace: true });
}

async function onLanguage(session) {
  if (session) { state.session = session; setCsrf(session.csrf); }
  if (!session?.user?.ui_language) store.set("memgres.lang", "auto");
  await applyLanguage();
  render();
}

async function applyLanguage() {
  const locales = state.session?.locales || state.options.locales || ["en"];
  const pref = state.session?.user ? (state.session.user.ui_language || "auto") : (store.get("memgres.lang") || "auto");
  await setLanguage(pref, locales);
  applyStatic();
}
state.applyLanguage = applyLanguage;
state.render = render;

$("#gate-lang").addEventListener("change", async (e) => {
  store.set("memgres.lang", e.target.value);
  await applyLanguage();
  render();
});

// ─── boot ───────────────────────────────────────────────────────────────────
async function boot() {
  try {
    state.options = await get("/signin-options");
  } catch { /* defaults */ }
  await refreshSession().catch(() => toast("…"));
  await applyLanguage();
  const { fillLanguageSelect } = await import("./i18n.js");
  await fillLanguageSelect($("#gate-lang"), store.get("memgres.lang") || "auto");
  // later areas load themselves; a missing one leaves the shell usable
  await Promise.all([
    import("./tokens.js").then((m) => { state.modules.tokens = m.renderTokens; }).catch(() => {}),
    import("./memory.js").then((m) => { state.modules.memory = m.renderMemory; }).catch(() => {}),
  ]);
  render();
}
boot();

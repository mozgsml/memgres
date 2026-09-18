// /signin (providers) and /signin/admin (a token, for administrators).

import { post, ApiError } from "./api.js";
import { applyStatic, t } from "./i18n.js";
import { $, esc, hexPath, PALETTE, reducedMotion, rnd } from "./ui.js";

let raf = null;

export function showGate({ path, options, onSignedIn }) {
  $("#shell").hidden = true;
  $("#gate").hidden = false;
  const admin = path.startsWith("/signin/admin");
  $("#door-main").hidden = admin;
  $("#door-admin").hidden = !admin;
  if (admin) {
    const redirected = new URLSearchParams(location.search).get("from") === "signin" || options.admin_redirect;
    $("#redirected").hidden = !redirected;
    $("#admin-back").hidden = redirected;
    $("#admin-err").hidden = true;
    const form = $("#door-admin");
    form.onsubmit = async (e) => {
      e.preventDefault();
      const input = $("#admin-token"), err = $("#admin-err"), btn = $("#admin-go");
      const token = input.value.trim();
      if (!token) { err.textContent = t("gate.admin.empty"); err.hidden = false; return; }
      btn.disabled = true;
      try {
        const session = await post("/session/token", { token });
        input.value = "";
        stopArt();
        onSignedIn(session);
      } catch (ex) {
        err.textContent = ex instanceof ApiError && ex.status === 429 ? t("gate.admin.throttled")
          : ex instanceof ApiError && ex.status === 403 ? t("gate.admin.refused") : t("err.network");
        err.hidden = false;
      } finally {
        btn.disabled = false;
      }
    };
    setTimeout(() => $("#admin-token").focus(), 0);
  } else {
    renderProviders(options.providers || []);
    const result = new URLSearchParams(location.search).get("auth");
    const note = $("#auth-note");
    note.hidden = !result;
    if (result) {
      note.className = "auth-note " + (result === "pending" ? "wait" : "bad");
      note.textContent = t("auth." + result) === "auth." + result ? t("auth.failed") : t("auth." + result);
    }
  }
  applyStatic($("#gate"));
  startArt();
}

function renderProviders(providers) {
  const box = $("#providers");
  if (!providers.length) {
    box.innerHTML = `<div class="empty-prov">${esc(t("gate.noProviders"))}</div>`;
    return;
  }
  box.innerHTML = providers.map((p) => `<a class="prov-btn" href="/ui/auth/${encodeURIComponent(p.id)}/start">
      <span class="prov">${esc((p.label || p.id).slice(0, 3))}</span>
      <span><b>${esc(t("gate.continueWith", { name: p.label || p.id }))}</b>${p.hint ? `<small>${esc(p.hint)}</small>` : ""}</span></a>`).join("");
}

export function hideGate() {
  stopArt();
  $("#gate").hidden = true;
}

// ─── the picture on the left: a memory you can pull about ──────────────────
// A small tree like a real space — a root, a handful of branches, records under
// them, a few links across — laid out by d3-force. Any hexagon but the root can be
// dragged, and its neighbours follow on their links. The layout is settled before
// the first frame, so it never opens as a tangle.
let art = null;

function buildGraph() {
  const nodes = [{ id: 0, r: 34, c: "#62e38d", depth: 0, fx: 0, fy: 0 }];
  const links = [];
  const add = (parent, depth, r, c, bare) => {
    const n = { id: nodes.length, r, c, depth, bare, ph: rnd("ph" + nodes.length) * 6.28 };
    nodes.push(n);
    links.push({ source: parent.id, target: n.id, depth });
    return n;
  };
  const hubs = 9;
  for (let h = 0; h < hubs; h++) {
    const c = PALETTE[h % PALETTE.length];
    const hub = add(nodes[0], 1, 13 + rnd("hr" + h) * 7, c, h % 4 === 3);
    const kids = 4 + Math.floor(rnd("hk" + h) * 5);
    for (let k = 0; k < kids; k++) {
      const key = `${h}.${k}`;
      const child = add(hub, 2, 6 + rnd("cr" + key) ** 2 * 8, c, rnd("cb" + key) < 0.14);
      if (rnd("cg" + key) < 0.32) {
        const grand = 1 + Math.floor(rnd("gn" + key) * 3);
        for (let g = 0; g < grand; g++) add(child, 3, 4.5 + rnd("gr" + key + g) * 3.5, c, false);
      }
    }
  }
  // a few links across branches, the way records cite each other
  const leaves = nodes.filter((n) => n.depth >= 2);
  for (let x = 0; x < 12; x++) {
    const a = leaves[Math.floor(rnd("xa" + x) * leaves.length)], b = leaves[Math.floor(rnd("xb" + x) * leaves.length)];
    if (a !== b && a.c !== b.c) links.push({ source: a.id, target: b.id, depth: 9, cross: true });
  }
  return { nodes, links };
}

function startArt() {
  stopArt();
  const d3 = window.d3, cv = $("#gate-canvas");
  if (!d3 || !cv) return;
  const ctx = cv.getContext("2d"), still = reducedMotion();
  const { nodes, links } = buildGraph();
  // The layout lives in a fixed circle and is scaled to whatever room the page
  // gives it, so a phone shows the same picture smaller rather than a cropped one.
  const bound = 260;
  const contain = () => {
    for (const n of nodes) {
      if (n.fx != null && n.depth === 0) continue;
      const d = Math.hypot(n.x, n.y), lim = bound - n.r;
      if (d > lim) { n.x *= lim / d; n.y *= lim / d; n.vx *= 0.5; n.vy *= 0.5; }
    }
  };
  const sim = d3.forceSimulation(nodes)
    .force("link", d3.forceLink(links).id((n) => n.id)
      .distance((l) => l.cross ? 150 : l.depth === 1 ? 118 : l.depth === 2 ? 40 : 24)
      .strength((l) => l.cross ? 0.02 : 0.7))
    .force("charge", d3.forceManyBody().strength((n) => n.depth === 0 ? -400 : -26 - n.r * 3).distanceMax(240))
    .force("collide", d3.forceCollide((n) => n.r + 4).iterations(2))
    .force("contain", contain)
    .stop();
  nodes.forEach((n, i) => { if (i) { const a = rnd("ia" + i) * 6.28, d = 60 + rnd("id" + i) * 160; n.x = Math.cos(a) * d; n.y = Math.sin(a) * d; } });
  for (let i = 0; i < 320; i++) sim.tick();
  sim.alphaTarget(0).alpha(0);
  sim.on("tick", () => {});                         // ticks run on d3's own timer only while dragging

  const view = { cx: 0, cy: 0, dpr: 1, k: 1 };
  const toGraph = (e) => {
    const r = cv.getBoundingClientRect();
    return [(e.clientX - r.left - view.cx) / view.k, (e.clientY - r.top - view.cy) / view.k];
  };
  const hit = ([x, y]) => {
    let best = null, bestD = Infinity;
    for (const n of nodes) {
      if (n.depth === 0) continue;
      const d = Math.hypot(n.x - x, n.y - y);
      if (d < n.r + 8 / view.k && d < bestD) { best = n; bestD = d; }
    }
    return best;
  };
  let dragged = null;
  const onDown = (e) => {
    const n = hit(toGraph(e));
    if (!n) return;
    dragged = n;
    n.fx = n.x; n.fy = n.y;
    cv.setPointerCapture(e.pointerId);
    cv.style.cursor = "grabbing";
    sim.alphaTarget(0.25).restart();
    e.preventDefault();
  };
  const onMove = (e) => {
    const p = toGraph(e);
    if (!dragged) { cv.style.cursor = hit(p) ? "grab" : ""; return; }
    const d = Math.hypot(p[0], p[1]), lim = bound - dragged.r;
    const k = d > lim ? lim / d : 1;
    dragged.fx = p[0] * k; dragged.fy = p[1] * k;
  };
  const onUp = (e) => {
    if (!dragged) return;
    dragged.fx = dragged.fy = null;
    dragged = null;
    cv.style.cursor = hit(toGraph(e)) ? "grab" : "";
    sim.alphaTarget(0);
  };
  cv.addEventListener("pointerdown", onDown);
  cv.addEventListener("pointermove", onMove);
  cv.addEventListener("pointerup", onUp);
  cv.addEventListener("pointercancel", onUp);

  const hex = (x, y, R, draw) => { ctx.save(); ctx.translate(x, y); draw(new Path2D(hexPath(R))); ctx.restore(); };
  const frame = (time) => {
    const dpr = view.dpr = Math.min(2, devicePixelRatio || 1);
    const cw = cv.clientWidth, ch = cv.clientHeight;
    if (cv.width !== cw * dpr || cv.height !== ch * dpr) { cv.width = cw * dpr; cv.height = ch * dpr; }
    view.cx = cw / 2; view.cy = ch * 0.42;
    view.k = Math.min(1.15, Math.min(cw * 0.47, ch * 0.41) / bound);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cw, ch);
    ctx.translate(view.cx, view.cy);
    ctx.scale(view.k, view.k);
    // a gentle drift, drawn only — the layout itself stays where it settled
    const pos = (n) => (still || n === dragged || n.depth === 0) ? [n.x, n.y]
      : [n.x + Math.sin(time / 2600 + n.ph) * 2.2, n.y + Math.cos(time / 3100 + n.ph) * 2.2];
    for (const l of links) {
      const [x1, y1] = pos(l.source), [x2, y2] = pos(l.target);
      ctx.globalAlpha = l.cross ? 0.22 : 0.4;
      ctx.strokeStyle = l.cross ? "#aeb6d6" : l.target.c;
      ctx.lineWidth = l.cross ? 1 : 1.3;
      ctx.setLineDash(l.cross ? [3, 6] : []);
      ctx.beginPath(); ctx.moveTo(x1, y1);
      ctx.quadraticCurveTo((x1 + x2) / 2 + (y2 - y1) * 0.12, (y1 + y2) / 2 - (x2 - x1) * 0.12, x2, y2);
      ctx.stroke();
    }
    ctx.setLineDash([]);
    for (const n of nodes) {
      if (n.depth === 0) continue;
      const [x, y] = pos(n);
      hex(x, y, n.r, (path) => {
        ctx.strokeStyle = n.c; ctx.fillStyle = n.c; ctx.lineWidth = n === dragged ? 2.6 : 1.8;
        ctx.setLineDash(n.bare ? [3, 4] : []);
        ctx.globalAlpha = n.bare ? 0.04 : n === dragged ? 0.3 : 0.15; ctx.fill(path);
        ctx.globalAlpha = 1; ctx.stroke(path);
      });
    }
    ctx.setLineDash([]);
    ctx.globalAlpha = 1;
    hex(0, 0, nodes[0].r, (p) => { ctx.fillStyle = "#121829"; ctx.strokeStyle = "#62e38d"; ctx.lineWidth = 2.4; ctx.fill(p); ctx.stroke(p); });
    raf = requestAnimationFrame(frame);
  };
  raf = requestAnimationFrame(frame);
  art = { sim, cv, off: () => {
    cv.removeEventListener("pointerdown", onDown);
    cv.removeEventListener("pointermove", onMove);
    cv.removeEventListener("pointerup", onUp);
    cv.removeEventListener("pointercancel", onUp);
    cv.style.cursor = "";
  } };
}

function stopArt() {
  if (raf) cancelAnimationFrame(raf);
  raf = null;
  if (art) { art.sim.stop(); art.off(); art = null; }
}

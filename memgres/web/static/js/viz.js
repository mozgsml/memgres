// Visualizers: how a space is drawn. Built-in "graph" and "tree"; anyone can
// register another with memgres.registerVisualizer({ id, nameKey|name, mount }).
//
// mount(host, api) returns { update(reason), focus(id)?, fit()?, zoomBy(f)?, destroy() }
//   reason: "data" (what is shown changed) | "focus" (selection/search) | "relabel" (language)
// api: node(id) root() links() degree(id) visibleIds() isContext(id) colorOf(id)
//      neighbours(id) matches(node) count(node) selected query local space t select(id) tip(id,e) hideTip()

import { esc, hexIcon, hexPath, reducedMotion, rnd } from "./ui.js";

export const VISUALIZERS = new Map();
export function registerVisualizer(v) { VISUALIZERS.set(v.id, v); }
window.memgres = Object.assign(window.memgres || {}, { registerVisualizer });

const NS = "http://www.w3.org/2000/svg";
const S = (tag, attrs = {}) => { const e = document.createElementNS(NS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); return e; };

// a label inside a hexagon: one line if it fits, else split after "-", "_" or ".", else "…"
function labelLines(label, perLine) {
  if (label.length <= perLine) return [label];
  let cut = -1;
  for (let i = 1; i < label.length - 1; i++) if ("-_.".includes(label[i]) && i + 1 <= perLine) cut = i + 1;
  if (cut > 0) {
    const rest = label.slice(cut);
    return [label.slice(0, cut), rest.length > perLine ? rest.slice(0, perLine - 1) + "…" : rest];
  }
  return [label.slice(0, perLine - 1) + "…"];
}

// ─── Graph ──────────────────────────────────────────────────────────────────
registerVisualizer({
  id: "graph",
  nameKey: "viz.graph",
  mount(host, api) {
    const d3 = window.d3;
    if (!d3) {
      host.innerHTML = `<p class="viz-empty">${esc(api.t("viz.noD3"))}</p>`;
      return { update() {}, destroy() { host.replaceChildren(); } };
    }
    const reduced = reducedMotion();
    const svg = S("svg", { class: "graph", role: "group", "aria-label": api.t("viz.graph") });
    const vp = S("g"), gE = S("g"), gL = S("g"), gN = S("g");
    vp.append(gE, gL, gN); svg.append(vp); host.append(svg);
    let nodes = [], byId = new Map(), tree = [], wiki = [], sim = null;
    const zoom = d3.zoom().scaleExtent([0.08, 4]).on("zoom", (e) => vp.setAttribute("transform", e.transform));
    const sel = d3.select(svg).call(zoom).on("dblclick.zoom", null);
    svg.addEventListener("click", (e) => { if (e.target === svg) api.select(null); });

    const FS = 11, CHAR = FS * 0.56, MIN_R = 40;
    const radius = (id) => id === "" ? 68 : Math.min(64, MIN_R + Math.sqrt(api.degree(id)) * 6);
    const perLine = (r) => Math.max(8, Math.floor((r * 1.5 - 10) / CHAR));
    const curve = (a, b) => {
      const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2, dx = b.x - a.x, dy = b.y - a.y, bend = (rnd(a.id + b.id) - 0.5) * 0.3;
      return `M${a.x.toFixed(1)} ${a.y.toFixed(1)}Q${(mx - dy * bend).toFixed(1)} ${(my + dx * bend).toFixed(1)} ${b.x.toFixed(1)} ${b.y.toFixed(1)}`;
    };
    const drag = d3.drag().clickDistance(4)
      .on("start", (e, d) => { if (!e.active) sim.alphaTarget(0.2).restart(); d.fx = d.x; d.fy = d.y; api.hideTip(); })
      .on("drag", (e, d) => { d.fx = e.x; d.fy = e.y; })
      .on("end", (e, d) => { if (!e.active) sim.alphaTarget(0); if (d.id !== "") { d.fx = null; d.fy = null; } });

    function layout() {
      const ids = api.visibleIds(), prev = byId;
      byId = new Map(); nodes = [];
      for (const id of ids) {
        const n = api.node(id), o = prev.get(id), d = { id, n, r: radius(id) };
        if (o) { d.x = o.x; d.y = o.y; } else {
          const p = prev.get(n.parent) || byId.get(n.parent), a = rnd(id) * Math.PI * 2, dist = 80 + rnd(id + "d") * 80;
          if (p) { d.x = p.x + Math.cos(a) * dist; d.y = p.y + Math.sin(a) * dist; }
          else { const R = 120 + (n.depth || 0) * 190; d.x = Math.cos(a) * R; d.y = Math.sin(a) * R; }
        }
        byId.set(id, d); nodes.push(d);
      }
      const hub = byId.get("");
      if (hub) { hub.fx = 0; hub.fy = 0; }
      tree = nodes.filter((d) => d.id !== "" && byId.has(d.n.parent)).map((d) => ({ source: d.n.parent, target: d.id }));
      wiki = api.links().filter((l) => byId.has(l.a) && byId.has(l.b)).map((l) => ({ source: l.a, target: l.b }));
      if (sim) sim.stop();
      sim = d3.forceSimulation(nodes)
        .force("tree", d3.forceLink(tree).id((d) => d.id).distance((e) => e.source.r + e.target.r + (e.source.id === "" ? 70 : 26)).strength(0.85))
        .force("wiki", d3.forceLink(wiki).id((d) => d.id).distance((e) => e.source.r + e.target.r + 140).strength(0.05))
        .force("charge", d3.forceManyBody().strength((d) => -60 - d.r * 10).distanceMax(900))
        .force("collide", d3.forceCollide((d) => d.r * 1.02 + 7).iterations(2))
        .force("x", d3.forceX(0).strength(0.03)).force("y", d3.forceY(0).strength(0.03))
        .stop();
      // settle before drawing: a big graph animating into place is a slideshow, not a map
      const steps = prev.size ? 180 : Math.min(340, 120 + Math.round(80000 / Math.max(nodes.length, 1)));
      for (let i = 0; i < steps; i++) sim.tick();
      sim.alpha(0).on("tick", position);
      render();
    }

    function render() {
      gE.replaceChildren(); gL.replaceChildren(); gN.replaceChildren();
      for (const e of tree) { e.el = S("path", { class: "edge" }); gE.append(e.el); }
      for (const e of wiki) { e.el = S("path", { class: "wiki" }); gL.append(e.el); }
      for (const d of [...nodes].sort((a, b) => (a.id === "") - (b.id === "") || a.r - b.r)) {
        const n = d.n, hub = d.id === "";
        const g = S("g", { class: "node", tabindex: "0", role: "button", "data-id": d.id,
          "aria-label": hub ? api.space.name : n.record ? (n.record.title || n.id) : n.id });
        d.halo = S("path", { class: "halo", d: hexPath(d.r + 6) });
        const bg = S("path", { class: "bg", d: hexPath(d.r) });
        d.ring = S("path", { class: "ring" + (hub ? " hub" : n.record ? "" : " bare"), d: hexPath(d.r) });
        g.append(d.halo, bg, d.ring);
        const lines = hub ? labelLines(api.space.name, 12) : labelLines(n.label, perLine(d.r));
        const fs = hub ? 15 : FS, lh = fs * 1.2, top = hub ? -9 : 0;
        d.texts = lines.map((ln, i) => {
          const tx = S("text", { "font-size": fs, "font-weight": hub ? 600 : 500, y: (top + (i - (lines.length - 1) / 2) * lh).toFixed(1) });
          tx.textContent = ln; g.append(tx); return tx;
        });
        if (hub) {
          const sub = S("text", { class: "sub", "font-size": 10.5, y: 16 });
          sub.textContent = api.t("mem.records", { n: api.count(n) });
          g.append(sub);
        }
        g.addEventListener("click", () => api.select(d.id));
        g.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); api.select(d.id); } });
        g.addEventListener("pointermove", (e) => api.tip(d.id, e));
        g.addEventListener("pointerleave", () => api.hideTip());
        d3.select(g).datum(d).call(drag);
        d.el = g; gN.append(g);
      }
      position(); paint();
    }

    function position() {
      for (const d of nodes) d.el.setAttribute("transform", `translate(${d.x.toFixed(1)} ${d.y.toFixed(1)})`);
      for (const e of tree) e.el.setAttribute("d", curve(e.source, e.target));
      for (const e of wiki) e.el.setAttribute("d", curve(e.source, e.target));
    }

    // the selection and its direct neighbours bright, its own branch half, the rest faint;
    // in the local view the way back to the root stays as faint context
    function paint() {
      const s = api.selected, near = s !== null && byId.has(s) ? api.neighbours(s) : null;
      const selRoot = s ? api.node(s)?.root : null, q = api.query;
      for (const d of nodes) {
        const c = api.colorOf(d.id);
        d.ring.setAttribute("stroke", c); d.ring.setAttribute("fill", c); d.halo.setAttribute("stroke", c);
        for (const tx of d.texts) tx.setAttribute("fill", c);
        let o = 1;
        if (q) o = d.id === "" || api.matches(d.n) ? 1 : 0.14;
        else if (near) o = near.has(d.id) ? 1 : api.isContext(d.id) ? 0.4 : d.n.root && d.n.root === selRoot ? 0.5 : 0.16;
        d.el.style.opacity = o;
        d.el.classList.toggle("sel", d.id === s);
      }
      for (const e of tree) {
        const a = e.source, b = e.target;
        e.el.setAttribute("stroke", api.colorOf(b.id));
        e.el.setAttribute("stroke-width", a.id === "" ? 2.2 : 1.5);
        let o = 0.5;
        if (q) o = api.matches(b.n) ? 0.5 : 0.06;
        else if (near) o = near.has(a.id) && near.has(b.id) ? 0.9 : api.isContext(a.id) ? 0.35 : b.n.root === selRoot ? 0.25 : 0.06;
        e.el.style.opacity = o;
        e.el.setAttribute("stroke-dasharray", api.isContext(a.id) && api.isContext(b.id) ? "4 5" : "");
      }
      for (const e of wiki) {
        let o = 0.28;
        if (q) o = 0.05; else if (near) o = e.source.id === s || e.target.id === s ? 0.9 : 0.05;
        e.el.style.opacity = o;
      }
    }

    function fit(animate = true) {
      if (!nodes.length) return;
      const w = host.clientWidth || 800, h = host.clientHeight || 600, top = api.local ? 92 : 52;
      let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
      for (const d of nodes) { x0 = Math.min(x0, d.x - d.r); y0 = Math.min(y0, d.y - d.r); x1 = Math.max(x1, d.x + d.r); y1 = Math.max(y1, d.y + d.r); }
      const k = Math.min(1.3, (w - 50) / (x1 - x0), (h - top - 50) / (y1 - y0));
      const tr = d3.zoomIdentity.translate(w / 2, top / 2 + h / 2).scale(k).translate(-(x0 + x1) / 2, -(y0 + y1) / 2);
      (animate && !reduced ? sel.transition().duration(450) : sel).call(zoom.transform, tr);
    }

    layout();
    let fitted = false;
    const ro = new ResizeObserver(() => { if (!fitted && host.clientWidth) { fitted = true; fit(false); } });
    ro.observe(host);
    return {
      update(reason) { if (reason === "data") { layout(); fit(); } else if (reason === "relabel") render(); else paint(); },
      focus(id) { const d = byId.get(id); if (d) (reduced ? sel : sel.transition().duration(450)).call(zoom.translateTo, d.x, d.y + 20); },
      fit,
      zoomBy: (f) => sel.transition().duration(200).call(zoom.scaleBy, f),
      destroy() { if (sim) sim.stop(); ro.disconnect(); host.replaceChildren(); },
    };
  },
});

// ─── Tree list ──────────────────────────────────────────────────────────────
registerVisualizer({
  id: "tree",
  nameKey: "viz.tree",
  mount(host, api) {
    const wrap = document.createElement("div");
    wrap.className = "treeviz";
    wrap.setAttribute("role", "tree");
    host.append(wrap);
    const open = new Set([""]);
    for (const r of api.root().children) open.add(r.id);
    const caret = `<svg width="10" height="10" viewBox="0 0 10 10"><path d="M3.5 2 7 5 3.5 8" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
    // a very large space: draw rows in pages instead of thousands at once
    const PAGE = 400;
    let limit = PAGE;

    function render() {
      wrap.classList.toggle("local", api.local);
      const vis = new Set(api.visibleIds()), show = new Set(), ctx = new Set();
      for (const id of vis) {
        if (api.query && !api.matches(api.node(id))) continue;
        let cur = api.node(id), first = true;
        while (cur) {
          if (!show.has(cur.id)) { show.add(cur.id); if (!first || api.isContext(cur.id)) ctx.add(cur.id); }
          else if (first) ctx.delete(cur.id);
          if (cur.id === "") break;
          cur = api.node(cur.parent); first = false;
        }
      }
      if (api.selected !== null) {
        let cur = api.node(api.node(api.selected)?.parent);
        while (cur) { open.add(cur.id); if (cur.id === "") break; cur = api.node(cur.parent); }
      }
      const rows = [];
      let more = false;
      const walk = (n, lvl) => {
        if (!show.has(n.id)) return;
        if (rows.length >= limit) { more = true; return; }
        if (n.id !== "") {
          const c = api.colorOf(n.id), kids = n.children.filter((k) => show.has(k.id)), isOpen = open.has(n.id) || !!api.query;
          const inner = api.count(n) - (n.record ? 1 : 0), deg = api.degree(n.id);
          rows.push(`<div class="trow${n.id === api.selected ? " sel" : ""}${ctx.has(n.id) ? " ctx" : ""}" style="--lvl:${lvl - 1};--c:${c}" role="treeitem" aria-level="${lvl}"${kids.length ? ` aria-expanded="${isOpen}"` : ""}>
            ${kids.length ? `<button class="caret" data-toggle="${esc(n.id)}" aria-expanded="${isOpen}" aria-label="${esc(n.id)}">${caret}</button>` : "<span></span>"}
            <button class="tmain" data-id="${esc(n.id)}">${hexIcon(c, !n.record, 14)}<span class="tseg">${esc(n.label)}</span><span class="tttl">${n.record ? esc(n.record.title) : ""}</span></button>
            <span class="tmeta">${inner ? `<span>${esc(api.t("mem.records", { n: inner }))}</span>` : ""}${deg ? `<span>↔ ${deg}</span>` : ""}</span></div>`);
          if (!isOpen) return;
        }
        for (const k of n.children) walk(k, lvl + 1);
      };
      walk(api.root(), 0);
      wrap.innerHTML = rows.join("") + (more ? `<button class="btn small tmore" data-more>${esc(api.t("viz.showMore"))}</button>` : "");
    }
    wrap.addEventListener("click", (e) => {
      if (e.target.closest("[data-more]")) { limit += PAGE; render(); return; }
      const tg = e.target.closest("[data-toggle]");
      if (tg) { const id = tg.dataset.toggle; open.has(id) ? open.delete(id) : open.add(id); render(); return; }
      const m = e.target.closest("[data-id]");
      if (m) api.select(m.dataset.id);
    });
    wrap.addEventListener("pointermove", (e) => { const m = e.target.closest("[data-id]"); if (m) api.tip(m.dataset.id, e); else api.hideTip(); });
    wrap.addEventListener("pointerleave", () => api.hideTip());
    render();
    return {
      update() { render(); },
      focus(id) { const el = wrap.querySelector(`[data-id="${CSS.escape(id)}"]`); if (el) el.scrollIntoView({ block: "center", behavior: reducedMotion() ? "auto" : "smooth" }); },
      destroy() { host.replaceChildren(); },
    };
  },
});

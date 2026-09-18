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

// ─── the picture on the left: a small constellation of hexagons ────────────
function startArt() {
  stopArt();
  const cv = $("#gate-canvas"), ctx = cv.getContext("2d"), dpr = Math.min(2, devicePixelRatio || 1);
  const still = reducedMotion();
  const pts = Array.from({ length: 30 }, (_, i) => {
    const a = rnd("a" + i) * Math.PI * 2, d = 0.22 + Math.sqrt(rnd("d" + i)) * 0.78;
    return { x: Math.cos(a) * d, y: Math.sin(a) * d, r: 8 + rnd("r" + i) ** 2 * 14,
      c: PALETTE[Math.floor(a / (Math.PI * 2) * 6) % 6], ph: rnd("p" + i) * 6.28, bare: i % 6 === 0 };
  });
  const edges = [];
  pts.forEach((p, i) => {
    const near = pts.map((q, j) => [j, (q.x - p.x) ** 2 + (q.y - p.y) ** 2]).filter(([j]) => j !== i).sort((a, b) => a[1] - b[1]);
    edges.push([i, near[0][0]]);
    if (i % 3 === 0) edges.push([i, near[1][0]]);
    if (Math.hypot(p.x, p.y) < 0.42) edges.push([i, -1]);
  });
  const frame = (time) => {
    const w = (cv.width = cv.clientWidth * dpr), h = (cv.height = cv.clientHeight * dpr);
    const cx = w / 2, cy = h * 0.4, s = Math.min(w * 0.42, h * 0.34);
    ctx.clearRect(0, 0, w, h);
    const at = (i) => i < 0 ? [cx, cy] : [
      cx + pts[i].x * s + Math.sin((still ? 0 : time / 2600) + pts[i].ph) * 5 * dpr,
      cy + pts[i].y * s + Math.cos((still ? 0 : time / 3100) + pts[i].ph) * 5 * dpr];
    ctx.lineWidth = 1.3 * dpr;
    for (const [a, b] of edges) {
      const [x1, y1] = at(a), [x2, y2] = at(b);
      ctx.globalAlpha = 0.35; ctx.strokeStyle = pts[a].c;
      ctx.beginPath(); ctx.moveTo(x1, y1);
      ctx.quadraticCurveTo((x1 + x2) / 2 + (y2 - y1) * 0.12, (y1 + y2) / 2 - (x2 - x1) * 0.12, x2, y2);
      ctx.stroke();
    }
    const hex = (x, y, R, draw) => { ctx.save(); ctx.translate(x, y); draw(new Path2D(hexPath(R))); ctx.restore(); };
    pts.forEach((q, i) => {
      const [x, y] = at(i);
      hex(x, y, q.r * dpr, (p) => {
        ctx.strokeStyle = q.c; ctx.fillStyle = q.c; ctx.lineWidth = 2 * dpr;
        ctx.setLineDash(q.bare ? [3 * dpr, 4 * dpr] : []);
        ctx.globalAlpha = q.bare ? 0.03 : 0.14; ctx.fill(p); ctx.globalAlpha = 1; ctx.stroke(p);
      });
    });
    ctx.setLineDash([]);
    hex(cx, cy, 36 * dpr, (p) => { ctx.fillStyle = "#121829"; ctx.strokeStyle = "#62e38d"; ctx.lineWidth = 2.4 * dpr; ctx.fill(p); ctx.stroke(p); });
    if (!still) raf = requestAnimationFrame(frame);
  };
  raf = requestAnimationFrame(frame);
}

function stopArt() {
  if (raf) cancelAnimationFrame(raf);
  raf = null;
}

// Small shared pieces: DOM helpers, escaping, the palette and hexagon marks, toasts.

export const $ = (s, el = document) => el.querySelector(s);
export const $$ = (s, el = document) => [...el.querySelectorAll(s)];

export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

export const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* private window */ } },
};

export const reducedMotion = () => matchMedia("(prefers-reduced-motion: reduce)").matches;

// ─── colors ────────────────────────────────────────────────────────────────
// Red is deliberately absent: it is kept for warnings.
export const PALETTE = ["#4fd6f2", "#62e38d", "#ffa24c", "#b9a4ff", "#ff7cc0", "#f1de6b", "#7da2ff", "#c4e36a"];
export const PALETTE_KEYS = ["sky", "green", "amber", "lavender", "pink", "butter", "periwinkle", "lime"];

export function fnv(s) {
  let h = 2166136261;
  for (const c of String(s)) { h ^= c.codePointAt(0); h = Math.imul(h, 16777619); }
  return h >>> 0;
}

// well-mixed [0,1) from a string — for layout jitter, not for colors
export function rnd(s) {
  let h = fnv(s);
  h ^= h >>> 16; h = Math.imul(h, 0x85ebca6b); h ^= h >>> 13; h = Math.imul(h, 0xc2b2ae35); h ^= h >>> 16;
  return (h >>> 0) / 4294967296;
}

// A color slot per name, from the name itself, so the same name looks the same
// for everyone. Two names on one slot: the later one (by sort order) moves on to
// the next free slot, deterministically.
export function autoSlots(names) {
  const out = {}, taken = new Set();
  for (const n of [...names].sort()) {
    let i = fnv(n) % PALETTE.length;
    for (let k = 0; k < PALETTE.length && taken.has(i); k++) i = (i + 1) % PALETTE.length;
    out[n] = i; taken.add(i);
  }
  return out;
}

// ─── hexagons ──────────────────────────────────────────────────────────────
export function hexPath(R) {
  const pts = [];
  for (let i = 0; i < 6; i++) { const a = -Math.PI / 2 + i * Math.PI / 3; pts.push([Math.cos(a) * R, Math.sin(a) * R]); }
  const k = 0.24; let d = "";
  for (let i = 0; i < 6; i++) {
    const p = pts[i], pr = pts[(i + 5) % 6], nx = pts[(i + 1) % 6];
    const a1 = [p[0] + (pr[0] - p[0]) * k, p[1] + (pr[1] - p[1]) * k];
    const a2 = [p[0] + (nx[0] - p[0]) * k, p[1] + (nx[1] - p[1]) * k];
    d += `${i ? "L" : "M"}${a1[0].toFixed(1)} ${a1[1].toFixed(1)}Q${p[0].toFixed(1)} ${p[1].toFixed(1)} ${a2[0].toFixed(1)} ${a2[1].toFixed(1)}`;
  }
  return d + "Z";
}

// solid outline for a record or a space, dashed for a path with no record of its own
export function hexIcon(color, bare = false, size = 14) {
  const c = /^#[0-9a-f]{6}$/i.test(color) ? color : "#707aa0";
  return `<svg class="hexi" width="${size}" height="${size}" viewBox="-8 -8 16 16" aria-hidden="true"><path d="${hexPath(6.4)}" fill="${c}" fill-opacity="${bare ? 0 : 0.2}" stroke="${c}" stroke-width="1.7"${bare ? ' stroke-dasharray="2.2 1.8"' : ""}/></svg>`;
}

// ─── toast ─────────────────────────────────────────────────────────────────
let toastTimer = null;
export function toast(message) {
  const el = $("#toast");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 3200);
}

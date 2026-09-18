// People: a person's profile, their write activity, and what an administrator can do to the account.

import { del, get, patch, post, ApiError } from "./api.js";
import { ago, fmtDate, nf, t } from "./i18n.js";
import { openTokenDialog } from "./tokens.js";
import { $, esc, hexIcon, PALETTE, fnv, toast } from "./ui.js";

export const personName = (p) => (p && (p.full_name || p.name || p.email)) || t("people.unnamed");

export const personLink = (id, name) => id
  ? `<a class="person" href="/people?id=${encodeURIComponent(id)}">${esc(name)}</a>`
  : `<span class="person">${esc(name)}</span>`;

// ─── activity ────────────────────────────────────────────────────────────────
// A week-by-week grid of the days in the window, Monday on top. Colour steps are
// relative to this person's own busiest day, so a quiet person's chart still
// shows its shape.
export function activityCard(act, { note } = {}) {
  const byDay = new Map(act.days.map((d) => [d.day, d.n]));
  const today = new Date(act.today + "T00:00:00Z");
  const weeks = Math.ceil(act.window_days / 7);
  const dow = (today.getUTCDay() + 6) % 7;                       // 0 = Monday
  const start = new Date(today.getTime() - ((weeks - 1) * 7 + dow) * 86400000);
  const max = Math.max(1, ...act.days.map((d) => d.n));
  const level = (n) => (n === 0 ? 0 : Math.min(4, Math.ceil((n / max) * 4)));
  const C = 13, G = 3, top = 18, left = 26;
  let cells = "", months = "", lastMonth = -1, lastLabel = -9;
  const fmtMonth = new Intl.DateTimeFormat(document.documentElement.lang || "en", { month: "short" });
  for (let w = 0; w < weeks; w++) {
    for (let d = 0; d < 7; d++) {
      const day = new Date(start.getTime() + (w * 7 + d) * 86400000);
      if (day > today) continue;
      const key = day.toISOString().slice(0, 10), n = byDay.get(key) || 0;
      // a month is labelled at its first full week, and only with room for the word
      if (d === 0 && day.getUTCMonth() !== lastMonth && w < weeks - 2 && w - lastLabel >= 3) {
        lastMonth = day.getUTCMonth();
        lastLabel = w;
        months += `<text class="hm-lbl" x="${left + w * (C + G)}" y="10">${esc(fmtMonth.format(day))}</text>`;
      }
      cells += `<rect class="hm l${level(n)}" x="${left + w * (C + G)}" y="${top + d * (C + G)}" width="${C}" height="${C}" rx="2.5"><title>${esc(t("act.cell", { n, date: fmtDate(day) }))}</title></rect>`;
    }
  }
  const W = left + weeks * (C + G), H = top + 7 * (C + G);
  const dayLbl = [0, 2, 4].map((d) => `<text class="hm-lbl" x="0" y="${top + d * (C + G) + 10}">${esc(new Intl.DateTimeFormat(document.documentElement.lang || "en", { weekday: "short" }).format(new Date(Date.UTC(2024, 0, 1 + d))))}</text>`).join("");
  const spaceMax = Math.max(1, ...act.by_space.map((s) => s.n));
  const ops = Object.entries(act.by_op).sort((a, b) => b[1] - a[1]);
  return `<div class="card"><div class="card-h"><h3>${esc(t("act.title"))}</h3><span class="note">${esc(t("act.total", { n: act.total, weeks }))}</span></div>
    <div class="x"><svg class="heatmap" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(t("act.aria", { n: act.total, weeks }))}">${months}${dayLbl}${cells}</svg></div>
    <div class="hm-legend"><span>${esc(t("act.less"))}</span>${[0, 1, 2, 3, 4].map((l) => `<svg width="11" height="11" aria-hidden="true"><rect class="hm l${l}" width="11" height="11" rx="2.5"/></svg>`).join("")}<span>${esc(t("act.more"))}</span></div>
    ${act.total ? `<div class="act-split">
      <div><p class="h3">${esc(t("act.bySpace"))}</p><div class="bars">${act.by_space.map((s) => `<div class="bar"><span class="mono">${esc(s.name || "—")}</span><i style="width:${Math.max(3, Math.round((s.n / spaceMax) * 100))}%"></i><b>${nf(s.n)}</b></div>`).join("")}</div></div>
      <div><p class="h3">${esc(t("act.byOp"))}</p><div class="tags">${ops.map(([op, n]) => `<span class="tag">${esc(t("op." + op) === "op." + op ? op : t("op." + op))} · ${nf(n)}</span>`).join("")}</div></div>
    </div>` : `<p class="note">${esc(t("act.none"))}</p>`}
    <p class="note" style="margin-top:12px">${esc(note || t("act.note"))}</p>
  </div>`;
}

// What the person changed last — only in spaces the viewer can read (the server
// filters); each row opens the record where it is.
export function recentCard(recent, { note } = {}) {
  if (!recent) return "";
  return `<div class="card"><div class="card-h"><h3>${esc(t("recent.title"))}</h3></div>
    ${recent.length ? `<div class="recent">${recent.map((r) => {
      const c = PALETTE[fnv(r.space || "") % PALETTE.length];
      const opLabel = t("op." + r.op) === "op." + r.op ? r.op : t("op." + r.op);
      return `<a class="rrow" href="/memory?space=${encodeURIComponent(r.space_id)}&record=${encodeURIComponent(r.record_id)}">
        <span class="op">${esc(opLabel)}</span>
        <span class="rt"><b>${esc(r.title || r.path || "—")}</b><small>${hexIcon(c, false, 10)}${esc(r.space)}${r.path ? " · " + esc(r.path) : ""}</small></span>
        <time title="${esc(fmtDate(r.at))}">${esc(ago(r.at))}</time></a>`;
    }).join("")}</div>` : `<p class="note">${esc(t("recent.none"))}</p>`}
    <p class="note" style="margin-top:10px">${esc(note || t("recent.note"))}</p></div>`;
}

export function spacesCard(spaces, { title } = {}) {
  return `<div class="card"><div class="card-h"><h3>${esc(title || t("people.spaces"))}</h3><span class="note">${nf(spaces.length)}</span></div>
    ${spaces.length ? `<div class="x"><table><tbody>${spaces.map((s) => `<tr>
      <td class="strong"><a class="mono person" href="/memory?space=${encodeURIComponent(s.id)}">${esc(s.name)}</a></td>
      <td>${s.permission ? `<span class="perm ${esc(s.permission)}">${esc(t("perm." + s.permission))}</span>` : ""}</td>
      <td class="note">${s.mine ? esc(t("people.owner")) : ""}</td></tr>`).join("")}</tbody></table></div>`
      : `<p class="note">${esc(t("people.noSpaces"))}</p>`}
  </div>`;
}

// ─── the page ────────────────────────────────────────────────────────────────
export async function renderPerson(root, ctx) {
  const id = new URLSearchParams(location.search).get("id") || "";
  if (ctx.session.user && id === ctx.session.user.id) return ctx.navigate("/account", { replace: true });
  root.innerHTML = `<div class="page-in"><p class="note">${esc(t("mem.loading"))}</p></div>`;
  let data;
  try {
    data = await get(`/people/${encodeURIComponent(id)}`);
  } catch (e) {
    root.innerHTML = `<div class="page-in"><div class="page-head"><h1>${esc(t("people.title"))}</h1></div>
      <div class="card"><p class="note">${esc(e instanceof ApiError && e.status === 404 ? t("people.notFound") : t("err.network"))}</p></div></div>`;
    return;
  }
  draw(root, ctx, data);
}

function draw(root, ctx, data) {
  root._wired?.abort();
  const p = data.person, admin = data.view === "admin";
  const sub = [p.department, p.position].filter(Boolean).join(" · ");
  root.innerHTML = `<div class="page-in">
    ${admin ? `<div class="admin-band"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><path d="M12 3 19.5 6v5.5c0 4.6-3.2 8.2-7.5 9.5-4.3-1.3-7.5-4.9-7.5-9.5V6z"/></svg><span>${esc(t("people.adminBand"))}</span></div>` : ""}
    <div class="person-head">
      <span class="avatar" aria-hidden="true">${esc(initials(personName(p)))}</span>
      <div class="page-head"><h1>${esc(personName(p))}</h1>
        <p>${esc(sub)}${sub && p.role ? " · " : ""}${p.role ? `<span class="perm ${p.role === "user" ? "read" : "admin"}">${esc(t("role." + p.role))}</span>` : ""}
        ${p.disabled ? ` <span class="dot off"><i></i>${esc(t("people.disabled"))}</span>` : ""}</p></div>
    </div>
    ${admin ? detailsCard(p) : ""}
    ${activityCard(data.activity, { note: data.view === "colleague" ? t("act.noteShared") : undefined })}
    ${recentCard(data.recent)}
    ${data.view === "colleague" ? spacesCard(data.shared_spaces, { title: t("people.shared") }) : spacesCard(data.spaces)}
    ${admin && data.signins ? signinsCard(data) : ""}
    ${admin && data.tokens ? tokensCard(data, ctx) : ""}
    ${admin ? actionsCard(ctx, data) : ""}
  </div>`;
  if (admin) wire(root, ctx, data);
}

const initials = (name) => {
  const parts = String(name).trim().split(/\s+/).filter(Boolean);
  return (parts.length > 1 ? parts[0][0] + parts[1][0] : (parts[0] || "?").slice(0, 2)).toUpperCase();
};

function detailsCard(p) {
  return `<div class="card"><div class="card-h"><h3>${esc(t("people.details"))}</h3>
      <button class="btn small" data-act="edit">${esc(t("people.edit"))}</button></div>
    <dl class="kv">
      <dt>${esc(t("acc.email"))}</dt><dd>${esc(p.email || "—")}</dd>
      <dt>${esc(t("people.login"))}</dt><dd class="mono">${esc(p.name || "—")}</dd>
      <dt>${esc(t("people.department"))}</dt><dd>${esc(p.department || "—")}</dd>
      <dt>${esc(t("people.position"))}</dt><dd>${esc(p.position || "—")}</dd>
      <dt>${esc(t("people.since"))}</dt><dd>${esc(fmtDate(p.created_at))}</dd>
    </dl></div>`;
}

function signinsCard(data) {
  const can = data.can?.manage;
  return `<div class="card"><div class="card-h"><h3>${esc(t("signins.title"))}</h3></div>
    <div class="stack">${data.signins.length ? data.signins.map((m) => `<div class="item">
      <span class="prov">${esc(m.label.slice(0, 3))}</span>
      <div class="grow"><b>${esc(m.label)}</b><small>${esc(m.email || "")}</small></div>
      <span class="note">${esc(t("signins.linkedOn", { date: fmtDate(m.linked_at), used: m.last_login_at ? ago(m.last_login_at) : t("tok.never") }))}</span>
      ${can ? `<button class="btn danger small" data-act="unlink" data-id="${esc(m.id)}" data-label="${esc(m.label)}">${esc(t("signins.unlink"))}</button>` : ""}
    </div>`).join("") : `<p class="note">${esc(t("people.noSignins"))}</p>`}</div></div>`;
}

function tokensCard(data) {
  const can = data.can?.manage;
  const rows = data.tokens;
  return `<div class="card"><div class="card-h"><h3>${esc(t("people.tokens"))}</h3>
      ${can && !data.person.disabled ? `<button class="btn small" data-act="token">${esc(t("people.issueToken"))}</button>` : ""}</div>
    ${rows.length ? `<div class="x"><table><thead><tr>${["tok.device", "tok.access", "tok.space", "tok.lastUsed", "tok.expires", "tok.status"].map((k) => `<th>${esc(t(k))}</th>`).join("")}<th></th></tr></thead><tbody>
    ${rows.map((x) => `<tr>
      <td class="strong">${esc(x.label || t("tok.unlabelled"))}</td>
      <td><span class="perm ${esc(x.permission)}">${esc(t("perm." + x.permission))}</span></td>
      <td class="mono">${x.namespace ? esc(x.namespace) : `<span class="note">${esc(t("tok.allTheirSpaces"))}</span>`}</td>
      <td>${esc(x.last_used_at ? ago(x.last_used_at) : t("tok.never"))}</td>
      <td class="num">${x.expires_at ? esc(fmtDate(x.expires_at)) : "—"}</td>
      <td><span class="dot ${x.state === "active" ? "on" : "off"}"><i></i>${esc(t("tok.state." + x.state))}</span></td>
      <td style="text-align:right">${can && x.state === "active" ? `<button class="btn danger small" data-act="revoke" data-id="${esc(x.id)}" data-label="${esc(x.label || t("tok.unlabelled"))}">${esc(t("tok.revoke"))}</button>` : ""}</td>
    </tr>`).join("")}</tbody></table></div>` : `<p class="note">${esc(t("tok.none"))}</p>`}</div>`;
}

function actionsCard(ctx, data) {
  const p = data.person, can = data.can || {};
  const self = ctx.session.user?.id === p.id;
  if (!can.manage) return `<div class="card"><p class="note">${esc(t("people.adminOnlySuper"))}</p></div>`;
  return `<div class="card"><div class="card-h"><h3>${esc(t("people.access"))}</h3></div>
    <div class="stack">
      ${can.set_role ? `<div class="item"><div class="grow"><b>${esc(t("acc.role"))}</b><small>${esc(t("people.roleNote"))}</small></div>
        <select id="pp-role" class="sel">${["user", "user_manager", "superadmin"].map((r) => `<option value="${r}"${r === p.role ? " selected" : ""}>${esc(t("role." + r))}</option>`).join("")}</select></div>` : ""}
      <div class="item"><div class="grow"><b>${esc(t("people.canCreate"))}</b><small>${esc(t("people.canCreateNote"))}</small></div>
        <label class="switch"><input type="checkbox" id="pp-cancreate" ${p.can_create_namespace ? "checked" : ""}><span>${esc(t(p.can_create_namespace ? "people.allowed" : "people.notAllowed"))}</span></label></div>
      <div class="item"><div class="grow"><b>${esc(t("people.sessions", { n: data.sessions }))}</b><small>${esc(t("people.sessionsNote"))}</small></div>
        <button class="btn small" data-act="end" ${data.sessions ? "" : "disabled"}>${esc(t("people.endSessions"))}</button></div>
      <div class="item"><div class="grow"><b>${esc(t(p.disabled ? "people.isOff" : "people.isOn"))}</b><small>${esc(t("people.disableNote"))}</small></div>
        ${self ? "" : `<button class="btn small ${p.disabled ? "adminbtn" : "danger"}" data-act="disable">${esc(t(p.disabled ? "people.enable" : "people.disable"))}</button>`}</div>
    </div></div>`;
}

function wire(root, ctx, data) {
  // the page redraws into the same element: drop the previous drawing's listeners
  root._wired?.abort();
  root._wired = new AbortController();
  const once = { signal: root._wired.signal };
  const p = data.person;
  const reload = async () => draw(root, ctx, await get(`/people/${encodeURIComponent(p.id)}`));
  const act = async (fn, done) => {
    try {
      await fn();
      if (done) toast(done);
      await reload();
    } catch (ex) {
      toast(ex instanceof ApiError && ex.detail ? String(ex.detail) : t("err.save"));
    }
  };
  $("#pp-cancreate", root)?.addEventListener("change", (e) => {
    const allowed = e.target.checked;
    act(() => post(`/admin/people/${encodeURIComponent(p.id)}/can-create-spaces`, { allowed }),
      t(allowed ? "people.canCreateOn" : "people.canCreateOff", { name: personName(p) }));
  });
  $("#pp-role", root)?.addEventListener("change", (e) => {
    const role = e.target.value;
    if (!confirm(t("people.roleConfirm", { name: personName(p), role: t("role." + role) }))) { e.target.value = p.role; return; }
    act(() => post(`/admin/people/${encodeURIComponent(p.id)}/role`, { role }), t("people.roleDone", { role: t("role." + role) }));
  });
  root.addEventListener("click", (e) => {
    const b = e.target.closest("[data-act]");
    if (!b || b.disabled) return;
    const base = `/admin/people/${encodeURIComponent(p.id)}`;
    switch (b.dataset.act) {
      case "edit": return editDialog(p, reload);
      case "token":
        return openTokenDialog({ endpoint: `${base}/tokens`, spaces: data.spaces, mcpUrl: ctx.session.mcp_url,
          title: t("people.issueTokenFor", { name: personName(p) }), note: t("people.issueTokenNote"), onCreated: reload });
      case "end":
        if (confirm(t("people.endConfirm", { name: personName(p) }))) act(() => post(`${base}/sessions/end`), t("people.ended"));
        return;
      case "disable":
        if (p.disabled || confirm(t("people.disableConfirm", { name: personName(p) })))
          act(() => post(`${base}/disabled`, { disabled: !p.disabled }), t(p.disabled ? "people.enabled" : "people.disabled"));
        return;
      case "unlink":
        if (confirm(t("people.unlinkConfirm", { name: b.dataset.label, person: personName(p) })))
          act(() => del(`${base}/signins/${encodeURIComponent(b.dataset.id)}`), t("signins.unlinked", { name: b.dataset.label }));
        return;
      case "revoke":
        if (confirm(t("tok.revokeConfirm", { label: b.dataset.label })))
          act(() => del(`${base}/tokens/${encodeURIComponent(b.dataset.id)}`), t("tok.revokedToast", { label: b.dataset.label }));
    }
  }, once);
}

function editDialog(p, onSaved) {
  const veil = document.createElement("div");
  veil.className = "veil";
  const field = (k, label, type = "text") => `<div class="field"><label for="pe-${k}">${esc(label)}</label><input id="pe-${k}" type="${type}" maxlength="200" value="${esc(p[k] || "")}"></div>`;
  veil.innerHTML = `<form class="dialog" role="dialog" aria-modal="true" aria-labelledby="pe-title">
    <h3 id="pe-title">${esc(t("people.editTitle", { name: personName(p) }))}</h3>
    ${field("full_name", t("acc.name"))}
    ${field("email", t("acc.email"), "email")}
    <div class="two">${field("department", t("people.department"))}${field("position", t("people.position"))}</div>
    <p class="note">${esc(t("people.emailNote"))}</p>
    <p class="err" id="pe-err" role="alert" hidden></p>
    <div class="row" style="justify-content:flex-end"><button type="button" class="btn quiet" data-x>${esc(t("common.cancel"))}</button>
      <button type="submit" class="btn adminbtn">${esc(t("common.save"))}</button></div>
  </form>`;
  document.body.append(veil);
  const close = () => veil.remove();
  veil.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
  veil.addEventListener("click", (e) => { if (e.target === veil || e.target.closest("[data-x]")) close(); });
  $("#pe-full_name", veil).focus();
  $("form", veil).addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = {};
    for (const k of ["full_name", "email", "department", "position"]) {
      const v = $("#pe-" + k, veil).value.trim();
      if (v !== (p[k] || "")) body[k] = v;
    }
    try {
      if (Object.keys(body).length) await patch(`/admin/people/${encodeURIComponent(p.id)}`, body);
      close();
      toast(t("common.saved"));
      await onSaved();
    } catch (ex) {
      const err = $("#pe-err", veil);
      err.textContent = ex instanceof ApiError && ex.detail ? String(ex.detail) : t("err.save");
      err.hidden = false;
    }
  });
}

// ─── picking someone (the sign-in queue) ─────────────────────────────────────
export function pickPerson({ title, note, onPick }) {
  const veil = document.createElement("div");
  veil.className = "veil";
  veil.innerHTML = `<div class="dialog" role="dialog" aria-modal="true" aria-labelledby="pk-title">
    <h3 id="pk-title">${esc(title)}</h3>
    <div class="filter" style="max-width:none"><svg width="12" height="12" viewBox="0 0 16 16" aria-hidden="true"><circle cx="7" cy="7" r="4.6" fill="none" stroke="#707aa0" stroke-width="1.6"/><path d="m10.4 10.4 3.4 3.4" stroke="#707aa0" stroke-width="1.6" stroke-linecap="round"/></svg>
      <input id="pk-q" type="search" autocomplete="off" placeholder="${esc(t("people.searchPh"))}" aria-label="${esc(t("people.searchPh"))}"></div>
    <div class="stack" id="pk-list"></div>
    ${note ? `<p class="note">${esc(note)}</p>` : ""}
    <div class="row" style="justify-content:flex-end"><button type="button" class="btn quiet" data-x>${esc(t("common.cancel"))}</button></div>
  </div>`;
  document.body.append(veil);
  const close = () => veil.remove();
  veil.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
  veil.addEventListener("click", (e) => {
    if (e.target === veil || e.target.closest("[data-x]")) return close();
    const b = e.target.closest("[data-pick]");
    if (b) { close(); onPick(found.find((x) => x.id === b.dataset.pick)); }
  });
  let found = [], seq = 0, timer = null;
  const search = async () => {
    const my = ++seq, q = $("#pk-q", veil).value.trim();
    try {
      const got = await get(`/admin/people?limit=20&q=${encodeURIComponent(q)}`);
      if (my !== seq) return;
      found = got.people;
      $("#pk-list", veil).innerHTML = found.length ? found.map((x) => `<button class="item pick" data-pick="${esc(x.id)}" ${x.disabled ? "disabled" : ""}>
          <div class="grow"><b>${esc(personName(x))}</b><small>${esc([x.email, x.department].filter(Boolean).join(" · "))}</small></div>
          <span class="perm ${x.role === "user" ? "read" : "admin"}">${esc(t("role." + x.role))}</span></button>`).join("")
        : `<p class="note">${esc(t("people.noneFound"))}</p>`;
    } catch { if (my === seq) $("#pk-list", veil).innerHTML = `<p class="note">${esc(t("err.network"))}</p>`; }
  };
  $("#pk-q", veil).addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(search, 220); });
  $("#pk-q", veil).focus();
  search();
}

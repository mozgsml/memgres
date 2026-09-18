// Admin: people waiting to be let in, and the directory of everyone on this memory.

import { get, post, ApiError } from "./api.js";
import { ago, nf, t } from "./i18n.js";
import { personLink, personName, pickPerson } from "./people.js";
import { $, esc, toast } from "./ui.js";

export async function renderAdmin(root, ctx) {
  const pane = ctx.path.startsWith("/admin/people") ? "people" : "requests";
  const waiting = ctx.session.pending_requests;
  root.innerHTML = `<div class="page-in">
    <div class="admin-band"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><path d="M12 3 19.5 6v5.5c0 4.6-3.2 8.2-7.5 9.5-4.3-1.3-7.5-4.9-7.5-9.5V6z"/></svg><span>${esc(t("adm.band"))}</span></div>
    <div class="page-head"><h1>${esc(t("adm.title"))}</h1></div>
    <div class="subnav" role="tablist">
      <a role="tab" href="/admin" aria-selected="${pane === "requests"}">${esc(t("adm.tab.requests"))}${waiting ? ` <span class="badge">${nf(waiting)}</span>` : ""}</a>
      <a role="tab" href="/admin/people" aria-selected="${pane === "people"}">${esc(t("adm.tab.people"))}</a>
    </div>
    <div class="pane" id="adm-pane"></div>
  </div>`;
  const box = $("#adm-pane", root);
  if (pane === "people") return renderPeople(box);
  return renderRequests(box, ctx);
}

// ─── sign-ins waiting ────────────────────────────────────────────────────────
async function renderRequests(box, ctx) {
  box.innerHTML = `<div class="card"><div class="card-h"><h3>${esc(t("req.title"))}</h3></div><div class="stack" id="req-list"><p class="note">…</p></div></div>
    <p class="note">${esc(t("req.note"))}</p>`;
  const list = $("#req-list", box);
  let reqs;
  const load = async () => {
    try {
      reqs = (await get("/admin/signin-requests")).requests;
    } catch {
      list.innerHTML = `<p class="note">${esc(t("err.network"))}</p>`;
      return;
    }
    list.innerHTML = reqs.length ? reqs.map((r) => {
      const s = r.suggested;
      const who = s ? personName(s) : "";
      const adminTarget = s && s.role !== "user";
      const mayLink = s && !(adminTarget && ctx.session.role !== "superadmin");
      const invites = (r.invites || []).map((i) => t("req.invitedTo", { space: i.space, by: i.by || "—" })).join(" ");
      return `<div class="item">
        <span class="prov">${esc((r.provider_label || r.provider).slice(0, 3))}</span>
        <div class="grow"><b>${esc(r.name || r.email || t("req.unnamed"))}</b>
          <small>${esc([r.email, r.email_verified ? t("req.verified") : r.email ? t("req.unverified") : "", r.provider_label || r.provider, ago(r.created_at)].filter(Boolean).join(" · "))}</small></div>
        <div class="row">
          ${mayLink ? `<button class="btn adminbtn small" data-act="link" data-id="${esc(r.id)}">${esc(t("req.link", { name: who }))}</button>` : ""}
          <button class="btn small" data-act="other" data-id="${esc(r.id)}">${esc(t(s ? "req.linkOther" : "req.linkExisting"))}</button>
          <button class="btn small ${mayLink ? "" : "adminbtn"}" data-act="create" data-id="${esc(r.id)}">${esc(t("req.create"))}</button>
          <button class="btn danger small" data-act="reject" data-id="${esc(r.id)}">${esc(t("req.reject"))}</button>
        </div>
        <div class="why">${s ? (adminTarget ? esc(t(mayLink ? "req.whyAdmin" : "req.whyAdminSuper", { name: who })) : esc(t("req.whyMatch", { name: who }))) : esc(t("req.whyNew"))}
          ${s ? ` ${personLink(s.id, t("req.seeProfile", { name: who }))}` : ""}
          ${invites ? `<br><em>${esc(invites)}</em>` : ""}</div>
      </div>`;
    }).join("") : `<p class="note">${esc(t("req.none"))}</p>`;
  };
  await load();

  const decide = async (id, body, name) => {
    for (const x of list.querySelectorAll(`[data-id="${CSS.escape(id)}"]`)) x.disabled = true;
    try {
      await post(`/admin/signin-requests/${encodeURIComponent(id)}`, body);
      toast(t("req.done." + body.action, { name }));
      await ctx.refreshSession();
      await load();
      window.memgresPanel?.render?.();
    } catch (ex) {
      toast(ex instanceof ApiError && ex.detail ? String(ex.detail) : t("err.save"));
      await load();
    }
  };
  list.addEventListener("click", (e) => {
    const b = e.target.closest("[data-act]");
    if (!b) return;
    const r = reqs.find((x) => x.id === b.dataset.id);
    const name = r?.name || r?.email || "";
    if (b.dataset.act === "reject" && !confirm(t("req.rejectConfirm", { name }))) return;
    if (b.dataset.act === "other") {
      pickPerson({
        title: t("req.pickTitle", { name }),
        note: t(ctx.session.role === "superadmin" ? "req.pickNote" : "req.pickNoteMgr"),
        onPick: (p) => {
          if (confirm(t("req.pickConfirm", { name, account: personName(p) }))) decide(r.id, { action: "link", user_id: p.id }, name);
        },
      });
      return;
    }
    decide(b.dataset.id, { action: b.dataset.act }, name);
  });
}

// ─── the directory ───────────────────────────────────────────────────────────
const PAGE = 25;

async function renderPeople(box) {
  box.innerHTML = `<div class="card"><div class="card-h"><h3>${esc(t("adm.people"))}</h3>
        <button class="btn adminbtn small" id="pp-new">${esc(t("adm.newPerson"))}</button></div>
      <div class="filter" style="max-width:none;margin-bottom:12px"><svg width="12" height="12" viewBox="0 0 16 16" aria-hidden="true"><circle cx="7" cy="7" r="4.6" fill="none" stroke="#707aa0" stroke-width="1.6"/><path d="m10.4 10.4 3.4 3.4" stroke="#707aa0" stroke-width="1.6" stroke-linecap="round"/></svg>
        <input id="pp-q" type="search" autocomplete="off" placeholder="${esc(t("people.searchPh"))}" aria-label="${esc(t("people.searchPh"))}"></div>
      <div class="x"><table id="pp-table"><tbody><tr><td class="note">…</td></tr></tbody></table></div>
      <div class="pager" id="pp-pager"></div>
    </div>
    <p class="note">${esc(t("adm.peopleNote"))}</p>`;
  let offset = 0, total = 0, seq = 0, timer = null;
  const load = async () => {
    const my = ++seq, q = $("#pp-q", box).value.trim();
    let got;
    try {
      got = await get(`/admin/people?limit=${PAGE}&offset=${offset}&q=${encodeURIComponent(q)}`);
    } catch {
      if (my === seq) $("#pp-table", box).innerHTML = `<tbody><tr><td class="note">${esc(t("err.network"))}</td></tr></tbody>`;
      return;
    }
    if (my !== seq) return;
    total = got.total;
    const rows = got.people;
    $("#pp-table", box).innerHTML = rows.length ? `<thead><tr>${["adm.col.person", "acc.role", "adm.col.signins", "adm.col.lastSignin", "adm.col.lastWrite", "tok.status"].map((k) => `<th>${esc(t(k))}</th>`).join("")}</tr></thead>
      <tbody>${rows.map((p) => `<tr>
        <td class="strong">${personLink(p.id, personName(p))}<small class="cell-sub">${esc([p.email, p.department].filter(Boolean).join(" · "))}</small></td>
        <td><span class="perm ${p.role === "user" ? "read" : "admin"}">${esc(t("role." + p.role))}</span></td>
        <td class="num">${nf(p.signins)}</td>
        <td>${esc(p.last_signin_at ? ago(p.last_signin_at) : t("tok.never"))}</td>
        <td>${esc(p.last_write_at ? ago(p.last_write_at) : t("tok.never"))}</td>
        <td><span class="dot ${p.disabled ? "off" : "on"}"><i></i>${esc(t(p.disabled ? "people.disabled" : "people.active"))}</span></td>
      </tr>`).join("")}</tbody>` : `<tbody><tr><td class="note">${esc(t("people.noneFound"))}</td></tr></tbody>`;
    const from = total ? offset + 1 : 0, to = offset + rows.length;
    $("#pp-pager", box).innerHTML = `<span class="note">${esc(t("adm.range", { from: nf(from), to: nf(to), total: nf(total) }))}</span>
      <button class="btn small quiet" data-page="-1" ${offset === 0 ? "disabled" : ""} aria-label="${esc(t("adm.prev"))}">‹ ${esc(t("adm.prev"))}</button>
      <button class="btn small quiet" data-page="1" ${to >= total ? "disabled" : ""} aria-label="${esc(t("adm.next"))}">${esc(t("adm.next"))} ›</button>`;
  };
  $("#pp-q", box).addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => { offset = 0; load(); }, 220); });
  $("#pp-pager", box).addEventListener("click", (e) => {
    const b = e.target.closest("[data-page]");
    if (!b || b.disabled) return;
    offset = Math.max(0, offset + Number(b.dataset.page) * PAGE);
    load();
  });
  $("#pp-new", box).onclick = () => newPersonDialog();
  await load();
}

function newPersonDialog() {
  const veil = document.createElement("div");
  veil.className = "veil";
  const field = (k, label, type = "text") => `<div class="field"><label for="np-${k}">${esc(label)}</label><input id="np-${k}" type="${type}" maxlength="200"></div>`;
  veil.innerHTML = `<form class="dialog" role="dialog" aria-modal="true" aria-labelledby="np-title">
    <h3 id="np-title">${esc(t("adm.newPerson"))}</h3>
    ${field("full_name", t("acc.name"))}
    ${field("email", t("acc.email"), "email")}
    <div class="two">${field("department", t("people.department"))}${field("position", t("people.position"))}</div>
    <p class="note">${esc(t("adm.newPersonNote"))}</p>
    <p class="err" id="np-err" role="alert" hidden></p>
    <div class="row" style="justify-content:flex-end"><button type="button" class="btn quiet" data-x>${esc(t("common.cancel"))}</button>
      <button type="submit" class="btn adminbtn">${esc(t("adm.create"))}</button></div>
  </form>`;
  document.body.append(veil);
  const close = () => veil.remove();
  veil.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
  veil.addEventListener("click", (e) => { if (e.target === veil || e.target.closest("[data-x]")) close(); });
  $("#np-full_name", veil).focus();
  $("form", veil).addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = {};
    for (const k of ["full_name", "email", "department", "position"]) body[k] = $("#np-" + k, veil).value.trim();
    try {
      const { id } = await post("/admin/people", body);
      close();
      toast(t("adm.created", { name: body.full_name || body.email }));
      history.pushState(null, "", `/people?id=${encodeURIComponent(id)}`);
      window.memgresPanel?.render?.();
    } catch (ex) {
      const err = $("#np-err", veil);
      err.textContent = ex instanceof ApiError && ex.detail ? String(ex.detail) : t("err.save");
      err.hidden = false;
    }
  });
}

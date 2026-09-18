// A space's members and access: invite by email, people waiting to join, members,
// open invitations, the link to share, and handing the space over.

import { del, get, patch, post, ApiError } from "./api.js";
import { ago, fmtDate, t } from "./i18n.js";
import { personLink, personName } from "./people.js";
import { $, autoSlots, esc, hexIcon, PALETTE, toast } from "./ui.js";

const PERMS = ["read", "write", "admin"];

export async function renderSpace(root, ctx) {
  const id = new URLSearchParams(location.search).get("id") || "";
  root.innerHTML = `<div class="page-in"><p class="note">${esc(t("mem.loading"))}</p></div>`;
  let data;
  try {
    data = await get(`/spaces/${encodeURIComponent(id)}/members`);
  } catch (e) {
    root.innerHTML = `<div class="page-in"><div class="page-head"><h1>${esc(t("sp.title"))}</h1></div>
      <div class="card"><p class="note">${esc(e instanceof ApiError && e.status === 404 ? t("sp.notAdmin") : t("err.network"))}</p>
      <div class="row" style="margin-top:12px"><a class="btn" href="/memory">${esc(t("sp.back"))}</a></div></div></div>`;
    return;
  }
  draw(root, ctx, data);
}

function permSelect(value, attrs) {
  return `<select class="sel" ${attrs}>${PERMS.map((p) => `<option value="${p}"${p === value ? " selected" : ""}>${esc(t("perm." + p))}</option>`).join("")}</select>`;
}

function draw(root, ctx, data) {
  const s = data.space, me = ctx.session.user?.id;
  const color = PALETTE[autoSlots([s.name])[s.name] ?? 0];
  const link = `${location.origin}/memory?space=${encodeURIComponent(s.id)}`;
  const others = data.members.filter((m) => !m.owner);
  root.innerHTML = `<div class="page-in">
    <div class="person-head">${hexIcon(color, false, 34)}
      <div class="page-head"><h1 class="mono">${esc(s.name)}</h1><p>${esc(t("sp.lead"))}</p></div>
      <a class="btn quiet" href="/memory?space=${encodeURIComponent(s.id)}" style="margin-left:auto">${esc(t("sp.open"))}</a>
    </div>

    <form class="card" id="sp-invite" autocomplete="off">
      <div class="card-h"><h3>${esc(t("sp.invite"))}</h3></div>
      <div class="invite-row">
        <div class="field grow"><label for="sp-email">${esc(t("acc.email"))}</label><input id="sp-email" type="email" required maxlength="254" placeholder="name@company.com"></div>
        <div class="field"><label for="sp-perm">${esc(t("tok.access"))}</label>${permSelect("read", 'id="sp-perm"')}</div>
        <button class="btn primary" type="submit">${esc(t("sp.inviteGo"))}</button>
      </div>
      <p class="note" style="margin-top:10px">${esc(t("sp.inviteNote", { days: data.invite_days }))}</p>
    </form>

    ${data.requests.length ? `<div class="card"><div class="card-h"><h3>${esc(t("sp.waiting"))}</h3><span class="badge">${data.requests.length}</span></div>
      <div class="stack">${data.requests.map((r) => `<div class="item">
        <div class="grow"><b>${personLink(r.person.id, personName(r.person))}</b><small>${esc([r.person.email, t("sp.asked", { perm: t("perm." + r.permission), ago: ago(r.created_at) })].filter(Boolean).join(" · "))}</small></div>
        ${permSelect(r.permission, `data-req-perm="${esc(r.id)}" aria-label="${esc(t("tok.access"))}"`)}
        <button class="btn primary small" data-act="approve" data-id="${esc(r.id)}">${esc(t("sp.approve"))}</button>
        <button class="btn danger small" data-act="deny" data-id="${esc(r.id)}">${esc(t("sp.deny"))}</button>
      </div>`).join("")}</div></div>` : ""}

    <div class="card"><div class="card-h"><h3>${esc(t("sp.members"))}</h3><span class="note">${data.members.length}</span></div>
      <div class="x"><table><tbody>${data.members.map((m) => `<tr>
        <td class="strong">${personLink(m.id, personName(m))}${m.id === me ? ` <span class="note">${esc(t("sp.you"))}</span>` : ""}
          ${m.email ? `<small class="cell-sub">${esc(m.email)}</small>` : ""}</td>
        <td>${m.owner ? `<span class="perm admin">${esc(t("sp.owner"))}</span>` : permSelect(m.permission, `data-member="${esc(m.id)}" aria-label="${esc(t("tok.access"))}"`)}</td>
        <td class="note">${m.disabled ? esc(t("people.disabled")) : m.since && !m.owner ? esc(t("sp.since", { date: fmtDate(m.since) })) : ""}</td>
        <td style="text-align:right">${m.owner ? "" : `<button class="btn danger small" data-act="remove" data-id="${esc(m.id)}">${esc(t(m.id === me ? "sp.leave" : "sp.remove"))}</button>`}</td>
      </tr>`).join("")}</tbody></table></div></div>

    ${data.invites.length ? `<div class="card"><div class="card-h"><h3>${esc(t("sp.invites"))}</h3></div>
      <div class="x"><table><tbody>${data.invites.map((i) => `<tr>
        <td class="strong mono">${esc(i.email)}</td>
        <td><span class="perm ${esc(i.permission)}">${esc(t("perm." + i.permission))}</span></td>
        <td class="note">${esc(i.invited_by ? t("sp.invitedBy", { name: i.invited_by }) : "")}</td>
        <td>${i.lapsed ? `<span class="dot off" title="${esc(t("sp.lapsedWhy"))}"><i></i>${esc(t("sp.lapsed"))}</span>` : i.expired ? `<span class="dot off"><i></i>${esc(t("sp.expired"))}</span>` : `<span class="dot wait"><i></i>${esc(t("sp.until", { date: fmtDate(i.expires_at) }))}</span>`}</td>
        <td style="text-align:right"><button class="btn quiet small" data-act="cancel" data-id="${esc(i.id)}">${esc(t("sp.cancel"))}</button></td>
      </tr>`).join("")}</tbody></table></div>
      <p class="note" style="margin-top:10px">${esc(t("sp.invitesNote"))}</p></div>` : ""}

    <div class="card"><div class="card-h"><h3>${esc(t("sp.link"))}</h3></div>
      <div class="secret plain"><code>${esc(link)}</code><button class="btn" data-act="copy">${esc(t("tok.copy"))}</button></div>
      <p class="note" style="margin-top:10px">${esc(t("sp.linkNote"))}</p></div>

    ${data.can_transfer ? `<div class="card"><div class="card-h"><h3>${esc(t("sp.transfer"))}</h3></div>
      ${others.length ? `<div class="invite-row">
        <div class="field grow"><label for="sp-to">${esc(t("sp.transferTo"))}</label><select id="sp-to" class="sel">${others.map((m) => `<option value="${esc(m.id)}">${esc(personName(m))}</option>`).join("")}</select></div>
        <label class="check"><input type="checkbox" id="sp-keep" checked> ${esc(t("sp.keepMe"))}</label>
        <button class="btn danger" data-act="transfer">${esc(t("sp.transferGo"))}</button>
      </div>` : `<p class="note">${esc(t("sp.transferNone"))}</p>`}
      <p class="note" style="margin-top:10px">${esc(t("sp.transferNote"))}</p></div>` : ""}
  </div>`;
  wire(root, ctx, data, link);
}

function wire(root, ctx, data, link) {
  // the page redraws into the same element: drop the previous drawing's listeners
  root._wired?.abort();
  root._wired = new AbortController();
  const once = { signal: root._wired.signal };
  const s = data.space, base = `/spaces/${encodeURIComponent(s.id)}`;
  const reload = async () => draw(root, ctx, await get(`${base}/members`));
  const run = async (fn, done, after) => {
    try {
      await fn();
      if (done) toast(done);
      if (after) return after();
      await reload();
    } catch (ex) {
      toast(ex instanceof ApiError && ex.detail ? String(ex.detail) : t("err.save"));
      await reload().catch(() => {});
    }
  };
  $("#sp-invite", root).addEventListener("submit", (e) => {
    e.preventDefault();
    const email = $("#sp-email", root).value.trim(), permission = $("#sp-perm", root).value;
    run(() => post(`${base}/members`, { email, permission }), t("sp.invited", { email }));
  });
  root.addEventListener("change", (e) => {
    const sel = e.target.closest("[data-member]");
    if (!sel) return;
    const m = data.members.find((x) => x.id === sel.dataset.member);
    if (m.id === ctx.session.user?.id && sel.value !== "admin" && !confirm(t("sp.demoteSelf"))) { sel.value = m.permission; return; }
    run(() => patch(`${base}/members/${encodeURIComponent(m.id)}`, { permission: sel.value }),
      t("sp.permDone", { name: personName(m), perm: t("perm." + sel.value) }));
  }, once);
  root.addEventListener("click", (e) => {
    const b = e.target.closest("[data-act]");
    if (!b || b.disabled) return;
    const id = b.dataset.id;
    switch (b.dataset.act) {
      case "copy":
        (navigator.clipboard?.writeText(link) || Promise.reject()).then(() => toast(t("toast.copied", { v: link })), () => toast(link));
        return;
      case "approve": {
        const r = data.requests.find((x) => x.id === id);
        const permission = $(`[data-req-perm="${CSS.escape(id)}"]`, root).value;
        run(() => post(`${base}/requests/${encodeURIComponent(id)}`, { approve: true, permission, expect_permission: r.permission }),
          t("sp.approved", { name: personName(r.person) }), () => { refreshSpaces(ctx); return reload(); });
        return;
      }
      case "deny": {
        const r = data.requests.find((x) => x.id === id);
        if (confirm(t("sp.denyConfirm", { name: personName(r.person) })))
          run(() => post(`${base}/requests/${encodeURIComponent(id)}`, { approve: false }), t("sp.denied"),
            () => { refreshSpaces(ctx); return reload(); });
        return;
      }
      case "remove": {
        const m = data.members.find((x) => x.id === id);
        const self = id === ctx.session.user?.id;
        if (!confirm(t(self ? "sp.leaveConfirm" : "sp.removeConfirm", { name: personName(m), space: s.name }))) return;
        run(() => del(`${base}/members/${encodeURIComponent(id)}`), t(self ? "sp.left" : "sp.removed", { name: personName(m), space: s.name }),
          self ? () => { refreshSpaces(ctx); ctx.navigate("/memory"); } : undefined);
        return;
      }
      case "cancel":
        run(() => del(`${base}/invites/${encodeURIComponent(id)}`), t("sp.cancelled"));
        return;
      case "transfer": {
        const to = data.members.find((x) => x.id === $("#sp-to", root).value);
        const keep = $("#sp-keep", root).checked;
        if (!confirm(t(keep ? "sp.transferConfirmKeep" : "sp.transferConfirm", { name: personName(to), space: s.name }))) return;
        run(() => post(`${base}/transfer`, { user_id: to.id, keep_me: keep }), t("sp.transferred", { name: personName(to) }),
          async () => { refreshSpaces(ctx); if (keep || ctx.session.role === "superadmin") await reload(); else ctx.navigate(`/memory?space=${encodeURIComponent(s.id)}`); });
      }
    }
  }, once);
}

function refreshSpaces(ctx) {
  ctx.invalidateSpaces?.();
  window.memgresPanel?.modules?.sidebarSpaces?.(ctx);
}

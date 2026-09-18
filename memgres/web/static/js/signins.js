// Account → Sign-in methods: the providers linked to this account.

import { del, get, post, ApiError } from "./api.js";
import { ago, fmtDate, t } from "./i18n.js";
import { $, esc, toast } from "./ui.js";

export async function renderSignins(box) {
  const result = new URLSearchParams(location.search).get("auth");
  box.innerHTML = `${result ? `<div class="auth-note ${result === "linked" ? "good" : "bad"}" role="status">${esc(message(result))}</div>` : ""}
    <div class="card"><div class="card-h"><h3>${esc(t("signins.title"))}</h3></div>
      <div class="stack" id="si-list"><p class="note">…</p></div></div>
    <div class="card" id="si-add-card"><div class="card-h"><h3>${esc(t("signins.add"))}</h3></div>
      <div class="row" id="si-add"></div><p class="note" style="margin-top:10px">${esc(t("signins.addNote"))}</p></div>
    <p class="note">${esc(t("signins.note"))}</p>`;
  let data;
  try {
    data = await get("/me/signins");
  } catch {
    $("#si-list", box).innerHTML = `<p class="note">${esc(t("err.network"))}</p>`;
    return;
  }
  const draw = () => {
    const list = $("#si-list", box);
    const only = data.signins.length === 1;
    list.innerHTML = data.signins.length ? data.signins.map((m) => `<div class="item">
        <span class="prov">${esc(m.label.slice(0, 3))}</span>
        <div class="grow"><b>${esc(m.label)}</b><small>${esc(m.email || "")}</small></div>
        <span class="note">${esc(t("signins.linkedOn", { date: fmtDate(m.linked_at), used: m.last_login_at ? ago(m.last_login_at) : t("tok.never") }))}</span>
        <button class="btn quiet small" data-unlink="${esc(m.id)}" ${only ? `disabled title="${esc(t("signins.onlyOne"))}"` : ""}>${esc(t("signins.unlink"))}</button>
      </div>`).join("") : `<p class="note">${esc(t("signins.none"))}</p>`;
    const linked = new Set(data.signins.map((m) => m.provider));
    const free = data.providers.filter((p) => !linked.has(p.id));
    $("#si-add-card", box).hidden = !free.length;
    $("#si-add", box).innerHTML = free.map((p) => `<button class="btn" data-link="${esc(p.id)}">${esc(t("signins.linkWith", { name: p.label }))}</button>`).join("");
  };
  draw();
  $("#si-list", box).addEventListener("click", async (e) => {
    const b = e.target.closest("[data-unlink]");
    if (!b || b.disabled) return;
    const m = data.signins.find((x) => x.id === b.dataset.unlink);
    if (!confirm(t("signins.unlinkConfirm", { name: m?.label || "" }))) return;
    b.disabled = true;
    try {
      await del(`/me/signins/${encodeURIComponent(b.dataset.unlink)}`);
      data = await get("/me/signins");
      draw();
      toast(t("signins.unlinked", { name: m?.label || "" }));
    } catch (ex) {
      b.disabled = false;
      toast(ex instanceof ApiError && ex.status === 409 ? t("signins.onlyOne") : t("err.save"));
    }
  });
  // linking is a state change: ask the server (CSRF-checked) for the provider's address, then go there
  $("#si-add", box).addEventListener("click", async (e) => {
    const b = e.target.closest("[data-link]");
    if (!b) return;
    b.disabled = true;
    try {
      const { url } = await post("/me/signins/link", { provider: b.dataset.link });
      location.assign(url);
    } catch (ex) {
      b.disabled = false;
      toast(ex instanceof ApiError && ex.status === 429 ? t("auth.throttled") : t("auth.provider_down"));
    }
  });
}

function message(code) {
  const key = "auth." + code;
  return t(key) === key ? t("auth.failed") : t(key);
}

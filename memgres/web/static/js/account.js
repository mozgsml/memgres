// Account: profile and language. Tokens live in tokens.js.

import { get, patch } from "./api.js";
import { fillLanguageSelect, t } from "./i18n.js";
import { activityCard, recentCard, spacesCard } from "./people.js";
import { $, esc, toast } from "./ui.js";

export function renderAccount(root, ctx) {
  const { session, pane } = ctx;
  const me = session.user;
  root.innerHTML = `<div class="page-in">
    <div class="page-head"><h1>${esc(t("acc.title"))}</h1><p>${esc(t("acc.lead"))}</p></div>
    <div class="subnav" role="tablist">
      <a role="tab" href="/account" aria-selected="${pane === "profile"}">${esc(t("acc.tab.profile"))}</a>
      ${me ? `<a role="tab" href="/account/signins" aria-selected="${pane === "signins"}">${esc(t("acc.tab.signins"))}</a>` : ""}
      ${session.can.tokens ? `<a role="tab" href="/account/tokens" aria-selected="${pane === "tokens"}">${esc(t("acc.tab.tokens"))}</a>` : ""}
    </div>
    <div class="pane" id="acc-pane"></div>
  </div>`;
  const box = $("#acc-pane", root);
  if (pane === "tokens" && ctx.renderTokens) { ctx.renderTokens(box, ctx); return; }
  if (pane === "signins" && ctx.renderSignins) { ctx.renderSignins(box, ctx); return; }

  if (!me) {
    box.innerHTML = `<div class="card"><p class="note" style="color:var(--text)">${esc(t("acc.rootNote"))}</p></div>`;
    return;
  }
  box.innerHTML = `<div class="card"><dl class="kv">
      <dt>${esc(t("acc.name"))}</dt><dd>${esc(me.full_name || me.name || "—")}</dd>
      <dt>${esc(t("acc.email"))}</dt><dd>${esc(me.email || "—")}</dd>
      <dt>${esc(t("acc.role"))}</dt><dd><span class="perm ${me.role === "user" ? "read" : "admin"}">${esc(t("role." + me.role))}</span></dd>
      <dt><label for="acc-lang">${esc(t("acc.language"))}</label></dt><dd><select id="acc-lang"></select></dd>
    </dl></div>
    <p class="note">${esc(t("acc.profileNote"))}</p>
    <div class="pane" id="acc-more"></div>`;
  get(`/people/${encodeURIComponent(me.id)}`).then((data) => {
    const more = $("#acc-more", box);
    if (more) more.innerHTML = activityCard(data.activity, { note: t("act.noteSelf") }) + recentCard(data.recent, { note: t("recent.noteSelf") }) + spacesCard(data.spaces);
  }).catch(() => {});
  const select = $("#acc-lang", box);
  fillLanguageSelect(select, me.ui_language || "auto");
  select.onchange = async () => {
    const value = select.value === "auto" ? null : select.value;
    try {
      const fresh = await patch("/me", { ui_language: value });
      await ctx.onLanguage(fresh);
    } catch {
      toast(t("err.save"));
    }
  };
}

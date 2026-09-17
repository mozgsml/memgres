// Account → Tokens: the person's own tokens for AI clients.

import { del, get, post, ApiError } from "./api.js";
import { ago, fmtDate, t } from "./i18n.js";
import { $, $$, esc, toast } from "./ui.js";

const SNIPPETS = {
  "Claude Code": (url, k) => `// ~/.claude.json\n{\n  "mcpServers": {\n    "memgres": {\n      "type": "http",\n      "url": "${url}",\n      "headers": { "Authorization": "Bearer ${k}" }\n    }\n  }\n}`,
  Cursor: (url, k) => `// ~/.cursor/mcp.json\n{\n  "mcpServers": {\n    "memgres": {\n      "url": "${url}",\n      "headers": { "Authorization": "Bearer ${k}" }\n    }\n  }\n}`,
  OpenCode: (url, k) => `// ~/.config/opencode/opencode.jsonc\n{\n  "mcp": {\n    "memgres": {\n      "type": "remote",\n      "url": "${url}",\n      "headers": { "Authorization": "Bearer ${k}" }\n    }\n  }\n}`,
};

export async function renderTokens(box) {
  box.innerHTML = `<div class="card"><div class="card-h"><h3>${esc(t("tok.title"))}</h3>
      <button class="btn primary" id="tok-new">${esc(t("tok.new"))}</button></div>
      <div class="x"><table id="tok-table"><tbody><tr><td class="note">…</td></tr></tbody></table></div></div>
    <p class="note">${esc(t("tok.note"))}</p>`;
  let data;
  try {
    data = await get("/tokens");
  } catch {
    $("#tok-table", box).innerHTML = `<tbody><tr><td class="note">${esc(t("err.network"))}</td></tr></tbody>`;
    return;
  }
  const draw = () => {
    const rows = data.tokens;
    $("#tok-table", box).innerHTML = rows.length ? `<thead><tr>${["tok.device", "tok.access", "tok.space", "tok.created", "tok.lastUsed", "tok.expires", "tok.status"]
      .map((k) => `<th>${esc(t(k))}</th>`).join("")}<th></th></tr></thead><tbody>${rows.map((x) => `<tr>
        <td class="strong">${esc(x.label || t("tok.unlabelled"))}</td>
        <td><span class="perm ${esc(x.permission)}">${esc(t("perm." + x.permission))}</span></td>
        <td class="mono">${x.namespace ? esc(x.namespace) : `<span class="note">${esc(t("tok.allSpaces"))}</span>`}</td>
        <td class="num">${esc(fmtDate(x.created_at))}</td>
        <td>${esc(x.last_used_at ? ago(x.last_used_at) : t("tok.never"))}</td>
        <td class="num">${x.expires_at ? esc(fmtDate(x.expires_at)) : "—"}</td>
        <td><span class="dot ${x.state === "active" ? "on" : "off"}"><i></i>${esc(t("tok.state." + x.state))}</span></td>
        <td style="text-align:right">${x.state === "active" ? `<button class="btn danger small" data-revoke="${esc(x.id)}">${esc(t("tok.revoke"))}</button>` : ""}</td>
      </tr>`).join("")}</tbody>` : `<tbody><tr><td class="note">${esc(t("tok.none"))}</td></tr></tbody>`;
  };
  draw();

  $("#tok-table", box).addEventListener("click", async (e) => {
    const b = e.target.closest("[data-revoke]");
    if (!b) return;
    const row = data.tokens.find((x) => x.id === b.dataset.revoke);
    if (!confirm(t("tok.revokeConfirm", { label: row?.label || t("tok.unlabelled") }))) return;
    b.disabled = true;
    try {
      await del(`/tokens/${encodeURIComponent(b.dataset.revoke)}`);
      data = await get("/tokens");
      draw();
      toast(t("tok.revokedToast", { label: row?.label || t("tok.unlabelled") }));
    } catch {
      b.disabled = false;
      toast(t("err.save"));
    }
  });

  $("#tok-new", box).onclick = async () => {
    let spaces = [];
    try { spaces = (await get("/spaces")).spaces; } catch { /* all spaces only */ }
    openNewTokenDialog(data, spaces, async () => { data = await get("/tokens"); draw(); });
  };
}

function openNewTokenDialog(data, spaces, onCreated) {
  const veil = document.createElement("div");
  veil.className = "veil";
  veil.innerHTML = `<form class="dialog" role="dialog" aria-modal="true" aria-labelledby="tk-title">
    <h3 id="tk-title">${esc(t("tok.new"))}</h3>
    <div class="field"><label for="tk-label">${esc(t("tok.label"))}</label>
      <input id="tk-label" maxlength="100" placeholder="${esc(t("tok.labelPlaceholder"))}"></div>
    <div class="two">
      <div class="field"><label for="tk-perm">${esc(t("tok.access"))}</label><select id="tk-perm">
        <option value="write">${esc(t("tok.permWrite"))}</option><option value="read">${esc(t("tok.permRead"))}</option></select></div>
      <div class="field"><label for="tk-exp">${esc(t("tok.expAfter"))}</label><select id="tk-exp">
        ${data.expiry_choices.map((n) => `<option value="${n}"${n === 90 ? " selected" : ""}>${esc(t("tok.days", { n }))}</option>`).join("")}</select></div>
    </div>
    <div class="field"><label for="tk-space">${esc(t("tok.space"))}</label><select id="tk-space">
      <option value="">${esc(t("tok.allSpaces"))}</option>
      ${spaces.map((s) => `<option value="${esc(s.id)}">${esc(s.name)}</option>`).join("")}</select></div>
    <p class="note">${esc(t("tok.ceiling"))}</p>
    <p class="err" id="tk-err" role="alert" hidden></p>
    <div class="row" style="justify-content:flex-end"><button type="button" class="btn quiet" data-x>${esc(t("common.cancel"))}</button>
      <button type="submit" class="btn primary">${esc(t("tok.create"))}</button></div>
  </form>`;
  document.body.append(veil);
  const dlg = $(".dialog", veil);
  const close = () => veil.remove();
  veil.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
  veil.addEventListener("click", (e) => { if (e.target === veil || e.target.closest("[data-x]")) close(); });
  $("#tk-label", veil).focus();

  dlg.addEventListener("submit", async (e) => {
    e.preventDefault();
    const submit = $("button[type=submit]", dlg);
    submit.disabled = true;
    try {
      const made = await post("/tokens", {
        label: $("#tk-label", dlg).value.trim(),
        permission: $("#tk-perm", dlg).value,
        expires_days: Number($("#tk-exp", dlg).value),
        namespace_id: $("#tk-space", dlg).value || null,
      });
      await onCreated();
      showSecret(dlg, made.token, data.mcp_url);
    } catch (ex) {
      const err = $("#tk-err", dlg);
      err.textContent = ex instanceof ApiError && ex.status === 422 ? String(ex.detail) : t("err.save");
      err.hidden = false;
      submit.disabled = false;
    }
  });
}

function showSecret(dlg, secret, mcpUrl) {
  const url = mcpUrl || "https://memgres.example/mcp";
  dlg.outerHTML = `<div class="dialog" role="dialog" aria-modal="true" aria-labelledby="tk-done">
    <h3 id="tk-done">${esc(t("tok.copyNow"))}</h3>
    <div class="secret"><code id="tk-secret">${esc(secret)}</code><button class="btn" data-copy>${esc(t("tok.copy"))}</button></div>
    <p class="note">${esc(t("tok.fingerprint"))}</p>
    ${mcpUrl ? "" : `<p class="note" style="color:var(--admin)">${esc(t("tok.noMcpUrl"))}</p>`}
    <div class="snips" role="tablist">${Object.keys(SNIPPETS).map((k, i) => `<button role="tab" data-snip="${esc(k)}" aria-selected="${i === 0}">${esc(k)}</button>`).join("")}</div>
    <pre class="code" id="tk-snip"></pre>
    <div class="row" style="justify-content:flex-end"><button class="btn primary" data-x>${esc(t("tok.done"))}</button></div>
  </div>`;
  const box = document.querySelector(".veil .dialog");
  const show = (name) => {
    $("#tk-snip", box).textContent = SNIPPETS[name](url, secret);
    for (const b of $$("[data-snip]", box)) b.setAttribute("aria-selected", String(b.dataset.snip === name));
  };
  show("Claude Code");
  box.addEventListener("click", (e) => {
    const s = e.target.closest("[data-snip]");
    if (s) show(s.dataset.snip);
    if (e.target.closest("[data-copy]")) {
      (navigator.clipboard?.writeText(secret) || Promise.reject()).then(() => toast(t("tok.copied")), () => {
        const range = document.createRange(); range.selectNodeContents($("#tk-secret", box));
        const sel = getSelection(); sel.removeAllRanges(); sel.addRange(range);
        toast(t("tok.copyFail"));
      });
    }
  });
}

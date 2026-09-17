// UI strings. One JSON file per language in /ui/static/locales/; en.json is the
// reference every other file is checked against. A missing key falls back to
// English, so a partial translation never breaks the panel.
//
// Plurals are objects keyed by Intl.PluralRules categories (one/few/many/other);
// {n} inside them is the formatted number.

const dicts = {};
let lang = "en";
let available = ["en"];

async function fetchLocale(code) {
  if (dicts[code]) return dicts[code];
  const r = await fetch(`/ui/static/locales/${encodeURIComponent(code)}.json`, { credentials: "same-origin" });
  dicts[code] = r.ok ? await r.json() : {};
  return dicts[code];
}

function browserLang() {
  for (const l of navigator.languages || [navigator.language || "en"]) {
    const base = String(l).slice(0, 2).toLowerCase();
    if (available.includes(base)) return base;
  }
  return "en";
}

// pref: "auto" or a code. Saved preference wins over the browser, the browser over English.
export async function setLanguage(pref, locales) {
  if (locales && locales.length) available = locales;
  lang = pref && pref !== "auto" && available.includes(pref) ? pref : browserLang();
  await fetchLocale("en");
  if (lang !== "en") await fetchLocale(lang);
  document.documentElement.lang = lang;
  return lang;
}

export const currentLang = () => lang;
export const languages = () => available;
export const browserLanguage = () => browserLang();

const missing = new Set();
export const missingKeys = () => [...missing];

export function t(key, vars = {}) {
  let v = dicts[lang]?.[key] ?? dicts.en?.[key];
  if (v === undefined) { missing.add(key); return key; }
  if (typeof v === "object") {
    const n = vars.n ?? 0;
    v = v[new Intl.PluralRules(lang).select(n)] ?? v.other;
    vars = { ...vars, n: nf(n) };
  }
  return String(v).replace(/\{(\w+)\}/g, (_, k) => (vars[k] ?? `{${k}}`));
}

export const nf = (n) => new Intl.NumberFormat(lang).format(n);

export function fmtDate(value) {
  const d = value instanceof Date ? value : new Date(value);
  return new Intl.DateTimeFormat(lang, { day: "numeric", month: "short", year: "numeric" }).format(d);
}

export function ago(value) {
  const d = value instanceof Date ? value : new Date(value);
  const s = (Date.now() - d.getTime()) / 1000;
  const rtf = new Intl.RelativeTimeFormat(lang, { numeric: "auto" });
  if (s < 60) return rtf.format(0, "second");
  if (s < 3600) return rtf.format(-Math.round(s / 60), "minute");
  if (s < 86400) return rtf.format(-Math.round(s / 3600), "hour");
  if (s < 45 * 86400) return rtf.format(-Math.round(s / 86400), "day");
  if (s < 400 * 86400) return rtf.format(-Math.round(s / (30 * 86400)), "month");
  return rtf.format(-Math.round(s / (365 * 86400)), "year");
}

// data-i18n → text, data-i18n-html → markup (our own dictionary only),
// data-i18n-attr="attr:key;attr:key" → attributes
export function applyStatic(root = document) {
  for (const el of root.querySelectorAll("[data-i18n]")) el.textContent = t(el.dataset.i18n);
  for (const el of root.querySelectorAll("[data-i18n-html]")) el.innerHTML = t(el.dataset.i18nHtml);
  for (const el of root.querySelectorAll("[data-i18n-attr]")) {
    for (const pair of el.dataset.i18nAttr.split(";")) {
      const [attr, key] = pair.split(":");
      el.setAttribute(attr, t(key));
    }
  }
}

// the options of a language picker: browser default first, then each language named in itself
export async function fillLanguageSelect(select, pref) {
  await Promise.all(available.map(fetchLocale));
  const own = (code) => dicts[code]?.["lang.self"] || code;
  const opts = [["auto", t("lang.auto", { lang: own(browserLang()) })], ...available.map((c) => [c, own(c)])];
  select.replaceChildren(...opts.map(([v, label]) => {
    const o = document.createElement("option");
    o.value = v; o.textContent = label; o.selected = v === (pref || "auto");
    return o;
  }));
}

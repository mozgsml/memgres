// A record's body as Markdown: headings, lists, tables, code, links — and the
// memory's own [[path]] links, which open the record they name.
//
// Bodies are written by agents and people, so nothing in them runs: raw HTML is
// shown as text, only http(s)/mailto links are kept (and open in a new tab),
// images are not fetched, and the result goes through DOMPurify as well.

import { Marked } from "../vendor/marked.esm.js";
import DOMPurify from "../vendor/purify.es.js";
import { esc } from "./ui.js";

let wikilink = (target, label) => esc(label);

const md = new Marked({ gfm: true, breaks: true, async: false });
md.use({
  extensions: [{
    name: "wikilink",
    level: "inline",
    start(src) { const i = src.indexOf("[["); return i < 0 ? undefined : i; },
    tokenizer(src) {
      const m = /^\[\[([^\]|#\n]+)(?:#[^\]|\n]*)?(?:\|([^\]\n]*))?\]\]/.exec(src);
      if (m) return { type: "wikilink", raw: m[0], target: m[1].trim(), label: (m[2] || m[1]).trim() };
    },
    renderer(token) { return wikilink(token.target, token.label); },
  }],
  renderer: {
    html(token) { return esc(token.text); },
    link({ href, title, tokens }) {
      const text = this.parser.parseInline(tokens);
      if (!/^(https?:|mailto:)/i.test(href || "")) return text;
      return `<a href="${esc(href)}" target="_blank" rel="noopener noreferrer nofollow"${title ? ` title="${esc(title)}"` : ""}>${text}</a>`;
    },
    image({ text }) { return text ? `<span class="md-img">[${esc(text)}]</span>` : ""; },
  },
});

// `link(target, label)` returns the HTML for one [[…]] link.
export function renderMarkdown(body, { link } = {}) {
  wikilink = link || ((t, label) => esc(label));
  try {
    const html = md.parse(String(body ?? ""));
    return DOMPurify.sanitize(html, { ADD_ATTR: ["target"], FORBID_TAGS: ["style", "form", "input", "textarea", "select"] });
  } finally {
    wikilink = (t, label) => esc(label);
  }
}

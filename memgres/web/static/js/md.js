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

const CLEAN = { ADD_ATTR: ["target"], FORBID_TAGS: ["style", "form", "input", "textarea", "select"] };
const clean = (html) => DOMPurify.sanitize(html, CLEAN);

// `link(target, label)` returns the HTML for one [[…]] link.
export function renderMarkdown(body, { link } = {}) {
  wikilink = link || ((t, label) => esc(label));
  try {
    return clean(md.parse(String(body ?? "")));
  } finally {
    wikilink = (t, label) => esc(label);
  }
}

// ─── blocks, for blame ───────────────────────────────────────────────────────
// Attribution is per source LINE, and a rendered document has no lines left in
// it. What it does have is blocks, and every block knows which lines it came
// from: the lexer keeps each token's `raw`, so walking the tokens in order and
// counting newlines gives each one its range. Rendering block by block rather
// than annotating one rendered tree afterwards keeps the mapping exact — a
// token that renders to no element, or to several, cannot slide the rest along.
//
// Returns, in document order:
//   { kind: "block", start, end, html }
//   { kind: "list", ordered, items: [{ start, end, html }] }
//   { kind: "table", head, rows: [{ start, end, html }] }   (html = the cells)
// Lists are split by item and tables by row, because that is where authorship
// usually differs — and because in both the source lines up one to one with
// what is drawn: a GFM row cannot span lines. Deeper nesting rides along with
// the item it sits in, and a cell cannot be told from its neighbours at all,
// since all of them are one line.
export function blameBlocks(body, { link } = {}) {
  wikilink = link || ((t, label) => esc(label));
  const src = String(body ?? "");
  const out = [];
  const nl = (str) => { let n = 0; for (let i = 0; i < str.length; i++) if (str[i] === "\n") n++; return n; };
  let line = 1;
  try {
    for (const tk of md.lexer(src)) {
      if (tk.type === "space") { line += nl(tk.raw); continue; }
      if (tk.type === "list" && tk.items?.length) {
        let at = line;
        const items = tk.items.map((it) => {
          const start = at;
          at += nl(it.raw);
          return { start, end: Math.max(start, start + nl(it.raw.replace(/\s+$/, ""))), html: clean(md.parser(it.tokens)) };
        });
        out.push({ kind: "list", ordered: !!tk.ordered, start: tk.ordered ? tk.start : null, items });
        line += nl(tk.raw);
        continue;
      }
      const rows = tk.type === "table" ? tableRows(tk, line) : null;
      const start = line;
      if (rows) out.push({ kind: "table", start, end: rows.end, head: rows.head, rows: rows.rows });
      else out.push({ kind: "block", start, end: Math.max(start, start + nl(tk.raw.replace(/\s+$/, ""))), html: clean(md.parser([tk])) });
      line += nl(tk.raw);
    }
  } finally {
    wikilink = (t, label) => esc(label);
  }
  return out;
}

// One source line per row: the header, the |---| rule under it, then the rows in
// order. The cells are handed back rather than a whole <tr>, because a <tr> on
// its own does not survive sanitising outside a table — so the caller builds the
// row and only the cell contents go through DOMPurify. Anything that does not
// line up exactly falls back to the table as one block, which is always safe.
function tableRows(tk, first) {
  const lines = String(tk.raw).replace(/\n+$/, "").split("\n");
  if (lines.length !== tk.rows.length + 2) return null;
  const align = tk.align || [];
  const cells = (row, tag) => row.map((c, i) => {
    const a = align[i] ? ` style="text-align:${align[i] === "center" ? "center" : align[i] === "right" ? "right" : "left"}"` : "";
    return `<${tag}${a}>${clean(md.parseInline(c.text ?? ""))}</${tag}>`;
  }).join("");
  return {
    head: { start: first, end: first, html: cells(tk.header, "th") },
    rows: tk.rows.map((row, i) => ({ start: first + 2 + i, end: first + 2 + i, html: cells(row, "td") })),
    end: first + 1 + tk.rows.length,
  };
}

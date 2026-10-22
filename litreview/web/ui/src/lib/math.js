import katex from "katex";
import "katex/dist/katex.min.css";

// LaTeX math in paper text, cards and abstracts, rendered with KaTeX. Stored texts carry it as
// $...$ and $$...$$ (litreview/fulltext.py); ADS abstracts and titles use $...$ too.
// $$...$$, \[...\], \(...\), then $...$ with no space just inside either dollar, so "$5 to $10"
// stays text. A digit may follow the closing dollar, as in "$\sim$15 days".
const MATH = /\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\]|\\\(([\s\S]+?)\\\)|(?<![\\$])\$(?=\S)((?:\\\$|[^$])+?)(?<=\S)\$/g;

// Astronomy macros, as fulltext.py's MATH_MACROS expands them in arXiv sources; arXiv HTML and
// ADS abstracts can still carry them.
const MACROS = {
  "\\msun": "M_\\odot", "\\Msun": "M_\\odot", "\\rsun": "R_\\odot", "\\Rsun": "R_\\odot",
  "\\lsun": "L_\\odot", "\\Lsun": "L_\\odot", "\\mearth": "M_\\oplus", "\\Mearth": "M_\\oplus",
  "\\rearth": "R_\\oplus", "\\Rearth": "R_\\oplus", "\\mjup": "M_{\\rm Jup}", "\\Mjup": "M_{\\rm Jup}",
  "\\rjup": "R_{\\rm Jup}", "\\Rjup": "R_{\\rm Jup}", "\\kms": "\\,{\\rm km\\,s^{-1}}",
  "\\micron": "\\mu{\\rm m}", "\\farcs": ".''", "\\arcsec": "''", "\\arcmin": "'", "\\degr": "^\\circ",
  "\\sun": "\\odot", "\\earth": "\\oplus", "\\teff": "T_{\\rm eff}", "\\Teff": "T_{\\rm eff}",
  "\\la": "\\lesssim", "\\ga": "\\gtrsim", "\\lapprox": "\\lesssim", "\\gapprox": "\\gtrsim",
  "\\Mj": "M_{\\rm J}", "\\Rj": "R_{\\rm J}", "\\eps": "\\epsilon",
  // LaTeX and package commands KaTeX lacks, approximated
  "\\ensuremath": "#1", "\\mbox": "\\text{#1}", "\\textsc": "\\text{#1}", "\\mrm": "\\mathrm{#1}",
  "\\si": "\\mathrm{#1}", "\\SI": "#1\\,\\mathrm{#2}", "\\mathbbm": "\\mathbb{#1}", "\\mbi": "\\boldsymbol{#1}",
};

function prepare(tex) {
  return tex
    .replace(/\\(begin|end)\{eqnarray\*?\}/g, (_, be) => (be === "begin" ? "\\begin{array}{rcl}" : "\\end{array}"))
    .replace(/\\(begin|end)\{flalign\*?\}/g, "\\$1{aligned}")
    .replace(/(\\begin\{array\})\[[^\]]*\]/g, "$1") // LaTeXML writes \begin{array}[]{ll}
    .replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&"); // ADS abstracts escape these
}

const cache = new Map();

// KaTeX's HTML for one formula, or null if KaTeX can't render it (the source is shown instead).
function render(tex, displayMode) {
  const key = (displayMode ? "D" : "I") + tex;
  if (!cache.has(key)) {
    let html = null;
    try {
      html = katex.renderToString(prepare(tex), { displayMode, throwOnError: true, strict: "ignore", trust: false,
                                                  macros: { ...MACROS } });
    } catch {
      // an unknown macro or unsupported environment
    }
    cache.set(key, html);
  }
  return cache.get(key);
}

// Text as [{t: "text", v}] and [{t: "math", v, html}] tokens. Only KaTeX's output is HTML; the
// rest stays text for Svelte to escape, and so does any formula KaTeX can't render.
export function mathTokens(text) {
  const out = [];
  let last = 0;
  text = text || "";
  for (const m of text.matchAll(MATH)) {
    const display = m[1] !== undefined || m[2] !== undefined;
    const html = render(m[1] ?? m[2] ?? m[3] ?? m[4], display);
    if (!html) continue;
    if (m.index > last) out.push({ t: "text", v: text.slice(last, m.index) });
    out.push({ t: "math", v: m[0], html });
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push({ t: "text", v: text.slice(last) });
  return out;
}

// The first n characters of s, cut before any formula that would be split there (share.py's
// short_quote cuts quotes the same way).
export function cutBeforeMath(s, n) {
  const cut = s.slice(0, n);
  let end = 0;
  for (const m of cut.matchAll(MATH)) end = m.index + m[0].length;
  const open = cut.slice(end).search(/\$|\\[([]/);
  return open < 0 ? cut : cut.slice(0, end + open).trimEnd();
}

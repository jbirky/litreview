import { mathTokens } from "./math.js";

// A few inline markdown marks (**bold**, *italic*, `code`) and LaTeX math as tokens, so
// model-written text is rendered by Svelte as text and never as HTML, except KaTeX's output.
const MARKS = /(\*\*[^*\n]+\*\*|\*[^*\s][^*\n]*\*|`[^`\n]+`)/g;

export function inlineTokens(text) {
  return mathTokens(text).flatMap((t) => (t.t === "math" ? [t] : markTokens(t.v)));
}

function markTokens(text) {
  const out = [];
  let last = 0;
  for (const m of text.matchAll(MARKS)) {
    if (m.index > last) out.push({ t: "text", v: text.slice(last, m.index) });
    const tok = m[0];
    if (tok.startsWith("**")) out.push({ t: "b", v: tok.slice(2, -2) });
    else if (tok.startsWith("`")) out.push({ t: "code", v: tok.slice(1, -1) });
    else out.push({ t: "i", v: tok.slice(1, -1) });
    last = m.index + tok.length;
  }
  if (last < text.length) out.push({ t: "text", v: text.slice(last) });
  return out;
}

// FTS snippets arrive with \u0002 ... \u0003 around matches.
export function snippetParts(s) {
  const out = [];
  let on = false;
  for (const part of (s || "").split(/([\u0002\u0003])/)) {
    if (part === "\u0002") on = true;
    else if (part === "\u0003") on = false;
    else if (part) out.push({ v: part, mark: on });
  }
  return out;
}

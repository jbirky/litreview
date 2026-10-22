// KaTeX's stylesheet offers each font as woff2, woff and ttf. Every browser the UI supports reads
// woff2, so the others are dropped: the shareable page inlines every font its stylesheet names.
export function katexWoff2Only() {
  return {
    name: "katex-woff2-only",
    enforce: "pre", // before Vite's CSS plugin turns the url()s into assets
    transform(code, id) {
      if (!/katex(\.min)?\.css(\?|$)/.test(id)) return null;
      return code.replace(/,\s*url\([^)]+\.(woff|ttf)\)\s*format\("(woff|truetype)"\)/g, "");
    },
  };
}

// Hash routing: #/ (reviews), #/review/<run>?tab=&cite=, #/paper/<bibcode>?src=&ver=&p=, #/library?...
function parse() {
  const raw = location.hash.replace(/^#/, "") || "/";
  const [path, qs] = raw.split("?");
  return {
    parts: path.split("/").filter(Boolean).map(decodeURIComponent),
    query: Object.fromEntries(new URLSearchParams(qs || "")),
  };
}

export const route = $state(parse());

// A new parts array only when the path changes: pages read their ids (route.parts[1]) in effects
// that reload everything, and a change of query alone, such as opening a card, mustn't set them off.
function apply(next) {
  if (next.parts.join("\u0000") !== route.parts.join("\u0000")) route.parts = next.parts;
  route.query = next.query;
}

window.addEventListener("hashchange", () => apply(parse()));

export function href(parts, query = {}) {
  const qs = new URLSearchParams(
    Object.entries(query).filter(([, v]) => v !== undefined && v !== null && v !== "" && v !== false),
  ).toString();
  return "#/" + parts.map((p) => encodeURIComponent(p)).join("/") + (qs ? `?${qs}` : "");
}

export function go(parts, query = {}, { replace = false } = {}) {
  const target = href(parts, query);
  if (replace) history.replaceState(null, "", target);
  else history.pushState(null, "", target);
  apply(parse());
}

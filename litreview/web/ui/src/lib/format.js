const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

export function money(x) {
  if (x === null || x === undefined) return "";
  if (x > 0 && x < 0.01) return "<$0.01";
  return `$${x.toFixed(2)}`;
}

export function day(ts) {
  if (!ts) return "";
  return new Date(ts * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

// '2017-09-13' or '2018-05-00' -> 'Sep 2017' / 'May 2018'
export function month(date) {
  if (!date || date.length < 7) return date || "";
  const m = parseInt(date.slice(5, 7), 10);
  return m >= 1 && m <= 12 ? `${MONTHS[m - 1]} ${date.slice(0, 4)}` : date.slice(0, 4);
}

export function authors(p) {
  if (!p.first_author) return "";
  if (p.n_authors === 1) return p.first_author;
  if (p.n_authors === 2 && p.authors) return `${p.first_author} & ${p.authors[1].split(",")[0]}`;
  return `${p.first_author} et al.`;
}

export const STATUS_TEXT = {
  done: "Done",
  paused: "Paused",
  failed: "Failed",
  running: "Running",
  queued: "Queued",
};

export const STAGES = ["plan", "search", "screen", "expand", "select", "fetch", "extract", "synthesize", "check", "render"];

// Rough cost of a review, from the first live run: planning and Haiku screening a few cents,
// Sonnet extraction about $0.08 a full-text paper ($0.04 through Batches), and Opus drafting and
// checks, not yet measured, guessed at $0.5-1.5.
export function estimateCost({ max_papers, use_batch, abstracts_only }) {
  const perPaper = abstracts_only ? 0.01 : use_batch ? 0.04 : 0.08;
  const fixed = 0.06 + (Number(max_papers) || 0) * perPaper;
  return [fixed + 0.5, fixed + 1.5];
}

// The settings a rerun of `source` changes, comparing the form's values with the review's own.
export function rerunChanges(form, source) {
  const value = (v) => (v === "" || v === null || v === undefined ? null : typeof v === "string" ? Number(v) : v);
  return Object.keys(source.rerun_from).filter((key) => value(form[key]) !== value(source.settings[key]));
}

// The stage a rerun starts at: the earliest that a changed setting affects (run.rerun_from, which is
// pipeline.RERUN_FROM), or fetch, which reads each paper's newest text.
export function rerunStart(form, source) {
  const at = rerunChanges(form, source).map((key) => STAGES.indexOf(source.rerun_from[key]));
  return STAGES[Math.min(STAGES.indexOf("fetch"), ...at)];
}

// Rough cost of running a review again: planning and screening only when it searches again, reading
// only the papers whose cards can't be copied (all of them when it switches between abstracts and full
// text, otherwise any beyond the papers read before), then drafting as for added papers.
export function estimateRerunCost(form, source, start) {
  const n = STAGES.indexOf(start) > STAGES.indexOf("select") ? source.selected : Number(form.max_papers) || 0;
  const perPaper = form.abstracts_only ? 0.01 : form.use_batch ? 0.04 : 0.08;
  const reading = (form.abstracts_only !== source.settings.abstracts_only ? n : Math.max(0, n - source.cards)) * perPaper;
  const finding = STAGES.indexOf(start) <= STAGES.indexOf("expand") ? 0.06 : 0;
  return [finding + reading + 0.5 + n * 0.08, finding + reading + 0.8 + n * 0.13];
}

// Calls are refused when their worst case would pass the cap; the drafting call's worst case is
// about $1.65, so leave that much room above the estimate.
export function suggestedCap(high) {
  return Math.ceil(high + 1.5);
}

// Rough cost of reading more papers for a finished review and writing it again, from the first
// two 20-paper reviews: Sonnet extraction as above, then drafting, checks and the revision on Opus,
// $2.1-3.3 for 20 papers, which grows with the number of cards.
export function estimateAddCost(added, read, use_batch) {
  const reading = added * (use_batch ? 0.04 : 0.08);
  const papers = added + read;
  return [reading + 0.5 + papers * 0.08, reading + 0.8 + papers * 0.13];
}

<script>
  import { get } from "../lib/api.js";
  import { go, href } from "../lib/router.svelte.js";
  import { authors } from "../lib/format.js";
  import { snippetParts } from "../lib/inline.js";
  import Histogram from "../components/Histogram.svelte";
  import MathText from "../components/MathText.svelte";
  import PaperTask from "../components/PaperTask.svelte";

  let { query } = $props();
  const PAGE = 100;
  const STATUSES = [
    ["read", "Read in full"],
    ["carded", "With cards"],
    ["cited", "Cited"],
    ["screened", "Screened"],
    ["found", "Found only"],
    ["all", "All"],
  ];
  const SORTS = [
    ["year", "Year"],
    ["author", "First author"],
    ["title", "Title"],
    ["citations", "Citations"],
    ["score", "Best screening score"],
    ["relevance", "Search relevance"],
  ];

  // Filters live in the URL so back/forward and links keep them.
  let mode = $derived(query.inside === "1" ? "inside" : "table");
  let status = $derived(query.status || "read");
  let q = $derived(query.q || "");
  let sort = $derived(query.sort || (q ? "relevance" : "year"));
  let desc = $derived(query.desc !== "0");
  let page = $derived(Number(query.page || 0));
  let yearFrom = $derived(query.from ? Number(query.from) : null);
  let yearTo = $derived(query.to ? Number(query.to) : null);

  let data = $state(null);
  let hits = $state(null);
  let canRun = $state(false);
  get("/api/overview").then((o) => (canRun = o.can_run && !o.missing_keys.length)).catch(() => {});
  let error = $state("");
  let typed = $state("");
  let timer;

  // Follow the URL (back/forward, links) except while the box is being typed in.
  $effect(() => {
    const fromUrl = query.q || "";
    if (document.activeElement?.getAttribute("type") !== "search") typed = fromUrl;
  });

  function set(changes, replace = true) {
    go(["library"], { ...query, page: undefined, ...changes }, { replace });
  }

  let params = $derived({ status, q, sort, desc: desc ? undefined : "false", year_from: yearFrom, year_to: yearTo,
                          refereed: query.refereed === "1" ? "true" : undefined, offset: page * PAGE, limit: PAGE });

  function loadRows() {
    return get("/api/library", params).then((d) => (data = d)).catch((e) => (error = e.message));
  }

  $effect(() => {
    error = "";
    if (mode === "inside") {
      hits = null;
      if (!q.trim()) { hits = []; return; }
      get("/api/search", { q, limit: 100 }).then((h) => (hits = h)).catch((e) => (error = e.message));
    } else {
      params;
      loadRows();
    }
  });

  // While any shown paper has a fetch or summary in progress, keep its row current.
  let working = $derived(data?.rows.some((r) => ["queued", "running"].includes(r.tasks?.fetch?.status) ||
                                                 ["queued", "running"].includes(r.tasks?.summarize?.status)));
  $effect(() => {
    if (!working || mode !== "table") return;
    const timer = setInterval(loadRows, 2500);
    return () => clearInterval(timer);
  });

  function onType(e) {
    typed = e.target.value;
    clearTimeout(timer);
    timer = setTimeout(() => set({ q: typed.trim() || undefined, sort: undefined }), 250);
  }
</script>

<h1>Library</h1>

<div class="controls">
  <div class="searchrow">
    <input type="search" placeholder={mode === "inside" ? "Search inside the papers that were read…" : "Search titles, authors and abstracts…"}
           value={typed} oninput={onType} aria-label="Search" />
    <div class="seg">
      <button class:on={mode === "table"} onclick={() => set({ inside: undefined })}>Papers</button>
      <button class:on={mode === "inside"} onclick={() => set({ inside: "1" })}>Inside papers</button>
    </div>
  </div>

  {#if mode === "table" && data}
    <div class="filters">
      <div class="seg">
        {#each STATUSES as [key, label]}
          <button class:on={status === key} onclick={() => set({ status: key })}>{label} <span class="count">{data.totals[key]}</span></button>
        {/each}
      </div>
      <label class="small"><input type="checkbox" checked={query.refereed === "1"} onchange={(e) => set({ refereed: e.target.checked ? "1" : undefined })} /> Refereed only</label>
      <label class="small">Sort
        <select value={sort} onchange={(e) => set({ sort: e.target.value })}>
          {#each SORTS as [key, label]}{#if key !== "relevance" || q}<option value={key}>{label}</option>{/if}{/each}
        </select>
      </label>
      {#if sort !== "relevance"}
        <button class="btn" onclick={() => set({ desc: desc ? "0" : undefined })} title="Reverse the order">{desc ? "↓" : "↑"}</button>
      {/if}
    </div>
    <div class="years">
      <Histogram years={data.years} from={yearFrom} to={yearTo} onRange={(a, b) => set({ from: a, to: b })} />
      <div class="small yearline">
        {#if yearFrom !== null || yearTo !== null}
          Years {yearFrom ?? "…"}–{yearTo ?? "…"} <button class="linkish" onclick={() => set({ from: undefined, to: undefined })}>all years</button>
        {:else}
          <span class="muted">Click a bar for one year, or drag across bars for a range.</span>
        {/if}
      </div>
    </div>
  {/if}
</div>

{#if error}
  <p class="callout">{error}</p>
{:else if mode === "inside"}
  {#if hits === null}
    <p class="muted">Searching…</p>
  {:else if !q.trim()}
    <p class="muted">Searches every stored paragraph of the papers read in full. Use quotes for a phrase.</p>
  {:else if !hits.length}
    <p class="muted">No paragraph matches.</p>
  {:else}
    <ul class="hits">
      {#each hits as h}
        <li class="card">
          <a href={href(["paper", h.paper.bibcode ?? h.bibcode], { tb: h.bibcode, src: h.source, ver: h.version, p: h.idx })}><MathText text={h.paper.title} /></a>
          <div class="muted small">{h.paper.year ?? ""} · {authors(h.paper)} · [{h.idx}] {h.label}</div>
          <p class="snippet">{#each snippetParts(h.snippet) as part}{#if part.mark}<mark>{part.v}</mark>{:else}{part.v}{/if}{/each}</p>
        </li>
      {/each}
    </ul>
  {/if}
{:else if !data}
  <p class="muted">Loading…</p>
{:else}
  <p class="small muted">{data.total} papers{q ? ` matching “${q}”` : ""}</p>
  <div class="table-wrap card">
    <table class="grid">
      <thead><tr><th>Year</th><th>First author</th><th>Title</th><th>Text</th><th>Summary</th><th>Reviews</th><th>Score</th><th>Cites</th></tr></thead>
      <tbody>
        {#each data.rows as r (r.bibcode)}
          <tr>
            <td class="nowrap">{r.year ?? "n.d."}</td>
            <td class="nowrap">{r.first_author}{r.n_authors > 1 ? " et al." : ""}</td>
            <td>
              <a href={href(["paper", r.bibcode])}><MathText text={r.title} /></a>
              <div class="muted small">{r.journal || "preprint"}</div>
            </td>
            <td class="small nowrap">
              {#if r.text}{r.text_version}{:else if canRun}<PaperTask bibcode={r.bibcode} kind="fetch" task={r.tasks.fetch}
                enabled={r.can_fetch} why="No arXiv version, and litreview reads full text only from arXiv" onQueued={loadRows} />{:else}—{/if}
            </td>
            <td class="small nowrap">
              {#if r.has_summary}<a href={href(["paper", r.bibcode])}>✓ summary</a>{:else if canRun}<PaperTask bibcode={r.bibcode} kind="summarize"
                task={r.tasks.summarize} enabled={r.can_fetch || !!r.text} why="No arXiv version to read, and summaries need the full text" onQueued={loadRows} />{:else}<span class="muted">—</span>{/if}
            </td>
            <td class="small nowrap">{#if r.cited}<span class="pill ok">cited {r.cited}</span>{:else if r.cards}{r.cards} card{r.cards > 1 ? "s" : ""}{:else if r.screened}screened{:else}<span class="muted">found</span>{/if}</td>
            <td class="nowrap">{r.best_score ?? "—"}</td>
            <td class="nowrap muted">{r.citation_count}</td>
          </tr>
        {/each}
      </tbody>
    </table>
  </div>
  {#if data.total > PAGE}
    <div class="pager small">
      <button class="btn" disabled={page === 0} onclick={() => set({ page: page - 1 || undefined }, false)}>← Previous</button>
      <span>{page * PAGE + 1}–{Math.min(data.total, (page + 1) * PAGE)} of {data.total}</span>
      <button class="btn" disabled={(page + 1) * PAGE >= data.total} onclick={() => set({ page: page + 1 }, false)}>Next →</button>
    </div>
  {/if}
{/if}

<style>
  .controls {
    display: grid;
    gap: 12px;
    margin-bottom: 16px;
  }
  .searchrow {
    display: flex;
    gap: 10px;
    flex-wrap: wrap;
  }
  .searchrow input {
    flex: 1;
    min-width: 220px;
  }
  .filters {
    display: flex;
    gap: 10px 16px;
    flex-wrap: wrap;
    align-items: center;
  }
  .count {
    opacity: 0.7;
    font-size: 12px;
  }
  .years {
    max-width: 720px;
  }
  .yearline {
    margin-top: 4px;
  }
  .hits {
    list-style: none;
    padding: 0;
    display: grid;
    gap: 10px;
  }
  .hits li {
    padding: 12px 14px;
  }
  .snippet {
    font-family: var(--serif);
    margin: 6px 0 0;
  }
  .pager {
    display: flex;
    gap: 12px;
    align-items: center;
    margin-top: 12px;
  }
</style>

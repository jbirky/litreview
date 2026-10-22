<script>
  import { get } from "../lib/api.js";
  import { go, href } from "../lib/router.svelte.js";
  import { day, money, STATUS_TEXT } from "../lib/format.js";
  import CitationGraph from "../components/CitationGraph.svelte";
  import EvidencePanel from "../components/EvidencePanel.svelte";
  import Markdown from "../components/Markdown.svelte";
  import MathText from "../components/MathText.svelte";
  import ReviewText from "../components/ReviewText.svelte";
  import RunControls from "../components/RunControls.svelte";
  import RunPapers from "../components/RunPapers.svelte";
  import Timeline from "../components/Timeline.svelte";

  let { runId, query } = $props();
  let run = $state(null);
  let review = $state(undefined);
  let papers = $state(null);
  let report = $state(undefined);
  let error = $state("");

  // Load the run; while it runs, RunControls calls this again, and a change of stage or status
  // refreshes the review, papers and report.
  async function load(id) {
    try {
      const r = await get(`/api/runs/${encodeURIComponent(id)}`);
      if (id !== runId) return;
      const changed = !run || run.stage !== r.stage || run.status !== r.status;
      run = r;
      if (changed) {
        review = (await get(`/api/runs/${encodeURIComponent(id)}/review`)).review;
        papers = null;
        report = undefined;
      }
    } catch (e) {
      error = e.message;
    }
  }

  $effect(() => {
    const id = runId;
    run = null;
    review = undefined;
    papers = null;
    report = undefined;
    error = "";
    load(id);
  });

  let tab = $derived(query.tab || (review === null ? "papers" : "review"));
  let cite = $derived(query.cite || null);

  $effect(() => {
    if (tab === "papers" && papers === null) get(`/api/runs/${encodeURIComponent(runId)}/papers`).then((p) => (papers = p));
    if (tab === "report" && report === undefined)
      get(`/api/runs/${encodeURIComponent(runId)}/report`).then((r) => (report = r.markdown));
  });

  let shareUrl = $derived(`/api/runs/${encodeURIComponent(runId)}/share`);

  // "2 more papers", "1 paper again, from its newer text" or "1 more paper and 1 again, from its newer text"
  function readAgain(a) {
    const again = a.reread?.length ?? 0;
    const more = a.added.length - again;
    const s = (n) => (n === 1 ? "" : "s");
    const from = `again, from ${again === 1 ? "its" : "their"} newer text`;
    if (!again) return `${more} more paper${s(more)}`;
    if (!more) return `${again} paper${s(again)} ${from}`;
    return `${more} more paper${s(more)} and ${again} ${from}`;
  }

  function setQuery(changes) {
    go(["review", runId], { ...query, ...changes }, { replace: true });
  }
  const TABS = [
    ["review", "Review"],
    ["timeline", "Timeline"],
    ["graph", "Citation graph"],
    ["papers", "Papers"],
    ["report", "Report"],
  ];
</script>

{#if error}
  <p class="callout">{error}</p>
{:else if !run}
  <p class="muted">Loading…</p>
{:else}
  <header class="head">
    <div class="crumbs small muted"><a href={href([])}>Reviews</a> ›</div>
    <h1 class="question">{run.question}</h1>
    <div class="meta small">
      <span class="pill {run.status}">{STATUS_TEXT[run.status] ?? run.status}{run.status !== "done" ? ` at ${run.stage}` : ""}</span>
      <span>{day(run.created_at)}</span>
      <span>{run.cards} read · {run.verified} with verified cards{review ? ` · ${review.stats.cited} cited` : ""}</span>
      {#if review}<span>{review.stats.full_text} in full text, {review.stats.abstract_only} from the abstract only</span>{/if}
      <span>{money(run.cost)}</span>
    </div>
    {#if run.interpretation}<p class="interp small"><strong>As interpreted:</strong> {run.interpretation}</p>{/if}
    {#each (run.additions ?? []).filter((a) => a.done && a.added?.length) as a}
      <p class="small muted rewritten">
        Written again on {day(a.at)} after reading {readAgain(a)}; the earlier version is in the run folder, in {a.previous}/.
      </p>
    {/each}
    {#if review}
      <p class="share small">
        <a class="btn" href="{shareUrl}?download=1" download
           title="One HTML file that opens in any browser, with no server: the review, the timeline, each cited paper's card with the paragraphs it cites, and refs.bib. Costs and the run report stay out.">Download shareable page</a>
        <a href={shareUrl} target="_blank" rel="noreferrer">Preview</a>
      </p>
    {/if}
    {#if run.status !== "done"}
      <RunControls {run} onChange={() => load(runId)} />
    {/if}
  </header>

  <nav class="tabs">
    {#each TABS as [key, label]}
      <a class:on={tab === key} href={href(["review", runId], { ...query, tab: key })}>{label}</a>
    {/each}
  </nav>

  <div class="layout" class:with-panel={!!cite}>
    <div class="main-col">
      {#if tab === "review"}
        {#if review}
          <ReviewText {review} selected={cite} citeHref={(b) => href(["review", runId], { ...query, cite: b })} />
          {#if review.references.length}
            <section class="refs">
              <h2>References</h2>
              <ol>
                {#each review.references as r}
                  <li>
                    <a class="cite-btn" href={href(["review", runId], { ...query, cite: r.bibcode })}>{r.label}</a>.
                    {r.authors.join("; ")}{r.et_al ? " et al." : ""} {r.cite_year}, {r.pub || r.journal}.
                    <em><MathText text={r.title} /></em> · <a href={href(["paper", r.bibcode])}>paper page</a>
                  </li>
                {/each}
              </ol>
            </section>
          {/if}
        {:else}
          <p class="callout info">The review is written at the end of the run. Papers read so far, and their cards, are on the Papers tab.</p>
        {/if}
      {:else if tab === "timeline"}
        {#if review}
          <Timeline {review} selected={cite} onCite={(b) => setQuery({ cite: b })} />
        {:else}
          <p class="callout info">The timeline appears once the review is written.</p>
        {/if}
      {:else if tab === "graph"}
        <CitationGraph {runId} {run} selected={cite} onCite={(b) => setQuery({ cite: b })} onChange={() => load(runId)}
                       reloadKey={`${run.status}|${run.stage}`} />
      {:else if tab === "papers"}
        {#if papers}
          <RunPapers {papers} {run} selected={cite} onCard={(b) => setQuery({ cite: b })} onChange={() => load(runId)} />
        {:else}
          <p class="muted">Loading…</p>
        {/if}
      {:else if tab === "report"}
        {#if report === undefined}
          <p class="muted">Loading…</p>
        {:else if report === null}
          <p class="callout info">report.md is written when the run finishes.</p>
        {:else}
          <div class="card report"><Markdown source={report} /></div>
        {/if}
      {/if}
    </div>
    {#if cite}
      <EvidencePanel run={runId} bibcode={cite} onClose={() => setQuery({ cite: undefined })} />
    {/if}
  </div>
{/if}

<style>
  .head {
    max-width: 900px;
  }
  .crumbs {
    margin-bottom: 4px;
  }
  .question {
    font-family: var(--serif);
    font-weight: 500;
    font-size: 28px;
  }
  .meta {
    display: flex;
    flex-wrap: wrap;
    gap: 6px 14px;
    align-items: center;
    color: var(--muted);
  }
  .interp {
    color: var(--ink-2);
    max-width: 80ch;
  }
  .rewritten {
    margin: 4px 0;
  }
  .share {
    display: flex;
    gap: 12px;
    align-items: center;
  }
  .share .btn:hover {
    text-decoration: none;
  }
  .layout {
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    gap: 28px;
    align-items: start;
  }
  .layout.with-panel {
    grid-template-columns: minmax(0, 1fr) minmax(320px, 420px);
  }
  .refs {
    max-width: 72ch;
    margin-top: 32px;
    font-size: 14px;
  }
  .refs ol {
    padding-left: 20px;
  }
  .refs li {
    margin-bottom: 6px;
  }
  .report {
    padding: 16px 20px;
  }
  @media (max-width: 900px) {
    .layout.with-panel {
      grid-template-columns: minmax(0, 1fr);
    }
  }
</style>

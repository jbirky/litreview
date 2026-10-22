<script>
  import { get } from "../lib/api.js";
  import { go, href } from "../lib/router.svelte.js";
  import { day, month } from "../lib/format.js";
  import CardItems from "../components/CardItems.svelte";
  import MathText from "../components/MathText.svelte";
  import PaperTask from "../components/PaperTask.svelte";
  import TextViewer from "../components/TextViewer.svelte";
  import UploadPdf from "../components/UploadPdf.svelte";

  let { bibcode, query } = $props();
  let paper = $state(null);
  let error = $state("");
  let open = $state({});
  let allAuthors = $state(false);

  let canRun = $state(false);
  let canUpload = $state(false);
  get("/api/overview").then((o) => {
    canRun = o.can_run && !o.missing_keys.length;
    canUpload = o.can_run;
  }).catch(() => {});

  function load(b) {
    return get("/api/papers", { bibcode: b }).then((p) => { if (b === bibcode) paper = p; }).catch((e) => (error = e.message));
  }
  $effect(() => {
    const b = bibcode;
    paper = null;
    error = "";
    load(b);
  });
  let fullText = $derived(paper?.texts.some((t) => t.source !== "abstract"));
  // Reviews read the newest full text; texts come newest first.
  let current = $derived(paper?.texts.find((t) => t.source !== "abstract"));
  let working = $derived(paper && Object.values(paper.tasks).some((t) => t && ["queued", "running"].includes(t.status)));
  $effect(() => {
    if (!working) return;
    const timer = setInterval(() => load(bibcode), 2500);
    return () => clearInterval(timer);
  });

  // The text viewer follows the URL, so a search hit or a passage link lands on its paragraph.
  let shown = $derived.by(() => {
    if (!paper) return null;
    if (query.src && query.ver) return { bibcode: query.tb || paper.texts.find((t) => t.source === query.src && t.version === query.ver)?.bibcode || paper.bibcode, source: query.src, version: query.ver };
    return null;
  });
  let focus = $derived(query.p !== undefined ? [Number(query.p)] : []);

  function openText(t, i) {
    go(["paper", bibcode], { tb: t.bibcode, src: t.source, ver: t.version, p: i }, { replace: true });
  }
  function closeText() {
    go(["paper", bibcode], {}, { replace: true });
  }
</script>

{#if error}
  <p class="callout">{error}</p>
{:else if !paper}
  <p class="muted">Loading…</p>
{:else}
  <div class="crumbs small muted"><a href={href(["library"])}>Library</a> ›</div>
  <h1 class="title"><MathText text={paper.title} /></h1>
  <p class="authors">
    {(allAuthors ? paper.authors : paper.authors.slice(0, 8)).join("; ")}
    {#if paper.authors.length > 8 && !allAuthors}<button class="linkish small" onclick={() => (allAuthors = true)}>and {paper.authors.length - 8} more</button>{/if}
  </p>
  <div class="meta small">
    {#if paper.refereed}<span class="pill ok">refereed</span>{/if}
    {#if paper.kind}<span class="pill">{paper.kind}</span>{:else if !paper.refereed}<span class="pill">preprint</span>{/if}
    <span>{paper.pub || paper.journal}{paper.pubdate ? `, ${month(paper.pubdate)}` : ""}</span>
    {#if paper.arxiv_id}<span>arXiv {paper.arxiv_id}{paper.first_arxiv_date ? `, first posted ${month(paper.first_arxiv_date)}` : ""}</span>{/if}
    <span>{paper.citation_count} citations</span>
    <span class="links">
      <a href={paper.links.ads} target="_blank" rel="noreferrer">ADS</a>
      {#if paper.links.arxiv} · <a href={paper.links.arxiv} target="_blank" rel="noreferrer">arXiv</a>{/if}
      {#if paper.links.doi} · <a href={paper.links.doi} target="_blank" rel="noreferrer">DOI</a>{/if}
    </span>
  </div>

  <div class="cols" class:reading={!!shown}>
    <div class="main-col">
      <section class="block card">
        <h2>Summary</h2>
        {#if paper.summary}
          <p class="muted small">
            The whole paper, not tied to any question. Written by {paper.summary.model} from the
            {paper.summary.text.label} on {day(paper.summary.created_at)}.
          </p>
          {#if !paper.summary.text.matches}<p class="callout small">The stored text has changed since, so its passages can't be shown.</p>{/if}
          <CardItems title="Aim" items={paper.summary.aim} showQuotes={false} onPassage={(i) => openText({ ...paper.summary.text, bibcode: paper.summary.bibcode }, i)} />
          <CardItems title="Methods" items={paper.summary.methods} showQuotes={false} onPassage={(i) => openText({ ...paper.summary.text, bibcode: paper.summary.bibcode }, i)} />
          <CardItems title="Results" items={paper.summary.results} showQuotes={false} onPassage={(i) => openText({ ...paper.summary.text, bibcode: paper.summary.bibcode }, i)} />
          <CardItems title="Limitations" items={paper.summary.limitations} showQuotes={false} onPassage={(i) => openText({ ...paper.summary.text, bibcode: paper.summary.bibcode }, i)} />
        {:else}
          <p class="muted small">None yet. A review that reads this paper's full text writes one, or write it now.</p>
          {#if canRun}
            <PaperTask bibcode={paper.bibcode} kind="summarize" task={paper.tasks.summarize} enabled={paper.can_fetch || fullText}
                       why="No arXiv version, ADS scan or open-access copy to read, and summaries need the full text: upload a PDF under Text" onQueued={() => load(bibcode)} />
          {/if}
        {/if}
      </section>

      <section class="block card">
        <h2>In your reviews</h2>
        {#if !paper.cards.length}
          <p class="muted small">No review has read this paper yet.</p>
        {/if}
        {#each paper.cards as c (c.run_id)}
          <div class="review-card">
            <div class="rc-head">
              <a class="rc-q" href={href(["review", c.run_id], { cite: c.bibcode })}>{c.question}</a>
              <div class="small meta">
                <span class="pill {c.status}">{c.status.replace("_", " ")}</span>
                {#if c.relevance}<span class="pill accent">relevance {c.relevance}</span>{/if}
                {#if c.cited}<span class="pill ok">cited</span>{/if}
                <span class="muted">{c.findings.length} findings · {c.limitations.length} limitations</span>
                <button class="linkish" onclick={() => (open[c.run_id] = !open[c.run_id])}>{open[c.run_id] ? "hide" : "show"}</button>
              </div>
            </div>
            {#if open[c.run_id]}
              {#if c.relevance_reason}<p class="small muted">{c.relevance_reason}</p>{/if}
              <CardItems title="Findings" items={c.findings} onPassage={(i) => openText({ ...c.text, bibcode: c.bibcode }, i)} />
              <CardItems title="Methods" items={c.methods} onPassage={(i) => openText({ ...c.text, bibcode: c.bibcode }, i)} />
              <CardItems title="Limitations" items={c.limitations} showSource onPassage={(i) => openText({ ...c.text, bibcode: c.bibcode }, i)} />
            {/if}
          </div>
        {/each}
      </section>

      <section class="block card">
        <h2>Text</h2>
        {#if !fullText}
          <p class="muted small">{paper.texts.length ? "Only the abstract is stored." : "No text stored. Only the record from ADS has been fetched."}</p>
          {#if canRun}
            <PaperTask bibcode={paper.bibcode} kind="fetch" task={paper.tasks.fetch} enabled={paper.can_fetch}
                       why="No arXiv version, ADS scan or open-access copy to fetch (Unpaywall is asked only when UNPAYWALL_EMAIL or LITREVIEW_CONTACT holds an email address): upload a PDF below" onQueued={() => load(bibcode)} />
          {/if}
        {/if}
        <ul class="texts small">
          {#each paper.texts as t}
            <li>
              {t.label}{#if t.origin} from <a href={t.origin} target="_blank" rel="noreferrer">{new URL(t.origin).hostname}</a>{/if} · {t.paragraphs} paragraphs · read {day(t.fetched_at)}
              {#if t.truncated}· cut at the length limit{/if}
              {#if t === current && paper.texts.filter((x) => x.source !== "abstract").length > 1}· <strong>the one reviews read</strong>{/if}
              <button class="linkish" onclick={() => openText(t, undefined)}>read the text</button>
            </li>
          {/each}
        </ul>
        {#if canUpload}<UploadPdf bibcode={paper.bibcode} onDone={() => load(bibcode)} />{/if}
      </section>

      <section class="block card">
        <h2>Screening</h2>
        {#if !paper.screening.length}<p class="muted small">Not screened by any review.</p>{/if}
        {#each paper.screening as s}
          <p class="small"><span class="pill {s.included ? 'ok' : ''}">score {s.score}</span> {s.reason}
            <span class="muted">— <a href={href(["review", s.run_id], { tab: "papers" })}>{s.question}</a>{s.round === "expand" ? ", via the citation graph" : ""}</span></p>
        {/each}
      </section>

      {#if paper.abstract}
        <section class="block card">
          <h2>Abstract</h2>
          <p class="abstract"><MathText text={paper.abstract} /></p>
          <p class="muted small">From {paper.abstract_source}.</p>
        </section>
      {/if}
    </div>

    {#if shown}
      <aside class="reader card">
        <header>
          <strong class="small">Stored text</strong>
          <button class="btn" onclick={closeText} aria-label="Close the text">✕</button>
        </header>
        <div class="reader-body">
          <TextViewer bibcode={shown.bibcode} source={shown.source} version={shown.version} {focus} />
        </div>
      </aside>
    {/if}
  </div>
{/if}

<style>
  .crumbs {
    margin-bottom: 4px;
  }
  .title {
    font-family: var(--serif);
    font-weight: 500;
    font-size: 26px;
    max-width: 900px;
  }
  .authors {
    color: var(--ink-2);
    margin: 0 0 8px;
    max-width: 900px;
  }
  .meta {
    display: flex;
    flex-wrap: wrap;
    gap: 6px 14px;
    align-items: center;
    color: var(--muted);
  }
  .cols {
    display: grid;
    grid-template-columns: minmax(0, 860px);
    gap: 24px;
    margin-top: 20px;
    align-items: start;
  }
  .cols.reading {
    grid-template-columns: minmax(0, 1fr) minmax(340px, 520px);
  }
  .block {
    padding: 14px 18px;
    margin-bottom: 14px;
  }
  .block h2 {
    font-size: 16px;
  }
  .review-card {
    padding: 10px 0;
    border-top: 1px solid var(--line);
  }
  .review-card:first-of-type {
    border-top: 0;
  }
  .rc-q {
    font-family: var(--serif);
    font-size: 16px;
  }
  .rc-head .meta {
    margin-top: 4px;
  }
  .texts {
    padding-left: 18px;
    margin: 0;
  }
  .abstract {
    font-family: var(--serif);
    line-height: 1.55;
  }
  .reader {
    position: sticky;
    top: 68px;
    max-height: calc(100vh - 88px);
    display: flex;
    flex-direction: column;
  }
  .reader header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 10px 14px;
    border-bottom: 1px solid var(--line);
  }
  .reader-body {
    overflow-y: auto;
    padding: 6px 14px 14px;
  }
  @media (max-width: 900px) {
    .cols.reading {
      grid-template-columns: minmax(0, 1fr);
    }
    .reader {
      position: fixed;
      inset: auto 0 0 0;
      top: auto;
      max-height: 75vh;
      z-index: 30;
      border-radius: 14px 14px 0 0;
    }
  }
</style>

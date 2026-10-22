<script>
  import { day } from "../lib/format.js";
  import { go, href, route } from "../lib/router.svelte.js";
  import CitationGraph from "../components/CitationGraph.svelte";
  import EvidencePanel from "../components/EvidencePanel.svelte";
  import MathText from "../components/MathText.svelte";
  import ReviewText from "../components/ReviewText.svelte";
  import RunPapers from "../components/RunPapers.svelte";
  import Timeline from "../components/Timeline.svelte";

  // A review exported as one file (litreview/web/share.py). Everything it shows came with the page;
  // the hash keeps the tab and the open card, as on the review page.
  let { data } = $props();
  let { run, review, cards } = $derived(data);

  const TABS = [
    ["review", "Review"],
    ["timeline", "Timeline"],
    ["graph", "Citation graph"],
    ["papers", "Papers"],
  ];
  let tab = $derived(TABS.some(([key]) => key === route.parts[0]) ? route.parts[0] : "review");
  let cite = $derived(cards[route.query.cite] ? route.query.cite : null);

  const citeHref = (b) => href([tab], { cite: b });
  const openCard = (b) => go([tab], { cite: b }, { replace: true });
  const ads = (b) => `https://ui.adsabs.harvard.edu/abs/${encodeURIComponent(b)}/abstract`;
  let bibHref = $derived(data.bib ? "data:application/x-bibtex;charset=utf-8," + encodeURIComponent(data.bib) : null);
</script>

<header class="topbar">
  <span class="brand">litreview</span>
  <span class="spend">Shared {day(data.exported_at)}</span>
</header>

<main>
  <header class="head">
    <h1 class="question">{run.question}</h1>
    <div class="meta small">
      <span>{day(run.created_at)}</span>
      <span>{review.stats.cited} cited, from {review.stats.read} read ({review.stats.full_text} in full text, {review.stats.abstract_only} from the abstract only) of {run.screened} screened</span>
      {#if run.since || run.until}<span>searched {run.since ?? "earliest"}–{run.until ?? "present"}</span>{/if}
      {#if bibHref}<a href={bibHref} download="refs.bib">refs.bib</a>{/if}
    </div>
    {#if run.interpretation}<p class="interp small"><strong>As interpreted:</strong> {run.interpretation}</p>{/if}
    <details class="about small">
      <summary>How this review was written</summary>
      <p>
        Claude searched NASA ADS with litreview, read the papers it selected, and wrote this review from the evidence cards
        it took from them. Every citation is to one of those papers: click one to see the paper's card, and a passage under
        a card item to see the paragraph it came from. The <strong>Citation graph</strong> tab shows which of the papers read
        cite each other, and <strong>Papers</strong> lists every paper screened, with the score and reason for keeping or
        leaving it out.
      </p>
      {#if run.queries.length}
        <p>ADS queries:</p>
        <ul>
          {#each run.queries as q}<li><code>{q}</code></li>{/each}
        </ul>
      {/if}
    </details>
  </header>

  <nav class="tabs">
    {#each TABS as [key, label]}
      <a class:on={tab === key} href={href([key], { cite })}>{label}</a>
    {/each}
  </nav>

  <div class="layout" class:with-panel={!!cite}>
    <div class="main-col">
      {#if tab === "review"}
        <ReviewText {review} selected={cite} {citeHref} />
        {#if review.references.length}
          <section class="refs">
            <h2>References</h2>
            <ol>
              {#each review.references as r}
                <li>
                  <a class="cite-btn" href={citeHref(r.bibcode)}>{r.label}</a>.
                  {r.authors.join("; ")}{r.et_al ? " et al." : ""} {r.cite_year}, {r.pub || r.journal}.
                  <em><MathText text={r.title} /></em> · <a href={ads(r.bibcode)} target="_blank" rel="noreferrer">ADS</a>
                  {#if r.arxiv_id}· <a href="https://arxiv.org/abs/{r.arxiv_id}" target="_blank" rel="noreferrer">arXiv</a>{/if}
                </li>
              {/each}
            </ol>
          </section>
        {/if}
      {:else if tab === "timeline"}
        <Timeline {review} selected={cite} onCite={openCard} />
      {:else if tab === "graph"}
        <CitationGraph shared={data.graph} selected={cite} onCite={openCard} />
      {:else}
        <RunPapers papers={data.papers} shared selected={cite} onCard={openCard} />
      {/if}
    </div>
    {#if cite}
      <EvidencePanel bibcode={cite} shared={cards[cite]} onClose={() => go([tab], {}, { replace: true })} />
    {/if}
  </div>

  <p class="small muted foot">Written with litreview {data.version} · run {run.run_id}</p>
</main>

<style>
  .head {
    max-width: 900px;
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
  .interp,
  .about {
    color: var(--ink-2);
    max-width: 80ch;
  }
  .about {
    margin-top: 10px;
  }
  .about summary {
    cursor: pointer;
    color: var(--accent);
  }
  .about code {
    overflow-wrap: anywhere;
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
  .foot {
    margin-top: 40px;
  }
  @media (max-width: 900px) {
    .layout.with-panel {
      grid-template-columns: minmax(0, 1fr);
    }
  }
</style>

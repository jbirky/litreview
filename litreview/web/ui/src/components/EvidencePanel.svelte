<script>
  import { get } from "../lib/api.js";
  import { href } from "../lib/router.svelte.js";
  import { authors } from "../lib/format.js";
  import CardItems from "./CardItems.svelte";
  import MathText from "./MathText.svelte";
  import TextViewer from "./TextViewer.svelte";

  // One paper's card for this run, beside the review. A passage opens the stored text in place.
  // A shared page passes the card itself, with only the paragraphs it cites (litreview/web/share.py).
  let { run, bibcode, onClose, shared = null } = $props();
  let card = $state(null);
  let error = $state("");
  let focus = $state(null);

  $effect(() => {
    const b = bibcode;
    card = null;
    error = "";
    focus = null;
    if (shared) {
      card = shared;
      return;
    }
    get("/api/cards", { run, bibcode: b })
      .then((c) => {
        if (c.bibcode === bibcode) card = c;
      })
      .catch((e) => (error = e.message));
  });
</script>

<aside class="panel card" aria-label="Evidence">
  <header class="panel-head">
    {#if card}
      <div>
        <div class="panel-title">{authors(card.paper)} {card.paper.year ?? ""}</div>
        <div class="muted small">{card.paper.journal || "preprint"}{#if !shared} · <a href={href(["paper", card.bibcode])}>paper page</a>{/if}</div>
      </div>
    {:else}
      <div class="panel-title">Evidence</div>
    {/if}
    <button class="btn close" onclick={onClose} aria-label="Close">✕</button>
  </header>

  <div class="panel-body">
    {#if error}
      <p class="callout">{error}</p>
    {:else if !card}
      <p class="muted">Loading the card…</p>
    {:else if focus !== null}
      <button class="linkish small" onclick={() => (focus = null)}>← Back to the card</button>
      {#if shared}
        {@const para = card.paragraphs[focus]}
        <div class="textview">
          <p class="muted small">{card.text.label} · one paragraph of the text the model read</p>
          {#if para}
            <div class="para focus" data-i={focus}>
              <div class="para-label">[{focus}] {para.label}</div>
              <p><MathText text={para.text} /></p>
            </div>
          {:else}
            <p class="callout small">This paragraph isn't in the shared page.</p>
          {/if}
        </div>
      {:else}
        <TextViewer bibcode={card.bibcode} source={card.text.source} version={card.text.version} focus={[focus]} />
      {/if}
    {:else}
      <p class="small paper-title"><MathText text={card.paper.title} /></p>
      <div class="meta small">
        <span class="pill {card.status}">{card.status.replace("_", " ")}</span>
        {#if card.relevance}<span class="pill accent">relevance {card.relevance}</span>{/if}
        <span class="muted">read: {card.text.label}</span>
      </div>
      {#if card.relevance_reason}<p class="small muted">{card.relevance_reason}</p>{/if}
      {#if !card.text.matches}
        <p class="callout small">The stored text no longer matches the one this card was checked against, so its passages can't be shown.</p>
      {/if}
      <CardItems title="Findings" items={card.findings} onPassage={(i) => (focus = i)} />
      <CardItems title="Methods" items={card.methods} onPassage={(i) => (focus = i)} />
      <CardItems title="Limitations" items={card.limitations} showSource onPassage={(i) => (focus = i)} />
      {#if card.limitations_note}<p class="small muted">Limitations: {card.limitations_note}</p>{/if}
      {#if card.note}<p class="small muted">Notes: {card.note}</p>{/if}
      <p class="small links">
        <a href={card.links.ads} target="_blank" rel="noreferrer">ADS</a>
        {#if card.links.arxiv}· <a href={card.links.arxiv} target="_blank" rel="noreferrer">arXiv</a>{/if}
      </p>
    {/if}
  </div>
</aside>

<style>
  .panel {
    display: flex;
    flex-direction: column;
    max-height: calc(100vh - 88px);
    position: sticky;
    top: 68px;
  }
  .panel-head {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 12px;
    padding: 12px 14px;
    border-bottom: 1px solid var(--line);
  }
  .panel-title {
    font-weight: 600;
  }
  .panel-body {
    padding: 4px 14px 16px;
    overflow-y: auto;
  }
  .paper-title {
    font-family: var(--serif);
    font-size: 15px;
    margin: 10px 0 8px;
  }
  .meta {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    align-items: center;
  }
  .close {
    padding: 2px 8px;
  }
  .links {
    margin-top: 16px;
  }
  @media (max-width: 900px) {
    .panel {
      position: fixed;
      inset: auto 0 0 0;
      top: auto;
      max-height: 75vh;
      z-index: 30;
      border-radius: 14px 14px 0 0;
      box-shadow: 0 -8px 30px rgb(0 0 0 / 0.18);
    }
  }
</style>

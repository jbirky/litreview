<script>
  import { get, post } from "../lib/api.js";
  import { go, href } from "../lib/router.svelte.js";
  import { day, estimateCost, estimateRerunCost, money, rerunChanges, rerunStart, suggestedCap } from "../lib/format.js";

  // With `from`, the form runs that finished review again as a new review of the same question,
  // copying what the changed settings don't affect (pipeline.Run.rerun).
  let { from = null } = $props();

  let overview = $state(null);
  get("/api/overview").then((o) => (overview = o)).catch(() => {});

  let form = $state({
    question: "",
    since: "",
    until: "",
    max_papers: 20,
    refereed_only: false,
    expand: true,
    abstracts_only: false,
    use_batch: true,
  });
  let cap = $state(4);
  let capTouched = $state(false);
  let error = $state("");
  let busy = $state(false);

  let source = $state(null);
  if (from)
    get(`/api/runs/${encodeURIComponent(from)}`)
      .then((r) => {
        const s = r.settings;
        form = { question: r.question, since: s.since ?? "", until: s.until ?? "", max_papers: s.max_papers,
                 refereed_only: s.refereed_only, expand: s.expand, abstracts_only: s.abstracts_only, use_batch: s.use_batch };
        source = r;
      })
      .catch((e) => (error = e.message));

  let start = $derived(source ? rerunStart(form, source) : null);
  let changed = $derived(source ? rerunChanges(form, source) : []);
  // What a rerun starting at each stage takes from the review, besides the cards of papers read from the same text.
  const REUSED = {
    search: "its plan: the interpretation and the queries, searched again with the new years or filter. Papers it screened keep its scores",
    expand: "its plan, its searches and its screening; the citation graph is followed again, and papers it screened there keep its scores",
    select: "its plan, searches, screening and citation-graph expansion; the papers to read are chosen again",
    fetch: "its plan, searches, screening, citation-graph expansion and choice of papers",
  };

  let estimate = $derived(source ? estimateRerunCost(form, source, start) : estimateCost(form));
  $effect(() => {
    const suggestion = suggestedCap(estimate[1]);
    if (!capTouched) cap = suggestion;
  });
  let blocked = $derived((overview && (!overview.can_run || overview.missing_keys.length)) || (from && !source));

  async function submit(e) {
    e.preventDefault();
    busy = true;
    error = "";
    try {
      const settings = {
        ...form,
        since: form.since ? Number(form.since) : null,
        until: form.until ? Number(form.until) : null,
        max_papers: Number(form.max_papers),
        cost_cap: Number(cap),
      };
      delete settings.question;
      const run = source
        ? await post(`/api/runs/${encodeURIComponent(source.run_id)}/rerun`, settings)
        : await post("/api/runs", { ...settings, question: form.question.trim() });
      go(["review", run.run_id]);
    } catch (err) {
      error = err.message;
      busy = false;
    }
  }
</script>

<div class="crumbs small muted">
  <a href={href([])}>Reviews</a> ›
  {#if from}<a href={href(["review", from])}>{source ? `Review of ${day(source.created_at)}` : "Review"}</a> ›{/if}
</div>
<h1>{from ? "Run this review again" : "New review"}</h1>
{#if from}
  <p class="intro">
    A new review of the same question. Change any settings below. Whatever they don't affect is copied
    from the review, along with the card of each paper read from the same text as before, so it isn't paid
    for twice. The review is then drafted and checked again. It stays as it is.
  </p>
{/if}

{#if overview && !overview.can_run}
  <p class="callout">This server was started read-only (<code>litreview web --read-only</code>), so it can't run reviews.</p>
{:else if overview?.missing_keys.length}
  <p class="callout">
    The server has no {overview.missing_keys.join(" or ")}, so a review would fail at its first step. In the docker stack,
    give the <code>litreview-web</code> service <code>env_file: ./litreview/.env</code>; with <code>litreview web</code>, put them in <code>litreview/.env</code>.
  </p>
{/if}

<form class="card form" onsubmit={submit}>
  <label class="field">
    <span>Question</span>
    <textarea bind:value={form.question} rows="3" minlength="3" maxlength="500" required readonly={!!from}
              placeholder="What drives radius inflation in hot Jupiters?"></textarea>
  </label>

  <div class="grid">
    <label class="field"><span>From year</span><input type="number" min="1900" max="2100" bind:value={form.since} placeholder="any" /></label>
    <label class="field"><span>To year</span><input type="number" min="1900" max="2100" bind:value={form.until} placeholder="any" /></label>
    <label class="field"><span>Papers to read</span><input type="number" min="1" max="100" bind:value={form.max_papers} required /></label>
  </div>

  <fieldset class="checks">
    <label><input type="checkbox" bind:checked={form.expand} /> Follow the citation graph <span class="muted small">— also screens papers that cite or are cited by the best matches</span></label>
    <label><input type="checkbox" bind:checked={form.use_batch} /> Use the Batches API <span class="muted small">— half the cost of reading papers, but that step can take up to an hour</span></label>
    <label><input type="checkbox" bind:checked={form.refereed_only} /> Refereed papers only</label>
    <label><input type="checkbox" bind:checked={form.abstracts_only} /> Abstracts only <span class="muted small">— much cheaper, but cards come from abstracts, not full text</span></label>
  </fieldset>

  {#if source}
    <p class="reuse small">
      {#if changed.length}
        Starts at <strong>{start}</strong>, copying {REUSED[start]}.
      {:else}
        Nothing changed, so it copies {REUSED.fetch}, reads each paper's newest text (such as a PDF uploaded
        since), and copies the card of every paper whose text is the same.
      {/if}
    </p>
  {/if}

  <div class="cost">
    <label class="field cap"><span>Cost cap ($)</span>
      <input type="number" min="0.5" max="100" step="0.5" bind:value={cap} oninput={() => (capTouched = true)} required />
    </label>
    <p class="small muted">
      Estimated {money(estimate[0])}–{money(estimate[1])}{source ? ", assuming each paper whose text changes is read again" : ""}. A rough guess until more reviews have run.
      The run pauses before any call whose worst case would pass the cap, and drafting alone has a
      worst case of about $1.65, so leave some room. You can raise the cap and continue later.
      {#if overview}This month: {money(overview.month_spend)}{overview.monthly_credits ? ` of ${money(overview.monthly_credits)}` : ""} spent.{/if}
    </p>
  </div>

  {#if error}<p class="callout small">{error}</p>{/if}
  <div class="actions">
    <button class="btn primary" disabled={busy || blocked}>{busy ? "Starting…" : source ? "Run it again" : "Start review"}</button>
    <span class="muted small">It's queued and runs in the background; you can close the page.</span>
  </div>
</form>

<style>
  .crumbs {
    margin-bottom: 4px;
  }
  .intro {
    max-width: 760px;
    color: var(--ink-2);
  }
  .reuse {
    margin: 0;
    padding: 10px 12px;
    border-radius: 7px;
    background: var(--surface-2);
    border: 1px solid var(--line-2);
  }
  textarea[readonly] {
    color: var(--ink-2);
    resize: none;
  }
  .form {
    padding: 18px 20px;
    max-width: 760px;
    display: grid;
    gap: 16px;
  }
  .field {
    display: grid;
    gap: 4px;
    font-weight: 550;
    font-size: 14px;
  }
  .field input,
  .field textarea {
    font-weight: 400;
  }
  textarea {
    font: 17px/1.45 var(--serif);
    color: inherit;
    background: var(--surface);
    border: 1px solid var(--line-2);
    border-radius: 7px;
    padding: 8px 10px;
    resize: vertical;
  }
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
    gap: 12px;
  }
  .grid input,
  .cap input {
    width: 100%;
    max-width: 160px;
  }
  .checks {
    border: 0;
    padding: 0;
    margin: 0;
    display: grid;
    gap: 8px;
    font-size: 14px;
  }
  .cost p {
    margin: 6px 0 0;
  }
  .actions {
    display: flex;
    gap: 12px;
    align-items: center;
    flex-wrap: wrap;
  }
</style>

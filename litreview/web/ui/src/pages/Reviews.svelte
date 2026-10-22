<script>
  import { get } from "../lib/api.js";
  import { href } from "../lib/router.svelte.js";
  import { day, money, STATUS_TEXT } from "../lib/format.js";

  let runs = $state(null);
  let error = $state("");

  function load() {
    get("/api/runs").then((r) => (runs = r)).catch((e) => (error = e.message));
  }
  load();
  // Keep queued and running reviews current.
  $effect(() => {
    const timer = setInterval(() => {
      if (runs?.some((r) => r.status === "queued" || r.status === "running")) load();
    }, 3000);
    return () => clearInterval(timer);
  });
</script>

<div class="head">
  <h1>Reviews</h1>
  <a class="btn primary" href={href(["new"])}>New review</a>
</div>
{#if error}
  <p class="callout">{error}</p>
{:else if !runs}
  <p class="muted">Loading…</p>
{:else if !runs.length}
  <p class="callout info">No reviews yet. <a href={href(["new"])}>Start one</a>, or run <code>litreview run "&lt;question&gt;"</code>.</p>
{:else}
  <div class="list">
    {#each runs as r (r.run_id)}
      <a class="run card" href={href(["review", r.run_id])}>
        <div class="q">{r.question}</div>
        <div class="meta small">
          <span class="pill {r.status}">{STATUS_TEXT[r.status] ?? r.status}{r.status !== "done" ? ` at ${r.stage}` : ""}</span>
          <span>{day(r.created_at)}</span>
          <span>{r.cards} read · {r.verified} verified{r.has_review ? ` · ${r.cited} cited` : ""}</span>
          <span>{money(r.cost)}</span>
        </div>
      </a>
    {/each}
  </div>
{/if}

<style>
  .head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    max-width: 860px;
    gap: 12px;
  }
  .head .btn:hover {
    text-decoration: none;
  }
  .list {
    display: grid;
    gap: 10px;
    max-width: 860px;
  }
  .run {
    display: block;
    padding: 14px 16px;
    color: var(--ink);
  }
  .run:hover {
    text-decoration: none;
    border-color: var(--accent);
  }
  .q {
    font-family: var(--serif);
    font-size: 18px;
    margin-bottom: 6px;
  }
  .meta {
    display: flex;
    flex-wrap: wrap;
    gap: 6px 14px;
    align-items: center;
    color: var(--muted);
  }
</style>

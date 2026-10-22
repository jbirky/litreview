<script>
  import { post } from "../lib/api.js";
  import { href } from "../lib/router.svelte.js";
  import { authors } from "../lib/format.js";
  import MathText from "./MathText.svelte";

  // Find papers in ADS to add to a finished review: bibcodes, arXiv IDs, DOIs or links to them,
  // "Kelly et al. 2009", or any ADS query. Each result says what the review made of it.
  let { run, picked, onToggle } = $props();
  let q = $state("");
  let found = $state(null);
  let busy = $state(false);
  let error = $state("");

  const STATUS = {
    read: "already read for this review",
    included: "included by screening, not read",
    excluded: "screened out",
    unscreened: "found by the review's searches, not screened",
    in_library: "in the library, not found by this review's searches",
    new: "not in the library yet",
  };

  async function find(e) {
    e.preventDefault();
    busy = true;
    error = "";
    try {
      found = await post(`/api/runs/${encodeURIComponent(run.run_id)}/lookup`, { q });
    } catch (err) {
      error = err.message;
      found = null;
    } finally {
      busy = false;
    }
  }
</script>

<form class="find" onsubmit={find}>
  <input type="search" bind:value={q} maxlength="500" aria-label="Find papers in ADS"
         placeholder="Bibcode, arXiv ID, DOI, “Kelly et al. 2009”, or an ADS query" />
  <button class="btn" disabled={busy || !q.trim()}>{busy ? "Searching…" : "Find in ADS"}</button>
</form>
{#if error}<p class="err small">{error}</p>{/if}
{#if found}
  {#if found.not_found.length}<p class="small muted">ADS has no record of {found.not_found.join(", ")}.</p>{/if}
  {#if !found.results.length}
    <p class="small muted">Nothing found.</p>
  {:else if found.total > found.results.length}
    <p class="small muted">{found.total} found; showing the first {found.results.length}. Add words, or a year, to narrow it.</p>
  {/if}
  <ul class="results">
    {#each found.results as p (p.bibcode)}
      <li>
        <input type="checkbox" disabled={p.status === "read"} checked={p.status === "read" || picked.includes(p.bibcode)}
               onchange={() => onToggle(p)} aria-label="Add {p.title} to the review" />
        <div>
          <div>{#if p.status === "new"}<MathText text={p.title} />{:else}<a href={href(["paper", p.bibcode])}><MathText text={p.title} /></a>{/if}</div>
          <div class="small muted">
            {p.year ?? "n.d."} · {authors(p)} · {p.journal || "preprint"} · {p.citation_count} citations ·
            <a href={p.ads} target="_blank" rel="noreferrer">ADS</a>
          </div>
          <div class="small">
            {STATUS[p.status] ?? p.status}{p.score != null ? ` (score ${p.score})` : ""}{p.reason ? `: ${p.reason}` : ""}
            {#if p.cited_by}<span class="pill accent">cited by {p.cited_by} of the papers read</span>{/if}
          </div>
        </div>
      </li>
    {/each}
  </ul>
{/if}

<style>
  .find {
    display: flex;
    gap: 8px;
  }
  .find input {
    flex: 1;
    min-width: 0;
  }
  .results {
    list-style: none;
    padding: 0;
    margin: 0;
    display: grid;
    gap: 10px;
    max-height: 420px;
    overflow-y: auto;
  }
  .results li {
    display: grid;
    grid-template-columns: 20px minmax(0, 1fr);
    gap: 8px;
    align-items: start;
  }
  .results input {
    margin-top: 3px;
  }
  .err {
    color: var(--bad);
  }
</style>

<script>
  import { post } from "../lib/api.js";
  import { estimateAddCost, money, suggestedCap } from "../lib/format.js";

  // Read the chosen papers for a finished review and write it again, from every card. rereading:
  // how many of them it read already and reads again, from a newer text.
  let { run, bibcodes, names = {}, rereading = 0, onDone, onClear } = $props();
  let useBatch = $state(false);
  let cap = $state("");
  let busy = $state(false);
  let error = $state("");

  let estimate = $derived(estimateAddCost(bibcodes.length, run.cards, useBatch));
  // Room above what the run has spent for these calls, and for the drafting call's worst case.
  $effect(() => {
    cap = suggestedCap(run.cost + estimate[1]);
  });
  let n = $derived(bibcodes.length);
  let heading = $derived(
    !rereading ? `Read ${n} more paper${n === 1 ? "" : "s"}`
    : rereading === n ? `Read ${n === 1 ? "a paper" : `${n} papers`} again, from ${n === 1 ? "its" : "their"} newer text,`
    : `Read ${n} papers, ${rereading} of them again from a newer text,`,
  );
  let next = $derived(`v${(run.additions ?? []).filter((a) => a.previous).length + 1}`);

  async function add(e) {
    e.preventDefault();
    busy = true;
    error = "";
    try {
      const detail = await post(`/api/runs/${encodeURIComponent(run.run_id)}/add`,
        { bibcodes, cost_cap: Number(cap), use_batch: useBatch });
      await onDone(detail);
    } catch (err) {
      error = err.message;
    } finally {
      busy = false;
    }
  }
</script>

<form class="add card" onsubmit={add} aria-live="polite">
  <div class="row">
    <strong>{heading} and write the review again</strong>
    <span class="small muted">about {money(estimate[0])}–{money(estimate[1])}</span>
  </div>
  <p class="small">{bibcodes.map((b) => names[b] ?? b).join("; ")}</p>
  <p class="small muted">
    The review is rewritten from all {run.cards + n - rereading} cards, so its text can change throughout. The current version
    stays in the run folder, in {next}/.
  </p>
  <div class="row">
    <label class="small">Cost cap $ <input type="number" min="0.5" max="100" step="0.5" bind:value={cap} required /></label>
    <span class="small muted">{money(run.cost)} spent so far</span>
    <label class="small"><input type="checkbox" bind:checked={useBatch} /> Use Batches <span class="muted">— half the cost of reading, can take up to an hour</span></label>
  </div>
  <div class="row">
    <button class="btn primary" disabled={busy}>Add and rewrite</button>
    <button type="button" class="btn" disabled={busy} onclick={onClear}>Clear</button>
    {#if error}<span class="err small">{error}</span>{/if}
  </div>
</form>

<style>
  .add {
    padding: 12px 16px;
    margin: 12px 0;
    display: grid;
    gap: 8px;
    border-color: var(--accent);
  }
  .add p {
    margin: 0;
  }
  .row {
    display: flex;
    flex-wrap: wrap;
    gap: 8px 16px;
    align-items: center;
  }
  .err {
    color: var(--bad);
  }
</style>

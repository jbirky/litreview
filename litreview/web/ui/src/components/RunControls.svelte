<script>
  import { tick, untrack } from "svelte";
  import { get, post } from "../lib/api.js";
  import { money, STAGES } from "../lib/format.js";

  // Progress and controls for a run that isn't done: watch it, pause it, or continue it with a new cap.
  let { run, onChange } = $props();
  let events = $state([]);
  let after = 0;
  let error = $state("");
  let busy = $state(false);
  let cap = $state("");
  let capFor = null;
  let logBox = $state(null);

  let live = $derived(run.status === "queued" || run.status === "running");
  let stageIndex = $derived(STAGES.indexOf(run.stage));
  let spentShare = $derived(Math.min(100, (run.cost / (run.settings?.cost_cap || 1)) * 100));

  // Suggest a cap when the run (or why it paused) changes, but never overwrite what was typed.
  $effect(() => {
    const key = `${run.run_id}|${run.status}|${run.cap_pause?.estimate ?? ""}`;
    if (key === capFor) return;
    capFor = key;
    const p = run.cap_pause;
    // Room for the next call and for drafting (worst case about $1.65), which every run reaches.
    cap = p ? Math.ceil(p.spent + Math.max(p.estimate, 1.65) + 0.5) : run.settings?.cost_cap ?? 5;
  });

  async function pull() {
    try {
      const fresh = await get(`/api/runs/${encodeURIComponent(run.run_id)}/events`, { after });
      if (fresh.length) {
        after = fresh.at(-1).seq;
        events = [...events, ...fresh].slice(-400);
        await tick();
        if (logBox) logBox.scrollTop = logBox.scrollHeight;
      }
    } catch {}
  }

  // The log restarts only for another run; polling runs only while this one is live. The parent
  // replaces `run` on every refresh, so depend on its id and liveness, not on the object.
  let runId = $derived(run.run_id);
  $effect(() => {
    runId;
    events = [];
    after = 0;
    untrack(pull);
  });
  $effect(() => {
    if (!live) return;
    const timer = setInterval(() => {
      pull();
      onChange();
    }, 2000);
    return () => clearInterval(timer);
  });

  async function act(path, body) {
    busy = true;
    error = "";
    try {
      await post(`/api/runs/${encodeURIComponent(run.run_id)}/${path}`, body);
      await onChange();
      pull();
    } catch (e) {
      error = e.message;
    } finally {
      busy = false;
    }
  }
</script>

<section class="controls card" aria-live="polite">
  {#if run.status === "queued"}
    <div class="row">
      <div><strong>Waiting to start</strong>{run.queue_position > 1 ? ` · ${run.queue_position - 1} review${run.queue_position > 2 ? "s" : ""} ahead` : ""}.
        <span class="muted small">Reviews run one at a time.</span></div>
      <button class="btn" disabled={busy} onclick={() => act("pause")}>Take off the queue</button>
    </div>
  {:else if run.status === "running"}
    <div class="row">
      <div><strong>Running</strong> · {run.stage}{run.here ? "" : " (started from the command line)"}</div>
      {#if run.here}<button class="btn" disabled={busy} onclick={() => act("pause")}>Pause</button>{/if}
    </div>
  {:else}
    <div class="why">
      <strong>{run.status === "failed" ? "Stopped by an error" : "Paused"}</strong> at {run.stage}{run.error ? `: ${run.error}` : "."}
    </div>
    {#if run.cap_pause}
      <p class="small muted">The next call could cost up to {money(run.cap_pause.estimate)}, and the run has spent {money(run.cap_pause.spent)} of its {money(run.cap_pause.cap)} cap. Usually a call costs much less than its worst case.</p>
    {/if}
    <form class="row" onsubmit={(e) => { e.preventDefault(); act("resume", { cost_cap: Number(cap) }); }}>
      <label class="small">Cost cap $ <input type="number" min="0.5" max="100" step="0.5" bind:value={cap} required /></label>
      <button class="btn primary" disabled={busy}>{run.status === "failed" ? "Try again" : "Continue"}</button>
    </form>
    {#if run.resume}<p class="small muted cli">Or from the command line: <code>{run.resume} --cost-cap {cap}</code></p>{/if}
  {/if}

  {#if stageIndex >= 0}
    <ol class="stages" aria-label="Stages">
      {#each STAGES as s, i}
        <li class:done={i < stageIndex} class:now={i === stageIndex && live} class:stopped={i === stageIndex && !live}>{s}</li>
      {/each}
    </ol>
  {/if}
  <div class="spend small">
    <div class="bar"><span style="width: {spentShare}%"></span></div>
    {money(run.cost)} spent of a {money(run.settings?.cost_cap)} cap
  </div>
  {#if error}<p class="callout small">{error}</p>{/if}

  {#if events.length}
    <details open={live}>
      <summary class="small muted">Progress log ({events.length} lines)</summary>
      <pre class="log" bind:this={logBox}>{events.map((e) => e.line).join("\n")}</pre>
    </details>
  {/if}
</section>

<style>
  .controls {
    padding: 14px 16px;
    margin: 14px 0 4px;
    max-width: 900px;
    display: grid;
    gap: 10px;
  }
  .row {
    display: flex;
    gap: 10px 16px;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
  }
  form.row {
    justify-content: flex-start;
  }
  .why {
    overflow-wrap: anywhere;
  }
  .cli {
    margin: 0;
    overflow-wrap: anywhere;
  }
  .stages {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
    list-style: none;
    padding: 0;
    margin: 0;
    font-size: 12px;
  }
  .stages li {
    padding: 2px 8px;
    border-radius: 999px;
    background: var(--surface-2);
    color: var(--muted);
  }
  .stages li.done {
    background: var(--ok-soft);
    color: var(--ok);
  }
  .stages li.now {
    background: var(--accent);
    color: var(--accent-ink);
    animation: pulse 1.6s ease-in-out infinite;
  }
  .stages li.stopped {
    background: var(--warn-soft);
    color: var(--warn);
    font-weight: 600;
  }
  @keyframes pulse {
    50% {
      opacity: 0.6;
    }
  }
  .spend {
    display: flex;
    align-items: center;
    gap: 10px;
    color: var(--muted);
  }
  .bar {
    width: 160px;
    height: 6px;
    border-radius: 3px;
    background: var(--surface-2);
    overflow: hidden;
  }
  .bar span {
    display: block;
    height: 100%;
    background: var(--accent);
  }
  .log {
    max-height: 260px;
    overflow: auto;
    margin: 6px 0 0;
    padding: 10px;
    background: var(--surface-2);
    border-radius: 6px;
    font: 12px/1.45 var(--mono);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  @media (prefers-reduced-motion: reduce) {
    .stages li.now {
      animation: none;
    }
  }
</style>

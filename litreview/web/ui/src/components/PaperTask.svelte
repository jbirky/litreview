<script>
  import { post } from "../lib/api.js";

  // "Get text" or "Summarize" for one paper, or where that task stands.
  // task: the paper's latest task of this kind ({status, error}) or null.
  let { bibcode, kind, task = null, enabled = true, why = "", onQueued } = $props();
  let busy = $state(false);
  let error = $state("");

  const LABEL = { fetch: "Get text", summarize: "Summarize" };
  const WORKING = { fetch: "fetching…", summarize: "writing…" };
  const TITLE = {
    fetch: "Fetch the full text from arXiv (LaTeX source, then HTML, then PDF), or else the scan ADS hosts or an open-access copy found through Unpaywall. No Claude cost.",
    summarize: "Write a summary of the whole paper with Claude Sonnet, about $0.07. Fetches the full text first if needed.",
  };

  let active = $derived(task && (task.status === "queued" || task.status === "running"));
  let failed = $derived(task?.status === "failed");

  async function queue() {
    busy = true;
    error = "";
    try {
      const res = await post("/api/tasks", { kind, bibcodes: [bibcode] });
      if (!res.queued.length && res.skipped.length) error = res.skipped[0].reason;
      await onQueued?.();
    } catch (e) {
      error = e.message;
    } finally {
      busy = false;
    }
  }
</script>

{#if active}
  <span class="pill accent working" title={task.status === "queued" ? "Waiting for the worker" : ""}>{task.status === "queued" ? "queued" : WORKING[kind]}</span>
{:else}
  <span class="task">
    <button class="btn mini" disabled={!enabled || busy} onclick={queue} title={enabled ? TITLE[kind] : why}>
      {failed ? "Retry" : LABEL[kind]}
    </button>
    {#if failed}<span class="pill bad" title={task.error}>failed</span>{/if}
    {#if error}<span class="small err">{error}</span>{/if}
  </span>
{/if}

<style>
  .task {
    display: inline-flex;
    gap: 6px;
    align-items: center;
    flex-wrap: wrap;
  }
  .mini {
    padding: 2px 9px;
    font-size: 12px;
    white-space: nowrap;
  }
  .err {
    color: var(--bad);
  }
  .working {
    animation: pulse 1.6s ease-in-out infinite;
  }
  @keyframes pulse {
    50% {
      opacity: 0.55;
    }
  }
  @media (prefers-reduced-motion: reduce) {
    .working {
      animation: none;
    }
  }
</style>

<script>
  import { get, post } from "../lib/api.js";
  import { href } from "../lib/router.svelte.js";
  import AddPapers from "./AddPapers.svelte";
  import MathText from "./MathText.svelte";

  // Who cites whom among the papers a run read: one row per paper in date order, placed on a year
  // axis, with each link drawn as a bracket on the left from a paper to one it cites. Every paper
  // between the two sits to the right of the bracket, so no line crosses a label. Below, the papers
  // several of them cite that the review didn't read. Links come from ADS reference lists; papers
  // stored without one can fetch it here, and gaps can be added to a finished review. A shared page
  // passes the graph itself (litreview/web/share.py), and its papers link to ADS.
  let { runId = null, run = null, selected = null, onCite, onChange = null, reloadKey = "", shared = null } = $props();

  const W = 1000;
  const PAD = 28;
  const LABELS = 230; // room kept right of the axis for the last papers' labels
  const ROW = 24;
  const TOP = 14;

  let graph = $state(null);
  let error = $state("");
  let hover = $state(null);
  let gapHover = $state(null);
  let fetching = $state(false);
  let queueError = $state("");
  let picked = $state([]);
  let timer;

  async function load() {
    try {
      const g = await get(`/api/runs/${encodeURIComponent(runId)}/graph`);
      graph = g;
      if (g.missing.length) await watch();
    } catch (e) {
      error = e.message;
    }
  }

  // While citation-link tasks run, check every 1.5 s, and reload the graph once they're done.
  async function watch() {
    clearTimeout(timer);
    const active = (await get("/api/tasks")).filter((t) => t.kind === "refs");
    if (active.length) {
      fetching = true;
      timer = setTimeout(watch, 1500);
    } else if (fetching) {
      fetching = false;
      await load();
    }
  }

  async function fetchLinks() {
    queueError = "";
    try {
      await post("/api/tasks", { kind: "refs", bibcodes: graph.missing });
      fetching = true;
      await watch();
    } catch (e) {
      queueError = e.message;
    }
  }

  $effect(() => {
    runId;
    reloadKey;
    graph = null;
    error = "";
    picked = [];
    if (shared) {
      graph = shared;
      return;
    }
    load();
    return () => clearTimeout(timer);
  });

  function toggle(b) {
    picked = picked.includes(b) ? picked.filter((x) => x !== b) : [...picked, b];
  }

  async function added() {
    picked = [];
    await onChange?.();
  }

  let nodes = $derived(
    (graph?.nodes ?? [])
      .filter((n) => n.sort_date)
      .map((n) => ({ ...n, t: Number(n.sort_date.slice(0, 4)) + (Number(n.sort_date.slice(5, 7)) - 1) / 12 }))
      .sort((a, b) => a.t - b.t),
  );
  let span = $derived.by(() => {
    if (!nodes.length) return [2000, 2001];
    return [Math.floor(nodes[0].t), Math.ceil(nodes.at(-1).t) + 0.001];
  });
  let x = $derived((t) => PAD + 24 + ((t - span[0]) / (span[1] - span[0])) * (W - 2 * PAD - 24 - LABELS));

  let placed = $derived(
    nodes.map((n, i) => ({ ...n, px: x(n.t), r: 4 + Math.log10(1 + n.citation_count) * 1.4, y: TOP + 10 + i * ROW })),
  );
  let at = $derived(Object.fromEntries(placed.map((n) => [n.bibcode, n])));
  let edges = $derived((graph?.edges ?? []).filter(([a, b]) => at[a] && at[b]));
  let height = $derived(TOP + 10 + placed.length * ROW + 34);
  let ticks = $derived.by(() => {
    const [a, b] = span;
    const step = b - a > 30 ? 5 : b - a > 12 ? 2 : 1;
    const out = [];
    for (let y = Math.ceil(a / step) * step; y <= b; y += step) out.push(y);
    return out;
  });

  // What to bring forward: a paper and the papers it links to, or the papers citing a gap row.
  let focus = $derived(hover ?? selected);
  let lit = $derived.by(() => {
    if (gapHover) return new Set(gapHover.by);
    if (!focus || !at[focus]) return null;
    const s = new Set([focus]);
    for (const [a, b] of edges) {
      if (a === focus) s.add(b);
      if (b === focus) s.add(a);
    }
    return s;
  });

  // Left from the citing paper, along to the cited paper's row, and in from the left. Rows run in date
  // order, so every paper in between sits at or right of the nearer of the two, clear of the vertical.
  function bracket(a, b) {
    // 14px clear of the nearer paper leaves at least 7.5px of straight line for the arrowhead.
    const xg = Math.min(a.px - a.r, b.px - b.r) - 14;
    const k = Math.min(5, Math.abs(a.y - b.y) / 2);
    const s = Math.sign(b.y - a.y);
    return `M${a.px - a.r} ${a.y}H${xg + k}Q${xg} ${a.y} ${xg} ${a.y + s * k}V${b.y - s * k}Q${xg} ${b.y} ${xg + k} ${b.y}H${b.px - b.r - 1.5}`;
  }

  // Links into or out of the focused paper are drawn last, so they sit on top.
  let drawn = $derived(
    edges
      .map(([a, b]) => {
        const on = !gapHover && !!lit && (a === focus || b === focus);
        return { key: `${a}>${b}`, d: bracket(at[a], at[b]), on, dim: !!lit && !on };
      })
      .sort((p, q) => p.on - q.on),
  );

  const ads = (b) => `https://ui.adsabs.harvard.edu/abs/${encodeURIComponent(b)}/abstract`;

  function found(g) {
    if (g.status === "not_found") return "not found by its searches";
    if (g.status === "unscreened") return "found, below the cut for screening";
    if (g.status === "excluded") return `screened out (score ${g.score}): ${g.reason}`;
    if (g.status === "not_read") return `included (score ${g.score}), past the number of papers to read`;
    return `included (score ${g.score}), not read`;
  }
</script>

{#if error}
  <p class="callout">{error}</p>
{:else if !graph}
  <p class="muted">Loading…</p>
{:else if !graph.nodes.length}
  <p class="callout info">The citation graph appears once the run has read papers.</p>
{:else}
  <p class="small muted lead">
    {graph.edges.length} citation link{graph.edges.length === 1 ? "" : "s"} among the {graph.nodes.length} papers read.
    <span class="key"><span class="dot cited"></span> cited in the review</span>
    <span class="key"><span class="dot"></span> read, not cited</span>
    <span>circle size: ADS citation count · each bracket runs from a paper to one it cites</span>
  </p>
  {#if graph.missing.length}
    <div class="callout info small missing">
      <span>
        {graph.missing.length} of {graph.nodes.length} papers have no citation links stored; they were saved before litreview
        kept ADS reference lists.
      </span>
      {#if fetching}
        <span class="pill accent working">fetching…</span>
      {:else if !shared}
        <button class="btn" onclick={fetchLinks} title="One ADS request for every 40 or so papers; no Claude cost">Get citation links from ADS</button>
      {/if}
      {#if queueError}<span class="err">{queueError}</span>{/if}
    </div>
  {/if}

  <div class="graph card">
    <svg viewBox="0 0 {W} {height}" role="img" aria-label="Citation links among the papers read, by year">
      <defs>
        {#each ["", "-on"] as v}
          <marker id="cite-arrow{v}" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="6" markerHeight="6" orient="auto">
            <path d="M0 1L10 5L0 9z" class="arrow{v}" />
          </marker>
        {/each}
      </defs>
      <line x1={PAD} x2={W - PAD} y1={height - 28} y2={height - 28} class="axis" />
      {#each ticks as t}
        <line x1={x(t)} x2={x(t)} y1={TOP} y2={height - 28} class="grid" />
        <line x1={x(t)} x2={x(t)} y1={height - 31} y2={height - 25} class="axis" />
        <text x={x(t)} y={height - 10} class="tick" text-anchor="middle">{t}</text>
      {/each}
      <g class="edges">
        {#each drawn as e (e.key)}
          <path d={e.d} marker-end="url(#cite-arrow{e.on ? '-on' : ''})" class:on={e.on} class:dim={e.dim} />
        {/each}
      </g>
      {#each placed as n (n.bibcode)}
        <g class="node" class:cited={n.cited} class:on={selected === n.bibcode} class:dim={lit && !lit.has(n.bibcode)}
           role="button" tabindex="0" aria-label="{n.label}: {n.title}"
           onclick={() => onCite(n.bibcode)} onkeydown={(e) => e.key === "Enter" && onCite(n.bibcode)}
           onmouseenter={() => (hover = n.bibcode)} onmouseleave={() => (hover = null)}
           onfocus={() => (hover = n.bibcode)} onblur={() => (hover = null)}>
          <circle cx={n.px} cy={n.y} r={n.r} />
          <text x={n.px + n.r + 5} y={n.y + 4}>{n.label}</text>
          <title>{n.label}: {n.title}{"\n"}cites {n.cites} and is cited by {n.cited_by} of the papers read</title>
        </g>
      {/each}
    </svg>
  </div>

  {#if graph.gaps.length}
    <section class="gaps">
      <h3>Cited by several of these papers, but not read for this review</h3>
      <p class="small muted">Hover a row to see which papers cite it.{#if run?.can_add} Tick papers to read them and write the review again.{/if}</p>
      <table class="grid">
        <thead><tr>{#if run?.can_add}<th class="pick" aria-label="Add to the review"></th>{/if}<th class="num">Cited by</th><th>Paper</th><th>In this review</th></tr></thead>
        <tbody>
          {#each graph.gaps as g (g.bibcode)}
            <tr class:on={gapHover === g} onmouseenter={() => (gapHover = g)} onmouseleave={() => (gapHover = null)}>
              {#if run?.can_add}
                <td class="pick"><input type="checkbox" checked={picked.includes(g.bibcode)} onchange={() => toggle(g.bibcode)}
                                        aria-label="Add {g.label ?? g.bibcode} to the review" /></td>
              {/if}
              <td class="num">{g.by.length}</td>
              <td>
                {#if g.in_library && shared}
                  <a href={ads(g.bibcode)} target="_blank" rel="noreferrer">{g.label}</a> <span class="muted"><MathText text={g.title} /></span>
                {:else if g.in_library}
                  <a href={href(["paper", g.bibcode])}>{g.label}</a> <span class="muted"><MathText text={g.title} /></span>
                {:else}
                  <a href={ads(g.bibcode)} target="_blank" rel="noreferrer">{g.bibcode}</a>
                  {#if !shared}<span class="muted">not in the library</span>{/if}
                {/if}
              </td>
              <td class="small">{found(g)}</td>
            </tr>
          {/each}
        </tbody>
      </table>
      {#if run?.can_add && picked.length}
        <AddPapers {run} bibcodes={picked} names={Object.fromEntries(graph.gaps.map((g) => [g.bibcode, g.label ?? g.bibcode]))}
                   onDone={added} onClear={() => (picked = [])} />
      {/if}
    </section>
  {/if}
{/if}

<style>
  .lead {
    display: flex;
    flex-wrap: wrap;
    gap: 4px 14px;
    align-items: center;
  }
  .key {
    display: inline-flex;
    gap: 5px;
    align-items: center;
  }
  .dot {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    border: 1.5px solid var(--accent);
    background: var(--surface);
  }
  .dot.cited {
    background: var(--accent);
  }
  .missing {
    display: flex;
    flex-wrap: wrap;
    gap: 8px 14px;
    align-items: center;
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
  .graph {
    padding: 12px;
    overflow-x: auto;
  }
  svg {
    width: 100%;
    min-width: 680px;
    font: 14px var(--sans);
  }
  .axis {
    stroke: var(--line-2);
  }
  .grid {
    stroke: var(--line);
    stroke-dasharray: 2 4;
  }
  .tick {
    fill: var(--muted);
  }
  .edges path {
    fill: none;
    stroke: var(--muted);
    stroke-width: 1.1;
    opacity: 0.45;
    transition: opacity 0.12s;
  }
  .edges path.on {
    stroke: var(--accent);
    stroke-width: 1.8;
    opacity: 1;
  }
  .edges path.dim {
    opacity: 0.08;
  }
  .arrow {
    fill: var(--muted);
  }
  .arrow-on {
    fill: var(--accent);
  }
  .node {
    cursor: pointer;
    transition: opacity 0.12s;
  }
  .node circle {
    fill: var(--surface);
    stroke: var(--accent);
    stroke-width: 1.5;
  }
  .node.cited circle {
    fill: var(--accent);
  }
  .node text {
    fill: var(--muted);
  }
  .node.cited text {
    fill: var(--ink-2);
  }
  .node.dim {
    opacity: 0.25;
  }
  .node.on circle {
    stroke: var(--ink);
    stroke-width: 2.5;
  }
  .node.on text,
  .node:hover text,
  .node:focus-visible text {
    fill: var(--accent);
    font-weight: 600;
  }
  .node:focus-visible {
    outline: none;
  }
  .gaps {
    margin-top: 28px;
    max-width: 1000px;
  }
  .gaps h3 {
    margin-bottom: 2px;
  }
  .gaps tr.on {
    background: var(--accent-soft);
  }
  .num {
    text-align: right;
    width: 72px;
  }
  .pick {
    width: 28px;
  }
  @media (prefers-reduced-motion: reduce) {
    .working {
      animation: none;
    }
    .edges path,
    .node {
      transition: none;
    }
  }
</style>

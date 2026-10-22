<script>
  // Cited papers on a year axis, under the review's period headings.
  let { review, selected = null, onCite } = $props();

  const W = 1000;
  const PAD = 36;
  const ROW = 24;

  let papers = $derived(
    review.references
      .filter((r) => r.sort_date)
      .map((r) => ({ ...r, t: Number(r.sort_date.slice(0, 4)) + (Number(r.sort_date.slice(5, 7)) - 1) / 12 }))
      .sort((a, b) => a.t - b.t),
  );
  let span = $derived.by(() => {
    const ys = [...papers.map((p) => p.t), ...review.periods.flatMap((p) => [p.start, p.end + 1])];
    if (!ys.length) return [2000, 2001];
    return [Math.floor(Math.min(...ys)), Math.ceil(Math.max(...ys)) + 0.001];
  });
  let x = $derived((t) => PAD + ((t - span[0]) / (span[1] - span[0])) * (W - 2 * PAD));

  // Stack papers that would overlap.
  let placed = $derived.by(() => {
    const lanes = [];
    return papers.map((p) => {
      const px = x(p.t);
      let lane = lanes.findIndex((end) => end < px - 6);
      if (lane === -1) lane = lanes.length;
      lanes[lane] = px + 190;
      return { ...p, px, lane };
    });
  });
  let height = $derived(80 + (Math.max(0, ...placed.map((p) => p.lane)) + 1) * ROW + 36);
  let ticks = $derived.by(() => {
    const [a, b] = span;
    const step = b - a > 30 ? 5 : b - a > 12 ? 2 : 1;
    const out = [];
    for (let y = Math.ceil(a / step) * step; y <= b; y += step) out.push(y);
    return out;
  });
</script>

<div class="timeline card">
  <svg viewBox="0 0 {W} {height}" role="img" aria-label="Cited papers by year">
    {#each review.periods as p, i}
      <rect x={x(p.start)} y="8" width={Math.max(2, x(p.end + 1) - x(p.start))} height={height - 36}
            class="band" class:alt={i % 2 === 1} />
      <text x={x(p.start) + 8} y="30" class="band-label">{p.start}–{p.end}: {p.theme}</text>
    {/each}
    <line x1={PAD} x2={W - PAD} y1={height - 28} y2={height - 28} class="axis" />
    {#each ticks as t}
      <line x1={x(t)} x2={x(t)} y1={height - 31} y2={height - 25} class="axis" />
      <text x={x(t)} y={height - 10} class="tick" text-anchor="middle">{t}</text>
    {/each}
    {#each placed as p}
      <g class="paper" class:on={selected === p.bibcode} role="button" tabindex="0"
         onclick={() => onCite(p.bibcode)} onkeydown={(e) => e.key === "Enter" && onCite(p.bibcode)}>
        <circle cx={p.px} cy={62 + p.lane * ROW} r="6" />
        <text x={p.px + 11} y={67 + p.lane * ROW}>{p.label}</text>
        <title>{p.title}</title>
      </g>
    {/each}
  </svg>
</div>

<style>
  .timeline {
    padding: 12px;
    overflow-x: auto;
  }
  svg {
    width: 100%;
    min-width: 640px;
    font: 15px var(--sans);
  }
  .band {
    fill: var(--surface-2);
  }
  .band.alt {
    fill: color-mix(in srgb, var(--accent-soft) 60%, var(--surface));
  }
  .band-label {
    fill: var(--muted);
    font-weight: 600;
  }
  .axis {
    stroke: var(--line-2);
  }
  .tick {
    fill: var(--muted);
  }
  .paper {
    cursor: pointer;
  }
  .paper circle {
    fill: var(--accent);
  }
  .paper text {
    fill: var(--ink-2);
  }
  .paper.on circle {
    stroke: var(--ink);
    stroke-width: 2;
  }
  .paper.on text,
  .paper:hover text {
    fill: var(--accent);
    font-weight: 600;
  }
</style>

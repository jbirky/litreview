<script>
  // Papers per year; click a bar to show that year, drag across bars to pick a range.
  let { years = {}, from = null, to = null, onRange } = $props();

  let keys = $derived.by(() => {
    const ys = Object.keys(years).map(Number);
    if (!ys.length) return [];
    const out = [];
    for (let y = Math.min(...ys); y <= Math.max(...ys); y++) out.push(y);
    return out;
  });
  let peak = $derived(Math.max(1, ...Object.values(years)));
  let dragStart = $state(null);
  let dragEnd = $state(null);

  function inRange(y) {
    if (dragStart !== null) return y >= Math.min(dragStart, dragEnd) && y <= Math.max(dragStart, dragEnd);
    return (from === null || y >= from) && (to === null || y <= to);
  }
  function finish() {
    if (dragStart === null) return;
    const a = Math.min(dragStart, dragEnd);
    const b = Math.max(dragStart, dragEnd);
    dragStart = dragEnd = null;
    onRange(a, b);
  }
</script>

<svelte:window onpointerup={finish} />

{#if keys.length}
  <div class="hist" role="group" aria-label="Papers per year">
    {#each keys as y}
      <button
        class="bar"
        class:on={inRange(y)}
        style="--h: {((years[y] || 0) / peak) * 100}%"
        title="{y}: {years[y] || 0} papers"
        onpointerdown={(e) => { e.preventDefault(); dragStart = dragEnd = y; }}
        onpointerenter={() => { if (dragStart !== null) dragEnd = y; }}
        onkeydown={(e) => e.key === "Enter" && onRange(y, y)}
      ><span></span></button>
    {/each}
  </div>
  <div class="hist-axis muted small"><span>{keys[0]}</span><span>{keys.at(-1)}</span></div>
{/if}

<style>
  .hist {
    display: flex;
    align-items: flex-end;
    gap: 2px;
    height: 56px;
    user-select: none;
    touch-action: none;
  }
  .bar {
    flex: 1;
    height: 100%;
    border: 0;
    padding: 0;
    background: none;
    cursor: pointer;
    display: flex;
    align-items: flex-end;
  }
  .bar span {
    display: block;
    width: 100%;
    height: max(2px, var(--h));
    background: var(--line-2);
    border-radius: 2px 2px 0 0;
  }
  .bar.on span {
    background: var(--accent);
  }
  .bar:hover span {
    opacity: 0.8;
  }
  .hist-axis {
    display: flex;
    justify-content: space-between;
    margin-top: 2px;
  }
</style>

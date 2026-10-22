<script>
  import { href } from "../lib/router.svelte.js";
  import { authors } from "../lib/format.js";
  import AddPapers from "./AddPapers.svelte";
  import FindPapers from "./FindPapers.svelte";
  import MathText from "./MathText.svelte";

  // Every paper this run screened or read; a card opens the evidence panel. On a finished review,
  // papers it didn't read, here or found in ADS, can be ticked to read them and write the review again,
  // and so can papers it read whose newer text (such as an uploaded PDF) it hasn't read.
  let { papers, run = null, selected = null, onCard, onChange = null } = $props();
  let show = $state("read");
  let picked = $state([]);
  let names = $state({});
  let canAdd = $derived(!!run?.can_add);

  function toggle(p) {
    names[p.bibcode] = `${authors(p)} ${p.year ?? ""}`.trim();
    picked = picked.includes(p.bibcode) ? picked.filter((x) => x !== p.bibcode) : [...picked, p.bibcode];
  }

  async function added() {
    picked = [];
    await onChange?.();
  }
  const FILTERS = [
    ["cited", "Cited", (p) => p.cited],
    ["read", "Read", (p) => p.selected],
    ["unread", "Included, not read", (p) => p.included && !p.selected],
    ["included", "Included", (p) => p.included],
    ["all", "All screened", () => true],
  ];
  let rows = $derived(papers.filter(FILTERS.find((f) => f[0] === show)[2]));
  let unread = $derived(papers.filter(FILTERS[2][2]).length);
  let newer = $derived(papers.filter((p) => p.newer_text).length);
  let rereading = $derived(picked.filter((b) => papers.find((p) => p.bibcode === b)?.selected).length);
</script>

{#if canAdd}
  <section class="adder card">
    <strong>Add papers to this review</strong>
    <p class="small muted">
      Tick papers in the table that weren't read{#if unread}, such as
        <button class="linkish" onclick={() => (show = "unread")}>the {unread} that screening included</button>{/if},
      or find any paper in ADS. The review is then written again with them.
      {#if newer}{newer === 1 ? "One paper it read has" : `${newer} papers it read have`} a newer text, such as an uploaded PDF,
        and can be ticked to read {newer === 1 ? "it" : "them"} again.{/if}
    </p>
    <FindPapers {run} {picked} onToggle={toggle} />
  </section>
{/if}

{#if canAdd && picked.length}
  <AddPapers {run} bibcodes={picked} {names} {rereading} onDone={added} onClear={() => (picked = [])} />
{/if}

<div class="bar">
  <div class="seg">
    {#each FILTERS as [key, label, test]}
      <button class:on={show === key} onclick={() => (show = key)}>{label} ({papers.filter(test).length})</button>
    {/each}
  </div>
</div>

<div class="table-wrap card">
  <table class="grid">
    <thead>
      <tr>{#if canAdd}<th class="pick" aria-label="Add to the review"></th>{/if}<th>Paper</th><th>Score</th><th>Screening reason</th><th>Read</th><th>Card</th></tr>
    </thead>
    <tbody>
      {#each rows as p (p.bibcode)}
        <tr class:on={selected === p.bibcode}>
          {#if canAdd}
            <td class="pick">{#if !p.selected || p.newer_text}<input type="checkbox" checked={picked.includes(p.bibcode)} onchange={() => toggle(p)}
                                                    aria-label="{p.selected ? 'Read again' : 'Add'} {p.title}" />{/if}</td>
          {/if}
          <td>
            <div><a href={href(["paper", p.bibcode])}><MathText text={p.title} /></a></div>
            <div class="muted small">{p.year ?? "n.d."} · {authors(p)} · {p.journal || "preprint"}{#if p.cited} · <span class="pill ok">cited</span>{/if}</div>
          </td>
          <td class="nowrap">{p.score ?? "—"}{#if p.round === "expand"} <span class="muted small" title="found through the citation graph">graph</span>{/if}</td>
          <td class="small">
            {p.reason}
            {#if p.added}<div><span class="pill accent">added later</span></div>{/if}
            {#if p.reread}<div><span class="pill accent">read again from a newer text</span></div>{/if}
            {#if p.newer_text}<div><span class="pill">newer text to read</span></div>{/if}
            {#if p.cited_by}<div class="muted">Read because {p.cited_by} of the other included papers cite it.</div>{/if}
          </td>
          <td class="small nowrap">{p.text ?? (p.selected ? "nothing readable" : "—")}</td>
          <td class="nowrap">
            {#if p.card}<button class="pill {p.card} linkish-pill" onclick={() => onCard(p.bibcode)}>{p.card.replace("_", " ")}</button>{:else}<span class="muted">—</span>{/if}
          </td>
        </tr>
      {:else}
        <tr><td colspan={canAdd ? 6 : 5} class="muted small">None.</td></tr>
      {/each}
    </tbody>
  </table>
</div>

<style>
  .adder {
    padding: 12px 16px;
    margin-bottom: 12px;
    display: grid;
    gap: 8px;
  }
  .adder p {
    margin: 0;
  }
  .bar {
    margin-bottom: 12px;
    display: flex;
    flex-wrap: wrap;
    gap: 8px 16px;
    align-items: center;
  }
  .pick {
    width: 28px;
  }
  tr.on td {
    background: var(--accent-soft);
  }
  .linkish-pill {
    border: 0;
    cursor: pointer;
  }
</style>

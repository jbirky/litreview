<script>
  import Inline from "./Inline.svelte";

  // The checked review as segments; citations are links that open the evidence panel.
  let { review, selected = null, citeHref } = $props();

  // Consecutive bullet blocks become one list; a bullet with depth d goes under the last item d levels down.
  let groups = $derived.by(() => {
    const out = [];
    for (const b of review.blocks) {
      if (b.kind !== "bullet") {
        out.push(b);
        continue;
      }
      if (out.at(-1)?.kind !== "list") out.push({ kind: "list", items: [] });
      let items = out.at(-1).items;
      for (let d = 0; d < (b.depth ?? 0) && items.length; d++) items = items.at(-1).children;
      items.push({ ...b, children: [] });
    }
    return out;
  });

  function venue(e) {
    if (!e.journal) return "(preprint)";
    return `(${e.journal}${e.journal_year ? " " + e.journal_year : ""})`;
  }
</script>

{#snippet segs(list)}{#each list as s}{#if s.t === "text"}<Inline text={s.v} />{:else}<span class="cite">({#each s.items as it, i}{#if i}{"; "}{/if}{#if it.invalid}<span class="invalid">invalid citation removed</span>{:else}<a class="cite-btn" class:active={selected === it.bibcode} href={citeHref(it.bibcode)}>{it.label}</a>{/if}{/each})</span>{/if}{/each}{/snippet}

{#snippet bullets(items)}<ul>{#each items as item}<li>{@render sents(item.sentences)}{#if item.children.length}{@render bullets(item.children)}{/if}</li>{/each}</ul>{/snippet}

{#snippet sents(list)}{#each list as s, i}{#if i}{" "}{/if}<span class="sent" class:partial={s.partial} title={s.partial ? `Partly supported${s.reason ? ": " + s.reason : " by the cited papers' cards"}` : undefined}>{@render segs(s.segments)}{#if s.partial}<span class="dagger">†</span>{/if}</span>{/each}{/snippet}

<article class="prose">
  {#each groups as b}
    {#if b.kind === "h2"}
      <h2>{@render segs(b.heading)}</h2>
    {:else if b.kind === "h3"}
      <h3>{@render segs(b.heading)}</h3>
    {:else if b.kind === "entry"}
      <div class="entry" class:active={selected === b.entry.bibcode}>
        <div class="entry-head">
          {b.entry.year ?? "n.d."}{b.entry.suffix} — <a class="cite-btn" class:active={selected === b.entry.bibcode} href={citeHref(b.entry.bibcode)}>{b.entry.name}</a>
          <span class="venue">{venue(b.entry)}</span>
        </div>
        <div>{@render sents(b.sentences)}</div>
      </div>
    {:else if b.kind === "list"}
      {@render bullets(b.items)}
    {:else}
      <p>{@render sents(b.sentences)}</p>
    {/if}
  {/each}
  {#if review.blocks.some((b) => b.sentences?.some((s) => s.partial))}
    <p class="small muted"><span class="dagger">†</span> The claim check rated this sentence only partly supported by the cited papers' evidence cards. Hover it for the reason.</p>
  {/if}
</article>

<script>
  import { cutBeforeMath } from "../lib/math.js";
  import Inline from "./Inline.svelte";
  import MathText from "./MathText.svelte";

  // items: [{text, passages: [{i, label}], quotes, source?}]; onPassage(i) opens the stored text there.
  let { title, items = [], onPassage, showSource = false, showQuotes = true } = $props();

  function short(q) {
    const s = q.replace(/^\[[^\]]*\]\s*/, "").replace(/\s+/g, " ").trim();
    return s.length > 280 ? cutBeforeMath(s, 277) + "…" : s;
  }
</script>

{#if items.length}
  <section class="items">
    <h4>{title}</h4>
    <ul>
      {#each items as it}
        <li>
          <div>
            {#if showSource && it.source}<span class="src-tag {it.source}">{it.source}</span>{/if}<Inline text={it.text} />
          </div>
          {#if showQuotes && it.quotes?.length}
            <blockquote class="quote"><MathText text={short(it.quotes[0])} /></blockquote>
          {/if}
          <div class="passages">
            {#each it.passages as p}
              <button class="passage" data-i={p.i} onclick={() => onPassage?.(p.i)} title="Open the stored text at this paragraph">
                {p.label ?? `paragraph ${p.i}`} ↗
              </button>
            {/each}
          </div>
        </li>
      {/each}
    </ul>
  </section>
{/if}

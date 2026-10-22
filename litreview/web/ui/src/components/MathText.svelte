<script>
  import { mathTokens } from "../lib/math.js";

  // Paper text with its LaTeX math rendered by KaTeX; everything else stays text.
  let { text = "" } = $props();
  let tokens = $derived(mathTokens(text));
</script>

{#each tokens as t}{#if t.t === "math"}{@html t.html}{:else}{t.v}{/if}{/each}

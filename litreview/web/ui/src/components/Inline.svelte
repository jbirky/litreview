<script>
  import { inlineTokens } from "../lib/inline.js";

  let { text = "" } = $props();
  let tokens = $derived(inlineTokens(text));
</script>

{#each tokens as t}{#if t.t === "b"}<strong>{t.v}</strong>{:else if t.t === "i"}<em>{t.v}</em>{:else if t.t === "code"}<code>{t.v}</code>{:else if t.t === "math"}{@html t.html}{:else}{t.v}{/if}{/each}

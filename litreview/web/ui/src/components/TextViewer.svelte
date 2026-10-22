<script>
  import { tick } from "svelte";
  import { get } from "../lib/api.js";
  import MathText from "./MathText.svelte";

  // The labelled paragraphs exactly as they were sent to the model, with `focus` highlighted.
  let { bibcode, source, version, focus = [] } = $props();
  let text = $state(null);
  let error = $state("");
  let box;

  $effect(() => {
    const key = [bibcode, source, version];
    text = null;
    error = "";
    get("/api/text", { bibcode: key[0], source: key[1], version: key[2] })
      .then((t) => {
        if (t.bibcode === bibcode && t.source === source && t.version === version) text = t;
      })
      .catch((e) => (error = e.message));
  });

  $effect(() => {
    const target = focus[0];
    if (!text || target === undefined) return;
    tick().then(() => {
      box?.querySelector(`[data-i="${target}"]`)?.scrollIntoView({ block: "center", behavior: "smooth" });
    });
  });
</script>

<div class="textview" bind:this={box}>
  {#if error}
    <p class="callout">Couldn't load the text: {error}</p>
  {:else if !text}
    <p class="muted">Loading the text…</p>
  {:else}
    <p class="muted small">
      {text.label}{#if text.origin} from <a href={text.origin} target="_blank" rel="noreferrer">{new URL(text.origin).hostname}</a>{/if} · {text.paragraphs.length} paragraph{text.paragraphs.length === 1 ? "" : "s"}{#if text.truncated} · cut at the length limit{/if}
    </p>
    {#each text.paragraphs as p (p.i)}
      <div class="para" class:focus={focus.includes(p.i)} data-i={p.i}>
        <div class="para-label">[{p.i}] {p.label}</div>
        <p><MathText text={p.text} /></p>
      </div>
    {/each}
  {/if}
</div>

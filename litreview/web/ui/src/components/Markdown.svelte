<script>
  import DOMPurify from "dompurify";
  import { marked } from "marked";

  // report.md holds text the model wrote, so the HTML is sanitised before it is shown.
  let { source = "" } = $props();
  let html = $derived(DOMPurify.sanitize(marked.parse(source, { gfm: true }), { USE_PROFILES: { html: true } }));
</script>

<div class="md">{@html html}</div>

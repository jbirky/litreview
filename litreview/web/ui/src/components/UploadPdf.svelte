<script>
  import { upload } from "../lib/api.js";

  // Store a PDF of a paper or book, all of it or some pages, as its full text.
  let { bibcode, onDone } = $props();
  let files = $state();
  let pages = $state("");
  let busy = $state(false);
  let error = $state("");
  let result = $state(null);

  async function send(e) {
    e.preventDefault();
    busy = true;
    error = "";
    result = null;
    try {
      result = await upload("/api/papers/upload", files[0], { bibcode, pages: pages.trim() });
      await onDone?.();
    } catch (err) {
      error = err.message;
    } finally {
      busy = false;
    }
  }
</script>

<form class="upload" onsubmit={send} aria-live="polite">
  <strong class="small">Upload a PDF</strong>
  <p class="small muted">
    Your own copy of this paper or book, read as its full text with no Claude cost; scanned pages are read by OCR.
    Reviews read it from then on.
    About 240,000 characters are read, roughly 80–110 pages of a textbook, so for a book give the pages you need,
    counting the file's first page as 1.
  </p>
  <div class="row">
    <input type="file" accept="application/pdf,.pdf" bind:files required disabled={busy} />
    <label class="small">Pages <input type="text" bind:value={pages} placeholder="all, or 120-180" size="12" disabled={busy} /></label>
    <button class="btn mini" disabled={busy || !files?.length}>{busy ? "Reading…" : "Upload"}</button>
  </div>
  {#if error}<p class="small err">{error}</p>{/if}
  {#if result}
    <p class="small">
      Stored {result.paragraphs} paragraphs{result.pages ? ` from pages ${result.pages[0]}–${result.pages[1]}` : ""}.
      {#if result.truncated}It was cut at the length limit after “{result.last_label}”; choose fewer pages to read the rest.{/if}
    </p>
  {/if}
</form>

<style>
  .upload {
    display: grid;
    gap: 6px;
    margin-top: 12px;
    padding-top: 10px;
    border-top: 1px solid var(--line);
  }
  .upload p {
    margin: 0;
  }
  .row {
    display: flex;
    flex-wrap: wrap;
    gap: 8px 14px;
    align-items: center;
  }
  .mini {
    padding: 2px 9px;
    font-size: 12px;
    white-space: nowrap;
  }
  .err {
    color: var(--bad);
  }
</style>

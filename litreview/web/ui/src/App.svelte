<script>
  import { get } from "./lib/api.js";
  import { href, route } from "./lib/router.svelte.js";
  import { money } from "./lib/format.js";
  import Library from "./pages/Library.svelte";
  import NewReview from "./pages/NewReview.svelte";
  import Paper from "./pages/Paper.svelte";
  import Review from "./pages/Review.svelte";
  import Reviews from "./pages/Reviews.svelte";

  let overview = $state(null);
  get("/api/overview").then((o) => (overview = o)).catch(() => {});

  let page = $derived(route.parts[0] || "reviews");

  function quotaTitle(q) {
    const reset = q.reset ? `; it resets at ${new Date(q.reset * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}` : "";
    return `ADS search requests used of today's limit, as of litreview's last ADS request${reset}`;
  }
</script>

<header class="topbar">
  <a class="brand" href={href([])}>litreview</a>
  <nav>
    <a href={href([])} class:on={page === "reviews" || page === "review" || page === "new"}>Reviews</a>
    <a href={href(["library"])} class:on={page === "library" || page === "paper"}>Library</a>
  </nav>
  {#if overview}
    <span class="spend" title="Claude spend recorded by litreview this calendar month">
      {money(overview.month_spend)} this month{overview.monthly_credits ? ` of ${money(overview.monthly_credits)}` : ""}
    </span>
    {#if overview.ads_quota}
      {@const q = overview.ads_quota}
      <span class="quota" class:low={q.used >= 0.9 * q.limit} title={quotaTitle(q)}>
        {q.used.toLocaleString()} of {q.limit.toLocaleString()} ADS queries today
      </span>
    {/if}
  {/if}
</header>

<main>
  {#if page === "new"}
    <NewReview />
  {:else if page === "review" && route.parts[1]}
    <Review runId={route.parts[1]} query={route.query} />
  {:else if page === "paper" && route.parts[1]}
    <Paper bibcode={route.parts[1]} query={route.query} />
  {:else if page === "library"}
    <Library query={route.query} />
  {:else}
    <Reviews />
  {/if}
</main>

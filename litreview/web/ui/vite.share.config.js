import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import { katexWoff2Only } from "./vite.katex.js";

// The shareable review page (src/share): one script and one stylesheet in dist/share, which
// litreview inlines with a review's data into a single HTML file (litreview/web/share.py).
export default defineConfig({
  plugins: [svelte(), katexWoff2Only()],
  build: {
    outDir: "dist/share",
    emptyOutDir: true,
    lib: {
      entry: "src/share/main.js",
      formats: ["iife"],
      name: "litreviewShare",
      fileName: () => "share.js",
      cssFileName: "share",
    },
  },
});

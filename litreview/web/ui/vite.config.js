import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import { katexWoff2Only } from "./vite.katex.js";

// `npm run dev` serves the UI on :5173 and forwards /api to `litreview web` on :5070.
export default defineConfig({
  plugins: [svelte(), katexWoff2Only()],
  build: { outDir: "dist", emptyOutDir: true },
  server: { proxy: { "/api": "http://127.0.0.1:5070" } },
});

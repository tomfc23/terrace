import { defineConfig } from "astro/config";
import tailwindcss from "@tailwindcss/vite";

// Static output: the whole frontend is HTML + one small script, which is what
// makes it deployable to Cloudflare Pages or Vercel as plain files.
export default defineConfig({
  output: "static",
  compressHTML: true,
  build: { inlineStylesheets: "auto" },
  vite: { plugins: [tailwindcss()] },
});

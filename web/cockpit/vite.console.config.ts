import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";

const require = createRequire(import.meta.url);
const threeLicense = readFileSync(resolve(dirname(require.resolve("three")), "../LICENSE"), "utf8");

export default defineConfig({
  esbuild: { legalComments: "inline" },
  build: {
    target: "es2022",
    minify: true,
    chunkSizeWarningLimit: 600,
    outDir: fileURLToPath(new URL("../../dimos/web/console/assets", import.meta.url)),
    emptyOutDir: true,
    rollupOptions: {
      input: fileURLToPath(new URL("./src/console/objectMap.ts", import.meta.url)),
      preserveEntrySignatures: "strict",
      output: {
        format: "es",
        entryFileNames: "object-map.js",
        banner: `/*!\nBundled Three.js / OrbitControls:\n${threeLicense}\n*/`,
      },
    },
  },
});

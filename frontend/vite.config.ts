import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Builds to ../web/dist, which api/main.py mounts at "/". In dev, the SPA runs
// on :5173 and proxies /api to the FastAPI server on :8000 (CORS already allows it).
export default defineConfig({
  plugins: [react()],
  base: "/",
  build: {
    outDir: "../web/dist",
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://localhost:8000",
    },
  },
});

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
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: false,
        // Vite answers an unreachable upstream with 500, which is indistinguishable
        // from the API itself erroring. Report 502 instead: the client only falls
        // back to demo data when the API never answered, and must never substitute
        // sample figures for a real server error.
        configure: (proxy) => {
          proxy.on("error", (_err, _req, res) => {
            const r = res as import("http").ServerResponse;
            if (!r.headersSent) r.writeHead(502, { "Content-Type": "application/json" });
            r.end(JSON.stringify({ detail: "control API not running on :8000" }));
          });
        },
      },
    },
  },
});

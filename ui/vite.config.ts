import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// The console talks to both services through this server, so the browser sees one origin:
//   /api/runtime/*     -> RUNTIME_URL     (extraction service)
//   /api/designtime/*  -> DESIGNTIME_URL  (design-time service)
// Defaults match designtime/docker-compose.yml (design-time on 8000, runtime on 8001).
export default defineConfig(({ mode }) => {
  const env = { ...process.env, ...loadEnv(mode, process.cwd(), "") };
  const runtime = env.RUNTIME_URL || "http://localhost:8001";
  const designtime = env.DESIGNTIME_URL || "http://localhost:8000";
  const proxy = {
    "/api/runtime": { target: runtime, changeOrigin: true, rewrite: (p: string) => p.replace(/^\/api\/runtime/, "") },
    "/api/designtime": {
      target: designtime, changeOrigin: true, timeout: 600_000, proxyTimeout: 600_000,
      rewrite: (p: string) => p.replace(/^\/api\/designtime/, ""),
    },
  };
  return {
    plugins: [react()],
    server: { port: 5173, host: true, proxy },
    preview: { port: 4173, host: true, proxy },
    build: { chunkSizeWarningLimit: 4000 },
  };
});

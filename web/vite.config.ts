import path from "node:path";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// 开发：npm run dev，接口转给本机跑着的 hls2strm（HLS2STRM_BACKEND 可改）
// 构建：产物写进 Python 包的 hls2strm/static，由 FastAPI 在 / 和 /static/ 下提供
const backend = process.env.HLS2STRM_BACKEND || "http://127.0.0.1:8091";

export default defineConfig(({ command }) => ({
  base: command === "build" ? "/static/" : "/",
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": path.resolve(import.meta.dirname, "src") } },
  server: {
    proxy: Object.fromEntries(["/api", "/play", "/hls", "/healthz"].map(p => [p, { target: backend, changeOrigin: true }])),
  },
  build: {
    outDir: path.resolve(import.meta.dirname, "../hls2strm/static"),
    emptyOutDir: true,
    chunkSizeWarningLimit: 900,
  },
}));

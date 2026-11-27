import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

/**
 * Vite 开发与生产构建配置：
 * 开发模式下将 /api 请求反向代理到本地 FastAPI 网关（http://127.0.0.1:8000）。
 */
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
});

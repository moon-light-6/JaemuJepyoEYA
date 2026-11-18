import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// API 서버가 정해지면 아래 proxy 의 target 을 맞춰 주세요. (현재는 목 데이터만 사용)
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://localhost:8000", changeOrigin: true },
    },
  },
});

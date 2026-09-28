import { fileURLToPath, URL } from "node:url";
import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

/**
 * Заголовки безопасности (фаза 6 §5). CSP без unsafe-inline/unsafe-eval для скриптов и стилей: сборка Vite
 * не создаёт инлайн-скриптов, Tailwind v4 — внешний CSS, инлайн-атрибуты style в коде запрещены (eslint).
 * В dev-режиме плагин React Fast Refresh вставляет инлайн-преамбулу — там CSP не применяется, в preview (dist/)
 * и в nginx (prod) — применяется. E2E и Lighthouse ходят в preview.
 */
export function securityHeaders(mediaOrigin: string, apiOrigin: string, sentryOrigin = ""): Record<string, string> {
  const csp = [
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self'",
    `img-src 'self' data: ${mediaOrigin}`.trim(),
    `connect-src 'self' ${apiOrigin} ${sentryOrigin} ws: wss:`.replace(/\s+/g, " ").trim(),
    "font-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "frame-ancestors 'none'",
    "form-action 'self'",
  ].join("; ");
  return {
    "Content-Security-Policy": csp,
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains; preload",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "X-Frame-Options": "DENY",
  };
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const api = env.VITE_API_ORIGIN || "http://127.0.0.1:8000";
  const media = env.VITE_MEDIA_ORIGIN || "http://127.0.0.1:9000";
  const sentryOrigin = env.VITE_SENTRY_DSN ? new URL(env.VITE_SENTRY_DSN).origin : "";
  const headers = securityHeaders(media, "", sentryOrigin);
  return {
    plugins: [react(), tailwindcss()],
    resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
    server: {
      port: 5173,
      strictPort: true,
      proxy: {
        "/api": { target: api, changeOrigin: false },
        "/ws": { target: api.replace(/^http/, "ws"), ws: true, changeOrigin: false },
      },
    },
    preview: {
      port: 4173,
      strictPort: true,
      headers,
      proxy: {
        "/api": { target: api, changeOrigin: false },
        "/ws": { target: api.replace(/^http/, "ws"), ws: true, changeOrigin: false },
      },
    },
    // hidden: карты генерируются для загрузки в Sentry из CI, но не ссылаются из бандла и не раздаются (nginx: 404 на *.map)
    build: {
      sourcemap: "hidden",
      target: "es2022",
      chunkSizeWarningLimit: 600, // чанк sentry (~570 kB) грузится только при заданном VITE_SENTRY_DSN
      rollupOptions: {
        output: { manualChunks: (id) => (id.includes("node_modules/@sentry") ? "sentry" : undefined) },
      },
    },
    test: {
      environment: "jsdom",
      include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
    },
  };
});

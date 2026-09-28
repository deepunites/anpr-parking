import { defineConfig, devices } from "@playwright/test";

/**
 * E2E против реального стека: backend/scripts/e2e_stack.py (отдельная БД parking_e2e, uvicorn :8010, publisher,
 * consumer) + `vite preview` (dist/, CSP-заголовки) с прокси /api и /ws на API. Никаких моков API.
 */
export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"], ["html", { open: "never" }]],
  globalSetup: "./e2e/global-setup.ts",
  globalTeardown: "./e2e/global-teardown.ts",
  use: {
    baseURL: "http://127.0.0.1:4173",
    trace: "retain-on-failure",
    locale: "ru-RU",
  },
  webServer: {
    command: "npm run preview",
    url: "http://127.0.0.1:4173",
    reuseExistingServer: false,
    env: { VITE_API_ORIGIN: "http://127.0.0.1:8010" },
    timeout: 60_000,
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});

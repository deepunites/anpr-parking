/**
 * Lighthouse для Live Parking на собранном приложении (vite preview + стенд e2e_stack).
 * Аутентификация — refresh-cookie из логина через прокси preview, передаётся Lighthouse'у как extra header:
 * страница /live сама восстанавливает access-токен (Bootstrap → POST /api/v1/auth/refresh).
 *
 *   node scripts/lighthouse.mjs  → docs/frontend/lighthouse-live.{json,html}, вывод оценок
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import lighthouse from "lighthouse";
import desktopConfig from "lighthouse/core/config/desktop-config.js";
import * as chromeLauncher from "chrome-launcher";

const here = path.dirname(fileURLToPath(import.meta.url));
const stack = JSON.parse(fs.readFileSync(path.resolve(here, "../e2e/.stack.json"), "utf8"));
const base = process.env.LH_BASE_URL ?? "http://127.0.0.1:4173";
const chromePath =
  process.env.CHROME_PATH ??
  path.join(process.env.HOME ?? "", ".cache/ms-playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell");

const user = stack.users.operator;
const login = await fetch(`${base}/api/v1/auth/login`, {
  method: "POST",
  headers: { "content-type": "application/json" },
  body: JSON.stringify({ email: user.email, password: user.password }),
});
if (!login.ok) throw new Error(`login failed: ${login.status}`);
const cookie = (login.headers.get("set-cookie") ?? "").split(";")[0];
if (!cookie.startsWith("parking_refresh=")) throw new Error("no refresh cookie");

const chrome = await chromeLauncher.launch({ chromePath, chromeFlags: ["--no-sandbox", "--disable-dev-shm-usage"] });
try {
  const result = await lighthouse(
    `${base}/live`,
    { port: chrome.port, output: ["json", "html"], onlyCategories: ["performance", "accessibility", "best-practices"], extraHeaders: { Cookie: cookie } },
    desktopConfig, // десктопный пресет: операторское рабочее место, не телефон в 3G
  );
  const outDir = path.resolve(here, "../../docs/frontend");
  fs.mkdirSync(outDir, { recursive: true });
  fs.writeFileSync(path.join(outDir, "lighthouse-live.json"), result.report[0]);
  fs.writeFileSync(path.join(outDir, "lighthouse-live.html"), result.report[1]);
  const scores = Object.fromEntries(Object.entries(result.lhr.categories).map(([k, v]) => [k, Math.round((v.score ?? 0) * 100)]));
  const title = result.lhr.finalDisplayedUrl;
  const audits = result.lhr.audits;
  console.log(JSON.stringify({ url: title, scores, lcp_ms: audits["largest-contentful-paint"]?.numericValue, tbt_ms: audits["total-blocking-time"]?.numericValue, cls: audits["cumulative-layout-shift"]?.numericValue }, null, 2));
  const failing = Object.values(audits).filter((a) => a.score !== null && a.score < 1 && a.scoreDisplayMode === "binary" && (a.id.includes("aria") || a.id.includes("color-contrast") || a.id.includes("label") || a.id.includes("button-name") || a.id.includes("link-name")));
  if (failing.length) console.log("accessibility issues:", failing.map((a) => a.id).join(", "));
  const ok = scores.performance >= 90 && scores.accessibility >= 90;
  process.exitCode = ok ? 0 : 1;
} finally {
  await chrome.kill();
}

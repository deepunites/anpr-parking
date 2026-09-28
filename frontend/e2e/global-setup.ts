import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const backend = path.resolve(__dirname, "../../backend");
const uv = path.join(process.env.HOME ?? "", ".local/bin/uv");

/** Прогрев только что поднятого стенда: первый вход (argon2, пулы соединений, кеши площадок) занимал до ~25 с,
    и первый по алфавиту тест упирался в лимит 60 с. После прогрева вход — доли секунды. */
async function warmUp(): Promise<void> {
  const state = JSON.parse(readFileSync(path.join(__dirname, ".stack.json"), "utf8")) as {
    api: string;
    users: Record<string, { email: string; password: string }>;
  };
  for (const role of ["tenant_admin", "manager", "operator"]) {
    const u = state.users[role];
    if (!u) continue;
    const res = await fetch(`${state.api}/api/v1/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: u.email, password: u.password }),
    });
    if (!res.ok) throw new Error(`warm-up login ${role}: ${res.status}`);
    const { access_token } = (await res.json()) as { access_token: string };
    for (const p of ["/api/v1/auth/me", "/api/v1/lots", "/api/v1/cameras", "/api/v1/sessions/review-queue"]) {
      await fetch(`${state.api}${p}`, { headers: { Authorization: `Bearer ${access_token}` } });
    }
  }
}

export default async function globalSetup(): Promise<void> {
  execFileSync(uv, ["run", "python", "scripts/e2e_stack.py", "start"], { cwd: backend, stdio: "inherit", timeout: 180_000 });
  await warmUp();
}

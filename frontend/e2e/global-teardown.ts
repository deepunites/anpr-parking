import { execFileSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const backend = path.resolve(__dirname, "../../backend");
const uv = path.join(process.env.HOME ?? "", ".local/bin/uv");

export default function globalTeardown(): void {
  execFileSync(uv, ["run", "python", "scripts/e2e_stack.py", "stop"], { cwd: backend, stdio: "inherit", timeout: 60_000 });
}

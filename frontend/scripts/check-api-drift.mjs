/** CI: типы регенерируются из docs/openapi.json и сравниваются с зафиксированными — расхождение фронта и бэка ломает сборку. */
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const tmp = path.join(os.tmpdir(), `schema-${process.pid}.d.ts`);
execFileSync("npx", ["openapi-typescript", "../docs/openapi.json", "-o", tmp], { stdio: "inherit" });
const fresh = fs.readFileSync(tmp, "utf8");
const committed = fs.readFileSync("src/api/schema.d.ts", "utf8");
fs.unlinkSync(tmp);
if (fresh !== committed) {
  console.error("src/api/schema.d.ts устарел относительно docs/openapi.json — выполните npm run gen:api");
  process.exit(1);
}
console.log("api types are in sync with docs/openapi.json");

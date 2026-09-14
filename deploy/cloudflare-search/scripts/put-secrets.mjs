import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";

const secretNames = [
  "ARTSEARCH_VISITOR_HASH_SECRET",
  "DATABASE_URL",
  "QDRANT_API_KEY",
  "QDRANT_URL"
];
const missing = secretNames.filter((name) => !process.env[name]?.trim());
if (missing.length > 0) {
  console.error(`Missing environment values: ${missing.join(", ")}`);
  console.error(
    "Load the ignored alpha environment first: node --env-file=../alpha/.env scripts/put-secrets.mjs"
  );
  process.exit(2);
}

const wrangler = fileURLToPath(new URL("../node_modules/wrangler/bin/wrangler.js", import.meta.url));
for (const name of secretNames) {
  await putSecret(wrangler, name, process.env[name]);
  console.log(`Configured Worker secret: ${name}`);
}

function putSecret(wranglerEntry, name, value) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [wranglerEntry, "secret", "put", name], {
      cwd: fileURLToPath(new URL("..", import.meta.url)),
      stdio: ["pipe", "inherit", "inherit"]
    });
    child.once("error", reject);
    child.once("exit", (code) => {
      if (code === 0) {
        resolve();
      } else {
        reject(new Error(`wrangler secret put ${name} exited with ${code}`));
      }
    });
    child.stdin.end(`${value}\n`);
  });
}

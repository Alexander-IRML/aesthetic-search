import { randomBytes } from "node:crypto";
import { chmod, readFile, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";

const envPath = fileURLToPath(new URL("../../alpha/.env", import.meta.url));
let content;
try {
  content = await readFile(envPath, "utf8");
} catch (error) {
  if (error?.code === "ENOENT") {
    console.error(`Missing ignored alpha environment: ${envPath}`);
    process.exit(2);
  }
  throw error;
}

const values = {
  ALPHA_ACCESS_CODE: `alpha_${randomBytes(18).toString("base64url")}`,
  ALPHA_SESSION_KEY: randomBytes(48).toString("base64url")
};
const generated = [];
for (const [name, value] of Object.entries(values)) {
  const pattern = new RegExp(`^${name}=(.*)$`, "m");
  const match = content.match(pattern);
  if (match?.[1]?.trim()) {
    continue;
  }
  if (match) {
    content = content.replace(pattern, `${name}=${value}`);
  } else {
    if (!content.endsWith("\n")) {
      content += "\n";
    }
    content += `${name}=${value}\n`;
  }
  generated.push(name);
}

await writeFile(envPath, content, { encoding: "utf8", mode: 0o600 });
await chmod(envPath, 0o600);
if (generated.length === 0) {
  console.log("Alpha access secrets already exist; no values changed.");
} else {
  console.log(`Generated local alpha secrets: ${generated.join(", ")}`);
}
console.log("Values remain only in the ignored deploy/alpha/.env file.");

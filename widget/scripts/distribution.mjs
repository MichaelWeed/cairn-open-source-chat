import { mkdir, readFile, writeFile } from "node:fs/promises";

const BUILD_PATH = new URL("../dist/widget.js", import.meta.url);
const SERVED_PATH = new URL("../../backend/app/static/widget/widget.js", import.meta.url);
const operation = process.argv[2];

if (operation !== "sync" && operation !== "check") {
  console.error("usage: node scripts/distribution.mjs <sync|check>");
  process.exit(2);
}

const built = await readFile(BUILD_PATH);

if (operation === "sync") {
  await mkdir(new URL(".", SERVED_PATH), { recursive: true });
  await writeFile(SERVED_PATH, built);
  console.log(`synced ${built.length} bytes to backend/app/static/widget/widget.js`);
  process.exit(0);
}

let served;
try {
  served = await readFile(SERVED_PATH);
} catch (error) {
  if (error && typeof error === "object" && "code" in error && error.code === "ENOENT") {
    console.error("widget distribution is missing; run npm run sync-distribution after npm run build");
    process.exit(1);
  }
  throw error;
}

if (!built.equals(served)) {
  console.error("widget distribution drifted; run npm run sync-distribution after npm run build");
  process.exit(1);
}

console.log(`widget distribution matches build byte-for-byte (${built.length} bytes)`);

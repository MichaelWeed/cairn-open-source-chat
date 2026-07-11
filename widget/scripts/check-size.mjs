import { gzipSync } from "node:zlib";
import { readFileSync } from "node:fs";

const BUDGET_BYTES = 100 * 1024;
const BUNDLE_PATH = new URL("../dist/widget.js", import.meta.url);

const source = readFileSync(BUNDLE_PATH);
const gzipped = gzipSync(source);

console.log(`widget.js: ${source.length} bytes raw, ${gzipped.length} bytes gzipped`);

if (gzipped.length > BUDGET_BYTES) {
  console.error(`size budget exceeded: ${gzipped.length} > ${BUDGET_BYTES} bytes gzipped`);
  process.exit(1);
}

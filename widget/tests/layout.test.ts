import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

async function main(): Promise<void> {
  const source = await readFile(new URL("../src/index.ts", import.meta.url), "utf8");

  assert.match(
    source,
    /\.panel \{[^}]*display: grid;[^}]*grid-template-rows: auto minmax\(0, 1fr\) auto;[^}]*height: min\(38rem, calc\(100vh - 6\.5rem\)\);[^}]*overflow: hidden;[^}]*\}/s,
    "the panel must reserve fixed header and composer tracks while its message track can shrink",
  );
  assert.match(
    source,
    /\.messages \{[^}]*min-height: 0;[^}]*overflow-y: auto;[^}]*\}/s,
    "the message region must be allowed to shrink and scroll instead of overlapping the composer",
  );
  assert.match(
    source,
    /@media \(max-width: 480px\) \{[\s\S]*?\.panel \{[^}]*height: calc\(100vh - 5\.25rem\);[^}]*\}/,
    "the mobile panel must use the same bounded-height grid contract",
  );

  console.log("widget layout tests passed");
}

void main();

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

async function main(): Promise<void> {
  const source = await readFile(new URL("../src/index.ts", import.meta.url), "utf8");

  assert.match(
    source,
    /\.panel \{[^}]*display: grid;[^}]*grid-template-rows: auto minmax\(0, 1fr\) auto;[^}]*height: min\(38rem, calc\(100vh[^}]*height: min\(38rem, calc\(100dvh[^}]*overflow: hidden;[^}]*\}/s,
    "the panel must reserve fixed header and composer tracks while its message track can shrink",
  );
  assert.match(
    source,
    /\.messages \{[^}]*min-height: 0;[^}]*overflow-y: auto;[^}]*padding: 0 1rem;[^}]*\}/s,
    "the message region must shrink and scroll without vertical box padding overlapping the composer",
  );
  assert.match(
    source,
    /article:first-of-type \{ margin-top: 1rem; \}[\s\S]*article:last-of-type \{ margin-bottom: 1rem; \}/,
    "the scrollable message content must retain its established vertical inset",
  );
  assert.match(
    source,
    /@media \(max-width: 480px\) \{[\s\S]*?\.panel \{[^}]*height: calc\(100vh[^}]*height: calc\(100dvh[^}]*\}/,
    "the mobile panel must use the same bounded-height grid contract",
  );
  for (const edge of ["top", "right", "bottom", "left"]) {
    assert.match(source, new RegExp(`env\\(safe-area-inset-${edge}, 0px\\)`));
  }
  assert.match(source, /@media \(prefers-reduced-motion: reduce\)/);
  assert.match(source, /@media \(forced-colors: active\)/);
  assert.match(source, /button, a \{ min-height: 44px; min-width: 44px; \}/);
  assert.doesNotMatch(source, /maxlength="500"/);

  console.log("widget layout tests passed");
}

void main();

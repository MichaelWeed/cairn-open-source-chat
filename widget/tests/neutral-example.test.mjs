import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const exampleDirectory = new URL("../../examples/neutral-site/", import.meta.url);
const html = await readFile(new URL("index.html", exampleDirectory), "utf8");
const css = await readFile(new URL("styles.css", exampleDirectory), "utf8");
const instructions = await readFile(new URL("README.md", exampleDirectory), "utf8");
const publicText = `${html}\n${css}\n${instructions}`;

function matches(pattern, text = html) {
  return [...text.matchAll(pattern)];
}

function openingTag(name) {
  const found = html.match(new RegExp(`<${name}\\b[^>]*>`, "iu"));
  assert.ok(found, `expected one ${name} element`);
  return found[0];
}

function attribute(tag, name) {
  const found = tag.match(new RegExp(`\\s${name}="([^"]*)"`, "iu"));
  return found?.[1] ?? null;
}

assert.match(html, /^<!doctype html>/iu);
assert.equal(matches(/<html\b/giu).length, 1);
assert.equal(attribute(openingTag("html"), "lang"), "en");
assert.equal(matches(/<meta\s+name="viewport"\s+content="width=device-width, initial-scale=1"\s*\/?>/giu).length, 1);
assert.equal(matches(/<title>Example Support<\/title>/giu).length, 1);
assert.equal(matches(/<main\b/giu).length, 1);
assert.equal(matches(/<h1\b/giu).length, 1);
assert.equal(matches(/<h2\b/giu).length, 3);
assert.deepEqual(matches(/<h[1-6]\b/giu).map((item) => item[0].slice(1, 3).toLowerCase()), ["h1", "h2", "h2", "h2"]);

const main = openingTag("main");
const mainId = attribute(main, "id");
assert.equal(mainId, "main-content");
const skip = html.match(/<a\b[^>]*class="skip-link"[^>]*>/iu)?.[0];
assert.ok(skip, "expected one skip link");
assert.equal(attribute(skip, "href"), `#${mainId}`);
assert.equal(matches(/class="skip-link"/giu).length, 1);

const ids = matches(/\sid="([^"]+)"/giu).map((item) => item[1]);
assert.equal(new Set(ids).size, ids.length, "IDs must be unique");
assert.equal(matches(/<cairn-chat\b/giu).length, 1);
const widget = openingTag("cairn-chat");
assert.deepEqual(
  ["api-url", "assistant-name", "theme", "privacy-url", "handoff-url", "nonce"].map((name) => [name, attribute(widget, name)]),
  [
    ["api-url", "https://cairn.example"],
    ["assistant-name", "Example Support"],
    ["theme", "auto"],
    ["privacy-url", "https://example.test/privacy"],
    ["handoff-url", "https://example.test/support"],
    ["nonce", "REPLACE_WITH_RESPONSE_NONCE"],
  ],
);
assert.match(html, /Privacy and contact destinations are placeholders\./u);
assert.match(html, /<script nonce="REPLACE_WITH_RESPONSE_NONCE" src="https:\/\/cairn\.example\/widget\/widget\.js" referrerpolicy="no-referrer" defer><\/script>\n<cairn-chat /u);
assert.match(html, /<meta http-equiv="Content-Security-Policy"/u);
assert.match(html, /default-src 'none';/u);
assert.match(html, /script-src 'nonce-REPLACE_WITH_RESPONSE_NONCE';/u);
assert.match(html, /style-src 'self' 'nonce-REPLACE_WITH_RESPONSE_NONCE';/u);
assert.match(html, /connect-src https:\/\/cairn\.example;/u);
assert.match(html, /base-uri 'none';/u);
assert.match(html, /form-action 'none';/u);
assert.match(html, /frame-ancestors 'none'/u);
assert.doesNotMatch(publicText, /unsafe-inline|unsafe-eval/iu);

for (const topic of ["shipping", "returns", "warranty"]) assert.match(html, new RegExp(topic, "iu"));
for (const element of ["header", "main", "footer"]) assert.equal(matches(new RegExp(`<${element}\\b`, "giu")).length, 1);
assert.equal(matches(/\son[a-z]+\s*=/giu).length, 0, "inline event handlers are forbidden");
assert.equal(matches(/<(?:img|iframe|video|audio|source)\b/giu).length, 0);
assert.equal(matches(/<script\b/giu).length, 1);
assert.equal(matches(/<link\b/giu).length, 1);
assert.equal(attribute(openingTag("link"), "href"), "./styles.css");

const denied = [
  ["Voice", "Verdict"].join(""),
  ["voice", "verdict"].join("-"),
  ["voice", "verdict"].join("_"),
  ["customer", "zero"].join("-"),
  ["customer", "zero"].join(" "),
  ["private", "plan"].join(" "),
  ["/", "Users", "/"].join(""),
  "api_key",
  "authorization",
];
for (const token of denied) assert.equal(publicText.toLowerCase().includes(token.toLowerCase()), false, `denied public token: ${token}`);
for (const token of ["tracker", "analytics"]) assert.equal(`${html}\n${css}`.toLowerCase().includes(token), false);

const urls = matches(/https?:\/\/[^\s"'<>`)]+/giu, publicText).map((item) => item[0].replace(/[.,;:]$/u, ""));
for (const value of urls) {
  const url = new URL(value);
  assert.equal(["cairn.example", "example.test"].includes(url.hostname), true, `unexpected URL: ${value}`);
}

assert.match(css, /:focus-visible/u);
assert.match(css, /min-height:\s*44px/u);
assert.match(css, /min-width:\s*44px/u);
assert.match(css, /@media \(prefers-reduced-motion: reduce\)/u);
assert.match(css, /@media \(prefers-color-scheme: dark\)/u);
assert.match(css, /@media \(forced-colors: active\)/u);
assert.match(css, /env\(safe-area-inset-(?:top|right|bottom|left)/u);
assert.doesNotMatch(css, /gradient|backdrop-filter|@keyframes/iu);

assert.match(instructions, /python3 -m http\.server/u);
assert.match(instructions, /ORIGIN_ALLOWLIST=<exact origin of this page>/u);
assert.match(instructions, /replace every occurrence of `https:\/\/cairn\.example`/iu);
assert.match(instructions, /replace every occurrence of `REPLACE_WITH_RESPONSE_NONCE`/iu);
assert.match(instructions, /at least 128 random bits/u);
assert.match(instructions, /The example does not provide a hosted service/u);

console.log("neutral example static contract passed");

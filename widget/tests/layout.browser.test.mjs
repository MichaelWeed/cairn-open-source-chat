import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { access, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

import { build } from "esbuild";

const viewport = { width: 640, height: 400 };
const browserWindow = {
  width: viewport.width,
  // Headless Chrome on macOS includes a 143px native frame in --window-size.
  height: viewport.height + (process.platform === "darwin" ? 143 : 0),
};

async function browserPath() {
  const candidates = [
    process.env.CHROME_BIN,
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
  ].filter(Boolean);

  for (const candidate of candidates) {
    try {
      await access(candidate);
      return candidate;
    } catch {
      // Try the next supported browser location.
    }
  }
  throw new Error(
    "widget layout browser test requires Chrome or Chromium; set CHROME_BIN to its executable",
  );
}

function fixture(widgetSource) {
  const safeWidgetSource = widgetSource.replace(/<\/script/gi, "<\\/script");
  return `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>KAN-85 pending</title>
  <style>html, body { margin: 0; }</style>
</head>
<body>
  <cairn-chat api-url="invalid-fixture-url"></cairn-chat>
  <pre id="result"></pre>
  <script>${safeWidgetSource}</script>
  <script>
    const host = document.querySelector("cairn-chat");
    const root = host.shadowRoot;
    root.querySelector(".launcher").click();

    const rectangle = (selector) => {
      const bounds = root.querySelector(selector).getBoundingClientRect();
      return {
        top: bounds.top,
        right: bounds.right,
        bottom: bounds.bottom,
        left: bounds.left,
        width: bounds.width,
        height: bounds.height,
      };
    };
    const panel = rectangle(".panel");
    const messages = rectangle(".messages");
    const composer = rectangle("form");
    const error = rectangle(".error");
    const controls = [rectangle("textarea"), rectangle(".send"), rectangle(".close")];
    const tolerance = 0.5;
    const result = {
      viewport: { width: innerWidth, height: innerHeight },
      panel,
      messages,
      composer,
      error,
      overlapPixels: Math.max(0, messages.bottom - composer.top),
      panelInsideViewport:
        panel.top >= -tolerance &&
        panel.left >= -tolerance &&
        panel.right <= innerWidth + tolerance &&
        panel.bottom <= innerHeight + tolerance,
      controlsInsideViewport: controls.every(
        (control) =>
          control.top >= -tolerance &&
          control.left >= -tolerance &&
          control.right <= innerWidth + tolerance &&
          control.bottom <= innerHeight + tolerance,
      ),
      messagesDoNotOverlapComposer: messages.bottom <= composer.top + tolerance,
      errorVisible: !root.querySelector(".error").hidden,
    };
    document.querySelector("#result").textContent = JSON.stringify(result);
    document.title = result.messagesDoNotOverlapComposer ? "KAN-85 pass" : "KAN-85 fail";
  </script>
</body>
</html>`;
}

function runBrowser(browser, fixturePath, temporaryDirectory, windowSize, profileName) {
  return new Promise((resolve, reject) => {
    const child = spawn(
      browser,
      [
        "--headless=new",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-extensions",
        "--disable-gpu",
        "--disable-sync",
        "--no-default-browser-check",
        "--no-first-run",
        "--no-sandbox",
        "--force-device-scale-factor=1",
        `--window-size=${windowSize.width},${windowSize.height}`,
        "--dump-dom",
        `--user-data-dir=${join(temporaryDirectory, profileName)}`,
        pathToFileURL(fixturePath).href,
      ],
      { stdio: ["ignore", "pipe", "pipe"] },
    );
    let stdout = "";
    let stderr = "";
    let result;
    let failure;
    const timeout = setTimeout(() => {
      failure = new Error(`browser fixture timed out: ${stderr}`);
      child.kill("SIGKILL");
    }, 30_000);

    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (chunk) => {
      stdout += chunk;
      const match = stdout.match(/<pre id="result">([^<]+)<\/pre>/);
      if (match && result === undefined) {
        result = JSON.parse(match[1].replaceAll("&quot;", '"').replaceAll("&amp;", "&"));
        child.kill("SIGKILL");
      }
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk;
    });
    child.on("error", (error) => {
      failure = error;
      clearTimeout(timeout);
      reject(error);
    });
    child.on("exit", (code, signal) => {
      clearTimeout(timeout);
      if (failure) {
        reject(failure);
      } else if (result !== undefined) {
        resolve(result);
      } else {
        reject(
          new Error(
            `browser exited with code ${code ?? "none"} via ${signal ?? "no signal"} before reporting geometry: ${stderr}\n${stdout}`,
          ),
        );
      }
    });
  });
}

async function main() {
  const browser = await browserPath();
  const bundle = await build({
    entryPoints: [new URL("../src/index.ts", import.meta.url).pathname],
    bundle: true,
    minify: true,
    write: false,
  });
  const temporaryDirectory = await mkdtemp(join(tmpdir(), "cairn-widget-layout-"));
  const fixturePath = join(temporaryDirectory, "fixture.html");

  try {
    await writeFile(fixturePath, fixture(bundle.outputFiles[0].text), "utf8");
    const result = await runBrowser(
      browser,
      fixturePath,
      temporaryDirectory,
      browserWindow,
      "fixture-profile",
    );

    assert.deepEqual(result.viewport, viewport, "browser must render the intended 640x400 viewport");
    assert.equal(result.errorVisible, true, "fixture must exercise the visible error state");
    assert.equal(result.panelInsideViewport, true, "panel must remain inside the viewport");
    assert.equal(result.controlsInsideViewport, true, "close and composer controls must stay reachable");
    assert.equal(
      result.messagesDoNotOverlapComposer,
      true,
      `message region overlaps composer by ${result.overlapPixels}px`,
    );
    console.log(`widget rendered layout test passed: ${JSON.stringify(result)}`);
  } finally {
    await rm(temporaryDirectory, { recursive: true, force: true });
  }
}

await main();

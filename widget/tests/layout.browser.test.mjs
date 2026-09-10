import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { access, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

import { build } from "esbuild";

const viewport = { width: 640, height: 400 };
const cleanupRetryDelays = [50, 100, 200, 400, 800, 1_000];
const transientCleanupErrors = new Set(["EBUSY", "ENOTEMPTY"]);

function pause(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function removeTemporaryDirectory(
  path,
  { remove = rm, pause: wait = pause } = {},
) {
  for (let attempt = 0; ; attempt += 1) {
    try {
      await remove(path, { recursive: true, force: true });
      return;
    } catch (error) {
      if (
        !transientCleanupErrors.has(error?.code) ||
        attempt === cleanupRetryDelays.length
      ) {
        throw error;
      }
      await wait(cleanupRetryDelays[attempt]);
    }
  }
}

async function verifyCleanupRetryContract() {
  for (const code of transientCleanupErrors) {
    const transientError = Object.assign(new Error("profile directory is still busy"), {
      code,
    });
    const attempts = [];
    const delays = [];

    await removeTemporaryDirectory("fixture-profile", {
      remove: async (path, options) => {
        attempts.push({ path, options });
        if (attempts.length < 3) {
          throw transientError;
        }
      },
      pause: async (milliseconds) => {
        delays.push(milliseconds);
      },
    });

    assert.equal(attempts.length, 3, `cleanup must retry transient ${code}`);
    assert.deepEqual(delays, [50, 100], "cleanup retries must be bounded and back off");
    assert.deepEqual(attempts[0], {
      path: "fixture-profile",
      options: { recursive: true, force: true },
    });
  }

  const exhaustedError = Object.assign(new Error("profile never became idle"), {
    code: "ENOTEMPTY",
  });
  let exhaustedAttempts = 0;
  const exhaustedDelays = [];
  await assert.rejects(
    removeTemporaryDirectory("fixture-profile", {
      remove: async () => {
        exhaustedAttempts += 1;
        throw exhaustedError;
      },
      pause: async (milliseconds) => {
        exhaustedDelays.push(milliseconds);
      },
    }),
    (error) => error === exhaustedError,
  );
  assert.equal(
    exhaustedAttempts,
    cleanupRetryDelays.length + 1,
    "cleanup must stop after its bounded retries",
  );
  assert.deepEqual(
    exhaustedDelays,
    cleanupRetryDelays,
    "cleanup must use only the declared retry window",
  );

  const unexpectedError = Object.assign(new Error("permission denied"), { code: "EACCES" });
  let unexpectedAttempts = 0;
  await assert.rejects(
    removeTemporaryDirectory("fixture-profile", {
      remove: async () => {
        unexpectedAttempts += 1;
        throw unexpectedError;
      },
      pause: async () => {
        throw new Error("unexpected cleanup error must not be retried");
      },
    }),
    (error) => error === unexpectedError,
  );
  assert.equal(unexpectedAttempts, 1, "cleanup must preserve unexpected failures");
  console.log("widget browser cleanup contract tests passed");
}

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
    const viewportBounds = { top: 0, right: innerWidth, bottom: innerHeight, left: 0 };
    const containedBy = (inner, outer) =>
      inner.top >= outer.top - tolerance &&
      inner.left >= outer.left - tolerance &&
      inner.right <= outer.right + tolerance &&
      inner.bottom <= outer.bottom + tolerance;
    const errorElement = root.querySelector(".error");
    const errorStyle = getComputedStyle(errorElement);
    const result = {
      viewport: { width: innerWidth, height: innerHeight },
      panel,
      messages,
      composer,
      error,
      controls,
      overlapPixels: Math.max(0, messages.bottom - composer.top),
      panelInsideViewport: containedBy(panel, viewportBounds),
      controlsInsideViewport: controls.every((control) => containedBy(control, viewportBounds)),
      controlsInsidePanel: controls.every((control) => containedBy(control, panel)),
      errorInsideViewport: containedBy(error, viewportBounds),
      errorInsidePanel: containedBy(error, panel),
      messagesDoNotOverlapComposer: messages.bottom <= composer.top + tolerance,
      errorVisible:
        !errorElement.hidden &&
        errorStyle.display !== "none" &&
        errorStyle.visibility !== "hidden" &&
        Number(errorStyle.opacity) > 0 &&
        error.width > tolerance &&
        error.height > tolerance,
    };
    document.querySelector("#result").textContent = JSON.stringify(result);
    document.title = result.messagesDoNotOverlapComposer ? "KAN-85 pass" : "KAN-85 fail";
  </script>
</body>
</html>`;
}

function waitForDebuggerUrl(child, stderr) {
  return new Promise((resolve, reject) => {
    const onData = (chunk) => {
      const match = chunk.match(/DevTools listening on (ws:\/\/\S+)/);
      if (match) {
        cleanup();
        resolve(match[1]);
      }
    };
    const onError = (error) => {
      cleanup();
      reject(error);
    };
    const onExit = (code, signal) => {
      cleanup();
      reject(
        new Error(
          `browser exited with code ${code ?? "none"} via ${signal ?? "no signal"} before opening DevTools: ${stderr()}`,
        ),
      );
    };
    const cleanup = () => {
      child.stderr.off("data", onData);
      child.off("error", onError);
      child.off("exit", onExit);
    };

    child.stderr.on("data", onData);
    child.once("error", onError);
    child.once("exit", onExit);
  });
}

async function connectToCdp(webSocketUrl) {
  const socket = new WebSocket(webSocketUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", reject, { once: true });
  });

  let nextId = 0;
  const pending = new Map();
  const closed = new Promise((resolve) => {
    socket.addEventListener("close", resolve, { once: true });
  });
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(String(event.data));
    const waiter = pending.get(message.id);
    if (waiter === undefined) {
      return;
    }
    pending.delete(message.id);
    if (message.error) {
      waiter.reject(new Error(`${waiter.method} failed: ${message.error.message}`));
    } else {
      waiter.resolve(message.result);
    }
  });
  socket.addEventListener("close", () => {
    for (const waiter of pending.values()) {
      waiter.reject(new Error(`DevTools closed before ${waiter.method} completed`));
    }
    pending.clear();
  });

  return {
    closed,
    close() {
      if (socket.readyState < WebSocket.CLOSING) {
        socket.close();
      }
    },
    send(method, params = {}, sessionId) {
      const id = ++nextId;
      return new Promise((resolve, reject) => {
        pending.set(id, { method, resolve, reject });
        socket.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
      });
    },
  };
}

async function settlesWithin(promise, milliseconds) {
  let timeout;
  try {
    return await Promise.race([
      promise.then(() => true),
      new Promise((resolve) => {
        timeout = setTimeout(() => resolve(false), milliseconds);
      }),
    ]);
  } finally {
    clearTimeout(timeout);
  }
}

async function stopBrowser(child, cdp, closed) {
  let gracefulCloseError;
  if (cdp !== undefined && child.exitCode === null && child.signalCode === null) {
    try {
      await settlesWithin(cdp.send("Browser.close"), 3_000);
    } catch (error) {
      gracefulCloseError = error;
    }
    await settlesWithin(Promise.race([cdp.closed, closed]), 3_000);
  }

  if (child.exitCode === null && child.signalCode === null) {
    child.kill("SIGTERM");
  }
  if (!(await settlesWithin(closed, 3_000))) {
    child.kill("SIGKILL");
    if (!(await settlesWithin(closed, 3_000))) {
      throw new Error("browser did not stop after bounded graceful and forced shutdown");
    }
  }

  if (cdp !== undefined) {
    cdp.close();
    if (!(await settlesWithin(cdp.closed, 3_000))) {
      throw new Error("DevTools socket did not close after browser shutdown");
    }
  }
  if (gracefulCloseError !== undefined) {
    throw gracefulCloseError;
  }
}

async function runBrowser(browser, fixturePath, temporaryDirectory, profileName) {
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
      "--remote-allow-origins=*",
      "--remote-debugging-address=127.0.0.1",
      "--remote-debugging-port=0",
      `--user-data-dir=${join(temporaryDirectory, profileName)}`,
      "about:blank",
    ],
    { stdio: ["ignore", "ignore", "pipe"] },
  );
  child.stderr.setEncoding("utf8");
  let stderr = "";
  child.stderr.on("data", (chunk) => {
    stderr += chunk;
  });
  const closed = new Promise((resolve) => {
    child.once("close", resolve);
  });
  let cdp;
  let timeout;

  try {
    const result = await Promise.race([
      (async () => {
        const debuggerUrl = await waitForDebuggerUrl(child, () => stderr);
        cdp = await connectToCdp(debuggerUrl);
        const { targetId } = await cdp.send("Target.createTarget", { url: "about:blank" });
        const { sessionId } = await cdp.send("Target.attachToTarget", {
          targetId,
          flatten: true,
        });
        // Set the CSS viewport directly instead of guessing each platform's native frame size.
        await cdp.send(
          "Emulation.setDeviceMetricsOverride",
          {
            width: viewport.width,
            height: viewport.height,
            deviceScaleFactor: 1,
            mobile: false,
            screenWidth: viewport.width,
            screenHeight: viewport.height,
          },
          sessionId,
        );
        await cdp.send("Page.navigate", { url: pathToFileURL(fixturePath).href }, sessionId);

        for (;;) {
          const evaluation = await cdp.send(
            "Runtime.evaluate",
            {
              expression: 'document.querySelector("#result")?.textContent ?? ""',
              returnByValue: true,
            },
            sessionId,
          );
          if (evaluation.result.value) {
            return JSON.parse(evaluation.result.value);
          }
          await new Promise((resolve) => setTimeout(resolve, 25));
        }
      })(),
      new Promise((_, reject) => {
        timeout = setTimeout(() => {
          reject(new Error(`browser fixture timed out: ${stderr}`));
        }, 30_000);
      }),
    ]);
    return result;
  } finally {
    clearTimeout(timeout);
    await stopBrowser(child, cdp, closed);
  }
}

async function main() {
  await verifyCleanupRetryContract();
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
      "fixture-profile",
    );

    assert.deepEqual(result.viewport, viewport, "browser must render the intended 640x400 viewport");
    assert.equal(result.errorVisible, true, "fixture must exercise the visible error state");
    assert.equal(result.panelInsideViewport, true, "panel must remain inside the viewport");
    assert.equal(result.controlsInsideViewport, true, "close and composer controls must stay reachable");
    assert.equal(result.controlsInsidePanel, true, "close and composer controls must stay inside the panel");
    assert.equal(result.errorInsideViewport, true, "error banner must remain inside the viewport");
    assert.equal(result.errorInsidePanel, true, "error banner must remain inside the panel");
    assert.equal(
      result.messagesDoNotOverlapComposer,
      true,
      `message region overlaps composer by ${result.overlapPixels}px`,
    );
    console.log(`widget rendered layout test passed: ${JSON.stringify(result)}`);
  } finally {
    await removeTemporaryDirectory(temporaryDirectory);
  }
}

await main();

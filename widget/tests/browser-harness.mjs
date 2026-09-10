import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import dns from "node:dns";
import { createServer } from "node:http";
import { access, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { isAbsolute, join } from "node:path";

const cleanupRetryDelays = [50, 100, 200, 400, 800, 1_000];
const transientCleanupErrors = new Set(["EBUSY", "ENOTEMPTY"]);

function pause(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

export async function removeTemporaryDirectory(
  path,
  { remove = rm, pause: wait = pause } = {},
) {
  for (let attempt = 0; ; attempt += 1) {
    try {
      await remove(path, { recursive: true, force: true });
      return;
    } catch (error) {
      if (!transientCleanupErrors.has(error?.code) || attempt === cleanupRetryDelays.length) {
        throw error;
      }
      await wait(cleanupRetryDelays[attempt]);
    }
  }
}

export async function browserPath() {
  if (process.env.CHROME_BIN !== undefined && !isAbsolute(process.env.CHROME_BIN)) {
    throw new Error("CHROME_BIN must be an absolute executable path");
  }
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
      // Continue through the fixed local-browser candidate list.
    }
  }
  throw new Error("widget browser tests require installed Chrome/Chromium; set absolute CHROME_BIN");
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
      reject(new Error(`browser exited ${code ?? "none"}/${signal ?? "none"} before DevTools: ${stderr()}`));
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
  const listeners = new Set();
  const closed = new Promise((resolve) => socket.addEventListener("close", resolve, { once: true }));
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(String(event.data));
    if (message.id === undefined) {
      for (const listener of listeners) listener(message);
      return;
    }
    const waiter = pending.get(message.id);
    if (waiter === undefined) return;
    pending.delete(message.id);
    if (message.error) waiter.reject(new Error(`${waiter.method} failed: ${message.error.message}`));
    else waiter.resolve(message.result);
  });
  socket.addEventListener("close", () => {
    for (const waiter of pending.values()) waiter.reject(new Error(`DevTools closed before ${waiter.method} completed`));
    pending.clear();
  });
  return {
    closed,
    onEvent(listener) { listeners.add(listener); return () => listeners.delete(listener); },
    close() { if (socket.readyState < WebSocket.CLOSING) socket.close(); },
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
      new Promise((resolve) => { timeout = setTimeout(() => resolve(false), milliseconds); }),
    ]);
  } finally {
    clearTimeout(timeout);
  }
}

export async function stopBrowser(child, cdp, closed) {
  let gracefulCloseError;
  if (cdp !== undefined && child.exitCode === null && child.signalCode === null) {
    try {
      await settlesWithin(cdp.send("Browser.close"), 3_000);
    } catch (error) {
      gracefulCloseError = error;
    }
    await settlesWithin(Promise.race([cdp.closed, closed]), 3_000);
  }
  if (child.exitCode === null && child.signalCode === null) child.kill("SIGTERM");
  if (!(await settlesWithin(closed, 3_000))) {
    child.kill("SIGKILL");
    if (!(await settlesWithin(closed, 3_000))) {
      throw new Error("browser did not stop after bounded graceful and forced shutdown", { cause: gracefulCloseError });
    }
  }
  if (cdp !== undefined) {
    cdp.close();
    if (!(await settlesWithin(cdp.closed, 3_000))) {
      throw new Error("DevTools socket did not close after browser shutdown", { cause: gracefulCloseError });
    }
  }
}

export async function verifyHarnessContracts() {
  for (const code of transientCleanupErrors) {
    const transientError = Object.assign(new Error("profile busy"), { code });
    const delays = [];
    let attempts = 0;
    await removeTemporaryDirectory("fixture-profile", {
      remove: async () => { attempts += 1; if (attempts < 3) throw transientError; },
      pause: async (milliseconds) => delays.push(milliseconds),
    });
    assert.equal(attempts, 3);
    assert.deepEqual(delays, [50, 100]);
  }
  const exhausted = Object.assign(new Error("profile busy"), { code: "ENOTEMPTY" });
  let exhaustedAttempts = 0;
  await assert.rejects(removeTemporaryDirectory("fixture-profile", {
    remove: async () => { exhaustedAttempts += 1; throw exhausted; },
    pause: async () => undefined,
  }), (error) => error === exhausted);
  assert.equal(exhaustedAttempts, cleanupRetryDelays.length + 1);
  const unexpected = Object.assign(new Error("permission denied"), { code: "EACCES" });
  let unexpectedAttempts = 0;
  await assert.rejects(removeTemporaryDirectory("fixture-profile", {
    remove: async () => { unexpectedAttempts += 1; throw unexpected; },
  }), (error) => error === unexpected);
  assert.equal(unexpectedAttempts, 1);

  let resolveProcessClose;
  let resolveSocketClose;
  const processClosed = new Promise((resolve) => { resolveProcessClose = resolve; });
  const socketClosed = new Promise((resolve) => { resolveSocketClose = resolve; });
  const signals = [];
  const child = { exitCode: null, signalCode: null, kill(signal) { signals.push(signal); } };
  const cdp = {
    closed: socketClosed,
    close() {},
    send() {
      return new Promise((_, reject) => queueMicrotask(() => {
        child.exitCode = 0;
        resolveSocketClose();
        resolveProcessClose();
        reject(new Error("DevTools closed before Browser.close completed"));
      }));
    },
  };
  await stopBrowser(child, cdp, processClosed);
  assert.deepEqual(signals, []);

  await assert.rejects(
    withMatrixDeadline(() => new Promise(() => undefined), 5),
    /total deadline/u,
  );
  const server = await startLoopbackServer((_request, response) => response.end("ok"));
  await server.close();
}

export function installDnsGuards() {
  const blocked = new Error("DNS is disabled in the local widget browser harness");
  const originals = {
    lookup: dns.lookup,
    resolve: dns.resolve,
    promiseLookup: dns.promises.lookup,
    promiseResolve: dns.promises.resolve,
  };
  dns.lookup = (...args) => {
    const callback = args.at(-1);
    if (typeof callback !== "function") throw blocked;
    queueMicrotask(() => callback(blocked));
  };
  dns.resolve = (...args) => {
    const callback = args.at(-1);
    if (typeof callback !== "function") throw blocked;
    queueMicrotask(() => callback(blocked));
  };
  dns.promises.lookup = async () => { throw blocked; };
  dns.promises.resolve = async () => { throw blocked; };
  return {
    async verify() {
      await assert.rejects(new Promise((resolve, reject) => dns.lookup("blocked.invalid", (error) => error ? reject(error) : resolve())), (error) => error === blocked);
      await assert.rejects(new Promise((resolve, reject) => dns.resolve("blocked.invalid", (error) => error ? reject(error) : resolve())), (error) => error === blocked);
      await assert.rejects(dns.promises.lookup("blocked.invalid"), (error) => error === blocked);
      await assert.rejects(dns.promises.resolve("blocked.invalid"), (error) => error === blocked);
    },
    restore() {
      dns.lookup = originals.lookup;
      dns.resolve = originals.resolve;
      dns.promises.lookup = originals.promiseLookup;
      dns.promises.resolve = originals.promiseResolve;
    },
  };
}

export async function startLoopbackServer(handler) {
  const sockets = new Set();
  const server = createServer((request, response) => {
    Promise.resolve(handler(request, response)).catch(() => {
      if (!response.headersSent) response.writeHead(500, { "content-type": "text/plain" });
      response.end("fixture failure");
    });
  });
  server.on("connection", (socket) => {
    sockets.add(socket);
    socket.once("close", () => sockets.delete(socket));
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const address = server.address();
  if (address === null || typeof address === "string") throw new Error("loopback server has no port");
  return {
    origin: `http://127.0.0.1:${address.port}`,
    async close() {
      server.closeAllConnections?.();
      for (const socket of sockets) socket.destroy();
      await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
    },
  };
}

export async function runBrowserScenario({
  browser,
  url,
  viewport,
  allowedOrigins,
  expression = 'document.querySelector("#result")?.textContent ?? ""',
  mediaFeatures = [],
  safeAreaInsets,
  timeoutMilliseconds = 30_000,
}) {
  const temporaryDirectory = await mkdtemp(join(tmpdir(), "cairn-widget-browser-"));
  const child = spawn(browser, [
    "--headless=new",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-extensions",
    "--disable-gpu",
    "--disable-sync",
    "--no-default-browser-check",
    "--no-first-run",
    "--no-sandbox",
    "--no-proxy-server",
    "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
    "--remote-allow-origins=*",
    "--remote-debugging-address=127.0.0.1",
    "--remote-debugging-port=0",
    `--user-data-dir=${join(temporaryDirectory, "profile")}`,
    "about:blank",
  ], { stdio: ["ignore", "ignore", "pipe"] });
  child.stderr.setEncoding("utf8");
  let stderr = "";
  child.stderr.on("data", (chunk) => { stderr += chunk; });
  const closed = new Promise((resolve) => child.once("exit", resolve));
  let cdp;
  const deadline = Date.now() + timeoutMilliseconds;
  let startupTimer;
  try {
    const debuggerUrl = await Promise.race([
      waitForDebuggerUrl(child, () => stderr),
      new Promise((_, reject) => {
        startupTimer = setTimeout(
          () => reject(new Error(`browser scenario timed out before DevTools: ${stderr}`)),
          timeoutMilliseconds,
        );
      }),
    ]);
    clearTimeout(startupTimer);
        cdp = await connectToCdp(debuggerUrl);
        const version = await cdp.send("Browser.getVersion");
        const { targetId } = await cdp.send("Target.createTarget", { url: "about:blank" });
        const { sessionId } = await cdp.send("Target.attachToTarget", { targetId, flatten: true });
        const requests = [];
        const exceptions = [];
        cdp.onEvent((event) => {
          if (event.sessionId === sessionId && event.method === "Network.requestWillBeSent") requests.push(event.params.request.url);
          if (event.sessionId === sessionId && event.method === "Runtime.exceptionThrown") {
            exceptions.push(event.params.exceptionDetails.exception?.description ?? event.params.exceptionDetails.text);
          }
        });
        await cdp.send("Network.enable", {}, sessionId);
        await cdp.send("Page.enable", {}, sessionId);
        await cdp.send("Runtime.enable", {}, sessionId);
        await cdp.send("Emulation.setDeviceMetricsOverride", {
          width: viewport.width,
          height: viewport.height,
          deviceScaleFactor: 1,
          mobile: false,
          screenWidth: viewport.width,
          screenHeight: viewport.height,
        }, sessionId);
        if (mediaFeatures.length > 0) {
          await cdp.send("Emulation.setEmulatedMedia", { features: mediaFeatures }, sessionId);
        }
        let safeAreaNegativeControl = null;
        if (safeAreaInsets !== undefined) {
          try {
            await cdp.send("Emulation.setSafeAreaInsets", { insets: safeAreaInsets }, sessionId);
            throw new Error("obsolete Emulation.setSafeAreaInsets unexpectedly succeeded");
          } catch (error) {
            if (error instanceof Error && error.message === "obsolete Emulation.setSafeAreaInsets unexpectedly succeeded") throw error;
            safeAreaNegativeControl = String(error);
          }
          try {
            await cdp.send("Emulation.setSafeAreaInsetsOverride", { insets: safeAreaInsets }, sessionId);
          } catch (error) {
            throw new Error(`installed ${version.product} CDP ${version.protocolVersion} lacks Emulation.setSafeAreaInsetsOverride`, { cause: error });
          }
        }
        await cdp.send("Page.navigate", { url }, sessionId);
        for (;;) {
          if (Date.now() >= deadline) {
            throw new Error(`browser scenario timed out with page exceptions ${JSON.stringify(exceptions)}: ${stderr}`);
          }
          const evaluation = await cdp.send("Runtime.evaluate", { expression, returnByValue: true }, sessionId);
          if (evaluation.result.value) {
            const unexpected = requests.filter((requestUrl) => {
              if (requestUrl === "about:blank" || requestUrl.startsWith("data:")) return false;
              try { return !allowedOrigins.includes(new URL(requestUrl).origin); }
              catch { return true; }
            });
            assert.deepEqual(unexpected, [], `browser made unexpected requests: ${unexpected.join(", ")}`);
            return { value: JSON.parse(evaluation.result.value), requests, product: version.product, protocolVersion: version.protocolVersion, safeAreaNegativeControl };
          }
          await pause(25);
        }
  } finally {
    clearTimeout(startupTimer);
    await stopBrowser(child, cdp, closed);
    await removeTemporaryDirectory(temporaryDirectory);
  }
}

export async function withMatrixDeadline(action, milliseconds = 300_000) {
  let timer;
  try {
    return await Promise.race([
      action(),
      new Promise((_, reject) => { timer = setTimeout(() => reject(new Error("widget browser matrix exceeded its total deadline")), milliseconds); }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}

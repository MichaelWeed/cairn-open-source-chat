import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import dns from "node:dns";
import { createServer } from "node:http";
import net from "node:net";
import { access, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { isAbsolute, join } from "node:path";

const cleanupRetryDelays = [50, 100, 200, 400, 800, 1_000];
const transientCleanupErrors = new Set(["EBUSY", "ENOTEMPTY"]);
const matrixCleanupRegistry = new WeakMap();

function pause(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function registerMatrixCleanup(signal, cleanup) {
  const cleanups = signal === undefined ? undefined : matrixCleanupRegistry.get(signal);
  cleanups?.add(cleanup);
  return () => cleanups?.delete(cleanup);
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

function beforeAbsoluteDeadline(promise, deadline, label, signal) {
  const remaining = deadline - Date.now();
  if (remaining <= 0) return Promise.reject(new Error(`browser scenario exceeded its absolute deadline during ${label}`));
  let timer;
  let onAbort;
  return Promise.race([
    promise,
    new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(`browser scenario exceeded its absolute deadline during ${label}`)), remaining); }),
    new Promise((_, reject) => {
      if (signal === undefined) return;
      onAbort = () => reject(new Error(`browser scenario cancelled during ${label}`));
      if (signal.aborted) onAbort();
      else signal.addEventListener("abort", onAbort, { once: true });
    }),
  ]).finally(() => {
    clearTimeout(timer);
    if (signal !== undefined && onAbort !== undefined) signal.removeEventListener("abort", onAbort);
  });
}

export async function stopBrowser(
  child,
  cdp,
  closed,
  { signalProcessGroup = process.kill } = {},
) {
  let gracefulCloseError;
  if (cdp !== undefined && child.exitCode === null && child.signalCode === null) {
    try {
      await settlesWithin(cdp.send("Browser.close"), 3_000);
    } catch (error) {
      gracefulCloseError = error;
    }
    await settlesWithin(Promise.race([cdp.closed, closed]), 3_000);
  }
  const groupSignaled = signalBrowserGroup(child, "SIGTERM", signalProcessGroup);
  if (child.exitCode === null && child.signalCode === null && !groupSignaled) child.kill("SIGTERM");
  if (!(await settlesWithin(closed, 3_000))) {
    const groupKilled = signalBrowserGroup(child, "SIGKILL", signalProcessGroup);
    if (!groupKilled) child.kill("SIGKILL");
    if (!(await settlesWithin(closed, 3_000))) {
      throw new Error("browser did not stop after bounded graceful and forced shutdown", { cause: gracefulCloseError });
    }
  }
  // The browser leader may exit before its renderer descendants. Always address the
  // detached group after the leader settles so no surviving child is orphaned.
  signalBrowserGroup(child, "SIGKILL", signalProcessGroup);
  if (cdp !== undefined) {
    cdp.close();
    if (!(await settlesWithin(cdp.closed, 3_000))) {
      throw new Error("DevTools socket did not close after browser shutdown", { cause: gracefulCloseError });
    }
  }
}

export async function cleanupBrowserResources({
  child,
  cdp,
  closed,
  temporaryDirectory,
  stop = stopBrowser,
  remove = removeTemporaryDirectory,
}) {
  let stopError;
  try {
    if (child !== undefined && closed !== undefined) await stop(child, cdp, closed);
  } catch (error) {
    stopError = error;
  }
  try {
    await remove(temporaryDirectory);
  } catch (removeError) {
    if (stopError !== undefined) throw new AggregateError([stopError, removeError], "browser and profile cleanup failed");
    throw removeError;
  }
  if (stopError !== undefined) throw stopError;
}

function signalBrowserGroup(child, signal, signalProcessGroup) {
  if (child.cairnProcessGroup === true && Number.isSafeInteger(child.pid)) {
    try {
      signalProcessGroup(-child.pid, signal);
      return true;
    } catch (error) {
      if (error?.code === "ESRCH") return false;
      throw error;
    }
  }
  return false;
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

  const groupSignals = [];
  const exitedLeader = { exitCode: 0, signalCode: null, pid: 4321, cairnProcessGroup: true, kill() { throw new Error("exited leader must not be signaled directly"); } };
  await stopBrowser(exitedLeader, undefined, Promise.resolve(), {
    signalProcessGroup(pid, signal) { groupSignals.push([pid, signal]); },
  });
  assert.deepEqual(groupSignals, [[-4321, "SIGTERM"], [-4321, "SIGKILL"]]);

  const stopFailure = new Error("synthetic browser shutdown failure");
  let removedAfterStopFailure = 0;
  await assert.rejects(cleanupBrowserResources({
    child: {},
    closed: Promise.resolve(),
    temporaryDirectory: "fixture-profile",
    stop: async () => { throw stopFailure; },
    remove: async () => { removedAfterStopFailure += 1; },
  }), (error) => error === stopFailure);
  assert.equal(removedAfterStopFailure, 1);

  let abortCleanupFinished = false;
  await assert.rejects(withMatrixDeadline((signal) => {
    let finishCleanup;
    const cleanup = new Promise((resolve) => { finishCleanup = resolve; });
    const unregister = registerMatrixCleanup(signal, cleanup);
    return new Promise((resolve) => signal.addEventListener("abort", () => {
      setTimeout(() => {
        abortCleanupFinished = true;
        finishCleanup();
        unregister();
        resolve();
      }, 20);
    }, { once: true }));
  }, 5), /total deadline/u);
  assert.equal(abortCleanupFinished, true);

  const hardDeadlineStarted = Date.now();
  await assert.rejects(withMatrixDeadline(() => new Promise(() => undefined), 5), /total deadline/u);
  assert.ok(Date.now() - hardDeadlineStarted < 250, "a callback that ignores AbortSignal must still reject promptly");
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
  const isLoopback = (hostname) => hostname === "127.0.0.1" || hostname === "::1";
  dns.lookup = (...args) => {
    if (isLoopback(args[0])) return originals.lookup(...args);
    const callback = args.at(-1);
    if (typeof callback !== "function") throw blocked;
    queueMicrotask(() => callback(blocked));
  };
  dns.resolve = (...args) => {
    if (isLoopback(args[0])) return originals.resolve(...args);
    const callback = args.at(-1);
    if (typeof callback !== "function") throw blocked;
    queueMicrotask(() => callback(blocked));
  };
  dns.promises.lookup = async (...args) => isLoopback(args[0]) ? originals.promiseLookup(...args) : Promise.reject(blocked);
  dns.promises.resolve = async (...args) => isLoopback(args[0]) ? originals.promiseResolve(...args) : Promise.reject(blocked);
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

function connectionHost(argumentsList) {
  const first = argumentsList[0];
  if (Array.isArray(first)) return connectionHost(first);
  if (first !== null && typeof first === "object") return first.host ?? first.hostname ?? null;
  if (typeof first === "number") return typeof argumentsList[1] === "string" ? argumentsList[1] : null;
  return null;
}

export function installSocketGuards() {
  const blocked = new Error("non-loopback sockets are disabled in the local widget browser harness");
  const originals = {
    connect: net.connect,
    createConnection: net.createConnection,
    socketConnect: net.Socket.prototype.connect,
  };
  const guard = (original) => function guardedConnect(...args) {
    const host = connectionHost(args);
    if (host !== "127.0.0.1") throw blocked;
    return original.apply(this, args);
  };
  net.Socket.prototype.connect = guard(originals.socketConnect);
  net.connect = guard(originals.connect);
  net.createConnection = guard(originals.createConnection);
  return {
    verify() {
      assert.throws(() => net.connect({ host: "blocked.invalid", port: 443 }), (error) => error === blocked);
      assert.throws(() => net.createConnection({ host: "blocked.invalid", port: 443 }), (error) => error === blocked);
      const socket = new net.Socket();
      assert.throws(() => socket.connect({ host: "blocked.invalid", port: 443 }), (error) => error === blocked);
      socket.destroy();
    },
    restore() {
      net.connect = originals.connect;
      net.createConnection = originals.createConnection;
      net.Socket.prototype.connect = originals.socketConnect;
    },
  };
}

export async function startLoopbackServer(handler) {
  const sockets = new Set();
  const server = createServer((request, response) => {
    Promise.resolve().then(() => handler(request, response)).catch(() => {
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
  deviceScaleFactor = 1,
  expression = 'document.querySelector("#result")?.textContent ?? ""',
  interact,
  mediaFeatures = [],
  onTemporaryDirectory = () => undefined,
  physicalViewport,
  safeAreaInsets,
  signal,
  timeoutMilliseconds = 30_000,
}) {
  const temporaryDirectory = await mkdtemp(join(tmpdir(), "cairn-widget-browser-"));
  let resolveCleanup;
  const cleanupComplete = new Promise((resolve) => { resolveCleanup = resolve; });
  const unregisterCleanup = registerMatrixCleanup(signal, cleanupComplete);
  const detached = process.platform !== "win32";
  let child;
  let closed;
  let stderr = "";
  let cdp;
  const requests = [];
  const exceptions = [];
  const deadline = Date.now() + timeoutMilliseconds;
  let startupTimer;
  try {
    onTemporaryDirectory(temporaryDirectory);
    child = spawn(browser, [
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
    ], { detached, stdio: ["ignore", "ignore", "pipe"] });
    child.cairnProcessGroup = detached;
    child.stderr.setEncoding("utf8");
    child.stderr.on("data", (chunk) => { stderr = `${stderr}${chunk}`.slice(-65_536); });
    closed = new Promise((resolve) => {
      child.once("exit", resolve);
      child.once("error", resolve);
    });
    const debuggerUrl = await Promise.race([
      beforeAbsoluteDeadline(waitForDebuggerUrl(child, () => stderr), deadline, "browser startup", signal),
      new Promise((_, reject) => {
        startupTimer = setTimeout(
          () => reject(new Error(`browser scenario timed out before DevTools: ${stderr}`)),
          timeoutMilliseconds,
        );
      }),
    ]);
    clearTimeout(startupTimer);
        cdp = await beforeAbsoluteDeadline(connectToCdp(debuggerUrl), deadline, "CDP connection", signal);
        const send = (method, params = {}, sessionId) => beforeAbsoluteDeadline(cdp.send(method, params, sessionId), deadline, method, signal);
        const version = await send("Browser.getVersion");
        const { targetId } = await send("Target.createTarget", { url: "about:blank" });
        await send("Target.activateTarget", { targetId });
        const { sessionId } = await send("Target.attachToTarget", { targetId, flatten: true });
        cdp.onEvent((event) => {
          if (event.sessionId === sessionId && event.method === "Network.requestWillBeSent") requests.push(event.params.request.url);
          if (event.sessionId === sessionId && event.method === "Runtime.exceptionThrown") {
            exceptions.push(event.params.exceptionDetails.exception?.description ?? event.params.exceptionDetails.text);
          }
        });
        await send("Network.enable", {}, sessionId);
        await send("Page.enable", {}, sessionId);
        await send("Runtime.enable", {}, sessionId);
        await send("Emulation.setDeviceMetricsOverride", {
          width: viewport.width,
          height: viewport.height,
          deviceScaleFactor,
          mobile: false,
          screenWidth: physicalViewport?.width ?? viewport.width,
          screenHeight: physicalViewport?.height ?? viewport.height,
        }, sessionId);
        if (mediaFeatures.length > 0) {
          await send("Emulation.setEmulatedMedia", { features: mediaFeatures }, sessionId);
        }
        let safeAreaNegativeControl = null;
        if (safeAreaInsets !== undefined) {
          try {
            await send("Emulation.setSafeAreaInsets", { insets: safeAreaInsets }, sessionId);
            throw new Error("obsolete Emulation.setSafeAreaInsets unexpectedly succeeded");
          } catch (error) {
            if (error instanceof Error && error.message === "obsolete Emulation.setSafeAreaInsets unexpectedly succeeded") throw error;
            safeAreaNegativeControl = String(error);
          }
          try {
            await send("Emulation.setSafeAreaInsetsOverride", { insets: safeAreaInsets }, sessionId);
          } catch (error) {
            throw new Error(`installed ${version.product} CDP ${version.protocolVersion} lacks Emulation.setSafeAreaInsetsOverride`, { cause: error });
          }
        }
        await send("Page.navigate", { url }, sessionId);
        await send("Page.bringToFront", {}, sessionId);
        if (interact !== undefined) {
          await beforeAbsoluteDeadline(interact({
            evaluate: (expressionText, awaitPromise = false) => send("Runtime.evaluate", { expression: expressionText, awaitPromise, returnByValue: true }, sessionId),
            key: async (key, modifiers = 0) => {
              const codes = { Tab: ["Tab", 9], Enter: ["Enter", 13], Escape: ["Escape", 27] };
              const [code, virtualKeyCode] = codes[key] ?? [key, 0];
              const parameters = { key, code, modifiers, windowsVirtualKeyCode: virtualKeyCode, nativeVirtualKeyCode: virtualKeyCode, ...(key === "Enter" ? { text: "\r", unmodifiedText: "\r" } : {}) };
              await send("Input.dispatchKeyEvent", { type: key === "Enter" ? "keyDown" : "rawKeyDown", ...parameters }, sessionId);
              await send("Input.dispatchKeyEvent", { type: "keyUp", ...parameters }, sessionId);
            },
            text: (value) => send("Input.insertText", { text: value }, sessionId),
          }), deadline, "interaction", signal);
        }
        for (;;) {
          assertAllowedBrowserRequests(requests, allowedOrigins);
          if (Date.now() >= deadline) {
            throw new Error(`browser scenario timed out with page exceptions ${JSON.stringify(exceptions)}: ${stderr}`);
          }
          const evaluation = await send("Runtime.evaluate", { expression, returnByValue: true }, sessionId);
          if (evaluation.result.value) {
            assertAllowedBrowserRequests(requests, allowedOrigins);
            return { value: JSON.parse(evaluation.result.value), requests, product: version.product, protocolVersion: version.protocolVersion, safeAreaNegativeControl };
          }
          await pause(25);
        }
  } finally {
    clearTimeout(startupTimer);
    try {
      await cleanupBrowserResources({ child, cdp, closed, temporaryDirectory });
    } finally {
      resolveCleanup();
      unregisterCleanup();
    }
  }
}

function assertAllowedBrowserRequests(requests, allowedOrigins) {
  const unexpected = requests.filter((requestUrl) => {
    if (requestUrl === "about:blank" || requestUrl.startsWith("data:")) return false;
    try { return !allowedOrigins.includes(new URL(requestUrl).origin); }
    catch { return true; }
  });
  assert.deepEqual(unexpected, [], `browser made unexpected requests: ${unexpected.join(", ")}`);
}

export async function withMatrixDeadline(action, milliseconds = 300_000) {
  const controller = new AbortController();
  const cleanups = new Set();
  matrixCleanupRegistry.set(controller.signal, cleanups);
  const deadlineError = new Error("widget browser matrix exceeded its total deadline");
  let timer;
  const actionPromise = Promise.resolve()
    .then(() => action(controller.signal))
    .then((result) => {
      if (controller.signal.aborted) throw deadlineError;
      return result;
    }, (error) => {
      if (controller.signal.aborted) throw deadlineError;
      throw error;
    });
  const deadlinePromise = new Promise((_, reject) => {
    timer = setTimeout(() => {
      controller.abort();
      if (cleanups.size === 0) {
        reject(deadlineError);
        return;
      }
      Promise.allSettled([...cleanups]).then(() => reject(deadlineError));
    }, milliseconds);
  });
  try {
    return await Promise.race([actionPromise, deadlinePromise]);
  } finally {
    clearTimeout(timer);
    matrixCleanupRegistry.delete(controller.signal);
  }
}

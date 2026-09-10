import assert from "node:assert/strict";

class FakeHTMLElement {
  attachShadow(): Record<string, never> {
    return {};
  }

  getAttribute(): string {
    return "https://support.example.test";
  }

  get isConnected(): boolean {
    return true;
  }

  dispatchEvent(): boolean {
    return true;
  }
}

const registry = new Map<string, unknown>();
Object.assign(globalThis, {
  HTMLElement: FakeHTMLElement,
  customElements: {
    define: (name: string, constructor: unknown) => registry.set(name, constructor),
    get: (name: string) => registry.get(name),
  },
});

const { CairnChat } = await import("../src/index");
const { CHAT_RESPONSE_MAX_BYTES } = await import("../src/protocol");

interface RequestBody {
  session_id: string;
  message: string;
  history: Array<{ role: "user" | "assistant"; content: string }>;
}

function sseResponse(delta: string, finishReason: "stop" | "limit"): Response {
  const chunk = delta === "" ? "" : `event: chunk\ndata: ${JSON.stringify({ type: "chunk", delta })}\n\n`;
  const done = `event: done\ndata: ${JSON.stringify({ type: "done", finish_reason: finishReason })}\n\n`;
  return new Response(chunk + done, { status: 200 });
}

function assistantExchange(): Record<string, unknown> {
  return {
    article: {
      dataset: {},
      classList: { add: () => undefined, remove: () => undefined },
    },
    content: { textContent: "" },
    citations: { append: () => undefined, replaceChildren: () => undefined },
    status: { textContent: "" },
    text: "",
    finishReason: null,
    citationCount: 0,
  };
}

function testWidget(): Record<string, unknown> {
  const widget = new CairnChat() as unknown as Record<string, unknown>;
  widget.controller = null;
  widget.chatController = null;
  widget.history = [];
  widget.sessionId = "session-1";
  widget.configuration = {
    apiBase: "https://support.example.test",
    assistantName: "Cairn",
    theme: "auto",
    privacyUrl: null,
    handoffUrl: null,
  };
  widget.compatibleBase = "https://support.example.test";
  widget.state = "ready";
  widget.generation = 0;
  widget.input = { value: "", disabled: false, setAttribute: () => undefined };
  widget.counter = { textContent: "" };
  widget.sendButton = { disabled: false };
  widget.clearButton = { disabled: false };
  widget.messages = {
    setAttribute(this: Record<string, unknown>, name: string, value: string) {
      this[name] = value;
    },
    querySelector: () => null,
    querySelectorAll: () => [],
    scrollTop: 0,
    scrollHeight: 0,
  };
  widget.emptyState = { textContent: "", hidden: false };
  widget.clearError = () => undefined;
  widget.hideHandoff = () => undefined;
  widget.showHandoff = () => undefined;
  widget.appendMessage = () => undefined;
  widget.appendAssistant = assistantExchange;
  return widget;
}

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (error: unknown) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

class FakeClock {
  current = 0;
  private nextId = 0;
  private readonly timers = new Map<number, { due: number; callback: () => void }>();

  now = (): number => this.current;

  setTimeout = (callback: () => void, milliseconds: number): number => {
    const id = ++this.nextId;
    this.timers.set(id, { due: this.current + Math.max(0, milliseconds), callback });
    return id;
  };

  clearTimeout = (id: number): void => {
    this.timers.delete(id);
  };

  advance(milliseconds: number): void {
    this.current += milliseconds;
    for (;;) {
      const due = [...this.timers.entries()]
        .filter(([, timer]) => timer.due <= this.current)
        .sort((left, right) => left[1].due - right[1].due || left[0] - right[0]);
      if (due.length === 0) return;
      const [id, timer] = due[0];
      this.timers.delete(id);
      timer.callback();
    }
  }
}

function hostileCancellation(onCatch: () => void): Promise<void> {
  const thenable = {
    catch: () => {
      onCatch();
      return thenable;
    },
    then: () => {
      throw new Error("hostile cancellation must not be awaited");
    },
  };
  return thenable as unknown as Promise<void>;
}

class ControlledReader {
  cancelCount = 0;
  cancelCatchCount = 0;
  releaseCount = 0;
  private readonly reads: Array<Deferred<ReadableStreamReadResult<Uint8Array>>> = [];

  read(): Promise<ReadableStreamReadResult<Uint8Array>> {
    const operation = deferred<ReadableStreamReadResult<Uint8Array>>();
    this.reads.push(operation);
    return operation.promise;
  }

  deliverText(value: string): void {
    this.deliverBytes(new TextEncoder().encode(value));
  }

  deliverBytes(value: Uint8Array): void {
    const operation = this.reads.shift();
    assert.notEqual(operation, undefined, "transport must own a pending read");
    operation?.resolve({ done: false, value });
  }

  finish(): void {
    const operation = this.reads.shift();
    assert.notEqual(operation, undefined, "transport must own a pending read");
    operation?.resolve({ done: true, value: undefined });
  }

  cancel(): Promise<void> {
    this.cancelCount += 1;
    return hostileCancellation(() => { this.cancelCatchCount += 1; });
  }

  releaseLock(): void {
    this.releaseCount += 1;
  }

  get pendingReads(): number {
    return this.reads.length;
  }
}

function responseWithReader(
  reader: ControlledReader,
  { ok = true, contentLength = null }: { ok?: boolean; contentLength?: string | null } = {},
): Response {
  return {
    ok,
    body: { getReader: () => reader },
    headers: { get: (name: string) => name.toLowerCase() === "content-length" ? contentLength : null },
  } as unknown as Response;
}

async function flushMicrotasks(): Promise<void> {
  for (let index = 0; index < 8; index += 1) await Promise.resolve();
}

function useClock(widget: Record<string, unknown>, clock: FakeClock): void {
  widget.transportClock = clock;
}

function streamAttempt(
  widget: Record<string, unknown>,
  controller: AbortController,
  assistant: Record<string, unknown>,
  current: () => boolean = () => true,
): Promise<{ kind: string; finishReason?: string }> {
  return (
    widget.streamAttempt as (
      endpoint: string,
      payload: RequestBody,
      controller: AbortController,
      assistant: Record<string, unknown>,
      current: () => boolean,
    ) => Promise<{ kind: string; finishReason?: string }>
  )(
    "https://support.example.test/api/v1/chat/message",
    { session_id: "session-1", message: "question", history: [] },
    controller,
    assistant,
    current,
  );
}

async function send(widget: Record<string, unknown>, message: string): Promise<void> {
  (widget.input as { value: string }).value = message;
  await (widget.send as () => Promise<void>)();
}

async function testLongCompletionSecondRequest(): Promise<void> {
  const requests: RequestBody[] = [];
  const responses = [sseResponse("😀".repeat(501), "limit"), sseResponse("ok", "stop")];
  globalThis.fetch = async (_input, init) => {
    requests.push(JSON.parse(String(init?.body)) as RequestBody);
    return responses.shift() as Response;
  };

  const widget = testWidget();
  await send(widget, "first question");
  await send(widget, "second question");

  assert.equal(requests.length, 2);
  assert.deepEqual(requests[0].history, []);
  assert.equal(requests[1].history.length, 2);
  assert.equal(requests[1].history[0].content, "first question");
  assert.equal(Array.from(requests[1].history[1].content).length, 500);
  assert.equal(requests[1].history[1].content, "😀".repeat(500));
}

async function testEmptyCompletionSecondRequest(): Promise<void> {
  const requests: RequestBody[] = [];
  const responses = [sseResponse("", "stop"), sseResponse("ok", "stop")];
  globalThis.fetch = async (_input, init) => {
    requests.push(JSON.parse(String(init?.body)) as RequestBody);
    return responses.shift() as Response;
  };

  const widget = testWidget();
  await send(widget, "unanswered question");
  await send(widget, "follow-up");

  assert.equal(requests.length, 2);
  assert.deepEqual(requests[1].history, []);
}

async function testDeclaredChatOverflowCancelsBody(): Promise<void> {
  let canceled = false;
  globalThis.fetch = async () => new Response(
    new ReadableStream<Uint8Array>({ cancel: () => { canceled = true; } }),
    { headers: { "content-length": "33554433" } },
  );
  const widget = testWidget();
  const result = await (
    widget.streamAttempt as (
      endpoint: string,
      payload: RequestBody,
      controller: AbortController,
      assistant: Record<string, unknown>,
    ) => Promise<{ kind: string }>
  )(
    "https://support.example.test/api/v1/chat/message",
    { session_id: "session-1", message: "question", history: [] },
    new AbortController(),
    assistantExchange(),
  );
  assert.equal(result.kind, "protocol");
  assert.equal(canceled, true, "declared chat overflow cancels its unread body");
}

async function testNonSuccessChatCancellationIsBounded(): Promise<void> {
  let cancelCount = 0;
  let catchCount = 0;
  globalThis.fetch = async () => ({
    ok: false,
    body: {
      cancel: () => {
        cancelCount += 1;
        return hostileCancellation(() => { catchCount += 1; });
      },
    },
    headers: { get: () => null },
  }) as unknown as Response;
  const widget = testWidget();
  useClock(widget, new FakeClock());
  const result = await streamAttempt(widget, new AbortController(), assistantExchange());
  assert.equal(result.kind, "network");
  assert.equal(cancelCount, 1);
  assert.equal(catchCount, 1, "non-2xx body cancellation is observed without awaiting it");
}

async function testCapabilityHeaderAndBodyDeadlines(): Promise<void> {
  {
    const clock = new FakeClock();
    const widget = testWidget();
    useClock(widget, clock);
    const fetchOperation = deferred<Response>();
    let signal: AbortSignal | undefined;
    const failures: string[] = [];
    globalThis.fetch = async (_input, init) => {
      signal = init?.signal ?? undefined;
      return fetchOperation.promise;
    };
    widget.panel = { hidden: false };
    widget.compatibilityFailure = (kind: string) => failures.push(kind);
    const checking = (widget.checkCompatibility as () => Promise<void>)();
    await flushMicrotasks();
    assert.equal(signal?.aborted, false);
    clock.advance(5_000);
    await checking;
    assert.equal(signal?.aborted, true);
    assert.deepEqual(failures, ["network"]);
  }

  {
    const clock = new FakeClock();
    const widget = testWidget();
    useClock(widget, clock);
    const reader = new ControlledReader();
    const failures: string[] = [];
    let signal: AbortSignal | undefined;
    globalThis.fetch = async (_input, init) => {
      signal = init?.signal ?? undefined;
      return responseWithReader(reader);
    };
    widget.panel = { hidden: false };
    widget.compatibilityFailure = (kind: string) => failures.push(kind);
    const checking = (widget.checkCompatibility as () => Promise<void>)();
    await flushMicrotasks();
    assert.equal(reader.pendingReads, 1);
    clock.advance(5_000);
    await checking;
    assert.equal(signal?.aborted, true);
    assert.deepEqual(failures, ["network"]);
    assert.equal(reader.cancelCount, 1);
    assert.equal(reader.cancelCatchCount, 1);
    assert.equal(reader.releaseCount, 1);
  }
}

async function testPartialDripDoesNotResetLiveness(): Promise<void> {
  const clock = new FakeClock();
  const widget = testWidget();
  useClock(widget, clock);
  const reader = new ControlledReader();
  globalThis.fetch = async () => responseWithReader(reader);
  const controller = new AbortController();
  const attempt = streamAttempt(widget, controller, assistantExchange());
  await flushMicrotasks();
  clock.advance(44_999);
  reader.deliverBytes(Uint8Array.of(101));
  await flushMicrotasks();
  assert.equal(reader.pendingReads, 1);
  clock.advance(1);
  const result = await attempt;
  assert.equal(result.kind, "protocol");
  assert.equal(controller.signal.aborted, true);
  assert.equal(reader.cancelCount, 1);
  assert.equal(reader.releaseCount, 1);
}

async function testPingResetsLiveness(): Promise<void> {
  const clock = new FakeClock();
  const widget = testWidget();
  useClock(widget, clock);
  const reader = new ControlledReader();
  globalThis.fetch = async () => responseWithReader(reader);
  const controller = new AbortController();
  let settled = false;
  const attempt = streamAttempt(widget, controller, assistantExchange());
  void attempt.then(() => { settled = true; });
  await flushMicrotasks();
  clock.advance(44_000);
  reader.deliverText('event: ping\ndata: {"type":"ping"}\n\n');
  await flushMicrotasks();
  clock.advance(44_999);
  await flushMicrotasks();
  assert.equal(settled, false, "a complete ping resets the 45-second liveness window");
  reader.deliverText('event: done\ndata: {"type":"done","finish_reason":"stop"}\n\n');
  const result = await attempt;
  assert.deepEqual(result, { kind: "done", finishReason: "stop", citationCount: 0 });
  assert.equal(controller.signal.aborted, false);
  assert.equal(reader.cancelCount, 1);
  assert.equal(reader.releaseCount, 1);
}

async function testAbsoluteStreamDeadline(): Promise<void> {
  const clock = new FakeClock();
  const widget = testWidget();
  useClock(widget, clock);
  const reader = new ControlledReader();
  globalThis.fetch = async () => responseWithReader(reader);
  const controller = new AbortController();
  let settled = false;
  const attempt = streamAttempt(widget, controller, assistantExchange());
  void attempt.then(() => { settled = true; });
  await flushMicrotasks();
  for (let index = 0; index < 13; index += 1) {
    clock.advance(44_000);
    reader.deliverText('event: ping\ndata: {"type":"ping"}\n\n');
    await flushMicrotasks();
  }
  assert.equal(clock.current, 572_000);
  clock.advance(27_999);
  await flushMicrotasks();
  assert.equal(settled, false);
  clock.advance(1);
  const result = await attempt;
  assert.equal(result.kind, "protocol");
  assert.equal(controller.signal.aborted, true);
  assert.equal(reader.cancelCount, 1);
  assert.equal(reader.releaseCount, 1);
}

async function testTerminalAndProtocolReaderOwnership(): Promise<void> {
  for (const fixture of [
    {
      expected: "done",
      bytes: new TextEncoder().encode('event: done\ndata: {"type":"done","finish_reason":"stop"}\n\n'),
    },
    { expected: "protocol", bytes: new Uint8Array([0xff, 10, 10]) },
  ]) {
    const widget = testWidget();
    useClock(widget, new FakeClock());
    const reader = new ControlledReader();
    globalThis.fetch = async () => responseWithReader(reader);
    const attempt = streamAttempt(widget, new AbortController(), assistantExchange());
    await flushMicrotasks();
    reader.deliverBytes(fixture.bytes);
    const result = await attempt;
    assert.equal(result.kind, fixture.expected);
    assert.equal(reader.cancelCount, 1);
    assert.equal(reader.cancelCatchCount, 1);
    assert.equal(reader.releaseCount, 1);
  }

  {
    const widget = testWidget();
    useClock(widget, new FakeClock());
    const reader = new ControlledReader();
    globalThis.fetch = async () => responseWithReader(reader);
    const attempt = streamAttempt(widget, new AbortController(), assistantExchange());
    await flushMicrotasks();
    reader.deliverText("event: chunk\ndata: {\"type\":\"chunk\",\"delta\":\"partial\"}\n");
    await flushMicrotasks();
    reader.finish();
    const result = await attempt;
    assert.equal(result.kind, "protocol", "an interrupted record is a protocol failure");
    assert.equal(reader.cancelCount, 1);
    assert.equal(reader.releaseCount, 1);
  }

  {
    const widget = testWidget();
    useClock(widget, new FakeClock());
    const reader = new ControlledReader();
    globalThis.fetch = async () => responseWithReader(reader);
    const attempt = streamAttempt(widget, new AbortController(), assistantExchange());
    await flushMicrotasks();
    reader.deliverBytes(new Uint8Array(CHAT_RESPONSE_MAX_BYTES + 1));
    const result = await attempt;
    assert.equal(result.kind, "protocol", "streamed overflow is a protocol failure");
    assert.equal(reader.cancelCount, 1);
    assert.equal(reader.releaseCount, 1);
  }

  {
    const widget = testWidget();
    useClock(widget, new FakeClock());
    const reader = new ControlledReader();
    const controller = new AbortController();
    globalThis.fetch = async () => responseWithReader(reader);
    const attempt = streamAttempt(widget, controller, assistantExchange());
    await flushMicrotasks();
    controller.abort();
    const result = await attempt;
    assert.equal(result.kind, "aborted", "external abort settles without awaiting cancellation");
    assert.equal(reader.cancelCount, 1);
    assert.equal(reader.cancelCatchCount, 1);
    assert.equal(reader.releaseCount, 1);
  }
}

async function testStaleTransportCannotApplyResponse(): Promise<void> {
  const widget = testWidget();
  useClock(widget, new FakeClock());
  const responseOperation = deferred<Response>();
  let current = true;
  let bodyCancelCount = 0;
  let bodyCatchCount = 0;
  const assistant = assistantExchange();
  globalThis.fetch = async () => responseOperation.promise;
  const attempt = streamAttempt(widget, new AbortController(), assistant, () => current);
  await flushMicrotasks();
  current = false;
  responseOperation.resolve({
    ok: true,
    body: {
      cancel: () => {
        bodyCancelCount += 1;
        return hostileCancellation(() => { bodyCatchCount += 1; });
      },
      getReader: () => {
        throw new Error("stale response must not acquire a reader");
      },
    },
    headers: { get: () => null },
  } as unknown as Response);
  const result = await attempt;
  assert.equal(result.kind, "aborted");
  assert.equal(bodyCancelCount, 1);
  assert.equal(bodyCatchCount, 1);
  assert.equal(assistant.text, "");
  assert.equal((assistant.content as { textContent: string }).textContent, "");

  {
    const afterHeadersWidget = testWidget();
    useClock(afterHeadersWidget, new FakeClock());
    const reader = new ControlledReader();
    const afterHeadersAssistant = assistantExchange();
    let afterHeadersCurrent = true;
    globalThis.fetch = async () => responseWithReader(reader);
    const afterHeadersAttempt = streamAttempt(
      afterHeadersWidget,
      new AbortController(),
      afterHeadersAssistant,
      () => afterHeadersCurrent,
    );
    await flushMicrotasks();
    afterHeadersCurrent = false;
    reader.deliverText('event: done\ndata: {"type":"done","finish_reason":"stop"}\n\n');
    const afterHeadersResult = await afterHeadersAttempt;
    assert.equal(afterHeadersResult.kind, "aborted");
    assert.equal(afterHeadersAssistant.finishReason, null);
    assert.equal(reader.cancelCount, 1);
    assert.equal(reader.releaseCount, 1);
  }
}

function testCloseClearDisconnectReleaseReader(): void {
  for (const action of ["close", "clearChat", "disconnectedCallback"] as const) {
    const widget = testWidget();
    const reader = new ControlledReader();
    widget.activeReader = reader;
    widget.chatController = new AbortController();
    widget.panel = { hidden: false };
    widget.launcher = {
      setAttribute: () => undefined,
      removeAttribute: () => undefined,
      focus: () => undefined,
    };
    widget.closeButton = { focus: () => undefined };
    widget.input = { ...widget.input as object, focus: () => undefined };
    widget.error = { textContent: "", hidden: true };
    widget.handoffLink = { hidden: true, removeAttribute: () => undefined };
    widget.dispatchHostEvent = () => true;
    if (action === "close") (widget.close as (reason: "button") => void)("button");
    else (widget[action] as () => void)();
    assert.equal(reader.cancelCount, 1, `${action} cancels its reader exactly once`);
    assert.equal(reader.cancelCatchCount, 1, `${action} observes a hostile cancellation`);
    assert.equal(reader.releaseCount, 1, `${action} releases reader ownership`);
  }


  const widget = testWidget();
  const reader = new ControlledReader();
  widget.activeReader = reader;
  widget.chatController = new AbortController();
  widget.styleElement = { nonce: "" };
  widget.panel = { hidden: false };
  widget.launcher = { setAttribute: () => undefined };
  widget.concealHostNonce = () => undefined;
  widget.showError = () => undefined;
  widget.getAttribute = () => null;
  (widget.syncConfiguration as () => void)();
  assert.equal(reader.cancelCount, 1, "an invalid configuration change cancels its reader");
  assert.equal(reader.releaseCount, 1, "an invalid configuration change releases its reader");
}

async function testSendTimeoutUsesTransportClock(): Promise<void> {
  const clock = new FakeClock();
  const widget = testWidget();
  useClock(widget, clock);
  const reader = new ControlledReader();
  const errors: Array<{ name: string; detail: Record<string, unknown> }> = [];
  let displayedError = "";
  globalThis.fetch = async () => responseWithReader(reader);
  widget.showError = (message: string) => { displayedError = message; };
  widget.dispatchHostEvent = (name: string, detail: Record<string, unknown>) => {
    errors.push({ name, detail });
    return true;
  };
  const sending = send(widget, "deadline question");
  await flushMicrotasks();
  assert.equal(widget.state, "sending");
  assert.equal((widget.messages as Record<string, unknown>)["aria-busy"], "true");
  clock.advance(45_000);
  await sending;
  assert.equal(widget.state, "terminal");
  assert.equal(displayedError, "Cairn returned an invalid response. Please try again later.");
  assert.deepEqual(errors.at(-1), {
    name: "cairn-error",
    detail: { version: "1.0", kind: "protocol", retryable: false },
  });
  assert.equal((widget.input as { disabled: boolean }).disabled, false);
  assert.equal((widget.sendButton as { disabled: boolean }).disabled, false);
  assert.equal((widget.messages as Record<string, unknown>)["aria-busy"], "false");
  assert.equal(reader.cancelCount, 1);
  assert.equal(reader.releaseCount, 1);
}

await testLongCompletionSecondRequest();
await testEmptyCompletionSecondRequest();
await testDeclaredChatOverflowCancelsBody();
await testNonSuccessChatCancellationIsBounded();
await testCapabilityHeaderAndBodyDeadlines();
await testPartialDripDoesNotResetLiveness();
await testPingResetsLiveness();
await testAbsoluteStreamDeadline();
await testTerminalAndProtocolReaderOwnership();
await testStaleTransportCannotApplyResponse();
testCloseClearDisconnectReleaseReader();
await testSendTimeoutUsesTransportClock();
console.log("widget history integration tests passed");

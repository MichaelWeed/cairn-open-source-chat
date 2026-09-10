import assert from "node:assert/strict";

class FakeHTMLElement {
  attachShadow(): Record<string, never> {
    return {};
  }

  getAttribute(): string {
    return "https://support.example.test";
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
  };
}

function testWidget(): Record<string, unknown> {
  const widget = new CairnChat() as unknown as Record<string, unknown>;
  widget.controller = null;
  widget.history = [];
  widget.sessionId = "session-1";
  widget.input = { value: "", disabled: false };
  widget.sendButton = { disabled: false };
  widget.clearError = () => undefined;
  widget.appendMessage = () => undefined;
  widget.appendAssistant = assistantExchange;
  return widget;
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

await testLongCompletionSecondRequest();
await testEmptyCompletionSecondRequest();
console.log("widget history integration tests passed");

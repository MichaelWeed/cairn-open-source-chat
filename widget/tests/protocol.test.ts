import assert from "node:assert/strict";

import {
  SseDecoder,
  boundedHistory,
  chatEndpoint,
  retryOnce,
  safeCitationUrl,
  type StreamAttemptResult,
} from "../src/protocol";

function expectThrows(action: () => void, message: string): void {
  assert.throws(action, message);
}

function testSseDecoder(): void {
  const decoder = new SseDecoder();

  assert.deepEqual(decoder.push("event: status\r"), []);
  assert.deepEqual(
    decoder.push(
      "\ndata: {\"type\":\"status\",\r\ndata: \"label\":\"Searching your docs\"}\r\n\r\n",
    ),
    [{ type: "status", label: "Searching your docs" }],
  );
  assert.deepEqual(decoder.push("event: ping\ndata: {\"type\":\"ping\"}\n\n"), []);
  assert.deepEqual(decoder.push("event: chunk\ndata: {\"type\":\"chunk\",\"delta\":\"First"), []);
  assert.deepEqual(
    decoder.push(" words\"}\n\n"),
    [{ type: "chunk", delta: "First words" }],
  );
  assert.deepEqual(
    decoder.push(
      "event: citations\ndata: {\"type\":\"citations\",\"sources\":[{\"id\":\"doc-1\",\"title\":\"Guide\",\"url\":\"https://example.test/guide\"}]}\n\n",
    ),
    [
      {
        type: "citations",
        sources: [
          { id: "doc-1", title: "Guide", url: "https://example.test/guide" },
        ],
      },
    ],
  );
  assert.deepEqual(
    decoder.push("event: done\ndata: {\"type\":\"done\",\"finish_reason\":\"stop\"}"),
    [],
  );
  assert.deepEqual(decoder.finish(), [{ type: "done", finishReason: "stop" }]);

  const refusalDecoder = new SseDecoder();
  assert.deepEqual(
    refusalDecoder.push(
      "event: done\ndata: {\"type\":\"done\",\"finish_reason\":\"refused\"}\n\n",
    ),
    [{ type: "done", finishReason: "refused" }],
  );

  const inventedReasonDecoder = new SseDecoder();
  expectThrows(
    () =>
      inventedReasonDecoder.push(
        "event: done\ndata: {\"type\":\"done\",\"finish_reason\":\"complete\"}\n\n",
      ),
    "the invented complete finish reason is rejected",
  );
}

function testMalformedSse(): void {
  const decoder = new SseDecoder();
  expectThrows(
    () => decoder.push("event: chunk\ndata: {bad json}\n\n"),
    "malformed event payload is rejected",
  );
}

function testHistoryAndEndpoint(): void {
  const turns = Array.from({ length: 7 }, (_, index) => ({
    role: index % 2 === 0 ? ("user" as const) : ("assistant" as const),
    content: `turn-${index}`,
  }));
  assert.deepEqual(
    boundedHistory(turns).map((turn) => turn.content),
    ["turn-2", "turn-3", "turn-4", "turn-5", "turn-6"],
  );
  assert.equal(chatEndpoint(" https://support.example.test/ "), "https://support.example.test/api/v1/chat/message");
  assert.equal(chatEndpoint("https://support.example.test//"), "https://support.example.test//api/v1/chat/message");
  assert.equal(chatEndpoint(null), null);
  assert.equal(chatEndpoint("ftp://support.example.test"), null);
}

function testCitationSafety(): void {
  assert.equal(safeCitationUrl("https://example.test/reference"), "https://example.test/reference");
  assert.equal(safeCitationUrl("http://example.test/reference"), "http://example.test/reference");
  assert.equal(safeCitationUrl("javascript:alert(1)"), null);
  assert.equal(safeCitationUrl("data:text/html,unsafe"), null);
  assert.equal(safeCitationUrl("/relative-reference"), null);
}

async function testRetryOnce(): Promise<void> {
  const retryableFailure: StreamAttemptResult<string> = {
    kind: "error",
    message: "The service is busy.",
    retryable: true,
  };
  const successfulAnswer: StreamAttemptResult<string> = {
    kind: "done",
    value: "second attempt answer",
  };
  const stablePayloads: Array<{ message: string; sessionId: string; history: string[] }> = [];
  let attempts = 0;
  let resetCount = 0;
  let visibleExchanges = 1;
  const partialAssistant = {
    citations: ["partial citation"],
    error: "The service is busy.",
    failed: true,
    status: "Answer unavailable",
    text: "partial answer",
  };
  const request = { message: "Where is my order?", sessionId: "session-1", history: ["prior turn"] };

  const retried = await retryOnce(
    async () => {
      attempts += 1;
      stablePayloads.push({ ...request, history: [...request.history] });
      return attempts === 1 ? retryableFailure : successfulAnswer;
    },
    () => {
      resetCount += 1;
      partialAssistant.text = "";
      partialAssistant.citations = [];
      partialAssistant.status = "Retrying Cairn…";
      partialAssistant.failed = false;
      partialAssistant.error = "";
    },
  );
  assert.deepEqual(retried, successfulAnswer);
  assert.equal(attempts, 2, "a retryable SSE error makes exactly one retry");
  assert.equal(resetCount, 1, "the partial assistant exchange resets before retrying");
  assert.equal(visibleExchanges, 1, "retrying does not add a second visible exchange");
  assert.deepEqual(partialAssistant, {
    citations: [],
    error: "",
    failed: false,
    status: "Retrying Cairn…",
    text: "",
  });
  assert.deepEqual(stablePayloads, [
    { message: "Where is my order?", sessionId: "session-1", history: ["prior turn"] },
    { message: "Where is my order?", sessionId: "session-1", history: ["prior turn"] },
  ]);

  for (const terminal of [
    { kind: "error", message: "Do not retry.", retryable: false } as const,
    { kind: "aborted" } as const,
  ]) {
    let terminalAttempts = 0;
    const result = await retryOnce(
      async () => {
        terminalAttempts += 1;
        return terminal;
      },
      () => assert.fail("terminal outcomes never reset or retry"),
    );
    assert.deepEqual(result, terminal);
    assert.equal(terminalAttempts, 1, "terminal outcomes use one request");
  }

  let secondFailureAttempts = 0;
  const secondFailure = await retryOnce(
    async () => {
      secondFailureAttempts += 1;
      return retryableFailure;
    },
    () => undefined,
  );
  assert.deepEqual(secondFailure, retryableFailure);
  assert.equal(secondFailureAttempts, 2, "a second retryable error is terminal");

  const history: string[] = [];
  if (retried.kind === "done") {
    history.push(request.message, retried.value);
  }
  assert.deepEqual(history, ["Where is my order?", "second attempt answer"], "history changes only after a clean done result");
}

async function main(): Promise<void> {
  testSseDecoder();
  testMalformedSse();
  testHistoryAndEndpoint();
  testCitationSafety();
  await testRetryOnce();
  console.log("widget protocol tests passed");
}

void main();

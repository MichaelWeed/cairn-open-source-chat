import assert from "node:assert/strict";

import {
  SseDecoder,
  boundedHistory,
  chatEndpoint,
  safeCitationUrl,
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

testSseDecoder();
testMalformedSse();
testHistoryAndEndpoint();
testCitationSafety();

console.log("widget protocol tests passed");

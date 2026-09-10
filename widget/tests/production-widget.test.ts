import assert from "node:assert/strict";

import {
  CAPABILITY_RESPONSE_MAX_BYTES,
  BoundedSseDecoder,
  CHAT_CITATIONS_MAX_COUNT,
  CHAT_NON_PING_EVENT_MAX_COUNT,
  CHAT_OUTPUT_MAX_CODE_POINTS,
  CHAT_RESPONSE_MAX_BYTES,
  SSE_PENDING_RECORD_MAX_BYTES,
  MESSAGE_MAX_CODE_POINTS,
  capabilityEndpoint,
  codePointLength,
  newChatAttemptState,
  parseWidgetConfiguration,
  readBoundedJsonResponse,
  streamDeadlineRemaining,
  validateCapabilityManifest,
  validateChatEventBatch,
} from "../src/protocol";

function attributes(values: Partial<Record<string, string>>): (name: string) => string | null {
  return (name) => values[name] ?? null;
}

function testConfiguration(): void {
  const parsed = parseWidgetConfiguration(
    attributes({
      "api-url": " https://support.example.test/prefix// ",
      "assistant-name": " Support Cairn ",
      theme: "dark",
      "privacy-url": "https://policy.example.test/privacy",
      "handoff-url": "https://support.example.test/contact",
      nonce: "YWJjZGVmZ2hpamtsbW5vcA==",
    }),
  );
  assert.equal(parsed.ok, true);
  if (!parsed.ok) return;
  assert.equal(parsed.value.apiBase, "https://support.example.test/prefix/");
  assert.equal(capabilityEndpoint(parsed.value.apiBase), "https://support.example.test/prefix//api/v1/capabilities");
  assert.equal(parsed.value.assistantName, "Support Cairn");
  assert.equal(parsed.value.theme, "dark");
  assert.equal(parsed.value.styleNonce, "YWJjZGVmZ2hpamtsbW5vcA==");

  const defaults = parseWidgetConfiguration(attributes({ "api-url": "https://support.example.test" }));
  assert.equal(defaults.ok, true);
  if (defaults.ok) {
    assert.deepEqual(defaults.value, {
      apiBase: "https://support.example.test",
      assistantName: "Cairn",
      theme: "auto",
      privacyUrl: null,
      handoffUrl: null,
      styleNonce: null,
    });
    assert.equal(Object.isFrozen(defaults.value), true);
  }

  for (const apiUrl of [
    "",
    "ftp://support.example.test",
    "https://user:pass@support.example.test",
    "https://support.example.test?q=secret",
    "https://support.example.test/#secret",
    "https://support.example.test/path?",
    "https://support.example.test/path#",
  ]) {
    assert.equal(parseWidgetConfiguration(attributes({ "api-url": apiUrl })).ok, false);
  }
  assert.equal(
    parseWidgetConfiguration(attributes({ "api-url": "https://support.example.test", "assistant-name": "😀".repeat(80) })).ok,
    true,
  );
  assert.equal(
    parseWidgetConfiguration(attributes({ "api-url": "https://support.example.test", "assistant-name": "😀".repeat(81) })).ok,
    false,
  );
  assert.equal(
    parseWidgetConfiguration(attributes({ "api-url": "https://support.example.test", "assistant-name": "bad\u0000name" })).ok,
    false,
  );
  for (const theme of ["auto", "light", "dark"]) {
    assert.equal(parseWidgetConfiguration(attributes({ "api-url": "https://support.example.test", theme })).ok, true);
  }
  assert.equal(parseWidgetConfiguration(attributes({ "api-url": "https://support.example.test", theme: "Dark" })).ok, false);
  for (const key of ["privacy-url", "handoff-url"] as const) {
    assert.equal(parseWidgetConfiguration(attributes({ "api-url": "https://support.example.test", [key]: "javascript:alert(1)" })).ok, false);
    assert.equal(parseWidgetConfiguration(attributes({ "api-url": "https://support.example.test", [key]: "https://user@example.test/path" })).ok, false);
  }
  assert.equal(parseWidgetConfiguration(attributes({ "api-url": "https://support.example.test", nonce: "abc+/_-=" })).ok, true);
  assert.equal(parseWidgetConfiguration(attributes({ "api-url": "https://support.example.test", nonce: "!bad!" })).ok, false);
  assert.equal(parseWidgetConfiguration(attributes({ "api-url": "https://support.example.test", nonce: "a".repeat(257) })).ok, false);
  assert.equal(codePointLength("a".repeat(500)), 500);
  assert.equal(codePointLength("a".repeat(501)), 501);
  assert.equal(codePointLength("😀".repeat(500)), 500);
  assert.equal(codePointLength("😀".repeat(501)), 501);
}

function testCapabilitySubset(): void {
  const valid = {
    schema_version: "1.1",
    compatibility: { chat_api: "1.0", sse_events: "1.1", widget: "0.2.0", future: true },
    capabilities: { widget: { production_configuration: "available", future: true } },
    future: true,
  };
  assert.equal(validateCapabilityManifest(valid), true);
  for (const invalid of [
    null,
    {},
    { ...valid, schema_version: "1.0" },
    { ...valid, compatibility: { ...valid.compatibility, chat_api: "2.0" } },
    { ...valid, compatibility: { ...valid.compatibility, sse_events: "1.0" } },
    { ...valid, compatibility: { ...valid.compatibility, widget: "0.1.0" } },
    { ...valid, capabilities: { widget: { production_configuration: "planned" } } },
  ]) assert.equal(validateCapabilityManifest(invalid), false);
}

function testFrozenSafetyConstants(): void {
  assert.equal(CAPABILITY_RESPONSE_MAX_BYTES, 16_384);
  assert.equal(CHAT_RESPONSE_MAX_BYTES, 33_554_432);
  assert.equal(SSE_PENDING_RECORD_MAX_BYTES, 33_554_432);
  assert.equal(CHAT_OUTPUT_MAX_CODE_POINTS, 6_000);
  assert.equal(CHAT_CITATIONS_MAX_COUNT, 6);
  assert.equal(CHAT_NON_PING_EVENT_MAX_COUNT, 6_016);
  assert.equal(MESSAGE_MAX_CODE_POINTS, 500);
}

async function testBoundedCapabilityBody(): Promise<void> {
  const exact = JSON.stringify({ value: "x".repeat(CAPABILITY_RESPONSE_MAX_BYTES - 12) });
  assert.equal(new TextEncoder().encode(exact).byteLength, CAPABILITY_RESPONSE_MAX_BYTES);
  assert.deepEqual(await readBoundedJsonResponse(new Response(exact)), JSON.parse(exact));
  await assert.rejects(
    readBoundedJsonResponse(new Response(`${exact}x`)),
    /too large/u,
  );
  await assert.rejects(
    readBoundedJsonResponse(new Response(new Uint8Array([0xc3, 0x28]))),
    /invalid/u,
  );
  await assert.rejects(
    readBoundedJsonResponse(new Response("{}", { status: 503 })),
    /unavailable/u,
  );
  await assert.rejects(readBoundedJsonResponse(new Response("{")), /invalid/u);
  await assert.rejects(readBoundedJsonResponse(new Response(null, { status: 204 })), /unavailable/u);
  assert.deepEqual(
    await readBoundedJsonResponse(new Response("{}", { headers: { "content-length": String(CAPABILITY_RESPONSE_MAX_BYTES) } })),
    {},
  );
  await assert.rejects(
    readBoundedJsonResponse(new Response("{}", { headers: { "content-length": String(CAPABILITY_RESPONSE_MAX_BYTES + 1) } })),
    /too large/u,
  );
  let canceled = false;
  const oversizedBody = new ReadableStream<Uint8Array>({
    cancel: () => { canceled = true; },
  });
  await assert.rejects(
    readBoundedJsonResponse(new Response(oversizedBody, {
      headers: { "content-length": String(CAPABILITY_RESPONSE_MAX_BYTES + 1) },
    })),
    /too large/u,
  );
  assert.equal(canceled, true, "declared capability overflow cancels its unread body");
}

function testBoundedChatDecoding(): void {
  const decoder = new BoundedSseDecoder();
  assert.deepEqual(
    decoder.push(new TextEncoder().encode('event: ping\ndata: {"type":"ping"}\n\n')),
    [],
  );
  assert.equal(decoder.recordsCompleted, 1, "a complete ping keeps the stream alive");
  assert.throws(
    () => new BoundedSseDecoder().push(new Uint8Array([0xff, 10, 10])),
    /UTF-8/u,
  );
  const exactBytes = new BoundedSseDecoder();
  exactBytes.push(new Uint8Array(CHAT_RESPONSE_MAX_BYTES));
  assert.equal(exactBytes.totalBytes, CHAT_RESPONSE_MAX_BYTES);
  assert.equal(exactBytes.pendingBytes, SSE_PENDING_RECORD_MAX_BYTES);
  assert.throws(() => exactBytes.push(new Uint8Array([0])), /byte limit/u);
  assert.throws(() => exactBytes.finish(), /incomplete/u);

  const state = newChatAttemptState();
  assert.equal(validateChatEventBatch([
    { type: "status", label: "Searching" },
    { type: "citations", sources: [] },
    { type: "chunk", delta: "answer" },
    { type: "done", finishReason: "stop" },
  ], state), true);
  assert.equal(validateChatEventBatch([{ type: "chunk", delta: "late" }], state), false);
  const terminalBatchState = newChatAttemptState();
  assert.equal(validateChatEventBatch([
    { type: "done", finishReason: "stop" },
    { type: "chunk", delta: "late" },
  ], terminalBatchState), false);
  assert.equal(terminalBatchState.terminal, false, "invalid batches apply no partial state");
  assert.equal(validateChatEventBatch([
    { type: "chunk", delta: "answer" },
    { type: "citations", sources: [] },
  ], newChatAttemptState()), false);
  assert.equal(validateChatEventBatch([
    { type: "chunk", delta: "😀".repeat(CHAT_OUTPUT_MAX_CODE_POINTS) },
  ], newChatAttemptState()), true);
  assert.equal(validateChatEventBatch([
    { type: "chunk", delta: "😀".repeat(CHAT_OUTPUT_MAX_CODE_POINTS) },
    { type: "chunk", delta: "x" },
  ], newChatAttemptState()), false);
  const citations = Array.from({ length: CHAT_CITATIONS_MAX_COUNT }, (_, index) => ({
    id: String(index), title: "Reference", url: "https://example.test",
  }));
  assert.equal(validateChatEventBatch([{ type: "citations", sources: citations }], newChatAttemptState()), true);
  assert.equal(validateChatEventBatch([
    { type: "citations", sources: citations },
    { type: "citations", sources: [] },
  ], newChatAttemptState()), false);
  const maximumEvents = Array.from({ length: CHAT_NON_PING_EVENT_MAX_COUNT }, () => ({
    type: "status" as const,
    label: "still working",
  }));
  assert.equal(validateChatEventBatch(maximumEvents, newChatAttemptState()), true);
  assert.equal(validateChatEventBatch([...maximumEvents, { type: "status", label: "too many" }], newChatAttemptState()), false);
  assert.equal(streamDeadlineRemaining(0, 10_000, 54_999), 1);
  assert.equal(streamDeadlineRemaining(0, 599_999, 600_000), 0);
}

testConfiguration();
testCapabilitySubset();
testFrozenSafetyConstants();
await testBoundedCapabilityBody();
testBoundedChatDecoding();
console.log("widget production contract tests passed");

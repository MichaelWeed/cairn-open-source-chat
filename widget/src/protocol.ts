export const HISTORY_LIMIT = 5;
export const HISTORY_TURN_MAX_CODE_POINTS = 500;
export const HISTORY_AGGREGATE_MAX_CODE_POINTS = 2_000;
export const MESSAGE_MAX_CODE_POINTS = 500;
export const CAPABILITY_RESPONSE_MAX_BYTES = 16_384;
export const CHAT_RESPONSE_MAX_BYTES = 33_554_432;
export const SSE_PENDING_RECORD_MAX_BYTES = 33_554_432;
export const CHAT_OUTPUT_MAX_CODE_POINTS = 6_000;
export const CHAT_CITATIONS_MAX_COUNT = 6;
export const CHAT_NON_PING_EVENT_MAX_COUNT = 6_016;
export const CHAT_COMPLETE_RECORD_IDLE_SECONDS = 45;
export const CHAT_TOTAL_DEADLINE_SECONDS = 600;
export const CAPABILITY_DEADLINE_SECONDS = 5;
const HISTORY_EDGE_WHITESPACE = /^[\p{White_Space}\u001c-\u001f\ufeff]+|[\p{White_Space}\u001c-\u001f\ufeff]+$/gu;
const FORBIDDEN_CONTROL = /[\u0000-\u001f\u007f-\u009f]/u;
const NONCE_PATTERN = /^[A-Za-z0-9+/_-]+={0,2}$/u;

export type WidgetTheme = "auto" | "light" | "dark";

export interface WidgetConfiguration {
  readonly apiBase: string;
  readonly assistantName: string;
  readonly theme: WidgetTheme;
  readonly privacyUrl: string | null;
  readonly handoffUrl: string | null;
}

export type WidgetConfigurationResult =
  | { readonly ok: true; readonly value: Readonly<WidgetConfiguration> }
  | { readonly ok: false; readonly message: string };

export const CONFIGURATION_ERROR =
  "Cairn widget configuration is invalid.";

export interface ChatTurn {
  role: "user" | "assistant";
  content: string;
}

export interface Citation {
  id: string;
  title: string;
  url: string;
}

export type DoneReason = "stop" | "refused" | "limit" | "cancelled";

export type ChatStreamEvent =
  | { type: "status"; label: string }
  | { type: "chunk"; delta: string }
  | { type: "citations"; sources: Citation[] }
  | { type: "error"; message: string; retryable: boolean }
  | { type: "done"; finishReason: DoneReason };

const ERROR_CODES = new Set([
  "invalid_request",
  "rate_limited",
  "budget_exhausted",
  "concurrency_limited",
  "provider_timeout",
  "provider_unavailable",
  "retrieval_unavailable",
  "guardrail_block",
  "request_cancelled",
  "internal",
]);

export type StreamAttemptResult<T> =
  | { kind: "done"; value: T }
  | { kind: "error"; message: string; retryable: boolean }
  | { kind: "aborted" };

type RawEvent = Record<string, unknown>;

export interface ChatAttemptState {
  terminal: boolean;
  sawOutput: boolean;
  sawCitations: boolean;
  outputCodePoints: number;
  citationCount: number;
  nonPingEvents: number;
}

export class SseDecodeError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "SseDecodeError";
  }
}

/** Decode complete server-sent event records while retaining partial chunks. */
export class SseDecoder {
  private buffer = "";

  push(chunk: string): ChatStreamEvent[] {
    this.buffer += chunk;
    const events: ChatStreamEvent[] = [];
    let boundary = this.buffer.match(/\r?\n\r?\n/);

    while (boundary?.index !== undefined) {
      const record = this.buffer.slice(0, boundary.index);
      this.buffer = this.buffer.slice(boundary.index + boundary[0].length);
      const event = decodeRecord(record);
      if (event !== null) {
        events.push(event);
      }
      boundary = this.buffer.match(/\r?\n\r?\n/);
    }

    return events;
  }

  finish(): ChatStreamEvent[] {
    const trailing = this.buffer;
    this.buffer = "";
    if (trailing.trim() === "") {
      return [];
    }
    const event = decodeRecord(trailing);
    return event === null ? [] : [event];
  }
}

export class BoundedSseDecoder {
  private bytes = new Uint8Array();
  private start = 0;
  private end = 0;
  private scan = 0;
  private responseBytes = 0;
  private completedRecords = 0;
  private scannedBytes = 0;
  private copiedBytes = 0;
  private readonly decoder = new TextDecoder("utf-8", { fatal: true });

  push(chunk: Uint8Array): ChatStreamEvent[] {
    this.completedRecords = 0;
    this.responseBytes += chunk.byteLength;
    if (this.responseBytes > CHAT_RESPONSE_MAX_BYTES) {
      throw new SseDecodeError("The chat response exceeded its safe byte limit.");
    }
    this.append(chunk);
    const events: ChatStreamEvent[] = [];

    for (;;) {
      const boundary = this.nextBoundary();
      if (boundary === null) break;
      const record = this.bytes.subarray(this.start, boundary.index);
      this.start = boundary.index + boundary.length;
      this.scan = this.start;
      this.completedRecords += 1;
      let decoded: string;
      try {
        decoded = this.decoder.decode(record);
      } catch {
        throw new SseDecodeError("The chat response was not valid UTF-8.");
      }
      const event = decodeRecord(decoded);
      if (event !== null) events.push(event);
      if (this.start === this.end) {
        this.start = 0;
        this.end = 0;
        this.scan = 0;
      }
    }
    if (this.pendingBytes > SSE_PENDING_RECORD_MAX_BYTES) {
      throw new SseDecodeError("The chat response record exceeded its safe byte limit.");
    }
    return events;
  }

  finish(): ChatStreamEvent[] {
    if (this.pendingBytes === 0) return [];
    const remaining = this.bytes.subarray(this.start, this.end);
    this.bytes = new Uint8Array();
    this.start = 0;
    this.end = 0;
    this.scan = 0;
    let decoded: string;
    try {
      decoded = this.decoder.decode(remaining);
    } catch {
      throw new SseDecodeError("The chat response was not valid UTF-8.");
    }
    if (decoded.trim() === "") return [];
    throw new SseDecodeError("The chat response ended with an incomplete record.");
  }

  get pendingBytes(): number {
    return this.end - this.start;
  }

  get totalBytes(): number {
    return this.responseBytes;
  }

  get recordsCompleted(): number {
    return this.completedRecords;
  }

  get work(): Readonly<{ scannedBytes: number; copiedBytes: number }> {
    return Object.freeze({
      scannedBytes: this.scannedBytes,
      copiedBytes: this.copiedBytes,
    });
  }

  private append(chunk: Uint8Array): void {
    if (chunk.byteLength === 0) return;
    const pending = this.pendingBytes;
    if (this.bytes.byteLength - this.end < chunk.byteLength) {
      const oldStart = this.start;
      const oldScan = this.scan;
      if (this.bytes.byteLength >= pending + chunk.byteLength) {
        this.bytes.copyWithin(0, this.start, this.end);
        this.copiedBytes += pending;
      } else {
        let capacity = Math.max(1_024, this.bytes.byteLength * 2);
        const required = pending + chunk.byteLength;
        while (capacity < required) capacity *= 2;
        const replacement = new Uint8Array(capacity);
        replacement.set(this.bytes.subarray(this.start, this.end));
        this.copiedBytes += pending;
        this.bytes = replacement;
      }
      this.start = 0;
      this.end = pending;
      this.scan = Math.max(0, oldScan - oldStart);
    }
    this.bytes.set(chunk, this.end);
    this.end += chunk.byteLength;
    this.copiedBytes += chunk.byteLength;
  }

  private nextBoundary(): { index: number; length: number } | null {
    for (let index = this.scan; index < this.end - 1; index += 1) {
      this.scannedBytes += 1;
      if (this.bytes[index] === 10 && this.bytes[index + 1] === 10) {
        return { index, length: 2 };
      }
      if (
        index < this.end - 3 &&
        this.bytes[index] === 13 &&
        this.bytes[index + 1] === 10 &&
        this.bytes[index + 2] === 13 &&
        this.bytes[index + 3] === 10
      ) {
        return { index, length: 4 };
      }
    }
    // Only a delimiter's three-byte prefix can become complete after the next push.
    this.scan = Math.max(this.start, this.end - 3);
    return null;
  }
}

export function boundedHistory(history: readonly ChatTurn[]): ChatTurn[] {
  const candidates = history
    .map((turn) => ({
      role: turn.role,
      content: Array.from(turn.content.replace(HISTORY_EDGE_WHITESPACE, ""))
        .slice(0, HISTORY_TURN_MAX_CODE_POINTS)
        .join(""),
    }))
    .filter((turn) => turn.content !== "")
    .slice(-HISTORY_LIMIT);
  const retained: ChatTurn[] = [];
  let aggregateCodePoints = 0;

  for (let index = candidates.length - 1; index >= 0; index -= 1) {
    const turn = candidates[index];
    const turnCodePoints = Array.from(turn.content).length;
    if (aggregateCodePoints + turnCodePoints > HISTORY_AGGREGATE_MAX_CODE_POINTS) {
      continue;
    }
    retained.unshift(turn);
    aggregateCodePoints += turnCodePoints;
  }
  return retained;
}

/** Persist only complete, usable exchanges; limited answers retain their safe prefix. */
export function completedHistory(
  history: readonly ChatTurn[],
  userMessage: string,
  assistantMessage: string,
  finishReason: DoneReason,
): ChatTurn[] {
  if (
    finishReason === "cancelled" ||
    finishReason === "refused" ||
    assistantMessage.replace(HISTORY_EDGE_WHITESPACE, "") === ""
  ) {
    return boundedHistory(history);
  }
  return boundedHistory([
    ...history,
    { role: "user", content: userMessage },
    { role: "assistant", content: assistantMessage },
  ]);
}

export function chatRequestPayload(
  sessionId: string,
  message: string,
  history: readonly ChatTurn[],
): { session_id: string; message: string; history: ChatTurn[] } {
  return { session_id: sessionId, message, history: boundedHistory(history) };
}

/** Run a stream once more only after its first explicit retryable error. */
export async function retryOnce<T>(
  attempt: () => Promise<StreamAttemptResult<T>>,
  onRetry: () => void,
): Promise<StreamAttemptResult<T>> {
  const first = await attempt();
  if (first.kind !== "error" || !first.retryable) {
    return first;
  }
  onRetry();
  return attempt();
}

export function chatEndpoint(apiUrl: string | null): string | null {
  const parsed = parseApiBase(apiUrl);
  return parsed === null ? null : endpointFromBase(parsed, "/api/v1/chat/message");
}

export function capabilityEndpoint(apiBase: string): string {
  return endpointFromBase(apiBase, "/api/v1/capabilities");
}

export function parseWidgetConfiguration(
  getAttribute: (name: string) => string | null,
  nonceProperty?: string,
): WidgetConfigurationResult {
  const apiBase = parseApiBase(getAttribute("api-url"));
  const assistantRaw = getAttribute("assistant-name")?.trim() ?? "";
  const assistantName = assistantRaw === "" ? "Cairn" : assistantRaw;
  const themeRaw = getAttribute("theme")?.trim() ?? "";
  const theme = themeRaw === "" ? "auto" : themeRaw;
  const privacyUrl = parseOptionalExternalUrl(getAttribute("privacy-url"));
  const handoffUrl = parseOptionalExternalUrl(getAttribute("handoff-url"));
  const nonceRaw = (nonceProperty ?? getAttribute("nonce") ?? "").trim();

  if (
    apiBase === null ||
    Array.from(assistantName).length > 80 ||
    FORBIDDEN_CONTROL.test(assistantName) ||
    (theme !== "auto" && theme !== "light" && theme !== "dark") ||
    privacyUrl === undefined ||
    handoffUrl === undefined ||
    (nonceRaw !== "" && (nonceRaw.length > 256 || !NONCE_PATTERN.test(nonceRaw)))
  ) {
    return { ok: false, message: CONFIGURATION_ERROR };
  }

  return {
    ok: true,
    value: Object.freeze({
      apiBase,
      assistantName,
      theme,
      privacyUrl,
      handoffUrl,
    }),
  };
}

export function validateCapabilityManifest(value: unknown): boolean {
  if (!isRecord(value)) return false;
  const compatibility = value.compatibility;
  const capabilities = value.capabilities;
  if (!isRecord(compatibility) || !isRecord(capabilities)) return false;
  const widget = capabilities.widget;
  return (
    value.schema_version === "1.1" &&
    compatibility.chat_api === "1.0" &&
    compatibility.sse_events === "1.1" &&
    compatibility.widget === "0.2.0" &&
    isRecord(widget) &&
    widget.production_configuration === "available"
  );
}

export async function readBoundedJsonResponse(
  response: Response,
  maximumBytes = CAPABILITY_RESPONSE_MAX_BYTES,
  readNext: (
    reader: ReadableStreamDefaultReader<Uint8Array>,
  ) => Promise<ReadableStreamReadResult<Uint8Array> | null> = (reader) => reader.read(),
): Promise<unknown> {
  if (!response.ok) {
    cancelUnreadBody(response.body);
    throw new Error("capability response unavailable");
  }
  if (response.body === null) {
    throw new Error("capability response unavailable");
  }
  const declaredLength = response.headers.get("content-length");
  if (declaredLength !== null) {
    const length = Number(declaredLength);
    if (!Number.isSafeInteger(length) || length < 0 || length > maximumBytes) {
      cancelUnreadBody(response.body);
      throw new Error("capability response too large");
    }
  }
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  let complete = false;
  try {
    for (;;) {
      const result = await readNext(reader);
      if (result === null) throw new Error("capability response timeout");
      const { done, value } = result;
      if (done) {
        complete = true;
        break;
      }
      total += value.byteLength;
      if (total > maximumBytes) {
        throw new Error("capability response too large");
      }
      chunks.push(value);
    }
  } finally {
    if (complete) releaseReader(reader);
    else cancelAndReleaseReader(reader);
  }
  const bytes = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  let text: string;
  try {
    text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    throw new Error("capability response invalid");
  }
  try {
    return JSON.parse(text) as unknown;
  } catch {
    throw new Error("capability response invalid");
  }
}

const cancelledBodies = new WeakSet<ReadableStream<Uint8Array>>();
const cleanedReaders = new WeakSet<ReadableStreamDefaultReader<Uint8Array>>();

/** Cancel an unread response without allowing hostile cancellation to delay cleanup. */
export function cancelUnreadBody(body: ReadableStream<Uint8Array> | null): void {
  if (body === null || cancelledBodies.has(body)) return;
  cancelledBodies.add(body);
  try {
    void body.cancel().catch(() => undefined);
  } catch {
    // Cleanup is best effort and deliberately synchronous-bounded.
  }
}

/** Relinquish a reader exactly once without awaiting its cancellation promise. */
export function cancelAndReleaseReader(
  reader: ReadableStreamDefaultReader<Uint8Array>,
): void {
  if (cleanedReaders.has(reader)) return;
  cleanedReaders.add(reader);
  try {
    void reader.cancel().catch(() => undefined);
  } catch {
    // Cleanup is best effort and deliberately synchronous-bounded.
  }
  releaseReader(reader);
}

function releaseReader(reader: ReadableStreamDefaultReader<Uint8Array>): void {
  try {
    reader.releaseLock();
  } catch {
    // A hostile or already-released reader cannot retain widget ownership.
  }
}

export function codePointLength(value: string): number {
  return Array.from(value).length;
}

export function newChatAttemptState(): ChatAttemptState {
  return {
    terminal: false,
    sawOutput: false,
    sawCitations: false,
    outputCodePoints: 0,
    citationCount: 0,
    nonPingEvents: 0,
  };
}

export function validateChatEventBatch(
  events: readonly ChatStreamEvent[],
  state: ChatAttemptState,
): boolean {
  const next = { ...state };
  for (const event of events) {
    if (next.terminal) return false;
    next.nonPingEvents += 1;
    if (next.nonPingEvents > CHAT_NON_PING_EVENT_MAX_COUNT) return false;
    if (event.type === "status") {
      if (next.sawOutput) return false;
    } else if (event.type === "citations") {
      if (next.sawCitations || next.sawOutput) return false;
      next.sawCitations = true;
      next.citationCount += event.sources.length;
      if (next.citationCount > CHAT_CITATIONS_MAX_COUNT) return false;
    } else if (event.type === "chunk") {
      next.sawOutput = true;
      next.outputCodePoints += codePointLength(event.delta);
      if (next.outputCodePoints > CHAT_OUTPUT_MAX_CODE_POINTS) return false;
    } else {
      next.terminal = true;
    }
  }
  Object.assign(state, next);
  return true;
}

export function streamDeadlineRemaining(
  startedMilliseconds: number,
  lastCompleteRecordMilliseconds: number,
  nowMilliseconds: number,
): number {
  return Math.min(
    CHAT_COMPLETE_RECORD_IDLE_SECONDS * 1_000 -
      (nowMilliseconds - lastCompleteRecordMilliseconds),
    CHAT_TOTAL_DEADLINE_SECONDS * 1_000 - (nowMilliseconds - startedMilliseconds),
  );
}

/** Return only absolute HTTP(S) destinations suitable for a citation link. */
export function safeCitationUrl(value: string): string | null {
  try {
    const parsed = new URL(value);
    if (parsed.protocol === "http:" || parsed.protocol === "https:") {
      return parsed.href;
    }
  } catch {
    // Invalid citations remain visible as inert text rather than links.
  }
  return null;
}

function parseApiBase(value: string | null): string | null {
  if (value === null) return null;
  const trimmed = value.trim();
  if (trimmed === "" || trimmed.includes("?") || trimmed.includes("#")) return null;
  const normalized = trimmed.replace(/\/$/u, "");
  try {
    const parsed = new URL(normalized);
    if (
      (parsed.protocol !== "http:" && parsed.protocol !== "https:") ||
      parsed.username !== "" ||
      parsed.password !== "" ||
      parsed.search !== "" ||
      parsed.hash !== ""
    ) return null;
    return normalized.slice(parsed.origin.length).startsWith("/")
      ? `${parsed.origin}${parsed.pathname}`
      : parsed.origin;
  } catch {
    return null;
  }
}

function parseOptionalExternalUrl(value: string | null): string | null | undefined {
  const trimmed = value?.trim() ?? "";
  if (trimmed === "") return null;
  try {
    const parsed = new URL(trimmed);
    if (
      (parsed.protocol !== "http:" && parsed.protocol !== "https:") ||
      parsed.username !== "" ||
      parsed.password !== ""
    ) return undefined;
    return parsed.href;
  } catch {
    return undefined;
  }
}

function endpointFromBase(apiBase: string, suffix: string): string {
  const parsed = new URL(apiBase);
  const basePath = apiBase === parsed.origin ? "" : parsed.pathname;
  parsed.pathname = `${basePath}${suffix}`;
  return parsed.href;
}

function decodeRecord(record: string): ChatStreamEvent | null {
  let eventName = "message";
  const data: string[] = [];

  for (const line of record.split(/\r?\n/)) {
    if (line === "" || line.startsWith(":")) {
      continue;
    }
    const separator = line.indexOf(":");
    const field = separator === -1 ? line : line.slice(0, separator);
    const value = separator === -1 ? "" : line.slice(separator + 1).replace(/^ /, "");
    if (field === "event") {
      eventName = value;
    } else if (field === "data") {
      data.push(value);
    }
  }

  if (data.length === 0 || eventName === "ping") {
    return null;
  }

  let payload: unknown;
  try {
    payload = JSON.parse(data.join("\n"));
  } catch {
    throw new SseDecodeError("The chat response could not be read.");
  }
  if (!isRecord(payload) || payload.type !== eventName) {
    throw new SseDecodeError("The chat response did not match its declared event.");
  }

  switch (eventName) {
    case "status":
      return { type: "status", label: boundedString(payload, "label", 80) };
    case "chunk":
      return { type: "chunk", delta: boundedString(payload, "delta", 1_000, false) };
    case "citations":
      return { type: "citations", sources: requiredCitations(payload) };
    case "error":
      if (!ERROR_CODES.has(requiredString(payload, "code"))) {
        throw new SseDecodeError("The chat response contained an unknown error code.");
      }
      if (typeof payload.retryable !== "boolean") {
        throw new SseDecodeError("The chat response contained an invalid retryable value.");
      }
      return {
        type: "error",
        message: boundedString(payload, "message", 240),
        retryable: payload.retryable,
      };
    case "done": {
      const finishReason = requiredString(payload, "finish_reason");
      if (
        finishReason !== "stop" &&
        finishReason !== "refused" &&
        finishReason !== "limit" &&
        finishReason !== "cancelled"
      ) {
        throw new SseDecodeError("The chat response ended unexpectedly.");
      }
      return { type: "done", finishReason };
    }
    default:
      return null;
  }
}

function isRecord(value: unknown): value is RawEvent {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function requiredString(value: RawEvent, key: string): string {
  const field = value[key];
  if (typeof field !== "string") {
    throw new SseDecodeError(`The chat response omitted ${key}.`);
  }
  return field;
}

function boundedString(
  value: RawEvent,
  key: string,
  maxChars: number,
  allowEmpty = true,
): string {
  const field = requiredString(value, key);
  if ((!allowEmpty && field === "") || Array.from(field).length > maxChars) {
    throw new SseDecodeError(`The chat response contained an invalid ${key}.`);
  }
  return field;
}

function requiredCitations(value: RawEvent): Citation[] {
  const sources = value.sources;
  if (!Array.isArray(sources)) {
    throw new SseDecodeError("The chat response citations were invalid.");
  }
  if (sources.length > 6) {
    throw new SseDecodeError("The chat response contained too many citations.");
  }
  return sources.map((source) => {
    if (!isRecord(source)) {
      throw new SseDecodeError("The chat response citation was invalid.");
    }
    return {
      id: requiredString(source, "id"),
      title: boundedString(source, "title", 160),
      url: requiredString(source, "url"),
    };
  });
}

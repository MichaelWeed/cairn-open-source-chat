export const HISTORY_LIMIT = 5;
export const HISTORY_TURN_MAX_CODE_POINTS = 500;
export const HISTORY_AGGREGATE_MAX_CODE_POINTS = 2_000;
const HISTORY_EDGE_WHITESPACE = /^[\p{White_Space}\u001c-\u001f\ufeff]+|[\p{White_Space}\u001c-\u001f\ufeff]+$/gu;

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
  if (apiUrl === null) {
    return null;
  }
  const normalized = apiUrl.trim().replace(/\/$/, "");
  if (normalized === "") {
    return null;
  }
  try {
    const parsed = new URL(normalized);
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
      return null;
    }
  } catch {
    return null;
  }
  return `${normalized}/api/v1/chat/message`;
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

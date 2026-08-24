export const HISTORY_LIMIT = 5;

export interface ChatTurn {
  role: "user" | "assistant";
  content: string;
}

export interface Citation {
  id: string;
  title: string;
  url: string;
}

export type ChatStreamEvent =
  | { type: "status"; label: string }
  | { type: "chunk"; delta: string }
  | { type: "citations"; sources: Citation[] }
  | { type: "error"; message: string; retryable: boolean }
  | { type: "done"; finishReason: "stop" | "refused" };

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
  return history.slice(-HISTORY_LIMIT);
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
      return { type: "status", label: requiredString(payload, "label") };
    case "chunk":
      return { type: "chunk", delta: requiredString(payload, "delta") };
    case "citations":
      return { type: "citations", sources: requiredCitations(payload) };
    case "error":
      return {
        type: "error",
        message: requiredString(payload, "message"),
        retryable: payload.retryable === true,
      };
    case "done": {
      const finishReason = requiredString(payload, "finish_reason");
      if (finishReason !== "stop" && finishReason !== "refused") {
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

function requiredCitations(value: RawEvent): Citation[] {
  const sources = value.sources;
  if (!Array.isArray(sources)) {
    throw new SseDecodeError("The chat response citations were invalid.");
  }
  return sources.map((source) => {
    if (!isRecord(source)) {
      throw new SseDecodeError("The chat response citation was invalid.");
    }
    return {
      id: requiredString(source, "id"),
      title: requiredString(source, "title"),
      url: requiredString(source, "url"),
    };
  });
}

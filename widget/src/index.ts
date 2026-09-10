import {
  BoundedSseDecoder,
  SseDecodeError,
  CAPABILITY_DEADLINE_SECONDS,
  CHAT_RESPONSE_MAX_BYTES,
  CONFIGURATION_ERROR,
  MESSAGE_MAX_CODE_POINTS,
  boundedHistory,
  capabilityEndpoint,
  chatEndpoint,
  chatRequestPayload,
  codePointLength,
  completedHistory,
  parseWidgetConfiguration,
  readBoundedJsonResponse,
  safeCitationUrl,
  streamDeadlineRemaining,
  validateCapabilityManifest,
  validateChatEventBatch,
  newChatAttemptState,
  type ChatStreamEvent,
  type ChatAttemptState,
  type ChatTurn,
  type Citation,
  type DoneReason,
  type WidgetConfiguration,
} from "./protocol";

export const CAIRN_WIDGET_VERSION = "0.2.0";

const CAPABILITY_ERROR = "This Cairn service is not compatible with this widget.";
const NETWORK_ERROR = "Cairn could not be reached. Please try again.";
const PROTOCOL_ERROR = "Cairn returned an invalid response. Please try again later.";
const SESSION_KEY = "cairn-chat-session-id";

type WidgetState = "closed" | "checking" | "ready" | "sending" | "terminal";
type ErrorKind = "configuration" | "compatibility" | "network" | "protocol" | "service";

interface AssistantExchange {
  article: HTMLElement;
  content: HTMLElement;
  citations: HTMLElement;
  status: HTMLElement;
  text: string;
  finishReason: DoneReason | null;
  citationCount: number;
}

type AttemptResult =
  | { kind: "done"; finishReason: DoneReason; citationCount: number }
  | { kind: "service"; message: string; retryable: boolean }
  | { kind: "network" }
  | { kind: "protocol" }
  | { kind: "aborted" };

let widgetCount = 0;

export class CairnChat extends HTMLElement {
  private readonly root = this.attachShadow({ mode: "open" });
  private readonly instanceId = `cairn-chat-${++widgetCount}`;
  private initialized = false;
  private state: WidgetState = "closed";
  private generation = 0;
  private configuration: Readonly<WidgetConfiguration> | null = null;
  private compatibleBase: string | null = null;
  private compatibilityController: AbortController | null = null;
  private chatController: AbortController | null = null;
  private activeReader: ReadableStreamDefaultReader<Uint8Array> | null = null;
  private activeAssistant: AssistantExchange | null = null;
  private history: ChatTurn[] = [];
  private sessionId = "";
  private reportedConfigurationGeneration = -1;
  private currentHandoffReason: "refused" | "error" | null = null;
  private styleElement: HTMLStyleElement | null = null;
  private suppressNonceAttributeChange = false;
  private launcher!: HTMLButtonElement;
  private panel!: HTMLElement;
  private heading!: HTMLElement;
  private closeButton!: HTMLButtonElement;
  private input!: HTMLTextAreaElement;
  private counter!: HTMLElement;
  private sendButton!: HTMLButtonElement;
  private clearButton!: HTMLButtonElement;
  private form!: HTMLFormElement;
  private messages!: HTMLElement;
  private emptyState!: HTMLElement;
  private error!: HTMLElement;
  private privacyLink!: HTMLAnchorElement;
  private handoffLink!: HTMLAnchorElement;

  static get observedAttributes(): string[] {
    return ["api-url", "assistant-name", "theme", "privacy-url", "handoff-url", "nonce"];
  }

  connectedCallback(): void {
    if (this.initialized) return;
    this.initialized = true;
    this.sessionId = getSessionId();
    this.render();
    this.syncConfiguration();
  }

  disconnectedCallback(): void {
    this.abortWork();
    this.generation += 1;
  }

  attributeChangedCallback(name: string, _oldValue: string | null, newValue: string | null): void {
    if (this.initialized && !this.suppressNonceAttributeChange) {
      this.syncConfiguration(name === "nonce" ? newValue ?? "" : undefined);
    }
  }

  private render(): void {
    const markup = this.ownerDocument.createElement("template");
    markup.innerHTML = `
      <button class="launcher" id="launcher" type="button" aria-expanded="false" aria-controls="${this.instanceId}-panel"><span aria-hidden="true">●</span><span class="launcher-label">Ask Cairn</span></button>
      <section class="panel" id="${this.instanceId}-panel" role="dialog" aria-modal="false" aria-labelledby="${this.instanceId}-title" hidden>
        <header><p class="title" id="${this.instanceId}-title"></p><button class="close" id="close" type="button" aria-label="Close chat">×</button></header>
        <div class="messages" id="messages" role="log" aria-label="Conversation with Cairn" aria-live="polite" aria-relevant="additions text" aria-busy="false"><p class="empty" id="empty">Ask a question about the documents your operator has connected.</p></div>
        <div class="tail"><form id="form"><label for="${this.instanceId}-input">Message</label><textarea id="${this.instanceId}-input" rows="2" aria-describedby="${this.instanceId}-counter" placeholder="Ask a question about your docs"></textarea><div class="composer-meta"><span class="hint">Enter to send · Shift+Enter for a new line</span><span class="counter" id="${this.instanceId}-counter" role="status" aria-live="polite">0 / 500</span></div><div class="actions"><button class="clear" id="clear" type="button">Clear chat</button><button class="send" id="send" type="submit">Send</button></div></form><p class="error" id="error" role="alert" hidden></p><a class="handoff" id="handoff" target="_blank" rel="noopener noreferrer" referrerpolicy="no-referrer" hidden>Contact support</a><footer><p class="disclosure">Messages go to this site’s Cairn service. History stays on this page, and Cairn does not store a server transcript.</p><a class="privacy" id="privacy" target="_blank" rel="noopener noreferrer" referrerpolicy="no-referrer" hidden>Privacy details</a></footer></div>
      </section>`;
    this.root.append(markup.content.cloneNode(true));
    this.updateStyleNonce((this.nonce ?? "").trim());

    this.launcher = this.requireElement("launcher");
    this.panel = this.requireElement(`${this.instanceId}-panel`);
    this.heading = this.requireElement(`${this.instanceId}-title`);
    this.closeButton = this.requireElement("close");
    this.input = this.requireElement(`${this.instanceId}-input`);
    this.counter = this.requireElement(`${this.instanceId}-counter`);
    this.sendButton = this.requireElement("send");
    this.clearButton = this.requireElement("clear");
    this.form = this.requireElement("form");
    this.messages = this.requireElement("messages");
    this.emptyState = this.requireElement("empty");
    this.error = this.requireElement("error");
    this.privacyLink = this.requireElement("privacy");
    this.handoffLink = this.requireElement("handoff");

    this.launcher.addEventListener("click", () => void this.open());
    this.closeButton.addEventListener("click", () => this.close("button"));
    this.clearButton.addEventListener("click", () => this.clearChat());
    this.form.addEventListener("submit", (event) => {
      event.preventDefault();
      void this.send();
    });
    this.input.addEventListener("input", () => this.updateCounter());
    this.input.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        this.form.requestSubmit();
      }
    });
    this.panel.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        this.close("escape");
      }
    });
    this.handoffLink.addEventListener("click", (event) => {
      if (this.currentHandoffReason === null) {
        event.preventDefault();
        return;
      }
      const accepted = this.dispatchHostEvent(
        "cairn-handoff",
        { version: "1.0", reason: this.currentHandoffReason },
        true,
      );
      if (!accepted) event.preventDefault();
    });
    this.updateCounter();
  }

  private syncConfiguration(nonceOverride?: string): void {
    const priorConfiguration = this.configuration;
    const priorBase = priorConfiguration?.apiBase ?? null;
    const nonceValue = nonceOverride ?? priorConfiguration?.styleNonce ?? this.nonce;
    const result = parseWidgetConfiguration((name) => this.getAttribute(name), nonceValue);
    if (!result.ok) {
      this.concealHostNonce();
      this.configuration = null;
      this.generation += 1;
      this.abortWork();
      this.compatibleBase = null;
      this.setState(this.panel.hidden ? "closed" : "terminal");
      this.showError(CONFIGURATION_ERROR);
      this.hideHandoff();
      this.launcher.setAttribute("aria-label", "Cairn chat configuration error");
      if (!this.panel.hidden) this.reportConfigurationError();
      return;
    }

    const next = result.value;
    if (priorBase !== null && priorBase !== next.apiBase) {
      this.generation += 1;
      this.abortWork();
      this.compatibleBase = null;
    }
    this.configuration = next;
    this.heading.textContent = next.assistantName;
    this.messages.setAttribute("aria-label", `Conversation with ${next.assistantName}`);
    this.launcher.querySelector(".launcher-label")!.textContent = `Ask ${next.assistantName}`;
    this.launcher.setAttribute("aria-label", `Chat with ${next.assistantName}`);
    this.setAttribute("data-theme", next.theme);
    this.updateStyleNonce(next.styleNonce ?? "");
    this.concealHostNonce();
    this.setSafeLink(this.privacyLink, next.privacyUrl);
    if (this.currentHandoffReason !== null) this.setSafeLink(this.handoffLink, next.handoffUrl);
    const requiresNegotiation = priorConfiguration === null || priorBase !== next.apiBase;
    if (!requiresNegotiation) return;
    this.clearError();
    if (!this.panel.hidden) {
      this.setState(this.compatibleBase === next.apiBase ? "ready" : "checking");
      if (this.compatibleBase !== next.apiBase) void this.checkCompatibility();
    }
  }

  private async open(): Promise<void> {
    if (!this.panel.hidden) return;
    this.panel.hidden = false;
    this.launcher.setAttribute("aria-expanded", "true");
    this.launcher.setAttribute("aria-hidden", "true");
    this.dispatchHostEvent("cairn-open", { version: "1.0" });
    if (this.configuration === null) {
      this.setState("terminal");
      this.showError(CONFIGURATION_ERROR);
      this.reportConfigurationError();
      this.closeButton.focus();
      return;
    }
    if (this.compatibleBase === this.configuration.apiBase) {
      this.setState("ready");
      queueMicrotask(() => {
        if (!this.panel.hidden && this.state === "ready") this.input.focus();
      });
      return;
    }
    this.setState("checking");
    this.closeButton.focus();
    await this.checkCompatibility();
  }

  private close(reason: "button" | "escape"): void {
    if (this.panel.hidden) return;
    this.generation += 1;
    this.abortWork();
    this.panel.hidden = true;
    this.setState("closed");
    this.launcher.setAttribute("aria-expanded", "false");
    this.launcher.removeAttribute("aria-hidden");
    queueMicrotask(() => {
      if (this.panel.hidden) this.launcher.focus();
    });
    this.dispatchHostEvent("cairn-close", { version: "1.0", reason });
  }

  private async checkCompatibility(): Promise<void> {
    const config = this.configuration;
    if (config === null || this.compatibilityController !== null) return;
    const generation = this.generation;
    const controller = new AbortController();
    this.compatibilityController = controller;
    const timeout = setTimeout(() => controller.abort(), CAPABILITY_DEADLINE_SECONDS * 1_000);
    let response: Response;
    try {
      response = await fetch(capabilityEndpoint(config.apiBase), {
        method: "GET",
        credentials: "omit",
        referrerPolicy: "no-referrer",
        cache: "no-store",
        headers: { Accept: "application/json" },
        signal: controller.signal,
      });
    } catch {
      if (this.isCurrent(generation, config.apiBase)) this.compatibilityFailure("network");
      clearTimeout(timeout);
      if (this.compatibilityController === controller) this.compatibilityController = null;
      return;
    }
    try {
      const manifest = await readBoundedJsonResponse(response);
      if (!this.isCurrent(generation, config.apiBase)) return;
      if (!validateCapabilityManifest(manifest)) {
        this.compatibilityFailure("compatibility");
        return;
      }
      this.compatibleBase = config.apiBase;
      this.clearError();
      this.setState("ready");
      if (!this.panel.hidden && this.root.activeElement === this.closeButton) this.input.focus();
    } catch {
      if (!this.isCurrent(generation, config.apiBase)) return;
      this.compatibilityFailure(controller.signal.aborted ? "network" : "compatibility");
    } finally {
      clearTimeout(timeout);
      if (this.compatibilityController === controller) this.compatibilityController = null;
    }
  }

  private compatibilityFailure(kind: "compatibility" | "network"): void {
    this.compatibleBase = null;
    this.setState("terminal");
    this.showError(kind === "compatibility" ? CAPABILITY_ERROR : NETWORK_ERROR);
    this.dispatchHostEvent("cairn-error", { version: "1.0", kind, retryable: kind === "network" });
  }

  private async send(): Promise<void> {
    const config = this.configuration;
    if (
      config === null ||
      this.compatibleBase !== config.apiBase ||
      this.chatController !== null ||
      (this.state !== "ready" && this.state !== "terminal")
    ) return;
    const message = this.input.value.trim();
    const messageLength = codePointLength(message);
    if (message === "" || messageLength > MESSAGE_MAX_CODE_POINTS) {
      this.updateCounter();
      if (messageLength > MESSAGE_MAX_CODE_POINTS) this.showError("Messages must be 500 characters or fewer.");
      return;
    }

    const history = boundedHistory(this.history);
    const payload = chatRequestPayload(this.sessionId, message, history);
    const endpoint = chatEndpoint(config.apiBase);
    if (endpoint === null) return;
    const generation = this.generation;
    const controller = new AbortController();
    this.chatController = controller;
    this.clearError();
    this.hideHandoff();
    this.appendMessage(message);
    this.input.value = "";
    this.updateCounter();
    const assistant = this.appendAssistant();
    this.activeAssistant = assistant;
    this.setState("sending");

    try {
      let result = await this.streamAttempt(endpoint, payload, controller, assistant);
      if (result.kind === "service" && result.retryable && this.isCurrent(generation, config.apiBase)) {
        this.resetAssistant(assistant);
        result = await this.streamAttempt(endpoint, payload, controller, assistant);
      }
      if (!this.isCurrent(generation, config.apiBase)) return;
      if (result.kind === "done") {
        this.history = completedHistory(this.history, message, assistant.text, result.finishReason);
        this.setState("terminal");
        if (result.finishReason === "refused") this.showHandoff("refused");
        this.dispatchHostEvent("cairn-complete", { version: "1.0", finishReason: result.finishReason, citationCount: result.citationCount });
      } else if (result.kind !== "aborted") {
        const kind: ErrorKind = result.kind === "service" ? "service" : result.kind;
        const messageText = result.kind === "service" ? result.message : result.kind === "network" ? NETWORK_ERROR : PROTOCOL_ERROR;
        this.failExchange(assistant, messageText);
        this.setState("terminal");
        this.showHandoff("error");
        this.dispatchHostEvent("cairn-error", { version: "1.0", kind, retryable: result.kind === "service" ? result.retryable : result.kind === "network" });
      }
    } finally {
      if (this.chatController === controller) this.chatController = null;
      if (this.activeAssistant === assistant) this.activeAssistant = null;
      if ((this.state as WidgetState) === "sending") this.setState("ready");
    }
  }

  private async streamAttempt(
    endpoint: string,
    payload: ReturnType<typeof chatRequestPayload>,
    controller: AbortController,
    assistant: AssistantExchange,
  ): Promise<AttemptResult> {
    const started = Date.now();
    let lastCompleteRecord = started;
    let reader: ReadableStreamDefaultReader<Uint8Array> | null = null;
    try {
      const response = await withDeadline(fetch(endpoint, {
        method: "POST",
        credentials: "omit",
        referrerPolicy: "no-referrer",
        cache: "no-store",
        headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
        body: JSON.stringify(payload),
        signal: controller.signal,
      }), streamDeadlineRemaining(started, lastCompleteRecord, Date.now()));
      if (response === null) {
        controller.abort();
        return { kind: "protocol" };
      }
      const declaredLength = response.headers.get("content-length");
      if (!response.ok || response.body === null) return { kind: "network" };
      if (declaredLength !== null) {
        const length = Number(declaredLength);
        if (!Number.isSafeInteger(length) || length < 0 || length > CHAT_RESPONSE_MAX_BYTES) {
          await response.body.cancel();
          return { kind: "protocol" };
        }
      }

      reader = response.body.getReader();
      this.activeReader = reader;
      const decoder = new BoundedSseDecoder();
      const streamState = newChatAttemptState();
      for (;;) {
        const now = Date.now();
        const remaining = streamDeadlineRemaining(started, lastCompleteRecord, now);
        if (remaining <= 0) return { kind: "protocol" };
        const read = await withDeadline(reader.read(), remaining);
        if (read === null) return { kind: "protocol" };
        if (read.done) {
          decoder.finish();
          return streamState.terminal ? this.completedResult(assistant, streamState) : { kind: "protocol" };
        }
        const events = decoder.push(read.value);
        if (decoder.recordsCompleted > 0) lastCompleteRecord = Date.now();
        if (!validateChatEventBatch(events, streamState)) return { kind: "protocol" };
        for (const event of events) {
          const result = this.applyEvent(event, assistant);
          if (result !== null) {
            await reader.cancel();
            return result;
          }
        }
      }
    } catch (error) {
      if (controller.signal.aborted) return { kind: "aborted" };
      return error instanceof SseDecodeError ? { kind: "protocol" } : { kind: "network" };
    } finally {
      if (reader !== null) {
        try {
          await reader.cancel();
        } catch {
          // Cancellation is best effort after terminal ownership has been released.
        }
        reader.releaseLock();
      }
      if (this.activeReader === reader) this.activeReader = null;
    }
  }

  private completedResult(assistant: AssistantExchange, state: ChatAttemptState): AttemptResult {
    if (assistant.finishReason === null) return { kind: "protocol" };
    return { kind: "done", finishReason: assistant.finishReason, citationCount: state.citationCount };
  }

  private applyEvent(event: ChatStreamEvent, assistant: AssistantExchange): AttemptResult | null {
    if (event.type === "status") assistant.status.textContent = event.label;
    else if (event.type === "chunk") {
      assistant.text += event.delta;
      assistant.content.textContent = assistant.text;
    } else if (event.type === "citations") {
      assistant.citationCount = event.sources.length;
      this.appendCitations(assistant.citations, event.sources);
    } else if (event.type === "error") return { kind: "service", message: event.message, retryable: event.retryable };
    else if (event.type === "done") {
      assistant.finishReason = event.finishReason;
      assistant.article.dataset.complete = "true";
      assistant.status.textContent = event.finishReason === "refused" ? "Cairn could not find a confident answer." : event.finishReason === "limit" ? "Answer reached its length limit." : event.finishReason === "cancelled" ? "Answer cancelled." : "Answer complete";
      if (event.finishReason === "refused") assistant.article.classList.add("refusal");
      return { kind: "done", finishReason: event.finishReason, citationCount: assistant.citationCount };
    }
    this.scrollMessages();
    return null;
  }

  private appendMessage(text: string): void {
    this.emptyState.hidden = true;
    const article = this.ownerDocument.createElement("article");
    article.className = "user";
    const content = this.ownerDocument.createElement("div");
    content.className = "message";
    content.textContent = text;
    article.append(content);
    this.messages.append(article);
    this.scrollMessages();
  }

  private appendAssistant(): AssistantExchange {
    this.emptyState.hidden = true;
    const article = this.ownerDocument.createElement("article");
    article.className = "assistant";
    const content = this.ownerDocument.createElement("div");
    content.className = "message";
    const citations = this.ownerDocument.createElement("div");
    citations.className = "citations";
    const status = this.ownerDocument.createElement("span");
    status.className = "stream-status";
    status.textContent = "Contacting Cairn…";
    article.append(content, citations, status);
    this.messages.append(article);
    this.scrollMessages();
    return { article, content, citations, status, text: "", finishReason: null, citationCount: 0 };
  }

  private appendCitations(container: HTMLElement, sources: Citation[]): void {
    for (const source of sources) {
      const target = safeCitationUrl(source.url);
      if (target === null) {
        const chip = this.ownerDocument.createElement("span");
        chip.className = "citation inert";
        chip.textContent = source.title;
        container.append(chip);
      } else {
        const link = this.ownerDocument.createElement("a");
        link.className = "citation";
        link.href = target;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.referrerPolicy = "no-referrer";
        link.textContent = source.title;
        container.append(link);
      }
    }
  }

  private failExchange(assistant: AssistantExchange, message: string): void {
    assistant.article.classList.add("failed");
    assistant.status.textContent = "Answer unavailable";
    if (assistant.text === "") assistant.content.textContent = "Cairn could not complete that answer.";
    this.showError(message);
    this.scrollMessages();
  }

  private resetAssistant(assistant: AssistantExchange): void {
    assistant.article.classList.remove("failed", "refusal");
    delete assistant.article.dataset.complete;
    assistant.content.textContent = "";
    assistant.citations.replaceChildren();
    assistant.status.textContent = "Retrying Cairn…";
    assistant.text = "";
    assistant.finishReason = null;
    assistant.citationCount = 0;
    this.clearError();
  }

  private clearChat(): void {
    this.generation += 1;
    this.abortWork();
    this.history = [];
    this.messages.querySelectorAll("article").forEach((article) => article.remove());
    this.emptyState.hidden = false;
    this.clearError();
    this.hideHandoff();
    this.sessionId = rotateSessionId();
    this.setState(this.panel.hidden ? "closed" : this.compatibleBase !== null ? "ready" : "checking");
    this.dispatchHostEvent("cairn-clear", { version: "1.0" });
    if (!this.panel.hidden && this.state === "ready") this.input.focus();
    else if (!this.panel.hidden) {
      this.closeButton.focus();
      void this.checkCompatibility();
    } else this.launcher.focus();
  }

  private abortWork(): void {
    this.compatibilityController?.abort();
    this.chatController?.abort();
    if (this.activeAssistant !== null && this.activeAssistant.finishReason === null) {
      this.activeAssistant.status.textContent = "Answer cancelled.";
    }
    this.compatibilityController = null;
    this.chatController = null;
    const reader = this.activeReader;
    this.activeReader = null;
    if (reader !== null) void reader.cancel().catch(() => undefined);
    if (this.initialized) this.messages.setAttribute("aria-busy", "false");
  }

  private setState(state: WidgetState): void {
    this.state = state;
    const compatible = this.configuration !== null && this.compatibleBase === this.configuration.apiBase;
    const sending = state === "sending";
    this.input.disabled = !compatible || sending;
    this.sendButton.disabled = !compatible || sending;
    this.clearButton.disabled = false;
    this.messages.setAttribute("aria-busy", String(sending));
    if (state === "checking") this.emptyState.textContent = "Checking service compatibility…";
    else if (this.history.length === 0 && this.messages.querySelector("article") === null) this.emptyState.textContent = "Ask a question about the documents your operator has connected.";
  }

  private updateCounter(): void {
    const count = codePointLength(this.input.value);
    this.counter.textContent = `${count} / ${MESSAGE_MAX_CODE_POINTS}`;
    this.input.setAttribute("aria-invalid", String(count > MESSAGE_MAX_CODE_POINTS));
  }

  private showError(message: string): void {
    this.error.textContent = "";
    this.error.textContent = message;
    this.error.hidden = false;
  }

  private clearError(): void {
    this.error.textContent = "";
    this.error.hidden = true;
  }

  private showHandoff(reason: "refused" | "error"): void {
    const target = this.configuration?.handoffUrl ?? null;
    if (target === null) return;
    this.currentHandoffReason = reason;
    this.setSafeLink(this.handoffLink, target);
  }

  private hideHandoff(): void {
    this.currentHandoffReason = null;
    this.setSafeLink(this.handoffLink, null);
  }

  private setSafeLink(link: HTMLAnchorElement, target: string | null): void {
    if (target === null) {
      link.hidden = true;
      link.removeAttribute("href");
    } else {
      link.href = target;
      link.hidden = false;
    }
  }

  private updateStyleNonce(nonce: string): void {
    const style = this.ownerDocument.createElement("style");
    if (nonce !== "") style.nonce = nonce;
    style.textContent = WIDGET_CSS;
    if (this.styleElement === null) this.root.prepend(style);
    else this.styleElement.replaceWith(style);
    this.styleElement = style;
  }

  private concealHostNonce(): void {
    if (!this.hasAttribute("nonce") || this.getAttribute("nonce") === "") return;
    this.suppressNonceAttributeChange = true;
    try {
      this.setAttribute("nonce", "");
    } finally {
      this.suppressNonceAttributeChange = false;
    }
  }

  private reportConfigurationError(): void {
    if (this.reportedConfigurationGeneration === this.generation) return;
    this.reportedConfigurationGeneration = this.generation;
    this.dispatchHostEvent("cairn-error", { version: "1.0", kind: "configuration", retryable: false });
  }

  private dispatchHostEvent(name: string, detail: object, cancelable = false): boolean {
    return this.dispatchEvent(new CustomEvent(name, { detail, bubbles: true, composed: true, cancelable }));
  }

  private isCurrent(generation: number, apiBase: string): boolean {
    return this.isConnected && this.generation === generation && this.configuration?.apiBase === apiBase;
  }

  private scrollMessages(): void {
    this.messages.scrollTop = this.messages.scrollHeight;
  }

  private requireElement<T extends Element>(id: string): T {
    const element = this.root.getElementById(id);
    if (element === null) throw new Error(`Widget template is missing ${id}.`);
    return element as unknown as T;
  }
}

async function withDeadline<T>(promise: Promise<T>, milliseconds: number): Promise<T | null> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([promise, new Promise<null>((resolve) => { timer = setTimeout(() => resolve(null), milliseconds); })]);
  } finally {
    clearTimeout(timer);
  }
}

function getSessionId(): string {
  try {
    const existing = sessionStorage.getItem(SESSION_KEY);
    if (existing !== null && existing !== "") return existing;
    return rotateSessionId();
  } catch {
    return crypto.randomUUID();
  }
}

function rotateSessionId(): string {
  const id = crypto.randomUUID();
  try {
    sessionStorage.removeItem(SESSION_KEY);
    sessionStorage.setItem(SESSION_KEY, id);
  } catch {
    return id;
  }
  return id;
}

const WIDGET_CSS = `
  :host { --cairn-bg: #ffffff; --cairn-surface: #eef1ec; --cairn-text: #1b1e1a; --cairn-muted: #515b50; --cairn-border: #d4d8cf; --cairn-accent: #315940; --cairn-accent-text: #f4faf6; bottom: max(1.25rem, env(safe-area-inset-bottom, 0px)); color: var(--cairn-text); display: block; font-family: Inter, ui-sans-serif, system-ui, sans-serif; max-width: calc(100vw - 2.5rem - env(safe-area-inset-left, 0px) - env(safe-area-inset-right, 0px)); position: fixed; right: max(1.25rem, env(safe-area-inset-right, 0px)); z-index: 2147483000; }
  :host([data-theme="dark"]) { --cairn-bg: #20251f; --cairn-surface: #2c352b; --cairn-text: #eef2ec; --cairn-muted: #c2c9bf; --cairn-border: #596355; --cairn-accent: #9ed2a8; --cairn-accent-text: #142018; }
  *, *::before, *::after { box-sizing: border-box; }
  button, textarea { font: inherit; }
  button, a { min-height: 44px; min-width: 44px; }
  button { cursor: pointer; }
  button:active { transform: translateY(1px); }
  button:focus-visible, textarea:focus-visible, a:focus-visible { outline: 3px solid var(--cairn-accent); outline-offset: 2px; }
  .launcher { align-items: center; background: var(--cairn-accent); border: 0; border-radius: 999px; box-shadow: 0 12px 30px rgb(27 30 26 / 24%); color: var(--cairn-accent-text); display: flex; font-weight: 700; gap: .5rem; max-width: 100%; min-height: 52px; padding: .75rem 1rem; }
  .launcher[aria-expanded="true"] { opacity: 0; pointer-events: none; }
  .launcher-label { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .launcher:hover, .send:hover { filter: brightness(.9); }
  .panel { background: var(--cairn-bg); border: 1px solid var(--cairn-border); border-radius: 1rem; bottom: calc(4.1rem + env(safe-area-inset-bottom, 0px)); box-shadow: 0 18px 48px rgb(27 30 26 / 23%); display: grid; grid-template-rows: auto minmax(0, 1fr) auto; height: min(38rem, calc(100vh - 6.5rem - env(safe-area-inset-top, 0px) - env(safe-area-inset-bottom, 0px))); height: min(38rem, calc(100dvh - 6.5rem - env(safe-area-inset-top, 0px) - env(safe-area-inset-bottom, 0px))); max-width: calc(100vw - env(safe-area-inset-left, 0px) - env(safe-area-inset-right, 0px)); overflow: hidden; position: absolute; right: 0; width: min(25rem, calc(100vw - 2.5rem - env(safe-area-inset-left, 0px) - env(safe-area-inset-right, 0px))); }
  .panel[hidden], [hidden] { display: none !important; }
  header { align-items: center; background: var(--cairn-surface); border-bottom: 1px solid var(--cairn-border); display: flex; justify-content: space-between; padding: .55rem .75rem; }
  .title { font-size: 1rem; font-weight: 750; margin: 0; overflow-wrap: anywhere; }
  .close { background: transparent; border: 0; border-radius: .375rem; color: var(--cairn-text); font-size: 1.25rem; height: 44px; line-height: 1; width: 44px; }
  .close:hover, .clear:hover { background: var(--cairn-surface); }
  .messages { display: flex; flex-direction: column; gap: .75rem; min-height: 0; overflow-y: auto; padding: 0 1rem; }
  .empty { color: var(--cairn-muted); font-size: .925rem; line-height: 1.5; margin: auto 0; text-align: center; }
  article { align-self: flex-start; max-width: 92%; }
  article:first-of-type { margin-top: 1rem; }
  article:last-of-type { margin-bottom: 1rem; }
  .message { border-radius: .75rem; line-height: 1.5; overflow-wrap: anywhere; padding: .7rem .8rem; white-space: pre-wrap; word-break: break-word; }
  .user { align-self: flex-end; }
  .user .message { background: #315940; color: #f4faf6; }
  .assistant .message { background: var(--cairn-surface); color: var(--cairn-text); }
  .assistant.refusal .message { background: #f8f3e8; border: 1px solid #9a7b2f; color: #2e2719; }
  :host([data-theme="dark"]) .assistant.refusal .message { background: #3c3526; border-color: #a99557; color: #fff7df; }
  .stream-status { color: var(--cairn-muted); display: block; font-size: .8rem; margin: .4rem .15rem 0; }
  .citations { display: flex; flex-wrap: wrap; gap: .4rem; margin-top: .5rem; }
  .citation { align-items: center; background: var(--cairn-bg); border: 1px solid var(--cairn-border); border-radius: .5rem; color: var(--cairn-accent); display: inline-flex; font-size: .78rem; max-width: 100%; overflow-wrap: anywhere; padding: .4rem .55rem; text-decoration: none; }
  .citation:hover { background: var(--cairn-surface); }
  .citation.inert { color: var(--cairn-muted); min-height: auto; min-width: auto; }
  .tail { background: var(--cairn-bg); border-top: 1px solid var(--cairn-border); max-height: 21rem; min-height: 0; overflow-y: auto; }
  form { display: grid; gap: .4rem; padding: .55rem .75rem .45rem; }
  label { color: var(--cairn-text); font-size: .8rem; font-weight: 650; }
  textarea { background: var(--cairn-bg); border: 1px solid #737c70; border-radius: .6rem; color: var(--cairn-text); min-height: 48px; padding: .5rem .6rem; resize: vertical; width: 100%; }
  textarea:disabled { background: var(--cairn-surface); cursor: not-allowed; }
  .composer-meta, .actions { align-items: center; display: flex; gap: .5rem; justify-content: space-between; }
  .hint, .counter { color: var(--cairn-muted); font-size: .75rem; line-height: 1.3; }
  .send { background: var(--cairn-accent); border: 0; border-radius: .5rem; color: var(--cairn-accent-text); font-weight: 700; min-height: 44px; padding: .45rem .85rem; }
  .send:disabled, .clear:disabled { cursor: not-allowed; opacity: .58; }
  .clear { background: transparent; border: 1px solid var(--cairn-border); border-radius: .5rem; color: var(--cairn-text); padding: .4rem .65rem; }
  .error { background: #fdecea; border-block: 1px solid #d98c83; color: #751c17; font-size: .84rem; line-height: 1.4; margin: 0; padding: .55rem .75rem; }
  :host([data-theme="dark"]) .error { background: #472722; border-color: #a7665d; color: #ffd7d2; }
  .handoff { align-items: center; background: var(--cairn-accent); color: var(--cairn-accent-text); display: flex; font-weight: 700; justify-content: center; margin: .5rem .75rem; padding: .55rem .75rem; text-decoration: none; }
  footer { align-items: center; display: flex; gap: .5rem; justify-content: space-between; padding: .4rem .75rem .55rem; }
  .disclosure { color: var(--cairn-muted); font-size: .69rem; line-height: 1.35; margin: 0; max-width: 18rem; }
  .privacy { align-items: center; color: var(--cairn-accent); display: inline-flex; font-size: .75rem; padding: .25rem; text-align: center; }
  @media (max-width: 480px) { :host { bottom: max(.75rem, env(safe-area-inset-bottom, 0px)); right: max(.75rem, env(safe-area-inset-right, 0px)); } .panel { bottom: calc(4rem + env(safe-area-inset-bottom, 0px)); height: calc(100vh - 5.5rem - env(safe-area-inset-top, 0px) - env(safe-area-inset-bottom, 0px)); height: calc(100dvh - 5.5rem - env(safe-area-inset-top, 0px) - env(safe-area-inset-bottom, 0px)); width: calc(100vw - 1.5rem - env(safe-area-inset-left, 0px) - env(safe-area-inset-right, 0px)); } .hint { max-width: 12rem; } }
  @media (prefers-color-scheme: dark) { :host([data-theme="auto"]) { --cairn-bg: #20251f; --cairn-surface: #2c352b; --cairn-text: #eef2ec; --cairn-muted: #c2c9bf; --cairn-border: #596355; --cairn-accent: #9ed2a8; --cairn-accent-text: #142018; } }
  @media (prefers-reduced-motion: reduce) { *, *::before, *::after { animation-duration: .01ms !important; animation-iteration-count: 1 !important; scroll-behavior: auto !important; transition-duration: .01ms !important; } }
  @media (forced-colors: active) { .launcher, .send, .clear, .close, textarea, a, .panel, .message, .error { border: 1px solid CanvasText; forced-color-adjust: auto; } button:focus-visible, textarea:focus-visible, a:focus-visible { outline-color: Highlight; } }
`;

if (!customElements.get("cairn-chat")) customElements.define("cairn-chat", CairnChat);

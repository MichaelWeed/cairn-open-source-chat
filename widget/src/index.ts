import {
  SseDecoder,
  boundedHistory,
  chatEndpoint,
  retryOnce,
  safeCitationUrl,
  type ChatStreamEvent,
  type ChatTurn,
  type Citation,
  type StreamAttemptResult,
} from "./protocol";

export const CAIRN_WIDGET_VERSION = "0.1.0";

let widgetCount = 0;

interface AssistantExchange {
  article: HTMLElement;
  content: HTMLElement;
  citations: HTMLElement;
  status: HTMLElement;
  text: string;
}

class CairnChat extends HTMLElement {
  private readonly root = this.attachShadow({ mode: "open" });
  private readonly instanceId = `cairn-chat-${++widgetCount}`;
  private controller: AbortController | null = null;
  private history: ChatTurn[] = [];
  private initialized = false;
  private sessionId = "";
  private launcher!: HTMLButtonElement;
  private panel!: HTMLElement;
  private heading!: HTMLElement;
  private closeButton!: HTMLButtonElement;
  private input!: HTMLTextAreaElement;
  private sendButton!: HTMLButtonElement;
  private form!: HTMLFormElement;
  private messages!: HTMLElement;
  private emptyState!: HTMLElement;
  private error!: HTMLElement;

  static get observedAttributes(): string[] {
    return ["api-url", "assistant-name"];
  }

  connectedCallback(): void {
    if (this.initialized) {
      return;
    }
    this.initialized = true;
    this.sessionId = getSessionId();
    this.render();
    this.syncConfiguration();
  }

  disconnectedCallback(): void {
    this.controller?.abort();
  }

  attributeChangedCallback(): void {
    if (!this.initialized) {
      return;
    }
    this.heading.textContent = this.assistantName;
    this.syncConfiguration();
  }

  private get assistantName(): string {
    return this.getAttribute("assistant-name")?.trim() || "Cairn";
  }

  private render(): void {
    this.root.innerHTML = `
      <style>
        :host { bottom: 1.25rem; color: #1b1e1a; display: block; font-family: Inter, ui-sans-serif, system-ui, sans-serif; position: fixed; right: 1.25rem; z-index: 2147483000; }
        *, *::before, *::after { box-sizing: border-box; }
        button, textarea { font: inherit; }
        button { cursor: pointer; }
        button:focus-visible, textarea:focus-visible, a:focus-visible { outline: 3px solid #3e6b4f; outline-offset: 3px; }
        .launcher { align-items: center; background: #3e6b4f; border: 0; border-radius: 999px; box-shadow: 0 12px 30px rgb(27 30 26 / 24%); color: #f4faf6; display: flex; font-weight: 700; gap: .5rem; min-height: 3.25rem; padding: .75rem 1rem; }
        .launcher:hover { background: #315940; }
        .panel { background: #fff; border: 1px solid #e2ded4; border-radius: 1rem; bottom: 4.1rem; box-shadow: 0 18px 48px rgb(27 30 26 / 23%); display: grid; grid-template-rows: auto minmax(0, 1fr) auto; height: min(38rem, calc(100vh - 6.5rem)); overflow: hidden; position: absolute; right: 0; width: min(25rem, calc(100vw - 2.5rem)); }
        .panel[hidden] { display: none; }
        header { align-items: center; background: #eef1ec; border-bottom: 1px solid #e2ded4; display: flex; justify-content: space-between; padding: .875rem 1rem; }
        .title { font-size: 1rem; font-weight: 750; margin: 0; }
        .close { background: transparent; border: 0; border-radius: .375rem; color: #384238; font-size: 1.25rem; height: 2rem; line-height: 1; width: 2rem; }
        .close:hover { background: #dce5dc; }
        .messages { display: flex; flex-direction: column; gap: .75rem; min-height: 0; overflow-y: auto; padding: 0 1rem; }
        .empty { color: #5e665d; font-size: .925rem; line-height: 1.5; margin: auto 0; text-align: center; }
        article { align-self: flex-start; max-width: 92%; }
        article:first-of-type { margin-top: 1rem; }
        article:last-of-type { margin-bottom: 1rem; }
        .message { border-radius: .75rem; line-height: 1.5; overflow-wrap: anywhere; padding: .7rem .8rem; white-space: pre-wrap; word-break: break-word; }
        .user { align-self: flex-end; }
        .user .message { background: #3e6b4f; color: #f4faf6; }
        .assistant .message { background: #eef1ec; color: #1b1e1a; }
        .assistant.refusal .message { background: #f8f3e8; border: 1px solid #e6d5a8; }
        .stream-status { color: #5e665d; display: block; font-size: .8rem; margin: .4rem .15rem 0; }
        .citations { display: flex; flex-wrap: wrap; gap: .4rem; margin-top: .5rem; }
        .citation { background: #f7f6f2; border: 1px solid #d9d5cb; border-radius: 999px; color: #315940; display: inline-block; font-size: .78rem; max-width: 100%; overflow-wrap: anywhere; padding: .25rem .5rem; text-decoration: none; }
        .citation:hover { background: #e7eee7; }
        .citation.inert { color: #5e665d; }
        form { border-top: 1px solid #e2ded4; display: grid; gap: .55rem; padding: .75rem; }
        label { color: #4f574e; font-size: .8rem; font-weight: 650; }
        textarea { border: 1px solid #bdb8ae; border-radius: .6rem; color: #1b1e1a; min-height: 3rem; padding: .55rem .65rem; resize: vertical; width: 100%; }
        textarea:disabled { background: #f0eee9; cursor: not-allowed; }
        .actions { align-items: center; display: flex; gap: .5rem; justify-content: space-between; }
        .hint { color: #6b7268; font-size: .75rem; line-height: 1.3; }
        .send { background: #3e6b4f; border: 0; border-radius: .5rem; color: #f4faf6; font-weight: 700; min-height: 2.35rem; padding: .45rem .75rem; }
        .send:disabled { background: #92a392; cursor: not-allowed; }
        .error { background: #fdecea; border-top: 1px solid #efc3bd; color: #8c211b; font-size: .84rem; line-height: 1.4; margin: 0; padding: .65rem .75rem; }
        .error[hidden] { display: none; }
        @media (max-width: 480px) { :host { bottom: .75rem; right: .75rem; } .panel { bottom: 4rem; height: calc(100vh - 5.25rem); width: calc(100vw - 1.5rem); } }
        @media (prefers-color-scheme: dark) { :host { color: #eef2ec; } .panel { background: #20251f; border-color: #495045; box-shadow: 0 18px 48px rgb(0 0 0 / 45%); } header, .assistant .message { background: #2c352b; border-color: #495045; color: #eef2ec; } .close { color: #dbe7da; } .close:hover { background: #3a4739; } .empty, .stream-status, .hint { color: #b9c0b6; } textarea { background: #182018; border-color: #596355; color: #eef2ec; } textarea:disabled { background: #293028; } form { border-color: #495045; } label { color: #d2d9d0; } .citation { background: #273026; border-color: #536052; color: #9ed2a8; } .citation:hover { background: #354335; } .citation.inert { color: #c4cbc0; } .assistant.refusal .message { background: #3c3526; border-color: #827144; } .error { background: #472722; border-color: #704039; color: #ffd7d2; } }
        @media (prefers-reduced-motion: reduce) { *, *::before, *::after { animation-duration: .01ms !important; animation-iteration-count: 1 !important; scroll-behavior: auto !important; transition-duration: .01ms !important; } }
      </style>
      <button class="launcher" id="launcher" type="button" aria-expanded="false" aria-controls="${this.instanceId}-panel"><span aria-hidden="true">●</span><span>Ask Cairn</span></button>
      <section class="panel" id="${this.instanceId}-panel" role="dialog" aria-modal="false" aria-labelledby="${this.instanceId}-title" hidden>
        <header><p class="title" id="${this.instanceId}-title"></p><button class="close" id="close" type="button" aria-label="Close chat">×</button></header>
        <div class="messages" id="messages" role="log" aria-live="polite" aria-relevant="additions text"><p class="empty" id="empty">Ask a question about the documents your operator has connected.</p></div>
        <div><form id="form"><label for="${this.instanceId}-input">Message</label><textarea id="${this.instanceId}-input" maxlength="500" rows="3" placeholder="Ask a question about your docs"></textarea><div class="actions"><span class="hint">Enter to send · Shift+Enter for a new line</span><button class="send" id="send" type="submit">Send</button></div></form><p class="error" id="error" role="alert" hidden></p></div>
      </section>
    `;

    this.launcher = this.requireElement<HTMLButtonElement>("launcher");
    this.panel = this.requireElement<HTMLElement>(`${this.instanceId}-panel`);
    this.heading = this.requireElement<HTMLElement>(`${this.instanceId}-title`);
    this.closeButton = this.requireElement<HTMLButtonElement>("close");
    this.input = this.requireElement<HTMLTextAreaElement>(`${this.instanceId}-input`);
    this.sendButton = this.requireElement<HTMLButtonElement>("send");
    this.form = this.requireElement<HTMLFormElement>("form");
    this.messages = this.requireElement<HTMLElement>("messages");
    this.emptyState = this.requireElement<HTMLElement>("empty");
    this.error = this.requireElement<HTMLElement>("error");
    this.heading.textContent = this.assistantName;

    this.launcher.addEventListener("click", () => this.open());
    this.closeButton.addEventListener("click", () => this.close());
    this.form.addEventListener("submit", (event) => {
      event.preventDefault();
      void this.send();
    });
    this.input.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        this.close();
      } else if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        this.form.requestSubmit();
      }
    });
    this.panel.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        this.close();
      }
    });
  }

  private syncConfiguration(): void {
    const valid = chatEndpoint(this.getAttribute("api-url")) !== null;
    this.input.disabled = !valid;
    this.sendButton.disabled = !valid || this.controller !== null;
    if (!valid) {
      this.showError("Cairn needs a valid http(s) api-url before it can answer.");
      this.launcher.setAttribute("aria-label", "Cairn chat configuration error");
    } else {
      this.clearError();
      this.launcher.setAttribute("aria-label", `Chat with ${this.assistantName}`);
    }
  }

  private open(): void {
    this.panel.hidden = false;
    this.launcher.setAttribute("aria-expanded", "true");
    queueMicrotask(() => this.input.focus());
  }

  private close(): void {
    this.controller?.abort();
    this.panel.hidden = true;
    this.launcher.setAttribute("aria-expanded", "false");
    this.launcher.focus();
  }

  private async send(): Promise<void> {
    if (this.controller !== null) {
      return;
    }
    const endpoint = chatEndpoint(this.getAttribute("api-url"));
    if (endpoint === null) {
      this.syncConfiguration();
      return;
    }
    const message = this.input.value.trim();
    if (message === "") {
      return;
    }

    this.clearError();
    this.appendMessage(message);
    this.input.value = "";
    const assistant = this.appendAssistant();
    const controller = new AbortController();
    this.controller = controller;
    this.input.disabled = true;
    this.sendButton.disabled = true;

    try {
      const history = boundedHistory(this.history);
      const result = await retryOnce(
        () => this.streamAttempt(endpoint, message, history, controller, assistant),
        () => this.resetAssistant(assistant),
      );
      if (result.kind === "done") {
        this.history = boundedHistory([...this.history, { role: "user", content: message }, { role: "assistant", content: assistant.text }]);
      } else if (result.kind === "error") {
        this.failExchange(assistant, result.message);
      }
    } finally {
      if (this.controller === controller) {
        this.controller = null;
        this.input.disabled = false;
        this.sendButton.disabled = false;
      }
    }
  }

  private async streamAttempt(
    endpoint: string,
    message: string,
    history: ChatTurn[],
    controller: AbortController,
    assistant: AssistantExchange,
  ): Promise<StreamAttemptResult<void>> {
    try {
      const response = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: this.sessionId, message, history }),
        signal: controller.signal,
      });
      if (!response.ok || response.body === null) {
        throw new Error("The chat service did not return a response.");
      }

      const decoder = new SseDecoder();
      const textDecoder = new TextDecoder();
      const reader = response.body.getReader();
      while (true) {
        const { done, value } = await reader.read();
        if (done) {
          break;
        }
        const error = this.handleEvents(decoder.push(textDecoder.decode(value, { stream: true })), assistant);
        if (error !== null) {
          return error;
        }
      }
      for (const events of [decoder.push(textDecoder.decode()), decoder.finish()]) {
        const error = this.handleEvents(events, assistant);
        if (error !== null) {
          return error;
        }
      }
      if (assistant.article.dataset.complete === "true") {
        return { kind: "done", value: undefined };
      }
      return {
        kind: "error",
        message: "The connection was interrupted before Cairn could finish. Please try again.",
        retryable: false,
      };
    } catch (error) {
      if (controller.signal.aborted) {
        return { kind: "aborted" };
      }
      return { kind: "error", message: userSafeError(error), retryable: false };
    }
  }

  private handleEvents(
    events: ChatStreamEvent[],
    assistant: AssistantExchange,
  ): Extract<StreamAttemptResult<void>, { kind: "error" }> | null {
    for (const event of events) {
      if (event.type === "status") {
        assistant.status.textContent = event.label;
      } else if (event.type === "chunk") {
        assistant.text += event.delta;
        assistant.content.textContent = assistant.text;
      } else if (event.type === "citations") {
        this.appendCitations(assistant.citations, event.sources);
      } else if (event.type === "error") {
        this.failExchange(assistant, event.message);
        return { kind: "error", message: event.message, retryable: event.retryable };
      } else if (event.type === "done") {
        assistant.article.dataset.complete = "true";
        assistant.status.textContent = event.finishReason === "refused" ? "Cairn could not find a confident answer." : "Answer complete";
        if (event.finishReason === "refused") {
          assistant.article.classList.add("refusal");
        }
      }
    }
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
    return { article, content, citations, status, text: "" };
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
        link.textContent = source.title;
        container.append(link);
      }
    }
    this.scrollMessages();
  }

  private failExchange(assistant: AssistantExchange, message: string): void {
    assistant.article.classList.add("failed");
    assistant.status.textContent = "Answer unavailable";
    if (assistant.text === "") {
      assistant.content.textContent = "Cairn could not complete that answer.";
    }
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
    this.clearError();
    this.scrollMessages();
  }

  private showError(message: string): void {
    this.error.textContent = message;
    this.error.hidden = false;
  }

  private clearError(): void {
    this.error.textContent = "";
    this.error.hidden = true;
  }

  private scrollMessages(): void {
    this.messages.scrollTop = this.messages.scrollHeight;
  }

  private requireElement<T extends Element>(id: string): T {
    const element = this.root.getElementById(id);
    if (element === null) {
      throw new Error(`Widget template is missing ${id}.`);
    }
    return element as unknown as T;
  }
}

function getSessionId(): string {
  const key = "cairn-chat-session-id";
  try {
    const existing = sessionStorage.getItem(key);
    if (existing !== null && existing !== "") {
      return existing;
    }
    const id = crypto.randomUUID();
    sessionStorage.setItem(key, id);
    return id;
  } catch {
    return crypto.randomUUID();
  }
}

function userSafeError(error: unknown): string {
  if (error instanceof Error && error.message === "The chat response ended before it was complete.") {
    return "The connection was interrupted before Cairn could finish. Please try again.";
  }
  return "Cairn could not complete that answer. Please try again.";
}

if (!customElements.get("cairn-chat")) {
  customElements.define("cairn-chat", CairnChat);
}

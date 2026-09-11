# Production widget configuration

The packaged `<cairn-chat>` custom element is Cairn's supported browser embed.
It is a client for an operator-run Cairn backend, not a hosted Cairn service.
Configure the backend's `ORIGIN_ALLOWLIST` with every exact site origin that may
embed it, serve the widget bundle, and use the two-line embed printed by
`make live`:

```html
<script src="https://cairn.example/widget/widget.js" defer></script>
<cairn-chat api-url="https://cairn.example"></cairn-chat>
```

The example host is illustrative. Use the URL of your own Cairn deployment.
Loading or connecting the element sends no request. The first user-open starts
capability negotiation, and chat remains disabled until it succeeds.

## Attributes

| Attribute | Contract |
| --- | --- |
| `api-url` | Required absolute HTTP(S) base. Credentials, query strings, and fragments are rejected. One trailing slash is removed; path prefixes and internal double slashes are preserved. |
| `assistant-name` | Optional display name. Blank means `Cairn`; otherwise 1 through 80 Unicode code points with no control characters. |
| `theme` | Exact `auto`, `light`, or `dark`; blank means `auto`. Palettes are fixed inside the widget. |
| `privacy-url` | Optional absolute credential-free HTTP(S) policy URL. The link sends no chat or page context. |
| `handoff-url` | Optional absolute credential-free HTTP(S) operator-owned support link, offered only after refusal or terminal chat failure. It is not built-in ticket or CRM integration. |
| `nonce` | Optional standard CSP nonce in base64 or base64url syntax, at most 256 ASCII characters. Generate at least 128 random bits for every response. |

All six attributes are observed. The widget parses a complete immutable snapshot
before applying it. An invalid snapshot fails closed with fixed content-free UI.
Changing `api-url` aborts active work and requires a new capability check;
cosmetic changes do not alter a captured request, history, session, or stream.

Optional configuration can be added without changing the basic embed:

```html
<script nonce="{{response_nonce}}" src="{{cairn_base}}/widget/widget.js" defer></script>
<cairn-chat api-url="{{cairn_base}}" nonce="{{response_nonce}}"
  assistant-name="Support" theme="auto"
  privacy-url="{{operator_privacy_url}}"
  handoff-url="{{operator_support_url}}"></cairn-chat>
```

Do not reuse a nonce between responses. The widget validates the host element's
standard `nonce` property, authorizes its fixed shadow-root style before insertion,
and conceals the readable attribute copy. It never places the nonce in CSS text,
URLs, storage, requests, errors, events, the configuration snapshot, or another
widget-owned field. Dynamic changes are read from and applied through the standard
`nonce` property without creating a second retained copy.

## Capability negotiation and CORS

On first open, the element sends one `GET <api-url>/api/v1/capabilities` with
credentials omitted, no referrer, no cache, and `Accept: application/json`. It
accepts canonical capability schema `1.x` values from `1.1` onward, including the
packaged `1.2` manifest, then requires chat API `1.0`, SSE `1.1`, widget `0.2.0`,
and `capabilities.widget.production_configuration = "available"`. Unknown
additive fields are ignored only after that same-major schema check succeeds.
The 5-second deadline includes headers and the body, which is fatally decoded as
UTF-8 and limited to 16 KiB. Malformed, oversized, timed-out, CORS-blocked, or
mismatched responses leave chat disabled and send no message request. Success is
cached only for that element and canonical API base; failure can be retried by
closing and opening again.

The browser supplies the standard `Origin` header. Cairn's exact
`ORIGIN_ALLOWLIST` and CORS response are authoritative. Cookies and authorization
are never sent by the widget. Every chat attempt uses `POST`, credentials omitted,
no referrer, no cache, `Content-Type: application/json`, and
`Accept: text/event-stream`.

## Data, history, and clear

The chat body contains only `session_id`, the current `message`, and bounded
`history`. Messages are limited to 500 Unicode code points with an accessible
counter. The widget retains at most five nonblank completed turns, 500 code points
per turn and 2,000 in aggregate, in memory for the page lifetime. Completed
`stop` and `limit` exchanges can enter history; refusals, cancellations, errors,
aborts, and empty answers cannot. Citations are not retained in history.

The opaque session UUID lives in `sessionStorage` when available. `Clear chat`
aborts active work, clears rendered exchanges, citations, errors, handoff, and
in-memory history, then rotates that UUID. It does not claim to delete server data;
the Cairn chat server does not store a transcript.

Safe citation, privacy, and handoff destinations are reparsed as absolute HTTP(S)
URLs and open with `_blank`, `noopener noreferrer`, and no referrer. A handoff
requires a user click and appends no message, session, citation, page, or error
context.

## Host events

Events originate on `<cairn-chat>`, bubble, and cross the shadow boundary. Only
`cairn-handoff` is cancelable; preventing it suppresses navigation. Details are
content-free:

| Event | Detail |
| --- | --- |
| `cairn-open` | `{ version: "1.0" }` |
| `cairn-close` | `{ version: "1.0", reason: "button" \| "escape" }` |
| `cairn-complete` | `{ version: "1.0", finishReason, citationCount }` |
| `cairn-error` | `{ version: "1.0", kind, retryable }` |
| `cairn-handoff` | `{ version: "1.0", reason: "refused" \| "error" }` |
| `cairn-clear` | `{ version: "1.0" }` |

Error kinds are `configuration`, `compatibility`, `network`, `protocol`, or
`service`. Details never contain messages, URLs, response bodies, exceptions,
citations, sessions, or DOM objects.

## Accessibility, CSP, and browser support

The component uses a named nonmodal dialog, a polite named message log, a dedicated
alert, predictable focus, Enter-to-send, Shift+Enter newline, Escape-to-close,
44 by 44 CSS pixel targets, fixed AA contrast palettes, reduced-motion and
forced-colors modes, dynamic viewport units, safe-area insets, and wrapping for
long, CJK, RTL, and emoji content. It targets current evergreen browsers with
custom elements, shadow DOM, Fetch, readable streams, `TextDecoder`, and
`crypto.randomUUID`.

A strict host policy can use:

```text
default-src 'none';
script-src 'nonce-<response nonce>';
style-src 'nonce-<response nonce>';
connect-src <exact Cairn origin>;
base-uri 'none';
form-action 'none';
frame-ancestors 'none'
```

The host must authorize its own bootstrap and the widget script as appropriate.
No `unsafe-inline`, `unsafe-eval`, remote font, image, or style source is required.

## Stream safety and 0.1 migration

Chat streams use fatal UTF-8 decoding and fixed limits: 32 MiB response and
pending record, 6,000 output code points, six aggregate citations in one citation
event, 6,016 non-ping events, 45 seconds without a complete valid SSE record, and a
600-second absolute deadline. Readers are canceled and released on terminal,
timeout, overflow, close, clear, disconnect, or stale configuration. Pending SSE
bytes are scanned with a retained cursor and copied with amortized linear work,
including when an unterminated record arrives one byte at a time. Cancellation is
initiated without awaiting a producer-controlled cancellation promise.
Record boundaries preserve SSE `LF+LF`, `LF+CRLF`, `CRLF+LF`, and `CRLF+CRLF`
compatibility across arbitrary transport splits.
Only a recognized, successfully decoded record refreshes inactivity. A ping is a
visible no-op and refreshes inactivity only when its JSON body decodes to exactly
one property, `type: "ping"`; blank, comment-only, data-less, unknown, malformed,
and type-mismatched records do not refresh it.

Widget compatibility `0.2.0` tightens configurations that `0.1.0` accepted.
Remove credentials, queries, and fragments from `api-url`; use exact lowercase
themes; keep names within 80 code points; and use only valid absolute HTTP(S)
privacy and handoff URLs. Refused answers no longer enter later history. The chat
API and SSE compatibility versions remain unchanged.

`widget.production_configuration = "available"` describes this packaged widget
configuration boundary only. It does not certify an operator environment, provide
deployment-wide abuse controls, assert provider readiness, or create a hosted
service. Signed widget tokens, deployment-wide budgets, built-in ticket creation,
CRM integration, and an author-hosted support service remain planned.

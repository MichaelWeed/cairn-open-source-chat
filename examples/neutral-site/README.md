# Neutral embed example

This directory is a static, customer-neutral host page for the production
`<cairn-chat>` widget. The example does not provide a hosted service, proxy chat,
store history, ingest documents, or add authentication.

1. Replace every occurrence of `https://cairn.example` with the absolute base URL
   of your own Cairn service.
2. Replace `https://example.test/privacy` and `https://example.test/support` with
   operator-owned destinations, or remove the optional attributes.
3. Set `ORIGIN_ALLOWLIST=<exact origin of this page>` for the Cairn service. The
   scheme, host, and port must match exactly.
4. For every response, replace every occurrence of `REPLACE_WITH_RESPONSE_NONCE`
   with a new CSP nonce containing at least 128 random bits. Send the matching
   Content-Security-Policy header; the meta policy in `index.html` documents the
   same strict policy for static inspection.
5. Serve this directory from its own origin. For a local inspection, run:

   ```sh
   python3 -m http.server --directory examples/neutral-site 4173
   ```

The two adjacent lines at the end of `index.html` are the complete embed. The page
uses no remote font, image, analytics, cookie, or script other than the widget
served by the operator's Cairn base URL. The widget can negotiate capabilities with
that configured origin, but it sends no chat message until a person submits one.
Privacy and contact links are inert until a person activates them.

The strict policy permits only the per-response script nonce, the local stylesheet,
the widget's nonce-authorized shadow style, and connections to the exact Cairn
origin. It does not require relaxed script or style execution. If the nonce is
missing or mismatched, the browser blocks the widget style visibly instead of
silently weakening the policy.

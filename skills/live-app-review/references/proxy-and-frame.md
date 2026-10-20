# The proxy, frame mode, and the run folder

## What the skill ships

| Path | Purpose |
|---|---|
| `scripts/review.py` | The command line. Python 3 standard library only |
| `scripts/review_config.py` | The one validator for `review.json`. `review.py` and the proxy both import it |
| `scripts/review-proxy.py` | Token-gated reverse proxy. It serves the wrapper, the layer files, the comments API, mocks, read-only rules, reset, and the expiry page |
| `scripts/review_verify.py` | Helper for `review.py verify`. It drives headless Chromium |
| `assets/template/review-frame.html` | The wrapper page. The proxy renders it from `review.json`. It loads `layer.css` and `layer.js` from `/__review/` |
| `tests/fixtures/` | A fixture app, a reset command, a fake `tailscale`, stub apps, and a complete example `review.json` |
| `scripts/test_*.py` | `test_browser.py` (layer, proxy in a browser, whole stack), `test_proxy.py`, `test_review_cli.py` |

The proxy serves the layer files from `../mockup-review/assets/layer/`. It refuses to start when they are missing.

## How frame mode works

1. The layer runs in the wrapper page. It reads and draws inside the same-origin iframe.
2. Pins sit in the wrapper. The layer draws them again on iframe scroll, resize, load, and DOM changes.
3. The screen id is `path:` plus the iframe pathname. A hash route that starts with `#/` is part of it.
4. A tab loads its `src`. The `id` only picks the first tab (`#screen=<id>`).
5. When the iframe is on another origin, the browser blocks access. A banner says so. The Page comment button still works. `review.json` cannot name another origin. Only a hand-written wrapper can.
6. The proxy answers `/__review/session` for auto-login. The wrapper writes the values into its storage, then loads the iframe. Without a `session` block, the proxy sets `"session": false` in `cmt-config`, and the wrapper never calls `/__review/session`.
7. The wrapper area around a small viewport is not the app. It gets no outline and no comment.
8. The proxy puts `startMode`, `session`, and the screens (with `prefill`) into `cmt-config`. The layer runs a screen's `prefill` inside the iframe, once each time the reviewer arrives at the screen.

## Proxy rules

1. The proxy checks the expiry first. Then it checks the cookie. Every request needs the `review_session` cookie. A request with `?t=<token>` sets the cookie and redirects to the same URL without `t`. Other requests get 403.
2. The cookie is an HMAC of the token. It is HttpOnly, Secure, SameSite Lax, and lasts 7 days.
3. The proxy adds `Referrer-Policy: no-referrer` and `X-Robots-Tag: noindex` to every response.
4. The proxy sends traffic only to the `routes` upstreams. It refuses absolute-URL request lines and `CONNECT`.
5. The proxy rewrites `X-Frame-Options: DENY` to `SAMEORIGIN`. It changes no other header value.
6. The proxy tunnels a WebSocket upgrade after the token check, and only when the app answers HTTP 101. Any other answer is a normal response and closes the connection. An open tunnel closes at the expiry time.
7. The proxy does not log request lines, cookies, tokens, or credentials.
8. The proxy closes a connection that sends nothing for 30 seconds. Change this with `proxy.idleTimeout`.
9. The proxy flags are runtime values from `review.py`: `--port`, `--public-host` (an empty value turns the host of the config off), `--expires-at`, and `--check`. Nothing else overrides `review.json`.

## What `up` does

1. Preflight runs the proxy in check mode (`review-proxy.py --config review.json --check`). It finds every config error that the proxy would find, before an app starts.
2. `up` marks the state complete only when every component runs. A state without that mark is stale (an `up` that was killed). The next `up` stops what is left and starts again. SIGTERM and SIGHUP roll back like Ctrl-C.
3. `up` checks `REVIEW_TOKEN` before it starts anything. A bad value exits 2.
4. `down` also stops the children of an app whose leader process died first.
5. `review.py` starts every component with `start_new_session`. Each component leads its own session, so it survives the agent shell.
6. Runtime files live in `<review>/.review/`. The folder has mode 0700 and a `.gitignore` with `*`.
7. `link` still prints the public link for a state that an older version wrote with `funnel.mode` "external". `down` then turns that Funnel off.

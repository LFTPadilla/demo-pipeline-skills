# Limits and troubleshooting

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Preflight says `run "sudo tailscale set --operator=$USER" once.` | Your user is not the Tailscale operator. Run that line once yourself. Then run `review.py up` again. `review.py` never runs `sudo` |
| `up` exits 5 and prints the same line | The node refused the Funnel call. Run that line once yourself, then run `up` again |
| `preflight` says `Funnel is off for this node` | Turn on HTTPS and add the `funnel` node attribute in the tailnet policy |
| `preflight` says `port <n> is in use` | Stop that process or change `proxy.port` or the app `port`. If an old review holds the port, run `review.py down` |
| Apps or proxy die after some time, for example under memory pressure | A process that belongs to the agent shell is killed with it. Use `review.py up`. It starts each component in its own session. Check with `review.py status`, and restart with `up` |
| `status` says `stale: run review.py down` | The processes died and the state file is left. Run `review.py down`, then `up` |
| `up` exits 4 | An app or the proxy was not healthy. `review.py` prints the last log lines. Run `review.py logs <name>` |
| `up` exits 3 | An env file has group or world permissions, or a secret is in `review.json`. Run `chmod 600 <file>`. Move the value to the env file |
| `up` exits 2 | `review.json` is invalid. The message names the key |
| `up` exits 2 and names `funnel.mode` | The old external Funnel mode was removed. Delete `mode` from the `funnel` block. Set `"enabled": true`. Run the `sudo` line above once. Then run `review.py down` for the old review and `review.py up` |
| `up` exits 2 and says `comment layer not found` | The sibling skill `mockup-review` is missing. Install both skills in the same folder |
| Reviewer sees "This review has ended" | The expiry time passed and the proxy still runs (the watchdog is late or failed). Run `down`, set a new `ttl`, and run `up` again for a new link |
| Reviewer sees a connection error after the expiry time | The watchdog stopped the proxy and turned Funnel off. This is expected. Set a new `ttl` and run `up` again for a new link |
| `down` exits 5 and says `Funnel is still public` | `tailscale funnel off` failed. Run the `sudo tailscale funnel --https=<port> off` line that it prints, or fix Tailscale. Then run `review.py down` again |
| `up` exits 2 and says `REVIEW_TOKEN must have 16 to 200 characters` | The token format is wrong. Use a token from `python3 -c "import secrets; print(secrets.token_urlsafe(24))"`, or unset `REVIEW_TOKEN` |
| Reviewer sees "Review paused" | An app is down. Run `review.py status` and `review.py logs <name>` |
| No read-only toast, but the write fails | The app saved `window.fetch` before the layer wrapped it. The 403 still reaches the app. Check the app's error message |
| Banner "This app runs on another origin" | The iframe `src` is on another host. Use paths in `frame.screens` |
| The iframe is blank or says it refused to connect | The app sends `X-Frame-Options: ALLOW-FROM` or a CSP `frame-ancestors` list. The proxy only turns `DENY` into `SAMEORIGIN`. Remove the rule in the review build |
| The app shows its login page inside the iframe | The `store` key in `session` does not match the key the app reads |
| Pins vanish after a deploy of the app | The element path changed. Give stable elements an `id`. Raise `reviewVersion` |
| `verify` row says `Chromium not found` | Set `CHROMIUM` to the browser path |
| A tab shows `Sample data did not load (...)` | The `prefill` selector did not match within 15 s. Open the page in a browser, copy a stable selector, and fix `prefill`. Prefer `id`, `placeholder`, or `role` over generated class names |
| `verify` says `waited N s for Funnel to propagate` and still fails | Funnel was not reachable for 60 s. Run `tailscale funnel status`, then run `review.py verify` again |

## Known limits

1. The proxy stores comments in one JSON file with one lock. The cap is 500 comments and 100 replies per comment, and 50 names per reaction.
2. Author names are typed text. Edit and delete rules stop mistakes. They do not stop a determined user.
3. Frame mode needs one origin. The app must call its API with relative URLs.
4. Pins attach by CSS path. Lists that the app builds again on each render can lose their pins.
5. A screen id is `path:` plus the pathname and a `#/` hash route, cut to 64 characters.
6. The proxy talks to upstreams over plain HTTP on the same machine. It does not support `https://` upstreams.
7. The proxy keeps the browser `Origin` header. An API that checks `Origin` against its own host can reject calls.
8. The proxy uses the default `Host` of the upstream.
9. A new pin shows a number from the browser until the next sync (20 seconds). Then the proxy sets the final number.
10. The proxy has no cap on open connections. It only closes silent ones after the idle timeout.
11. The read-only toast needs the app to call `fetch` or `XMLHttpRequest` after the iframe loads.
12. Public `verify` connects to a real TLS port. Only the DNS step, the retry logic, and `verify --local` have automatic tests.
13. The layer is two files from the sibling skill `mockup-review`. A copy of this skill alone does not run.

## Tests

Run from the repository root. The tests use a fake `tailscale`. They never touch a real tailnet, Docker, or Cloudflare.

```bash
uv run --with playwright python plugins/dev-skills/skills/live-app-review/scripts/test_browser.py
python3 plugins/dev-skills/skills/live-app-review/scripts/test_proxy.py
python3 plugins/dev-skills/skills/live-app-review/scripts/test_review_cli.py
```

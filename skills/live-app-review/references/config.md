# review.json and its features

`scripts/review_config.py` checks `review.json`. `review.py` and `review-proxy.py` both call it. The proxy reads no other settings.
Relative paths resolve against the review folder. A bad file exits with code 2. A security refusal exits with code 3.

## Keys

| Key | Meaning |
|---|---|
| `project`, `title`, `reviewVersion`, `reviewNote` | Names that the wrapper shows. `reviewVersion` is the review round, not the app version |
| `startMode` | `"comment"` (default) or `"browse"`. It sets the mode on the first visit of a reviewer. With `"browse"`, the one-time hint says: `Press C or click Comment to leave feedback.` A mode that the reviewer chose stays |
| `publicHost` | Public host name. When `null` and Funnel is on, `review.py` reads it from `tailscale status --json` |
| `funnel` | `enabled` and `port` (443, 8443, or 10000). `up --funnel` and `up --no-funnel` override `enabled`. The key `mode` was removed |
| `proxy` | `port`, `idleTimeout`, `upstreamTimeoutSeconds`, `commentsFile`, `envFile`. The proxy binds `127.0.0.1` only |
| `apps` | One item per app. An item without `command` is external. `review.py` does not start or stop it. The names `proxy`, `watchdog`, and `funnel` are reserved |
| `routes` | Path prefix to an app name or to `http://127.0.0.1:<port>`. The key `/` is required |
| `session` | Auto-login. Every login field is a `$VARIABLE` name. Without it, the wrapper never calls `/__review/session` |
| `readOnly`, `allowWrite` | Write rules. See "Read-only" |
| `mocks`, `mocksFile` | Answers that the proxy gives itself. See "Mocks" |
| `expiresAt` or `ttl` | End of the review. Set one, not both |
| `appVersion` | A string, or `{"command": [...]}` that prints the version. Shown on comments |
| `reset` | Command that puts the demo data back. See "Reset" |
| `frame.screens` | Tabs. Each has `id`, `title`, `src`, and optional `note`, `highlight`, and `prefill` |

In an `argv` list and in an app `env` value, the text `{port}` becomes the app port. No command runs through a shell.

## Keep credentials in a 0600 env file

1. Write `KEY=VALUE` lines in an env file, for example `.review.env`. Use no quotes and no `$` expansion.
2. Run `chmod 600 .review.env`. `review.py` refuses a file that is not yours, or that has a group or world permission bit. It exits with code 3.
3. Name the file in `proxy.envFile` (the login values for `session`) and in `apps[].envFile` (what an app reads).
4. Every value in `session.login.fields` must be a `$VARIABLE` name. A literal value is a security refusal.
5. `apps[].env` is for non-secret values only. A key that looks like a secret (pass, secret, token, key, credential, auth) is a security refusal.
6. Keep the env file out of git. The proxy never serves a file that `proxy.envFile` or `apps[].envFile` names.
7. A command is public in `ps`. Put no secret in `command`, `reset.command`, or `appVersion.command`. `review.py` warns about an argument like `--password=...`.
8. The reset and `appVersion` commands do not inherit the `$VARIABLE` values of `session.login.fields`.

## The share link and the token

1. `review.py up` prints the share link once. The link holds the token that the proxy really uses. No file holds the token. The proxy keeps it in its environment.
2. On Linux, `link` and `verify` read the token from the proxy environment. On other systems set `REVIEW_TOKEN`. `REVIEW_TOKEN` wins when it is set.
3. No log, `state.json`, or message holds the token. Treat the link as a password. Whoever holds it gets the rights of the auto-login account.
4. When `REVIEW_TOKEN` is set, `up` uses it. It must have 16 to 200 characters from `A-Z a-z 0-9 _ . ~ -`. A bad value exits 2 before anything starts. Use this to move a review and keep the links that you already sent.
5. When `REVIEW_TOKEN` is not set, every `up` makes a new token. Every old link and cookie becomes invalid.
6. Pass `REVIEW_TOKEN` in the environment of the command only. Do not write it into a file, the repo, or `review.json`.

## Funnel

1. `review.py up --funnel` runs `tailscale funnel --bg`. `down` and the expiry watchdog run `tailscale funnel --https=<port> off`. It never runs `tailscale serve` or `funnel reset`.
2. It needs the Tailscale operator. Run `sudo tailscale set --operator=$USER` once. Preflight says: `run "sudo tailscale set --operator=$USER" once.` `review.py` never runs `sudo`.
3. When `funnel off` fails, `review.py` never says that Funnel is off. It exits 5, keeps `state.json` with a marker, and `status` shows "still public" and exits 1. Fix Tailscale and run `down` again.
4. `up --funnel` or `up --no-funnel` on a review that runs with the other Funnel setting exits 1. Run `down` first.

## Mocks

A mock is an answer that the proxy gives itself. The app is not called. A mock beats routes and the read-only check.

1. A rule has `method` (default `GET`, or `*`), `path`, and `json` or `table`. It can have `status` (default 200).
2. A `path` segment `{name}` matches exactly one segment. It never matches `.`, `..`, or a value with a slash, also when the value is percent-encoded. Other segments are literal.
3. With `by` and `table`, the value of the named segment picks an entry. A miss gives the rule's own `status` and `json`.
4. The most specific rule wins: more literal segments, then more literal characters, then an exact method, then the earlier rule.
5. `mocksFile` names a JSON file with a rule array. The proxy reads no file named `mocks.json` on its own. Inline rules come first.
6. A mock never matches a path under `/__review`.

## Read-only

1. `readOnly` defaults to `true`. The proxy answers POST, PUT, PATCH, and DELETE to an app with 403 and the header `X-Review-Read-Only: 1`.
2. `allowWrite` lists `{method, path}` pairs that pass. `method` is in capital letters. The path uses the same pattern syntax as mocks.
3. The auto-login call and the `/__review/` routes are never blocked. GET, HEAD, OPTIONS, and WebSocket upgrades are never blocked.
4. The layer shows a toast when a request gets that 403: `Read-only review: this change was not sent to the app.`
5. A request with the header `X-HTTP-Method-Override`, `X-HTTP-Method`, or `X-Method-Override`, or with a `_method` query parameter, gets the same 403. The proxy never forwards those headers.
6. The proxy checks the HTTP method only. It cannot check a message inside a WebSocket. Do not enable writes over WebSocket in the app for a review.
7. Set `"readOnly": false` only when the app holds sample data and the reviewers must write.

## Expiry

1. Set `ttl` (for example `7d` or `36h`) or `expiresAt` (ISO 8601). `ttl` counts from the moment of `review.py up`.
2. After that time, the proxy answers every request with 410 and the page "This review has ended".
3. A watchdog runs the `down` logic at the expiry time. It stops the proxy and turns Funnel off. A reviewer then sees a connection error. The 410 page shows only when the watchdog is late or has failed.
4. When `funnel off` fails, the watchdog tries again 6 times, 5 seconds apart. It keeps `state.json` with the marker. Run `review.py down` to finish the job.
5. `status` shows the time left.

## Paused page

When an app refuses a connection or times out before it sends headers, the proxy answers 503 with `Retry-After: 30`.
A browser gets the page "Review paused, try again later". Other clients get JSON `{"detail":"review paused","retryAfterSeconds":30}`.

## App version on comments

1. `GET /__review/meta` answers `version`, `expiresAt`, `readOnly`, and `resetEnabled`.
2. A new comment stores the app version.
3. A comment from an older version is stale. Its pin has a dashed ring, and a tag "from <version>" shows.
4. A stale comment has two buttons. "Still an issue" adds a reply. "Resolved in <version>" adds a reply and resolves the comment.

## Per-tab notes and highlights

1. `note` shows as plain text under the tab bar when that tab is active.
2. `highlight` is a CSS selector. The layer outlines each match in the iframe. The Highlights button turns it on and off, and the layer remembers the choice.

## Prefill: sample data for a screen that starts empty

Some pages show data only after the reviewer types in a search box. Add `prefill` to that screen. The layer then does it for the reviewer.

```json
"prefill": [
  { "selector": "input[placeholder*='Search by client']", "value": "Quillfeather", "submit": "Enter", "delayMs": 2000, "waitForSelector": "[role=tablist]" },
  { "click": "[role=tab]:nth-of-type(2)" }
]
```

1. `prefill` is a list of up to 10 steps. They run in order.
2. A step with `selector` and `value` types the value. `submit` is `Enter` (default), `Tab`, or `none`. `delayMs` (0 to 10000, default 0) is the wait before the submit key. Set it above the debounce time of the app.
3. A step with `click` clicks the first match. Use it for a tab or a button. Add `"optional": true` when the element can be absent, for example a tab that exists only after data loads. An optional click with no element is skipped at once. Without `optional`, a missing element stops the whole prefill after 15 s.
4. `waitForSelector` (optional) makes the step wait, up to 15 s, for an element that proves that the data loaded.
5. A step first waits up to 15 s for its `selector` or `click` element. The layer then focuses it.
6. The layer types one character at a time with `document.execCommand('insertText')`. The browser marks its input events as real, so React and Reflex accept them. When `execCommand` fails, it falls back to the native value setter and an `input` event. It then sends `change`, waits `delayMs`, and sends the submit key.
7. Set `"keyEvents": true` only for a page that reacts to key events and not to input events. The layer then also sends `keydown`, `keypress` and `keyup` for each character. Leave it off for Reflex: its on-key-down handler runs for every character and the search then fails.
8. The prefill runs once each time the reviewer arrives at the screen: a new load in the iframe, a click on the tab, or a client-side move to its `src`. It never runs on a poll. A screen without `prefill` is never touched.
9. While it runs, the note area shows `Loading sample data…` before the note. When an element never appears, it shows `Sample data did not load (<selector> not found). Reload this tab to try again.`
9. A click step does not open a comment, also in Comment mode.
10. Keep `value` free of real data. The wrapper sends the whole `prefill` to every reviewer.
11. `startMode` and `prefill` take effect for a reviewer who has no saved choice. A reviewer who already opened the review keeps their mode.

## Reset

1. Set `reset.command` (an argv list). The wrapper then shows a "Reset demo data" button.
2. The button asks first. The command runs once for the whole proxy, at most once every 30 seconds.
3. **Reset is visible to every reviewer.** It puts the shared app data back to its start state. Comments stay. Tell all reviewers before you use it.
4. The command runs without a shell, with the proxy environment minus `REVIEW_TOKEN` and the `$VARIABLE` values of `session.login.fields`. Its output goes to `.review/reset.log`.
5. Request data never reaches the command.

## Viewport selector

The toolbar has Desktop, Tablet (834 by 1112), and Mobile (390 by 844) buttons. The iframe resizes and pins move with their elements. The layer remembers the choice.

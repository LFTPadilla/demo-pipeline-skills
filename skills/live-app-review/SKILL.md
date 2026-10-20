---
name: live-app-review
description: Reviews a running local web app with outside stakeholders. One review.json describes the apps, mocks, read-only rules, expiry, and demo reset. review.py starts the apps and a token-gated proxy and turns Tailscale Funnel on and off. Comments pin to real elements. Use for a review of a running app, a localhost share link, or comments on the real app. For a static mockup on Cloudflare, use mockup-review instead.
---

# Live App Review

Let a reviewer use your real app and pin comments on real elements.
The app runs on your machine. `review-proxy.py` sits in front of it. Tailscale Funnel exposes the proxy.
`review.py` starts and stops all of it from one `review.json`. `review.json` is the only source of settings.
`mockup-review` builds a static mockup instead. It owns the comment layer that this skill serves.

## When to use

1. A reviewer must see real screens, a real API, or a real login.
2. The reviewer is outside your team, so a hosted chat page cannot reach them.
3. You want comments pinned to real elements of the running app.

Do not use it when:

1. A static mockup is enough. Use `mockup-review`. It needs no running stack.
2. You cannot run a production build of the app, or the app calls absolute URLs on other hosts.
3. The human does not approve public exposure of the app. Funnel is public on the internet.

## How it works

```text
reviewer ──► Funnel (HTTPS) ──► review-proxy.py 127.0.0.1:8787 ──┬─ /__review/  wrapper, layer.js, comments, meta, reset
                                  expiry, token gate, cookie      ├─ mocks      answered by the proxy
                                  mocks, read-only                ├─ /api   ──► API app
                                                                  └─ /      ──► web app
review.py up ──► checks review.json, starts the apps, the proxy, Funnel, and a watchdog. Each one runs in its own session.
```

In this file, `SKILL` means the folder that holds this `SKILL.md`. The sibling skill `mockup-review` must sit next to it: the proxy serves its layer files.

## Minimal review.json

Put it in a review folder. Paths in it resolve against that folder. Unknown keys exit with code 2.

```json
{
  "project": "demo-shop",
  "title": "Demo shop review",
  "reviewNote": "Sample data only. Do not enter real personal data.",
  "funnel": { "enabled": false, "port": 443 },
  "proxy": { "port": 8787 },
  "apps": [ { "name": "web", "port": 3000, "command": ["npm", "run", "start", "--", "--port", "{port}"] } ],
  "routes": { "/": "web" },
  "ttl": "7d",
  "frame": { "screens": [ { "id": "home", "title": "Home", "src": "/" } ] }
}
```

`tests/fixtures/review.json` uses nearly every key. `references/config.md` explains each key, the env file, the token, mocks, read-only, expiry, reset, `startMode`, and `prefill`.

Two keys help a reviewer who meets an empty page. `frame.screens[].prefill` types sample data into the app, for example in a search box. `startMode: "browse"` opens the review in Browse mode.

## Commands

All commands take `-C <review folder>`. Without it, they use the current folder.

| Command | What it does |
|---|---|
| `review.py preflight [--funnel]` | Checks the config, env files, ports, and Tailscale. Each failed check prints its fix |
| `review.py up [--funnel \| --no-funnel]` | Runs preflight, starts everything, prints the status table and the share link once |
| `review.py status [--json]` | Shows each component, its state, and the time left. Exit code 1 when one is not healthy |
| `review.py logs [NAME] [-n N] [-f]` | Shows the log of `proxy`, `watchdog`, or an app |
| `review.py verify [--local]` | Opens the link like a reviewer. Loads each tab in headless Chromium and saves one PNG per tab. A public verify retries the gate check for up to 60 s, because Funnel needs time to start. It reports the wait. `REVIEW_VERIFY_WAIT` (seconds) changes the limit |
| `review.py link` | Prints the share link again |
| `review.py down` | Turns Funnel off, stops the watchdog, the proxy, and the managed apps. It is safe to run twice |
| `uv run --with playwright python SKILL/scripts/test_browser.py` | Browser tests: the layer, the proxy, and the whole stack |
| `python3 SKILL/scripts/test_proxy.py` and `test_review_cli.py` | Proxy and command line tests. They need no browser |

Exit codes: 0 success, 1 a check failed, 2 usage or invalid config, 3 security refusal, 4 an app or the proxy did not become healthy, 5 a Tailscale command failed, 6 nothing is running.

## Workflow

1. Use a production build of the app, not a dev server. Make it call its API with relative URLs. Seed it with fictional data.
2. Write `review.json`. Put credentials in an env file with mode 0600, never in `review.json`.
3. Run `review.py preflight --funnel`. Fix every FAIL.
4. Run `review.py up`. Open the local link. Check the login, the tabs, and one comment.
5. Get the human to approve the public exposure. Then run `review.py up --funnel`.
6. Run `review.py verify`. Send the link. Tell the reviewer to press `C` and click any element.
7. Collect the comments: Copy as Markdown, or Download JSON. The proxy also keeps them in `.review/comments.json`.
8. Run `review.py down`. Delete the reviewer account and the env file.

## Rules

1. Use sample data only. Put no real client names, people, emails, or credentials in the app.
2. Keep `readOnly` on, unless the review needs writes. Add only the needed pairs to `allowWrite`.
3. Set an expiry (`ttl` or `expiresAt`) for every review that uses Funnel.
4. Get the human to approve before `up --funnel`. Funnel is public on the internet. `review.py` never runs `sudo`.
5. Credentials go only in an env file with mode 0600, or in the shell environment.
6. Never write the token into the repo, a doc, or a commit. The link is a password.
7. Tell every reviewer before you press Reset. It changes the data for all of them.
8. The auto-login token goes into the reviewer's browser storage. Use a short-lived token and an account with minimal rights.
9. Run `review.py down` after the review.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Preflight says `run "sudo tailscale set --operator=$USER" once.` | Your user is not the Tailscale operator. Run that line once yourself. `review.py` never runs `sudo` |
| `up` exits 4 | An app or the proxy was not healthy. Run `review.py logs <name>` |
| `up` exits 3 | An env file has group or world permissions, or a secret is in `review.json`. Run `chmod 600 <file>` |
| `up` exits 2 and names `funnel.mode` | The old external Funnel mode was removed. Delete `mode` from the `funnel` block |
| Reviewer sees "This review has ended" or a connection error | The expiry time passed. Set a new `ttl`, run `down`, then `up` for a new link |
| Reviewer sees "Review paused" | An app is down. Run `review.py status` and `review.py logs <name>` |

## References

1. `references/config.md`: every `review.json` key, the env file, the token, mocks, read-only, expiry, app version, reset, prefill, start mode.
2. `references/proxy-and-frame.md`: how frame mode works, the proxy rules, how `up` and `down` behave.
3. `references/limits-and-troubleshooting.md`: the full troubleshooting table and the known limits.
4. `../mockup-review/references/layer.md`: the comment layer (keys, reactions, pin numbers).

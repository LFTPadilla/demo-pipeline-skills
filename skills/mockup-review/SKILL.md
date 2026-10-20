---
name: mockup-review
description: Builds a clickable single-file static UI prototype with a built-in comment layer, hosts it on Cloudflare Workers with a KV comment API, and collects stakeholder comments. Use when a hosted chat page cannot be shared outside the team and the page does not need a real backend. For a running local app, use live-app-review instead.
---

# Mockup Review

Ship one `index.html` that reviewers can click through and comment on.
A comment layer is built into the page. Cloudflare Workers hosts the page and stores the comments.
`live-app-review` reviews a RUNNING local app. It uses the same comment layer.

## When to use

1. A stakeholder in another company must see a UI prototype and give feedback.
2. A hosted chat page cannot be shared with that person.
3. You want comments pinned to page elements, not screenshots in an email.

Do not use it when:

1. The reviewer is inside your team. Share the page on your normal internal host instead.
2. The page needs a real backend, real data, or real logins. Use `live-app-review`.
3. The prototype must stay private from anyone who holds the link. See "Rules".

## How it works

```text
prototype/index.html  (holds the marker <!-- cmt:layer -->)
        │  scripts/inject.py   adds assets/layer/layer.css and layer.js. Build time only.
        ▼
   built page ──► scripts/deploy.sh ──► Cloudflare Worker ──► reviewer opens link with ?k=KEY
                                         ├─ the page (static asset)
                                         └─ /api/comments (KV: one key per project)
```

In this file, `SKILL` means the folder that holds this `SKILL.md`.

## Minimal page

Write the screens between `<!-- proto:begin -->` and `<!-- proto:end -->`. Start from `SKILL/assets/template/index.html`. Edit only the config and the screens.

```html
<script type="application/json" id="cmt-config">
{ "project": "my-prototype", "title": "My prototype", "version": "v1", "reviewNote": "Sample data only." }
</script>
<!-- proto:begin -->
<main><section data-screen="overview" data-title="Overview"><h1 id="overview-title">Overview</h1></section></main>
<!-- proto:end -->
<!-- cmt:layer -->
```

1. Use one `<section data-screen="id" data-title="Title">` per screen. Two or more screens give a tab bar.
2. Give stable elements an `id`. Comments anchor to the nearest `id`, so they survive edits.
3. Keep the page self-contained. Use inline CSS and JS. Load no fonts, images, or scripts from other hosts.
4. Keep the marker `<!-- cmt:layer -->`. Never paste the layer into the page.

## Commands

| Command | What it does |
|---|---|
| `python3 SKILL/scripts/inject.py page.html [out.html]` | Builds the layer into the page. It also upgrades a page that holds an older layer block |
| `bash SKILL/scripts/deploy.sh <dir> <worker-name> [--temporary]` | Builds `<dir>/index.html` and deploys the Worker. Prints the share link |
| `bash SKILL/scripts/pull-comments.sh "<link>"` | Prints all comments as Markdown |
| `uv run --with playwright python SKILL/scripts/test_layer.py` | Headless test of the layer, the build, the Worker, and the deploy guard. It never touches Cloudflare |

## Workflow

1. Build. Copy the template, write the screens, edit `cmt-config`.
2. Preview. Run `inject.py prototype/index.html /tmp/preview/index.html`. Run `python3 -m http.server 8000 --directory /tmp/preview`. Open `http://localhost:8000/`. The layer runs in local mode.
3. Deploy. Ask the human to run `npx wrangler login` once. Do not run it yourself. Then run `deploy.sh`. See `references/deploy.md`.
4. Share. Send the link with its `?k=` part. Tell the reviewer to press `C` to switch between Browse and Comment mode.
5. Collect. Run `pull-comments.sh`, or use Copy as Markdown in the page.
6. Iterate. Raise `version` in `cmt-config`. Old comments stay read-only. Deploy again with the same worker name and the same `REVIEW_KEY`.
7. Tear down. Pull the comments first. Then delete the Worker and the KV namespace.

## Rules

1. Use sample data only. Put no real client names, people, emails, or credentials in the page.
2. Treat the review link as a password. The `?k=` key opens the comments to read and write.
3. The page itself is public to anyone who knows the URL. Only the comments need the key. Add Cloudflare Access when the page must stay private.
4. Delete the Worker and the KV namespace after the review.
5. Never print the claim URL in chat, logs, or files you commit. Only the human opens it.
6. Never write the review key into the repo, a doc, or a commit.
7. Do not run `wrangler login`. Do not deploy before the human approves the target Cloudflare account.
8. Keep prototype data fictional even when the human gives you real screenshots.
9. Edit the layer only in `SKILL/assets/layer/`. Run `test_layer.py` after each change.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `deploy.sh` says Wrangler is not logged in | The human runs `npx wrangler login`. Then run the script again. Without a login, use `--temporary` |
| `deploy.sh` exits with code 3 and lists accounts | The login has more than one account. Ask the human which one. Set `CLOUDFLARE_ACCOUNT_ID` and run again |
| Yellow notice "This link has no review key" | The link has no `?k=`. Comments stay on this device. Send the full link |
| Panel says "Review key rejected" | The key changed. Send the current link |
| Pins vanish after an edit | The element path changed. Give stable elements an `id`. Raise `version` |
| `inject.py` says it expected exactly one marker | The page has no `<!-- cmt:layer -->`, or it has two. Keep one marker |

## References

1. `references/deploy.md`: deploy, temporary deploy and claim, share, collect, iterate, tear down.
2. `references/layer.md`: what a click can comment on, keys, reactions, `window.cmt`, pin numbers, how to change the layer.
3. `references/limits-and-troubleshooting.md`: the full troubleshooting table and the known limits.

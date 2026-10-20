# Deploy, share, collect, iterate, tear down

In this file, `SKILL` means the folder that holds `SKILL.md`.

## What the skill ships

| Path | Purpose |
|---|---|
| `assets/layer/layer.css`, `assets/layer/layer.js` | The ONE canonical comment layer. `live-app-review` serves the same two files |
| `assets/template/index.html` | Skeleton page with the screens area, `cmt-config`, and the marker `<!-- cmt:layer -->` |
| `assets/worker/worker.js` | The Worker. It serves the page and the `/api/comments` API on KV |
| `assets/worker/wrangler.jsonc.template` | Worker config. `deploy.sh` renders it |
| `scripts/inject.py` | Builds the layer into a page |
| `scripts/deploy.sh` | Builds and deploys. Prints the share link |
| `scripts/pull-comments.sh` | Prints all comments of a deployed prototype as Markdown |
| `scripts/test_layer.py` | Headless test of the layer, the build, the Worker, and the deploy guard |

## Test locally

1. Build a preview: `python3 SKILL/scripts/inject.py prototype/index.html /tmp/preview/index.html`.
2. Serve it: `python3 -m http.server 8000 --directory /tmp/preview`.
3. Open `http://localhost:8000/`. The layer runs in local mode. Comments stay in this browser.
4. Click an element. Write a comment. Reload. Confirm the pin returns.
5. Run the full test once: `uv run --with playwright python SKILL/scripts/test_layer.py`. It starts `wrangler dev` on a local KV. It does not touch Cloudflare.

## Deploy

1. Ask the human to log in once: `npx wrangler login`. Do not run this command yourself.
2. Deploy: `bash SKILL/scripts/deploy.sh prototype my-review-name`.
3. `deploy.sh` builds the layer into a copy of `index.html`. Your source page does not change.
4. When the login has more than one Cloudflare account, set `CLOUDFLARE_ACCOUNT_ID` first. Without it, the script stops with exit code 3 and prints the account list. Ask the human which account to use.
5. The script creates the KV namespace if it is missing. It sets the `REVIEW_KEY` secret. It deploys the Worker.
6. The script prints one line on stdout: `https://my-review-name.<subdomain>.workers.dev/?k=<key>`.
7. To choose the key, set `REVIEW_KEY` first. Use 8 to 128 characters from `A-Z a-z 0-9 _ -`.
8. The script uses `npm exec --yes wrangler@<version> --`. The version is pinned in `deploy.sh` (`WRANGLER_VERSION`). It needs Node and Python 3.

### Temporary deploy, claim, and upgrade

1. `--temporary` needs no login. It deploys a static page without KV. Comments stay in each reviewer's browser.
2. Each claim of a temporary deploy creates a new Cloudflare account under the human's login. After a claim, the login has more than one account. Set `CLOUDFLARE_ACCOUNT_ID` from then on.
3. To upgrade a claimed temporary site to shared mode at the same URL, set `CLOUDFLARE_ACCOUNT_ID` to the ID of the claimed account. Deploy with the same worker name. Do not use `--temporary`.
4. Claim with a browser automation tool:
   1. Read the claim URL from the `deploy.log` file named in the script output. Never print it.
   2. Open the claim URL in the human's logged-in browser.
   3. Login is a human step. Wait for the human.
   4. Click Claim only after the human approves.
   5. Do not paste the claim URL into chat, logs, or files you commit.

## Share

1. Send the link to the reviewer. Keep the `?k=` part. It opens the comment API.
2. A link without `?k=` opens the page in local mode. The status says "Saved on this device". A reviewer then thinks the sync failed.
3. The layer shows a yellow notice when the page comes from a Worker, the API answers 401, and the link has no key or a wrong key. The notice has a Dismiss button.
4. Local comments do not move to the shared store by themselves. On the keyless page, use Download JSON. Open the full link. Use Import JSON.
5. Tell the reviewer: press `C` to switch between Browse and Comment mode. Click anything to comment. Press `Esc` to close.
6. The reviewer types a name once. The browser stores it.
7. The page shows "Synced" when the shared API works. It shows "Offline, saved locally" when the network fails.

## Collect

1. Pull everything as Markdown: `bash SKILL/scripts/pull-comments.sh "<link>"`.
2. Paste the Markdown into Slack or Linear. Resolved threads show `[x]`. Replies are indented. Reactions show as `[👍 2 · ✅ 1]`.
3. A reviewer can also click Copy as Markdown, or Download JSON, and send you the file.
4. Load a reviewer file with Import JSON in the panel. Import merges by comment id.

## Iterate

1. Change the prototype. Raise `version` in `cmt-config` (for example `v2`).
2. Old comments stay. They show "from v1" in the panel, read-only. Their pins hide.
3. An old prototype that holds an embedded layer block upgrades with `python3 SKILL/scripts/inject.py prototype/index.html`. The script replaces the block. A new prototype holds only the marker.
4. Deploy again with the same worker name. Set `REVIEW_KEY` to the old key. Otherwise the script makes a new key and old links stop working.
5. Resolved threads and replies stay in KV across deploys.

## Tear down

1. Pull the comments first.
2. Delete the Worker: `npx wrangler delete my-review-name --force`.
3. Delete the KV namespace: `npx wrangler kv namespace delete --namespace-id <id>`. The deploy script prints the exact command.
4. Open the old link. Confirm it no longer loads.

# Limits and troubleshooting

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Pastel or another proxy cannot load the page | Cloudflare bot checks block proxies on `workers.dev`. Use the built-in layer. Do not use the proxy |
| BrowserSkill cannot upload the local file | The extension needs "Allow access to file URLs". Turn it on in the extension settings |
| `deploy.sh` says Wrangler is not logged in | The human runs `npx wrangler login`, then you run the script again |
| The human cannot log in now | Run `bash SKILL/scripts/deploy.sh prototype my-review-name --temporary`. It runs `wrangler deploy --temporary` |
| After `--temporary`, comments are not shared | The static version has no KV. Each reviewer keeps comments in their own browser. Ask for Download JSON |
| The temporary deploy disappears | The human must open the claim URL within 60 minutes. The URL is in the `deploy.log` file named in the output. Do not print it |
| Panel says "Review key rejected" | The key changed. Send the current link |
| Yellow notice "This link has no review key" | The link has no `?k=`. Comments stay on this device. Send the full link. To move comments: Download JSON, open the full link, Import JSON |
| `deploy.sh` exits with code 3 and lists accounts | The Wrangler login has more than one account. Ask the human which one. Set `CLOUDFLARE_ACCOUNT_ID` and run again |
| Claimed a temporary site and now deploys go to the wrong account | The claim made a new account. Set `CLOUDFLARE_ACCOUNT_ID` to that account and deploy with the same worker name |
| Pins vanish after an edit of the prototype | The element path changed. Give stable elements an `id`. Raise `version` in `cmt-config` |
| Status says "Not saved" | Browser storage is blocked, for example in a private window. Use Copy as Markdown before you leave |
| A new comment appears late | The page polls every 20 seconds and on window focus. KV can also serve reads up to 60 seconds old |
| `inject.py` says it expected exactly one marker | The page has no `<!-- cmt:layer -->`, or it has two. An older page needs one `<!-- cmt:begin -->` and one `<!-- cmt:end -->` around its layer |
| A reaction or an edit is rejected in shared mode | The server is an older Worker. Deploy again with the current `worker.js` |

## Known limits

1. One KV key per project holds all comments. The cap is 500 comments and 100 replies per comment, and 50 names per reaction.
2. Two writes in the same second can overwrite each other in KV. Plan for a few reviewers.
3. Author names are typed text. Edit and delete rules stop mistakes. They do not stop a determined user. A rename does not change old comments, so the reviewer cannot edit them any more.
4. Pins attach by CSS path. Lists that the page builds again on each render can lose their pins. Give stable elements an `id`.
5. In shared mode a new pin shows a number from the browser until the next sync. The server then sets the final number. If two reviewers comment on the same screen and version within one sync (20 seconds), or one reviewer is offline, a pin can get a different number at the sync. After that sync the number does not change.
6. The unsent text that the layer keeps for the next composer lives in memory. A reload loses it.

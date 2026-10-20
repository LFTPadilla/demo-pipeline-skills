#!/usr/bin/env bash
# Deploy a prototype and its comment API to Cloudflare Workers.
# Usage: deploy.sh <prototype-dir> <worker-name> [--temporary]
# Env:   REVIEW_KEY  optional review key (8 to 128 chars of A-Z a-z 0-9 _ -). Generated when empty.
#        CLOUDFLARE_ACCOUNT_ID  required when the Wrangler login has more than one account. The script stops with exit code 3 and lists them.
#        BUILD_DIR   optional build directory. Default: a new directory under $TMPDIR. It must be outside any git repo.
# The share link goes to stdout. Everything else goes to stderr. The key is never written into the repo.
set -euo pipefail
umask 077

die() { echo "error: $*" >&2; exit 1; }
WRANGLER_VERSION=4.148.0   # pinned: a new major version can change the deploy flags
wr() { npm exec --yes "wrangler@$WRANGLER_VERSION" -- "$@"; }
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
assets="$here/../assets"

dir="${1:-}"; name="${2:-}"; temporary=0
[[ -n "$dir" && -n "$name" ]] || die "usage: deploy.sh <prototype-dir> <worker-name> [--temporary]"
[[ "${3:-}" == "--temporary" ]] && temporary=1
[[ "$name" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] || die "worker name must be lowercase letters, digits and dashes"
[[ -f "$dir/index.html" ]] || die "$dir/index.html not found"

build="${BUILD_DIR:-$(mktemp -d "${TMPDIR:-/tmp}/mockup-review.XXXXXX")}"
mkdir -p "$build/public"
if git -C "$build" rev-parse --is-inside-work-tree >/dev/null 2>&1; then die "BUILD_DIR is inside a git repo: $build"; fi
python3 "$here/inject.py" "$dir/index.html" "$build/public/index.html" >&2   # the layer goes in at build time only
cp "$assets/worker/worker.js" "$build/worker.js"
today="$(date -u +%F)"

# Hide the claim URL: it gives account access to whoever opens it.
redact() { sed -E 's#https://dash\.cloudflare\.com/claim[^[:space:]]*#<claim-url-hidden>#g'; }
site_url() { grep -Eo 'https://[A-Za-z0-9.-]+\.workers\.dev' "$build/deploy.log" | head -1 || true; }

who="$(wr whoami 2>&1 </dev/null || true)"
if grep -qi "not authenticated" <<<"$who"; then
  logged_in=0
else
  logged_in=1
fi

if [[ "$logged_in" == 0 ]]; then
  if [[ "$temporary" == 0 ]]; then
    cat >&2 <<MSG
Wrangler is not logged in to Cloudflare.
Option 1 (shared comments): ask the human to run this command in a terminal, then run this script again:
  npx wrangler login
Option 2 (static page, comments stay in each reviewer's browser): run this script again with --temporary:
  $here/deploy.sh $dir $name --temporary
  The temporary account is deleted after 60 minutes unless the human opens the claim URL and claims it.
MSG
    [[ -z "${BUILD_DIR:-}" ]] && rm -rf "$build"
    exit 2
  fi
  printf '{ "name": "%s", "compatibility_date": "%s", "assets": { "directory": "./public" } }\n' "$name" "$today" > "$build/wrangler.jsonc"
  (cd "$build" && wr deploy --temporary) >"$build/deploy.log" 2>&1 </dev/null || { redact < "$build/deploy.log" >&2; die "deploy failed"; }
  echo "Deployed without KV. Comments stay in each reviewer's browser." >&2
  echo "The human must claim the account within 60 minutes. The claim URL is in $build/deploy.log. Do not print or share it." >&2
  url="$(site_url)"
  if [[ -n "$url" ]]; then echo "$url/"; else echo "Could not read the site URL. Read $build/deploy.log." >&2; fi
  exit 0
fi

# More than one account on this login: Wrangler would ask or pick one. Make the human choose.
if [[ -z "${CLOUDFLARE_ACCOUNT_ID:-}" ]] && [[ "$(grep -Eo '[0-9a-f]{32}' <<<"$who" | sort -u | grep -c . || true)" -gt 1 ]]; then
  {
    echo "This Wrangler login has more than one Cloudflare account:"
    grep -E '[0-9a-f]{32}' <<<"$who" | sed 's/^/  /'
    echo "Ask the human which account to use. Then set CLOUDFLARE_ACCOUNT_ID to its ID and run this script again:"
    echo "  CLOUDFLARE_ACCOUNT_ID=<id> $here/deploy.sh $dir $name"
  } >&2
  [[ -z "${BUILD_DIR:-}" ]] && rm -rf "$build"
  exit 3
fi

# Shared mode: KV namespace, secret, deploy.
ns_title="$name-comments"
kv_id="$(wr kv namespace list 2>/dev/null </dev/null | python3 -c '
import json, sys
s = sys.stdin.read()
rows = json.JSONDecoder().raw_decode(s[s.index("["):])[0]
print(next((r["id"] for r in rows if r["title"] == sys.argv[1]), ""))' "$ns_title" || true)"
if [[ -z "$kv_id" ]]; then
  echo "Creating KV namespace $ns_title" >&2
  out="$(wr kv namespace create "$ns_title" 2>&1 </dev/null)" || { echo "$out" >&2; die "kv namespace create failed"; }
  kv_id="$(grep -Eo '[0-9a-f]{32}' <<<"$out" | head -1 || true)"
fi
[[ -n "$kv_id" ]] || die "could not read the KV namespace id"

sed -e "s/__NAME__/$name/" -e "s/__KV_ID__/$kv_id/" -e "s/__DATE__/$today/" "$assets/worker/wrangler.jsonc.template" > "$build/wrangler.jsonc"

key="${REVIEW_KEY:-$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')}"
[[ "$key" =~ ^[A-Za-z0-9_-]{8,128}$ ]] || die "REVIEW_KEY must be 8 to 128 chars of A-Z a-z 0-9 _ -"
printf 'REVIEW_KEY=%s\n' "$key" > "$build/secrets.env"
trap 'rm -f "$build/secrets.env"' EXIT

(cd "$build" && wr deploy --secrets-file secrets.env) >"$build/deploy.log" 2>&1 </dev/null || { cat "$build/deploy.log" >&2; die "deploy failed"; }
echo "Deployed. Build directory: $build (outside the repo)." >&2
echo "Tear down after the review: npx wrangler delete $name --force && npx wrangler kv namespace delete --namespace-id $kv_id" >&2
url="$(site_url)"
if [[ -n "$url" ]]; then
  echo "$url/?k=$key"
else
  echo "Could not read the site URL. Read $build/deploy.log and add ?k=<key> to it." >&2
  echo "REVIEW_KEY=$key"
fi

#!/usr/bin/env bash
# Print the review comments of a deployed prototype as Markdown.
# Usage: pull-comments.sh "https://<name>.<subdomain>.workers.dev/?k=<key>"
# Optional env: CMT_VERSION=v2 marks older comments "(from vN)". Default: version of the newest comment.
# The format matches "Copy as Markdown" in the page.
set -euo pipefail

url="${1:?usage: pull-comments.sh <url-with-k>}"
origin="$(python3 -c 'import sys,urllib.parse as u; p=u.urlsplit(sys.argv[1]); print(f"{p.scheme}://{p.netloc}")' "$url")"
key="$(python3 -c 'import sys,urllib.parse as u; print(u.parse_qs(u.urlsplit(sys.argv[1]).query).get("k",[""])[0])' "$url")"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

curl -fsS "$origin/" -o "$tmp/page.html"

# The page names its project (cmt-config block) and its screens (data-screen sections).
python3 - "$tmp/page.html" > "$tmp/config.json" <<'PY'
import json, re, sys
from html.parser import HTMLParser

html = open(sys.argv[1], encoding="utf-8").read()
m = re.search(r'<script[^>]*id="cmt-config"[^>]*>(.*?)</script>', html, re.S)
if not m:
    sys.exit("cmt-config block not found in the page")
cfg = json.loads(m.group(1))
cfg["screens"] = []

class Screens(HTMLParser):
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("data-screen"):
            cfg["screens"].append({"id": a["data-screen"], "title": a.get("data-title") or a["data-screen"]})

Screens().feed(html)
print(json.dumps(cfg))
PY
project="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["project"])' "$tmp/config.json")"

# Pass the key through stdin so it does not appear in the process list.
printf 'header = "x-review-key: %s"\n' "$key" | curl -fsS -K - "$origin/api/comments?project=$project" -o "$tmp/comments.json"

python3 - "$tmp/config.json" "$tmp/comments.json" <<'PY'
import json, os, sys

cfg = json.load(open(sys.argv[1]))
comments = json.load(open(sys.argv[2]))["comments"]
title = cfg.get("title") or cfg["project"]
current = os.environ.get("CMT_VERSION") or (max(comments, key=lambda c: c["createdAt"])["version"] if comments else cfg.get("version", "v1"))
one = lambda s: " ".join(s.split())
EMOJI = ["👍", "✅", "👀", "❤️", "🎉", "❓"]
rx = lambda o: (" [" + " · ".join(f"{e} {len(o['reactions'][e])}" for e in EMOJI if o.get("reactions", {}).get(e)) + "]") if any(o.get("reactions", {}).get(e) for e in EMOJI) else ""

def order(c):
    return (c["createdAt"], c["id"])

# Pin numbers are the stored n, as in the page. Old comments without n get the next free number in creation order.
nums, top = {}, {}
for c in comments:
    k = (c["screen"], c["version"])
    top[k] = max(top.get(k, 0), c.get("n") or 0)
for c in sorted(comments, key=order):
    k = (c["screen"], c["version"])
    if not c.get("n"):
        top[k] += 1
    nums[c["id"]] = c.get("n") or top[k]

# Screen order and titles come from the page. Without them, use first-seen order and the screen id.
titles = {s["id"]: s["title"] for s in cfg["screens"]}
screens = [s["id"] for s in cfg["screens"]]
for c in sorted(comments, key=order):
    if c["screen"] not in screens:
        screens.append(c["screen"])

out = [f"# {title}: review comments", ""]
for sid in screens:
    rows = sorted((c for c in comments if c["screen"] == sid), key=order)
    if not rows:
        continue
    out.append(f"## {titles.get(sid, 'Page' if sid == 'main' else sid)}")
    for c in rows:
        old = f"(from {c['version']}) " if c["version"] != current else ""
        out.append(f"- [{'x' if c['resolved'] else ' '}] **#{nums[c['id']]}** {old}[{c['where']}] {c['author']}: {one(c['text'])}{rx(c)}")
        out += [f"  - {r['author']}: {one(r['text'])}{rx(r)}" for r in c["replies"]]
    out.append("")
print("\n".join(out).strip())
PY

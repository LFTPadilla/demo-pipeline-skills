#!/usr/bin/env bash
# Build dist/demo-pipeline-skills.zip from tracked files only.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p dist
out=dist/demo-pipeline-skills.zip
git archive --format=zip --output "$out" HEAD
bytes=$(wc -c < "$out" | tr -d ' ')
count=$(unzip -l "$out" | awk 'END { print $2 }')
echo "zip: $out"
echo "bytes: $bytes"
echo "files: $count"

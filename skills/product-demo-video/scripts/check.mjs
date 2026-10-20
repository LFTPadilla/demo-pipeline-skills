#!/usr/bin/env node
// Duration, caption sync from the corner marker, loudness, one still per chapter, and a secrets scan.
import { mkdirSync, readdirSync, readFileSync, statSync, writeFileSync } from 'node:fs';
import { execFileSync, spawnSync } from 'node:child_process';
import path from 'node:path';
import { readConfig } from './lib.mjs';

const project = path.resolve(process.argv[2] ?? '.');
const cfg = readConfig(project);
const timing = JSON.parse(readFileSync(path.join(project, 'out', 'timing.json'), 'utf8'));
const voice = JSON.parse(readFileSync(path.join(project, 'voice', 'cues.json'), 'utf8'));
const file = path.join(project, 'out', `${cfg.name ?? 'demo'}.mp4`);
const fps = timing.fps ?? 30;
const limit = Number(process.env.MAX_OFFSET_MS ?? 150);
const duration = Number(execFileSync('ffprobe', ['-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', file]).toString());
const bytes = statSync(file).size;

const raw = execFileSync('ffmpeg', ['-v', 'error', '-i', file, '-vf', `fps=${fps},crop=16:16:iw-16:ih-16,scale=1:1`, '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'], { maxBuffer: 1 << 28 });
const flips = [];
let last = null;
for (let i = 0; i < raw.length / 3; i++) {
  const [r, g] = [raw[i * 3], raw[i * 3 + 1]];
  const col = r > 160 && g < 110 ? 'm' : g > 160 && r < 110 ? 'c' : null;
  if (col && col !== last) {
    flips.push(i / fps);
    last = col;
  }
}
const rows = timing.cues.map((cue, index) => {
  const seen = flips[index];
  const off = seen == null ? Infinity : Math.round((seen - cue.at) * 1000);
  return { id: cue.id, at: cue.at, seen, off };
});
console.log(`file: ${file}`);
console.log(`duration: ${duration.toFixed(2)}s  size: ${bytes} bytes`);
console.log('| cue | audio start (s) | highlight seen (s) | offset (ms) | ok |');
for (const row of rows) {
  const ok = Math.abs(row.off) <= limit ? 'yes' : 'NO';
  console.log(`| ${row.id} | ${row.at.toFixed(2)} | ${row.seen?.toFixed(2) ?? '-'} | ${row.off} | ${ok} |`);
}
const worst = Math.max(...rows.map((row) => Math.abs(row.off)));
const syncOk = worst <= limit && flips.length === rows.length;

let loud = 'skipped (silent placeholder)';
let loudOk = true;
if (voice.provider !== 'silent') {
  const probe = spawnSync('ffmpeg', ['-hide_banner', '-nostats', '-i', file, '-af', 'ebur128=peak=true', '-f', 'null', '-'], { encoding: 'utf8' });
  const tail = probe.stderr.slice(probe.stderr.lastIndexOf('Summary:'));
  const integrated = Number(/I:\s+(-?[\d.]+) LUFS/.exec(tail)?.[1]);
  loud = `${integrated} LUFS`;
  loudOk = integrated <= -14 && integrated >= -18;
  console.log(`loudness: ${loud} (target -16)`);
} else {
  console.log(`loudness: ${loud}`);
}

const stillDir = path.join(project, 'out', 'stills');
mkdirSync(stillDir, { recursive: true });
for (const chapter of timing.chapters) {
  const still = path.join(stillDir, `${chapter.id}.jpg`);
  execFileSync('ffmpeg', ['-v', 'error', '-y', '-ss', String(chapter.at + 0.2), '-i', file, '-frames:v', '1', still]);
  console.log(`still: ${still}`);
}

const secret = scanSecrets(project);
console.log(`secrets: ${secret.length ? secret.join(', ') : 'none'}`);
const ok = syncOk && loudOk && secret.length === 0;
console.log(`max |offset|: ${worst} ms (limit ${limit}). ${ok ? 'PASS' : 'FAIL'}`);
if (!ok) process.exitCode = 1;

function scanSecrets(dir) {
  const hits = [];
  const skip = new Set(['node_modules', 'out', 'voice', '.git']);
  const pattern = new RegExp(['sk_', '[a-z0-9]{10}'].join(''), 'i');
  const assigned = /(?:ELEVENLABS_API_KEY|DEEPGRAM_API_KEY)\s*=\s*['"]?\S{8,}/;
  const walk = (folder) => {
    for (const name of readdirSync(folder)) {
      if (skip.has(name)) continue;
      const full = path.join(folder, name);
      const stat = statSync(full);
      if (stat.isDirectory()) walk(full);
      else if (stat.size < 2_000_000 && /\.(mjs|js|json|md|html|css|py|sh|txt)$/.test(name)) {
        const text = readFileSync(full, 'utf8');
        if (pattern.test(text) || assigned.test(text)) hits.push(path.relative(dir, full));
      }
    }
  };
  walk(dir);
  writeFileSync(path.join(dir, 'out', 'check.json'), JSON.stringify({ duration, bytes, worst, loud, hits }, null, 2));
  return hits;
}

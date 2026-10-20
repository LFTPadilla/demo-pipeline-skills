#!/usr/bin/env node
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseNarration } from './lib.mjs';

const voice = path.join(path.dirname(fileURLToPath(import.meta.url)), 'voice.mjs');
const rows = parseNarration('| id | start (s) | on screen | line |\n| A.1 |  | Inbox | Hello there friend. |\n');
if (rows.length !== 1 || rows[0].line !== 'Hello there friend.') throw new Error('narration parse failed');

function run(provider, extra = {}) {
  const dir = mkdtempSync(path.join(tmpdir(), 'pdv-'));
  writeFileSync(path.join(dir, 'narration.md'), '| id | start (s) | on screen | line |\n|---|---|---|---|\n| A.1 |  | Inbox | Hello there friend. |\n');
  writeFileSync(path.join(dir, 'demo.config.json'), JSON.stringify({ name: 't', stt: false, voice: { provider, id: 'sample-voice', lang: 'en' } }));
  const result = spawnSync(process.execPath, [voice, dir], {
    encoding: 'utf8',
    env: { PATH: process.env.PATH, ...extra },
  });
  return { dir, result };
}

const eleven = run('elevenlabs');
const elevenErr = eleven.result.stderr;
if (eleven.result.status === 0 || !elevenErr.includes('ELEVENLABS_API_KEY') || !elevenErr.includes('DEEPGRAM_API_KEY') || !elevenErr.includes('voice-studio')) {
  throw new Error(`elevenlabs missing-key error was:\n${elevenErr}`);
}
rmSync(eleven.dir, { recursive: true, force: true });

const deep = run('deepgram');
if (deep.result.status === 0 || !deep.result.stderr.includes('DEEPGRAM_API_KEY') || !deep.result.stderr.includes('voice-studio')) {
  throw new Error(`deepgram missing-key error was:\n${deep.result.stderr}`);
}
rmSync(deep.dir, { recursive: true, force: true });

const silent = run('silent', { DEEPGRAM_API_KEY: 'present' });
if (silent.result.status !== 0) throw new Error(silent.result.stderr || silent.result.stdout);
const cues = JSON.parse(readFileSync(path.join(silent.dir, 'voice', 'cues.json'), 'utf8'));
if (cues.provider !== 'silent' || !(cues.cues['A.1'].dur > 1) || cues.cues['A.1'].stt !== 'skipped') throw new Error('silent clip missing or speech-to-text ran');
rmSync(silent.dir, { recursive: true, force: true });
console.log('self-check: ok');

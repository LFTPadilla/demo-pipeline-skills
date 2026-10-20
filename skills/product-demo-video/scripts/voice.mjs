#!/usr/bin/env node
// One narration line, one audio file. Provider comes from demo.config.json or VOICE_PROVIDER.
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { execFileSync, spawnSync } from 'node:child_process';
import path from 'node:path';
import { loadNarration, probeDuration, readConfig, wordsOf } from './lib.mjs';

const project = path.resolve(process.argv[2] ?? '.');
const cfg = readConfig(project);
const provider = process.env.VOICE_PROVIDER ?? cfg.voice?.provider ?? 'silent';
const voiceId = process.env.VOICE_ID ?? cfg.voice?.id ?? '';
const lang = cfg.voice?.lang ?? 'en';
const sttOn = cfg.stt === true;

function die(message) {
  console.error(message);
  process.exit(1);
}

if (provider === 'elevenlabs' && !process.env.ELEVENLABS_API_KEY) {
  die([
    'ELEVENLABS_API_KEY is not set.',
    'ElevenLabs is the highest quality option and the most expensive one.',
    'A cheaper option is Deepgram. Set DEEPGRAM_API_KEY and provider "deepgram".',
    'A free option is voice-studio. Set provider "voice-studio". It runs on this machine and needs no key.',
  ].join('\n'));
}
if (provider === 'deepgram' && !process.env.DEEPGRAM_API_KEY) {
  die([
    'DEEPGRAM_API_KEY is not set.',
    'Deepgram Aura-2 is medium quality and cheaper than ElevenLabs.',
    'A free option is voice-studio. Set provider "voice-studio". It runs on this machine and needs no key.',
  ].join('\n'));
}
if (!['elevenlabs', 'deepgram', 'voice-studio', 'silent'].includes(provider)) {
  die(`Unknown voice provider "${provider}". Use elevenlabs, deepgram, voice-studio, or silent.`);
}
if ((provider === 'elevenlabs' || provider === 'deepgram') && !voiceId) {
  die('Set voice.id in demo.config.json, or set VOICE_ID. The script does not pick a voice for you.');
}

const lines = loadNarration(project);
const audioDir = path.join(project, 'voice', 'audio');
mkdirSync(audioDir, { recursive: true });

function silentSeconds(text) {
  const words = text.trim().split(/\s+/).filter(Boolean).length;
  return Math.round(Math.max(1.6, words * 0.42) * 100) / 100;
}

function writeSilent(file, seconds) {
  execFileSync('ffmpeg', ['-v', 'error', '-y', '-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo', '-t', String(seconds), '-c:a', 'pcm_s16le', file]);
}

function scrub(text, key) {
  return key ? text.replaceAll(key, '[key]') : text;
}

async function elevenLabs(text, file) {
  const key = process.env.ELEVENLABS_API_KEY;
  const res = await fetch('https://api.elevenlabs.io/v1/text-to-dialogue?output_format=mp3_44100_128', {
    method: 'POST',
    headers: { 'xi-api-key': key, 'Content-Type': 'application/json' },
    body: JSON.stringify({
      model_id: process.env.EL_MODEL ?? 'eleven_v4',
      inputs: [{ text, voice_id: voiceId }],
      language_code: lang,
      seed: Number(process.env.EL_SEED ?? 7),
    }),
  });
  if (!res.ok) die(`ElevenLabs HTTP ${res.status}: ${scrub(await res.text(), key).slice(0, 300)}`);
  const mp3 = file.replace(/\.wav$/, '.mp3');
  writeFileSync(mp3, Buffer.from(await res.arrayBuffer()));
  execFileSync('ffmpeg', ['-v', 'error', '-y', '-i', mp3, '-ar', '48000', '-ac', '2', '-c:a', 'pcm_s16le', file]);
}

async function deepgram(text, file) {
  const key = process.env.DEEPGRAM_API_KEY;
  const model = voiceId;
  const url = `https://api.deepgram.com/v1/speak?model=${encodeURIComponent(model)}&encoding=linear16&container=wav&sample_rate=48000`;
  const res = await fetch(url, {
    method: 'POST',
    headers: { Authorization: `Token ${key}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({ text }),
  });
  if (!res.ok) die(`Deepgram HTTP ${res.status}: ${scrub(await res.text(), key).slice(0, 300)}`);
  writeFileSync(file, Buffer.from(await res.arrayBuffer()));
}

function voiceStudio(text, file) {
  const voice = voiceId || 'narrator';
  const result = spawnSync('vsay', ['--no-play', '-l', lang, '-v', voice, '-N', '16', '-s', '1.0', '-o', file, text], { encoding: 'utf8' });
  if (result.status !== 0) {
    die([
      'voice-studio did not write the clip.',
      result.stderr?.trim() || 'vsay failed.',
      'voice-studio is the free option. It needs the VoiceStudio server on 127.0.0.1:3900, plus CPU or GPU and a downloaded model.',
      'Start VoiceStudio and run again, or set provider to "silent" for a placeholder with no key.',
    ].join('\n'));
  }
}

function level(file) {
  if (provider === 'silent') return;
  const tmp = `${file}.norm.wav`;
  execFileSync('ffmpeg', ['-v', 'error', '-y', '-i', file, '-af', 'loudnorm=I=-16:TP=-1.5:LRA=11', '-ar', '48000', '-ac', '2', '-c:a', 'pcm_s16le', tmp]);
  execFileSync('mv', [tmp, file]);
}

async function maybeStt(file, text) {
  if (!sttOn) return 'skipped';
  const key = process.env.DEEPGRAM_API_KEY;
  if (!key) die('DEEPGRAM_API_KEY is not set. stt is true. Set the key, or set stt to false.');
  const res = await fetch('https://api.deepgram.com/v1/listen?model=nova-3&punctuate=false', {
    method: 'POST',
    headers: { Authorization: `Token ${key}`, 'Content-Type': 'audio/wav' },
    body: readFileSync(file),
  });
  if (!res.ok) die(`Speech-to-text HTTP ${res.status}: ${scrub(await res.text(), key).slice(0, 200)}`);
  const heard = (await res.json()).results?.channels?.[0]?.alternatives?.[0]?.transcript ?? '';
  return wordsOf(heard) === wordsOf(text) ? 'ok' : `DIFF: ${heard}`;
}

const cues = {};
let failed = false;
for (const row of lines) {
  const file = path.join(audioDir, `${row.id}.wav`);
  if (provider === 'silent') writeSilent(file, silentSeconds(row.line));
  else if (provider === 'elevenlabs') await elevenLabs(row.line, file);
  else if (provider === 'deepgram') await deepgram(row.line, file);
  else voiceStudio(row.line, file);
  level(file);
  const stt = await maybeStt(file, row.line);
  if (stt.startsWith('DIFF')) failed = true;
  cues[row.id] = { file: path.relative(project, file), dur: probeDuration(file), text: row.line, stt };
  console.log(`${row.id}  ${cues[row.id].dur.toFixed(2)}s  stt:${stt}  ${provider}`);
}
writeFileSync(path.join(project, 'voice', 'cues.json'), JSON.stringify({ provider, voiceId, cues }, null, 2));
if (!sttOn) console.log('speech-to-text off. Set "stt": true in demo.config.json to check the words.');
if (failed) process.exit(2);

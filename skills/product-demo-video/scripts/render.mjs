#!/usr/bin/env node
// Portable render. Burns the synced captions and chapter chips. HyperFrames can render index.html later.
import { existsSync, readFileSync } from 'node:fs';
import { execFileSync, spawnSync } from 'node:child_process';
import path from 'node:path';
import { readConfig } from './lib.mjs';

const project = path.resolve(process.argv[2] ?? '.');
const cfg = readConfig(project);
const timing = JSON.parse(readFileSync(path.join(project, 'out', 'timing.json'), 'utf8'));
const timeline = JSON.parse(readFileSync(path.join(project, 'out', 'timeline.json'), 'utf8'));
const { start } = JSON.parse(readFileSync(path.join(project, 'out', 'mux.json'), 'utf8'));
const fps = timing.fps ?? 30;
const uiPath = path.join(project, 'out', 'ui.mp4');
const outPath = path.join(project, 'out', `${cfg.name ?? 'demo'}.mp4`);
const matched = spawnSync('fc-match', ['-f', '%{file}', 'sans'], { encoding: 'utf8' }).stdout?.trim();
const font = matched && existsSync(matched) ? matched : '';
if (!font) throw new Error('No sans font found. Install fontconfig and a sans font, then run render again.');

const abs = (file) => (path.isAbsolute(file) ? file : path.join(project, file));
execFileSync('ffmpeg', [
  '-v', 'error', '-y', '-i', abs(timeline.video), '-ss', String(start), '-an', '-r', String(fps),
  '-c:v', 'libx264', '-crf', '16', '-preset', 'veryfast', '-pix_fmt', 'yuv420p', uiPath,
]);

const esc = (text) => text.replace(/\\/g, '\\\\').replace(/:/g, '\\:').replace(/'/g, "\\'").replace(/%/g, '\\%').replace(/,/g, '\\,');
const draw = (text, a, b, size, y) => {
  const enable = `between(t\\,${a}\\,${b})`;
  return `drawtext=fontfile=${font}:text='${esc(text)}':fontsize=${size}:fontcolor=white:box=1:boxcolor=0x0f172a@0.85:boxborderw=12:x=(w-text_w)/2:y=${y}:enable='${enable}'`;
};
const filters = [
  ...timing.chapters.map((chapter) => draw(chapter.label, chapter.at, chapter.end, 22, 24)),
  ...timing.cues.map((cue) => draw(cue.text, cue.at, cue.at + cue.dur, 28, 'h-78')),
];
const args = ['-v', 'error', '-y', '-i', uiPath];
timing.cues.forEach((cue) => args.push('-i', abs(cue.file)));
const delays = timing.cues.map((cue, index) => {
  const ms = Math.max(0, Math.round(cue.at * 1000));
  return `[${index + 1}:a]adelay=${ms}|${ms},apad[a${index}]`;
});
const mix = timing.cues.map((_, index) => `[a${index}]`).join('') + `amix=inputs=${timing.cues.length}:normalize=0:duration=longest[aout]`;
args.push(
  '-filter_complex', `${delays.join(';')};${mix}`,
  '-vf', filters.join(','),
  '-map', '0:v', '-map', '[aout]',
  '-r', String(fps), '-c:v', 'libx264', '-crf', '16', '-preset', 'veryfast', '-pix_fmt', 'yuv420p',
  '-c:a', 'aac', '-ar', '48000', '-t', String(timing.total), outPath,
);
execFileSync('ffmpeg', args);
console.log(`render: ${outPath}`);

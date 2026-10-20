#!/usr/bin/env node
// Default render is HyperFrames 0.8.143. ffmpeg is the fallback when that command fails.
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
const abs = (file) => (path.isAbsolute(file) ? file : path.join(project, file));

execFileSync('ffmpeg', [
  '-v', 'error', '-y', '-i', abs(timeline.video), '-ss', String(start), '-an', '-r', String(fps),
  '-c:v', 'libx264', '-g', String(fps), '-keyint_min', String(fps), '-sc_threshold', '0',
  '-movflags', '+faststart', '-crf', '16', '-preset', 'veryfast', '-pix_fmt', 'yuv420p', uiPath,
]);

const hyper = spawnSync('npx', ['--yes', 'hyperframes@0.8.143', 'render', '-o', path.relative(project, outPath)], {
  cwd: project,
  stdio: 'inherit',
});
if (hyper.status === 0) {
  console.log(`render: hyperframes ${outPath}`);
  process.exit(0);
}
console.error('HyperFrames render failed. Falling back to ffmpeg.');
ffmpegRender();
console.log(`render: ffmpeg ${outPath}`);

function ffmpegRender() {
  const matched = spawnSync('fc-match', ['-f', '%{file}', 'sans'], { encoding: 'utf8' }).stdout?.trim();
  const font = matched && existsSync(matched) ? matched : '';
  if (!font) throw new Error('No sans font found. Install fontconfig and a sans font, then run render again.');
  const esc = (text) => text.replace(/\\/g, '\\\\').replace(/:/g, '\\:').replace(/'/g, "\\'").replace(/%/g, '\\%').replace(/,/g, '\\,');
  const draw = (text, a, b, size, x, y) => {
    const enable = `between(t\\,${a}\\,${b})`;
    return `drawtext=fontfile=${font}:text='${esc(text)}':fontsize=${size}:fontcolor=white:box=1:boxcolor=0x0f172a@0.85:boxborderw=8:x=${x}:y=${y}:enable='${enable}'`;
  };
  const filters = [
    ...timing.chapters.map((chapter) => draw(chapter.label, chapter.at, chapter.end, 18, '(w-text_w)/2', 16)),
    ...timing.cues.map((cue) => draw(cue.text, cue.at, cue.at + cue.dur, 22, 40, 'h-64')),
    ...mapFilters(timing.map, draw),
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
}

function mapFilters(map, draw) {
  if (!map?.scenes?.length) return [];
  const filters = [];
  for (const scene of map.scenes) {
    if (scene.hide) continue;
    const label = map.steps.map((step, index) => `${index + 1} ${step.id === scene.step ? step.label.toUpperCase() : step.label}`).join('  ');
    filters.push(draw(`${map.title}: ${label}`, scene.at, scene.end, 16, scene.x, scene.y));
  }
  return filters;
}

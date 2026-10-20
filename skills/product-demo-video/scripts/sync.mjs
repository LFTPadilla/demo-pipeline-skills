#!/usr/bin/env node
// Place each spoken line and each chapter chip on the voice clock. Writes the composition and the start column.
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import path from 'node:path';
import { loadNarration, readConfig } from './lib.mjs';

const project = path.resolve(process.argv[2] ?? '.');
const cfg = readConfig(project);
const timeline = JSON.parse(readFileSync(path.join(project, 'out', 'timeline.json'), 'utf8'));
const { start } = JSON.parse(readFileSync(path.join(project, 'out', 'mux.json'), 'utf8'));
const narration = loadNarration(project);
const byId = Object.fromEntries(narration.map((row) => [row.id, row]));
const width = cfg.viewport?.width ?? 1280;
const height = cfg.viewport?.height ?? 720;
const accent = cfg.accent ?? '#1d4ed8';
const round = (n) => Math.round(n * 1000) / 1000;

const cues = timeline.cues.map((cue) => ({
  id: cue.id,
  at: round(cue.at - start),
  dur: round(cue.dur),
  text: byId[cue.id]?.line ?? '',
  screen: byId[cue.id]?.screen ?? '',
  file: path.relative(project, path.resolve(project, cue.file)),
  box: cue.box,
}));
const chapters = timeline.groups.map((group, index) => {
  const next = timeline.groups[index + 1];
  const end = next ? next.at - start : cues.at(-1).at + cues.at(-1).dur + 0.4;
  return {
    id: group.id,
    title: group.title,
    label: `${String(index + 1).padStart(2, '0')}  ${group.title}`,
    at: round(Math.max(0, group.at - start)),
    end: round(end),
  };
});
const total = round(cues.at(-1).at + cues.at(-1).dur + 0.5);
const timing = { total, fps: cfg.fps ?? 30, width, height, accent, cues, chapters };
mkdirSync(path.join(project, 'out'), { recursive: true });
writeFileSync(path.join(project, 'out', 'timing.json'), JSON.stringify(timing, null, 2));

const captionDivs = cues.map((cue) => `      <div id="cap-${cue.id.replace('.', '')}" class="caption">${escapeHtml(cue.text)}</div>`).join('\n');
const chipDivs = chapters.map((chapter) => `      <div id="chip-${chapter.id}" class="chip">${escapeHtml(chapter.label)}</div>`).join('\n');
const audioTags = cues.map((cue, index) => `      <audio id="vo-${cue.id.replace('.', '')}" src="${cue.file}" data-start="${cue.at}" data-duration="${cue.dur}" data-track-index="${index + 1}" data-volume="1"></audio>`).join('\n');
const tween = [
  ...chapters.flatMap((chapter) => [
    `      tl.set("#chip-${chapter.id}", { opacity: 1 }, ${chapter.at});`,
    `      tl.set("#chip-${chapter.id}", { opacity: 0 }, ${chapter.end});`,
  ]),
  ...cues.flatMap((cue) => [
    `      tl.set("#cap-${cue.id.replace('.', '')}", { opacity: 1 }, ${cue.at});`,
    `      tl.set("#cap-${cue.id.replace('.', '')}", { opacity: 0 }, ${round(cue.at + cue.dur)});`,
  ]),
].join('\n');

const html = `<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=${width}, height=${height}" />
    <title>${escapeHtml(cfg.title ?? cfg.name ?? 'Demo')}</title>
    <script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>
    <style>
      body { margin: 0; background: #0f172a; color: #f8fafc; font-family: system-ui, sans-serif; }
      #root { position: relative; width: ${width}px; height: ${height}px; overflow: hidden; background: #0f172a; }
      video { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }
      .chip, .caption { position: absolute; opacity: 0; }
      .chip { top: 20px; left: 50%; transform: translateX(-50%); padding: 8px 14px; border-radius: 8px; background: #0f172acc; color: #f8fafc; font-size: 18px; font-weight: 700; letter-spacing: 0.08em; }
      .caption { left: 48px; right: 160px; bottom: 36px; padding: 12px 16px; border-radius: 10px; background: #0f172af2; color: #f8fafc; font-size: 28px; font-weight: 650; }
      .chip i, .caption i { color: ${accent}; }
    </style>
  </head>
  <body>
    <div id="root" data-composition-id="main" data-start="0" data-width="${width}" data-height="${height}" data-duration="${total}">
      <video id="ui" class="clip" src="out/ui.mp4" muted playsinline data-start="0" data-duration="${total}" data-track-index="0"></video>
${chipDivs}
${captionDivs}
${audioTags}
    </div>
    <script>
      const tl = gsap.timeline({ paused: true });
${tween}
      window.__timelines["main"] = tl;
    </script>
  </body>
</html>
`;
writeFileSync(path.join(project, 'index.html'), html);

const narrationPath = path.join(project, 'narration.md');
const starts = Object.fromEntries(cues.map((cue) => [cue.id, cue.at.toFixed(2)]));
writeFileSync(narrationPath, readFileSync(narrationPath, 'utf8').replace(/^\| ([A-Z]\.\d+) \| [^|]* \|/gm, (match, id) => (starts[id] == null ? match : `| ${id} | ${starts[id]} |`)));
console.log(`sync: ${total}s, ${cues.length} captions, ${chapters.length} chapter chips`);

function escapeHtml(value) {
  return String(value).replace(/[&<>]/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[char]));
}

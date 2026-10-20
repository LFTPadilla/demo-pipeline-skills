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
const zoom = height / (height - 40);
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
const map = mapPlan(cfg.map, cues, width, height);
const timing = { total, fps: cfg.fps ?? 30, width, height, accent, cues, chapters, map };
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
  ...mapTweens(map),
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
      video { position: absolute; left: 0; top: 0; width: ${Math.ceil(width * zoom)}px; height: ${Math.ceil(height * zoom)}px; }
      .chip, .caption, #map-dock { position: absolute; }
      .chip, .caption { opacity: 0; }
      .chip { top: 16px; left: 50%; transform: translateX(-50%); padding: 8px 14px; border-radius: 8px; background: #0f172acc; color: #f8fafc; font-size: 16px; font-weight: 700; letter-spacing: 0.08em; }
      .caption { left: 40px; bottom: 28px; width: max-content; max-width: 680px; padding: 10px 14px; border-radius: 10px; background: #0f172af2; color: #f8fafc; font-size: 22px; font-weight: 650; }
      #map-dock { left: 0; top: 0; width: ${map.panelW}px; opacity: 0; z-index: 4; }
      #map-panel { background: rgba(15, 23, 42, 0.94); border-radius: 12px; padding: 10px 12px 8px; }
      .map-title { font-size: 12px; letter-spacing: 0.08em; font-weight: 700; color: #94a3b8; margin-bottom: 6px; }
      .step { display: flex; align-items: center; gap: 8px; height: 26px; font-size: 16px; font-weight: 750; color: #94a3b8; white-space: nowrap; }
      .step i { font-style: normal; width: 16px; color: #94a3b8; }
    </style>
  </head>
  <body>
    <div id="root" data-composition-id="main" data-start="0" data-width="${width}" data-height="${height}" data-duration="${total}">
      <video id="ui" class="clip" src="out/ui.mp4" muted playsinline data-start="0" data-duration="${total}" data-track-index="0"></video>
${chipDivs}
${captionDivs}
${mapHtml(map)}
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
console.log(`sync: ${total}s, ${cues.length} captions, ${chapters.length} chapter chips, ${map.scenes.length} map scenes`);

function mapPlan(spec, cueList, frameW, frameH) {
  const steps = spec?.steps ?? [];
  const panelW = 240;
  const panelH = 28 + steps.length * 26;
  const place = {
    tr: { x: frameW - panelW - 16, y: 16 },
    br: { x: frameW - panelW - 16, y: frameH - panelH - 48 },
    tl: { x: 16, y: 16 },
    bl: { x: 16, y: frameH - panelH - 48 },
  };
  const scenes = [];
  for (const cue of cueList) {
    const row = spec?.scenes?.[cue.id];
    if (!row) continue;
    const spot = place[row.dock] ?? place.tr;
    scenes.push({
      id: cue.id,
      at: cue.at,
      end: round(cue.at + cue.dur),
      step: row.step,
      dock: row.dock ?? 'tr',
      hide: row.hide === true,
      x: spot.x,
      y: spot.y,
    });
  }
  return { title: spec?.title ?? '', panelW, panelH, steps, scenes };
}

function mapHtml(plan) {
  if (!plan.steps.length) return '';
  const steps = plan.steps.map((step, index) => `      <div class="step" id="step-${step.id}"><i>${index + 1}</i><span>${escapeHtml(step.label)}</span></div>`).join('\n');
  return `    <div id="map-dock">
      <div id="map-panel">
        <div class="map-title">${escapeHtml(plan.title)}</div>
${steps}
      </div>
    </div>`;
}

function mapTweens(plan) {
  if (!plan.scenes.length) return [];
  const lines = ['      tl.set("#map-dock", { opacity: 0 }, 0);'];
  for (const scene of plan.scenes) {
    lines.push(`      tl.set("#map-dock", { x: ${scene.x}, y: ${scene.y}, opacity: ${scene.hide ? 0 : 1} }, ${scene.at});`);
    for (const step of plan.steps) {
      const on = step.id === scene.step && !scene.hide;
      lines.push(`      tl.set("#step-${step.id} i, #step-${step.id} span", { color: "${on ? '#f8fafc' : '#94a3b8'}" }, ${scene.at});`);
    }
    lines.push(`      tl.set("#map-dock", { opacity: 0 }, ${scene.end});`);
  }
  return lines;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>]/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[char]));
}

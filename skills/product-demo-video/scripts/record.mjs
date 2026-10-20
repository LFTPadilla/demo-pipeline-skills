#!/usr/bin/env node
// Record the app. One cue is one highlight. Mocks answer API calls before the page loads.
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { execFileSync, spawn } from 'node:child_process';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { readConfig } from './lib.mjs';

const project = path.resolve(process.argv[2] ?? '.');
const cfg = readConfig(project);
const cuesFile = path.join(project, 'voice', 'cues.json');
if (!existsSync(cuesFile)) throw new Error('voice/cues.json is missing. Run voice.mjs first.');
const plan = JSON.parse(readFileSync(cuesFile, 'utf8')).cues;
const scenario = (await import(pathToFileURL(path.join(project, 'scenario.mjs')).href)).default;
const outDir = path.join(project, 'out');
mkdirSync(path.join(outDir, 'raw'), { recursive: true });

function playwrightApi(mod) {
  if (mod?.chromium) return mod;
  if (mod?.default?.chromium) return mod.default;
  return null;
}

async function loadPlaywright() {
  const files = [
    process.env.PLAYWRIGHT,
    path.join(project, 'node_modules', 'playwright', 'index.mjs'),
    path.join(project, 'node_modules', 'playwright', 'index.js'),
  ].filter(Boolean);
  for (const file of files) {
    if (!existsSync(file)) continue;
    const api = playwrightApi(await import(pathToFileURL(file).href));
    if (api) return api;
  }
  try {
    const api = playwrightApi(await import('playwright'));
    if (api) return api;
  } catch { /* missing package */ }
  throw new Error('Playwright is not installed. Run npm install playwright in the project, or set PLAYWRIGHT to the module file.');
}

async function startServer() {
  if (!cfg.serve) return null;
  const child = spawn(process.execPath, [path.join(project, cfg.serve)], {
    env: { ...process.env, PORT: String(cfg.port ?? 47331) },
    stdio: 'ignore',
  });
  const base = cfg.base;
  for (let i = 0; i < 50; i++) {
    try {
      const res = await fetch(base);
      if (res.ok || res.status < 500) return child;
    } catch { /* not up yet */ }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  child.kill();
  throw new Error(`The demo server did not answer at ${base}.`);
}

const server = await startServer();
try {
  const { chromium } = await loadPlaywright();
  const viewport = cfg.viewport ?? { width: 1280, height: 720 };
  const accent = cfg.accent ?? '#1d4ed8';
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport, recordVideo: { dir: path.join(outDir, 'raw'), size: viewport } });
  await context.addInitScript((color) => {
    const demo = (window.__demo = {});
    const el = (id, css) => {
      let node = document.getElementById(id);
      if (!node) {
        node = document.createElement('div');
        node.id = id;
        document.documentElement.appendChild(node);
      }
      node.style.cssText = 'position:fixed;z-index:2147483647;pointer-events:none;' + css;
      return node;
    };
    demo.slide = (title, text) => {
      const node = el('__demo-slide', 'inset:0;display:flex;flex-direction:column;justify-content:center;padding:0 12%;background:#0f172a;color:#f8fafc;font:20px/1.45 system-ui,sans-serif');
      node.replaceChildren();
      const heading = document.createElement('div');
      heading.textContent = title;
      heading.style.cssText = `font-size:42px;font-weight:700;color:${color};margin-bottom:12px`;
      const body = document.createElement('div');
      body.textContent = text ?? '';
      node.append(heading, body);
    };
    demo.unslide = () => document.getElementById('__demo-slide')?.remove();
    demo.marker = (fill) => { el('__demo-marker', `right:0;bottom:0;width:32px;height:32px;background:${fill}`); };
    demo.ring = (target) => {
      cancelAnimationFrame(demo.raf);
      document.getElementById('__demo-ring')?.remove();
      if (!target) return;
      const ring = el('__demo-ring', `border:3px solid ${color};border-radius:8px;box-shadow:0 0 0 6px ${color}33`);
      const follow = () => {
        const box = target.getBoundingClientRect();
        Object.assign(ring.style, { left: box.left - 6 + 'px', top: box.top - 6 + 'px', width: box.width + 12 + 'px', height: box.height + 12 + 'px' });
        demo.raf = requestAnimationFrame(follow);
      };
      follow();
    };
  }, accent);
  const page = await context.newPage();
  if (scenario.mocks) {
    for (const mock of scenario.mocks) {
      await page.route(mock.url, (route) => route.fulfill({
        status: mock.status ?? 200,
        contentType: 'application/json',
        body: JSON.stringify(mock.json),
      }));
    }
  }
  const t0 = Date.now();
  const now = () => (Date.now() - t0) / 1000;
  const timeline = { groups: [], cues: [], video: '' };
  let flip = 0;
  const slideMs = cfg.slideMs ?? 900;
  const gapMs = cfg.gapMs ?? 350;
  for (const group of scenario.groups) {
    await group.setup?.(page, cfg.base);
    const title = group.slide?.title ?? group.title ?? group.id;
    await page.evaluate(([heading, text]) => { __demo.slide(heading, text); }, [title, group.slide?.text ?? '']);
    timeline.groups.push({ id: group.id, title, at: now() });
    await page.waitForTimeout(slideMs);
    await page.evaluate(() => __demo.unslide());
    await page.waitForTimeout(200);
    for (const [index, cue] of group.cues.entries()) {
      const id = cue.id ?? `${group.id}.${index + 1}`;
      const spoken = plan[id];
      if (!spoken) throw new Error(`narration has no line for ${id}`);
      const locator = typeof cue.target === 'string' ? page.locator(cue.target) : await cue.target?.(page);
      const target = locator?.first();
      let box = null;
      if (target) {
        await target.waitFor({ state: 'visible', timeout: 10000 });
        await target.scrollIntoViewIfNeeded();
        await page.waitForTimeout(120);
        if (cue.action === 'hover' || cue.action === 'click') await target.hover();
        box = await target.boundingBox();
      }
      const color = flip++ % 2 ? '#ff00ff' : '#00ffff';
      if (target) await target.evaluate((node, fill) => { __demo.ring(node); __demo.marker(fill); }, color);
      else await page.evaluate((fill) => { __demo.ring(null); __demo.marker(fill); }, color);
      const at = now();
      timeline.cues.push({ id, at, dur: spoken.dur, file: spoken.file, ring: Boolean(target), box });
      await page.waitForTimeout(Math.max(0, (at + spoken.dur) * 1000 + gapMs - (Date.now() - t0)));
      if (cue.action === 'click' && target) {
        await page.evaluate(() => __demo.ring(null));
        await target.click();
      }
      await cue.after?.(page, cfg.base);
    }
    await page.evaluate(() => __demo.ring(null));
  }
  const video = page.video();
  await context.close();
  await browser.close();
  const videoPath = await video.path();
  timeline.video = path.relative(project, videoPath);
  alignToVideo(timeline, videoPath, cfg.fps ?? 30);
  const start = Math.max(0, timeline.groups[0].at - 0.25);
  writeFileSync(path.join(outDir, 'timeline.json'), JSON.stringify(timeline, null, 2));
  writeFileSync(path.join(outDir, 'mux.json'), JSON.stringify({ start }, null, 2));
  console.log(`record: ${timeline.groups.length} groups, ${timeline.cues.length} cues`);
} finally {
  server?.kill();
}

function alignToVideo(timeline, file, fps) {
  const raw = execFileSync('ffmpeg', [
    '-v', 'error', '-i', file, '-vf', `fps=${fps},crop=16:16:iw-16:ih-16,scale=1:1`,
    '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-',
  ], { maxBuffer: 1 << 28 });
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
  if (flips.length < timeline.cues.length) {
    throw new Error(`saw ${flips.length} marker changes, expected ${timeline.cues.length}`);
  }
  const lead = timeline.cues[0].at - flips[0];
  const shift = (t) => Math.max(0, Math.round((t - lead) * 1000) / 1000);
  for (const group of timeline.groups) group.at = shift(group.at);
  timeline.cues.forEach((cue, index) => { cue.at = shift(cue.at); });
  const worst = Math.max(...timeline.cues.map((cue, index) => Math.abs(cue.at - flips[index])));
  if (worst > 0.15) throw new Error(`video clock and page clock differ by ${Math.round(worst * 1000)} ms`);
}

import { readFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import path from 'node:path';

export function readConfig(project) {
  return JSON.parse(readFileSync(path.join(project, 'demo.config.json'), 'utf8'));
}

export function parseNarration(markdown) {
  return markdown
    .split('\n')
    .map((line) => line.split('|').map((cell) => cell.trim()))
    .filter((cells) => /^[A-Z]\.\d+$/.test(cells[1] ?? ''))
    .map((cells) => ({ id: cells[1], start: cells[2] ?? '', screen: cells[3] ?? '', line: cells[4] ?? '' }));
}

export function loadNarration(project) {
  const rows = parseNarration(readFileSync(path.join(project, 'narration.md'), 'utf8'));
  if (!rows.length) throw new Error('narration.md has no lines. Use rows like | A.1 |  | screen | spoken line |');
  return rows;
}

export function probeDuration(file) {
  return Number(execFileSync('ffprobe', ['-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', file]).toString());
}

export function wordsOf(text) {
  return text.toLowerCase().replace(/([a-z])-([a-z])/g, '$1$2').replace(/[^a-z0-9' ]+/g, ' ').replace(/\s+/g, ' ').trim();
}

#!/usr/bin/env node
import { spawnSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const project = path.resolve(process.argv[2] ?? '.');
const only = process.argv[3];
const steps = ['voice', 'record', 'sync', 'render', 'check'];
if (only && !steps.includes(only)) {
  console.error(`Unknown step ${only}. Use ${steps.join(', ')}.`);
  process.exit(1);
}
for (const step of steps) {
  if (only && step !== only) continue;
  const result = spawnSync(process.execPath, [path.join(here, `${step}.mjs`), project], { stdio: 'inherit' });
  if (result.status) process.exit(result.status ?? 1);
}

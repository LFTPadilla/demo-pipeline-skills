---
name: explainer-video
description: Produces a narrated faceless explainer video from a concept, a ticket, or a bug write-up. It uses HyperFrames and a voice track. Use when the audience will not read code and the order of events must be spoken. For a recording of a real web app, use product-demo-video instead.
---

# Explainer video

Turns a written concept into `renders/video.mp4`. Thin wrapper: HyperFrames owns
the rendering, this skill owns the pipeline order, the video direction, and the
voice step.

## When to use

- The explanation is a sequence in time: a race condition, a retry loop, a
  migration, an onboarding flow.
- The audience will not read code and will not open a diagram tool.
- Somebody must watch it later, without you present.

Do not use it for a single static picture. Route to `diagram-render` instead.

## Requirements

| Need | How |
|---|---|
| HyperFrames | `npx hyperframes` installs on demand. Pin nothing. |
| Voice (preferred) | `DEEPGRAM_API_KEY` in the environment |
| Voice (free) | The `voice-studio` skill, local, no key. Or HyperFrames local Kokoro: `npx hyperframes tts` |
| Media | `ffmpeg` and `ffprobe` on `PATH` |

Node 20 or later. The voice script has zero npm dependencies and uses `fetch`.

## Quick start

- `npx hyperframes init videos/<slug> --non-interactive --example=blank --skill=faceless-explainer`, then write `BRIEF.md`.
- Write `STORYBOARD.md` (8–10 frames, one `## Video direction` block) and `SCRIPT.md`, then get the frame table approved.
- `node <skill_folder>/scripts/deepgram-voice.mjs --script SCRIPT.md --out audio_meta.json`, then `audio.mjs sync-durations`.
- Write each frame. Give the frame the packet, the video direction, the word cues, and the real duration. Edit the frames one at a time, or in parallel if your agent can run parallel tasks.
- `npx hyperframes render --skill=faceless-explainer --quality high --output renders/video.mp4`.

Full commands, in order, live in [`references/pipeline.md`](references/pipeline.md).
The look and the negative list live in [`references/video-direction.md`](references/video-direction.md).

## Output contract

- File: `videos/<slug>/renders/video.mp4`
- Report the duration in seconds, measured with `ffprobe`, not the planned duration.
- Report the frame count and any frame that failed `npx hyperframes check`.

## Iteration rule

After every video, append what you learned to
[`references/lessons.md`](references/lessons.md) as one dated line. The pipeline
is only as good as the last failure somebody wrote down.

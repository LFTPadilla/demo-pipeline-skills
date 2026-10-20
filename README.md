# Demo pipeline skills

This pack takes a product from a prototype to a narrated demo video.

1. **Prototype.** Build a clickable mockup and collect comments (`mockup-review`).
2. **Stakeholder comments.** Share a running local app, with comments pinned to real elements (`live-app-review`).
3. **Final publish.** Ship the app with your normal release process. This pack does not publish it.
4. **Demo video.** Record the app with mocked data, add a voice, and render an MP4 (`product-demo-video`).

Four more skills explain a concept when a demo of the UI is the wrong medium: `explain-concept`, `diagram-render`, `explainer-video`, and `voice-studio`.

HyperFrames renders compositions. Its skills are separate. Install them with:

```bash
npx hyperframes skills update
```

## Skills

| Skill | What it does |
|---|---|
| `mockup-review` | Static one-file prototype, comment layer, Cloudflare Workers |
| `live-app-review` | Token-gated proxy in front of a running local app, Tailscale Funnel |
| `product-demo-video` | Narration, mocked recording, voice, captions, chapter chips, MP4, checks |
| `explain-concept` | Picks a diagram, a video, or a short code trail |
| `diagram-render` | Renders a flowchart or infrastructure picture as a PNG |
| `explainer-video` | Narrated explainer with invented frames, not a recording of your app |
| `voice-studio` | Free local speech. Heavy on CPU or GPU. No API key |

## Prerequisites

| Tool | Used by |
|---|---|
| Node.js 20+ | `product-demo-video`, `diagram-render`, `explainer-video` |
| ffmpeg, ffprobe, and fontconfig (`fc-match`) | `product-demo-video`, `explainer-video` |
| Playwright | `product-demo-video` (recording). Optional for the review skills' browser tests |
| Python 3 | `live-app-review`, `mockup-review` |
| Tailscale | `live-app-review`, only when you share a public link |
| Wrangler (`npx wrangler`) | `mockup-review`, only when you deploy |

## Install

Copy or link one skill folder into the place your agent reads.

| Agent | Folder |
|---|---|
| Claude Code | `~/.claude/skills/<name>/` or `.claude/skills/<name>/` in the project |
| Cursor | `~/.cursor/skills/<name>/` or `.cursor/skills/<name>/` in the project |
| Codex | `~/.codex/skills/<name>/` |
| Any agent | Point it at `skills/<name>/SKILL.md` and follow the shell commands in that file |

Example for one skill:

```bash
mkdir -p ~/.cursor/skills
ln -s "$PWD/skills/product-demo-video" ~/.cursor/skills/product-demo-video
```

## Voice providers

`product-demo-video` picks the provider with `voice.provider` in `demo.config.json`.
Each paid provider reads one environment variable. A missing key names that variable and a cheaper option.

| Provider | Quality | Cost | Environment variable |
|---|---|---|---|
| ElevenLabs `eleven_v4` | Highest | About $0.08 per 1,000 characters. Public list price on 2026-10-20 | `ELEVENLABS_API_KEY` |
| Deepgram Aura-2 | Medium | $0.030 per 1,000 characters, pay as you go. Public list price on 2026-10-20 | `DEEPGRAM_API_KEY` |
| voice-studio | Local profile | Free. Uses this machine (CPU or GPU) and a model download | none |
| `silent` | Placeholder, no speech | Free. For a pipeline check | none |

Prices change. Confirm them on the vendor page before a long run.

## Quick start (about 10 minutes)

The example is a fake inbox. It uses the silent voice, so you need no API key.

```bash
cd skills/product-demo-video/examples/desk-app
npm install
npx playwright install chromium
node ../../scripts/build.mjs .
```

Open `out/desk-demo.mp4`.

The build writes the narration audio, records the page, fills the start times, writes `index.html` (captions and chapter chips), renders the MP4, and prints the checks.

To speak for real, set `voice.provider` to `deepgram`, `elevenlabs`, or `voice-studio`, set `voice.id`, and export the key when that provider needs one. Then run the same build command.

## Zip

`scripts/make-zip.sh` writes `dist/demo-pipeline-skills.zip` from tracked files only. Commit first. The zip is not committed.

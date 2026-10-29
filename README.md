# Demo pipeline skills

This pack takes a product from a prototype to a narrated demo video.

1. **Prototype.** Build a clickable mockup and collect comments (`mockup-review`).
2. **Stakeholder comments.** Share a running local app, with comments pinned to real elements (`live-app-review`).
3. **Final publish.** Ship the app with your normal release process. This pack does not publish it.
4. **Demo video.** Record the app with mocked data, add a voice, and render an MP4 (`product-demo-video`).

Four more skills explain a concept when a demo of the UI is the wrong medium: `explain-concept`, `diagram-render`, `explainer-video`, and `voice-studio`.

Run the commands in this file from the repo root.

## Skills

| Skill | What it does |
|---|---|
| `mockup-review` | Static one-file prototype, comment layer, Cloudflare Workers |
| `live-app-review` | Token-gated proxy in front of a running local app, Tailscale Funnel |
| `product-demo-video` | Narration, mocked recording, voice, captions, chapter chips, corner map, MP4, checks |
| `explain-concept` | Picks a diagram, a video, or a short code trail |
| `diagram-render` | Renders a flowchart or infrastructure picture as a PNG |
| `explainer-video` | Narrated explainer with invented frames, not a recording of your app |
| `voice-studio` | Local speech for narration. Needs the VoiceStudio app. No API key |

## Prerequisites

| Tool | Used by |
|---|---|
| Node.js 20+ | `product-demo-video`, `diagram-render`, `explainer-video` |
| ffmpeg, ffprobe, and fontconfig (`fc-match`) | `product-demo-video`, `explainer-video` |
| Playwright | `product-demo-video` (recording). Optional for the review skills' browser tests |
| Python 3 | `live-app-review`, `mockup-review` |
| uv | Browser tests in `live-app-review` and `mockup-review` (`uv run --with playwright`) |
| HyperFrames | Default render for `product-demo-video`. Install skills with `npx hyperframes skills update`. The render command pins `hyperframes@0.8.143` |
| Tailscale | `live-app-review`, only when you share a public link |
| Wrangler (`npx wrangler`) | `mockup-review`, only when you deploy |
| VoiceStudio app | `voice-studio` and the free demo voice. Not included in this pack |
| archify skill (optional) | `explain-concept` uses it first for diagrams when it is installed. Install with `npx skills add tt-a1i/archify -g`. Not included in this pack |

## Install

Run commands from the repo root.

Copy or link one skill folder into the place your agent reads. `live-app-review` reads the comment layer from the sibling folder `mockup-review`. Install those two next to each other. If `mockup-review` is missing, the review page has no comment layer.

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
| voice-studio | Local profile | Free, but a clean machine cannot run it from this pack. Install the VoiceStudio desktop app from `https://github.com/debpalash/VoiceStudio`, start it, and let it download a model. It uses CPU or GPU. | none |
| `silent` | Placeholder, no speech | Free. For a pipeline check | none |

Speech-to-text is off unless the demo config sets `"stt": true`. The check sends each clip to Deepgram Nova-3 pre-recorded. The public pay-as-you-go price is $0.0043 per minute (2026-10-20). It also needs `DEEPGRAM_API_KEY`.

Prices change. Confirm them on the vendor page before a long run.

VoiceStudio install, from the repo root:

```bash
# 1. Install the VoiceStudio desktop app (see the link above). This pack does not include it.
# 2. Start the app. Then:
curl -s http://127.0.0.1:3900/health
install -m 755 skills/voice-studio/scripts/vsay.sh "$HOME/.local/bin/vsay"
```

## Quick start (about 10 minutes)

Run these commands from the repo root. The example is a fake inbox. It uses the silent voice, so you need no API key.

```bash
npm install --prefix skills/product-demo-video/examples/desk-app
npx --prefix skills/product-demo-video/examples/desk-app playwright install chromium
node skills/product-demo-video/scripts/build.mjs skills/product-demo-video/examples/desk-app
```

Open `skills/product-demo-video/examples/desk-app/out/desk-demo.mp4`.

The build writes the narration audio, records the page, fills the start times, writes `index.html` (captions, chapter chips, and the corner map), renders the MP4 with HyperFrames, and prints the checks. If HyperFrames fails, the same command renders with ffmpeg.

To speak for real, set `voice.provider` to `deepgram`, `elevenlabs`, or `voice-studio`, set `voice.id`, and export the key when that provider needs one. Then run the same build command.

## Zip

`scripts/make-zip.sh` writes `dist/demo-pipeline-skills.zip` from tracked files only. Commit first. The zip is not committed.

## License

MIT. See `LICENSE`.

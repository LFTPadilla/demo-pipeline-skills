---
name: product-demo-video
description: Builds a narrated product demo of a web app. It writes a narration table, records the app with mocked API data, generates a voice, syncs captions and chapter chips, renders an MP4, and runs the checks. Use for a stakeholder demo, a release tour, or a narrated walkthrough of UI.
---

# Product demo video

One narration table drives the voice, the captions, and the chapter chips.
One cue is one sentence and one highlight.
The highlight ring is in the recording. The captions and the chips are placed on the voice clock.

This skill includes the old browser-recording flow (one clip per cue, a corner marker, a sync check) and the composition flow (chapter chips, captions, a HyperFrames `index.html`).

## Needs

- Node 20 or newer, ffmpeg, ffprobe, and fontconfig (`fc-match`).
- Playwright. Install it in the demo project with `npm install playwright`, then `npx playwright install chromium`.
- A voice provider. See the table below. `silent` needs no key and writes placeholder audio.
- Optional. HyperFrames, for a second render of `index.html`: `npx hyperframes skills update`.

## Voice providers

Set `voice.provider` in `demo.config.json`, or set `VOICE_PROVIDER`.
Set `voice.id`, or set `VOICE_ID`. Keys come only from the environment. The scripts never print a key.

| Provider | Setting | Quality | Cost | Key |
|---|---|---|---|---|
| ElevenLabs `eleven_v4` | `elevenlabs` | Highest | About $0.08 per 1,000 characters (public list, 2026-10-20) | `ELEVENLABS_API_KEY` |
| Deepgram Aura-2 | `deepgram` | Medium | $0.030 per 1,000 characters, pay as you go (public list, 2026-10-20) | `DEEPGRAM_API_KEY` |
| voice-studio | `voice-studio` | Local voice profile | Free. Uses CPU or GPU and downloads a model | none |
| Silent placeholder | `silent` | No speech | Free | none |

A missing key stops the run. The error names the variable and the cheaper or free option.
Prices change. Read the vendor pricing page before a long run.

`voice.id` for Deepgram is an Aura-2 model name, for example `aura-2-thalia-en`.
`voice.id` for ElevenLabs is the voice id from your ElevenLabs account.
`voice.id` for voice-studio is a profile name from `vsay -L`, for example `narrator`.

## Project files

| File | Role |
|---|---|
| `narration.md` | The only spoken text. Columns: `id`, `start (s)`, `on screen`, `line` |
| `demo.config.json` | URL, port, size, colors, voice provider, voice id |
| `scenario.mjs` | Groups, cues, and `mocks` for API responses |
| `index.html` | Written by `sync.mjs`. HyperFrames composition |
| `out/<name>.mp4` | The rendered video |

Cue ids look like `A.1`. The letter is the chapter. The number is the line.

## Commands

Run them from anywhere. Replace `PROJECT` with the demo folder.

```bash
node <skill>/scripts/build.mjs PROJECT
node <skill>/scripts/voice.mjs PROJECT
node <skill>/scripts/record.mjs PROJECT
node <skill>/scripts/sync.mjs PROJECT
node <skill>/scripts/render.mjs PROJECT
node <skill>/scripts/check.mjs PROJECT
node <skill>/scripts/self-check.mjs
```

`build.mjs` runs voice, record, sync, render, and check.
`render.mjs` is the portable renderer. It uses ffmpeg.
`sync.mjs` also writes `index.html`. To render that file with HyperFrames, run `npx hyperframes render` in the project after you install the HyperFrames skills.

## Scenario

```js
export default {
  mocks: [{ url: '**/api/tasks', json: [{ id: '1', title: 'Ship the notes', priority: 'High' }] }],
  groups: [{
    id: 'A',
    title: 'Inbox',
    slide: { title: 'Inbox', text: 'See what needs a reply.' },
    setup: async (page, base) => { await page.goto(base + '/'); },
    cues: [
      { id: 'A.1', target: '#col-priority' },
      { id: 'A.2', target: '#open-1', action: 'click' },
    ],
  }],
};
```

`action` is `highlight` (default), `hover`, or `click`. A click runs at the end of the line.
`mocks` are installed before the first navigation. The app should call relative URLs.

## Write the lines

1. One short sentence per highlight. Keep it under 12 words.
2. Use contractions. Use a period where a person stops.
3. Open a chapter with a light word such as "First" or "Next" when it helps.
4. Do not make the voice spell a short form. Say the full term once.
5. The slide text is silent. Do not read it aloud.

## Checks

`check.mjs` prints the duration and the file size. It also:

1. Reads the corner marker in the MP4 and compares it to each line start. The limit is 150 ms (`MAX_OFFSET_MS`).
2. Measures loudness when the provider is not `silent`. The target is -16 LUFS.
3. Writes one JPEG per chapter in `out/stills/`.
4. Scans the project text for a pasted key. A hit fails the check.
5. Speech-to-text runs inside `voice.mjs` when `DEEPGRAM_API_KEY` is set and the provider is not `silent`.

## Example

`examples/desk-app` is a fake inbox. It uses the `silent` provider so the first run needs no key.

```bash
cd <skill>/examples/desk-app
npm install
npx playwright install chromium
node ../../scripts/build.mjs .
```

The video is `out/desk-demo.mp4`.
To use a real voice, change `voice.provider` and export the matching key.

# Lessons

One dated line per lesson. Append after every video. Newest at the bottom.

- 2026-09-08: Deepgram Aura-2 `orion` speaks about 1.6× slower than planned durations. Write scripts short and let the real durations win through `audio.mjs sync-durations`.
- 2026-09-08: Frame files put several clips on one `data-track-index`, and assembly fails. Renumber the indices sequentially, per frame, before you assemble.
- 2026-09-08: `check` reports `text_box_overflow` on caption words. Ignore that finding.
- 2026-09-08: Frame scripts crash on `querySelector` for `#root`-scoped lookups after a template clone. Use a guarded `getElementById`, or GSAP string selectors.
- 2026-09-08: Remixed presets do not stage fonts. Download the Google Fonts woff2 *latin* subset into `assets/fonts`, and parse the `/* latin */` block to find the URL.
- 2026-09-08: A frame cannot re-time `Scene` windows without data. Give each frame a word-cue file.
- 2026-09-08: The frame packet omits the storyboard's `## Video direction`. Pass it as a separate file.

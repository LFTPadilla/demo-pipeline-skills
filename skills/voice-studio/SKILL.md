---
name: voice-studio
description: Speaks demo narration and short lines on this machine through VoiceStudio. Use for a product-demo voice track, a local read-aloud, or expressive speech when you do not want a paid voice API.
---

# VoiceStudio

Speak a narration line or a short reply on this machine. No API key. The app uses CPU or GPU and a downloaded model.

## Install and start

This folder does not install VoiceStudio. A machine with only this pack cannot speak.

1. Install the VoiceStudio desktop app from `https://github.com/debpalash/VoiceStudio`.
2. Start the app. It listens on `http://127.0.0.1:3900`. The first start downloads a model.
3. Put `scripts/vsay.sh` on your `PATH` as `vsay`, or call that script by its path.
4. Check the server:

```bash
curl -s http://127.0.0.1:3900/health
```

The `device` field names the CPU or GPU that will run the model. A missing server means the app is not running. Start the app again, then run the check.

## Usage

```bash
# Speak in background (non-blocking, default: explainer voice, 1.1x speed, 24 steps)
vsay -b "Task completed successfully."

# Expressive speech with pauses and non-verbal tokens
vsay -b "[surprise-wa] Incredible! The benchmark finished... [breathing] and ran fast. [laughter]"
```

## Flags

- `-b, --bg`: Play in background without blocking caller.
- `-v <voice>`: `explainer` (default), `companion`, `enthusiast`, `narrator`, `radio`, `deep`, or profile ID.
- `-s <float>`: Speed multiplier (default: `1.1`).
- `-N <int>`: Diffusion steps (default: `24`; `16` fast, `32` studio).
- `--cfg <float>`: Guidance scale (default: `2.0`).
- `-o <path>`: Save WAV to target file.
- `--no-play`: Generate audio without playing.
- `-l <code>`: Language code (default: `es`).
- `-L, --list`: List installed voice profiles.

## Expressive Tokens & Prosody

Embed bracketed tokens for vocal inflections:
- **Reactions:** `[laughter]`, `[surprise-wa]`, `[surprise-ah]`, `[sigh]`, `[crying]`
- **Hesitations & Feedback:** `[uhm]`, `[confirmation-en]`, `[dissatisfaction-hnn]`, `[shh]`, `[cough]`
- **Questions:** `[question-en]`, `[question-ah]`, `[question-oh]`, `[question-ei]`, `[question-yi]`

**Pacing rules:**
1. Combine `...` and `[breathing]` between clauses for natural breathing pauses.
2. Use punctuation marks (!, ?, ¡, ¿) to shape expressive pitch contours.
3. Place commas before conjunctions to induce realistic pitch dips.

## Narration

For a demo, write one short sentence per line. Call `vsay` with `--no-play` and `-o` so the clip is a file, not playback.

```bash
vsay --no-play -l en -v narrator -N 16 -s 1.0 -o line.wav "Every task shows a priority."
```

`product-demo-video` calls `vsay` the same way when `voice.provider` is `voice-studio`.

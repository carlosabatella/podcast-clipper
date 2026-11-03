# Podcast Clipper

A Claude skill and a Python toolkit that turn podcast episodes into vertical short clips (TikTok, Reels, Shorts) in a saved per-client style. Use it to run podcast clipping as a service: each client gets a profile and a house style, picks are informed by what's trending in their niche, and every batch is checked and approved before anything is posted.

## What's in this repo

| File | What it is |
|---|---|
| `SKILL.md` | The skill: workflow, ground rules, style presets, moment-picking rubric, QA, delivery and the clip log |
| `clipkit.py` | The editing toolkit (v1) that the skill runs |
| `examples/profile.json` | A sample client profile |
| `examples/style.json` | A sample style preset (Montserrat captions with a karaoke sweep, logo, top progress bar) |
| `examples/picks.json` | A sample picks file: the moments to cut, with hooks, key phrases and post captions |
| `examples/logo.png`, `examples/cover.jpg` | Placeholder logo and cover art the sample style points to |

## What the toolkit does

1. **Transcribe:** speech-to-text with word-level timestamps (English, or 25 European languages), run locally with sherpa-onnx and NVIDIA's Parakeet model.
2. **Cut:** trims each moment to word boundaries and removes long pauses and filler words.
3. **Frame:** finds scene cuts and faces, then picks a layout per shot:
   - one face: a vertical crop that follows them
   - two faces: a stacked split-screen
   - no faces: the full frame over a blurred background
   - audio-only shows: an audiogram with cover art
4. **Style:** adds animated word-by-word captions, a hook title, punch-in zooms on key phrases, a progress bar, a logo, a color grade and loudness normalization (−14 LUFS).
5. **Batch:** renders every pick, plus a contact sheet per clip for review, a `posting.csv` with captions and hashtags, and a manifest.

On a test episode with two hosts, caption timing landed within 15 ms of the speech (median). Transcription ran about 6x faster than real time on 2 CPUs, and rendering took about twice the clip length.

## Quick start (command line)

Requires Python 3.10+, ffmpeg built with libass, and network access to GitHub release downloads for the models (about 0.5 GB on first run).

```bash
python3 clipkit.py setup
python3 clipkit.py transcribe episode.mp4 -o runs/ep1        # writes words.json + transcript.txt
# write runs/ep1/picks.json (see examples/picks.json), using times from transcript.txt
python3 clipkit.py font "Montserrat" --weights 800 -o examples/fonts
python3 clipkit.py batch runs/ep1/picks.json --style examples/style.json -o runs/ep1/out
```

`python3 clipkit.py -h` lists every command: `setup`, `transcribe`, `cut`, `render`, `batch`, `sheet`, `ref` (study reference clips), `font`, `style`, `find`, `fix`.

## How the skill gets the toolkit

At the start of each session, the skill fetches `clipkit.py` from the private **Clip Studio Toolkit** page on claude.ai. It checks the file's sha256 against the value in `SKILL.md` and stops if they differ.

v1 sha256: `fa49bb8de73e8c2fef1cc688c2ff03333fd66762cdc21ee4122dd5842653e91e`

If you change `clipkit.py`, republish it to that page as `clipkit.txt`, update `version.txt` there, and update the sha256 in `SKILL.md`. Anyone you hand the skill to also needs access to that page.

## Rights

Only clip footage you own, that a client supplied, that a show explicitly invites clippers to use, or that is openly licensed. The skill never downloads from YouTube, Spotify, TikTok or similar platforms.

## Third-party components

`clipkit.py` downloads or installs these at setup. Check their licenses before commercial use.
- sherpa-onnx
- NVIDIA Parakeet TDT 0.6B (speech recognition)
- Silero VAD (voice activity detection)
- OpenCV and the YuNet face detector
- ffmpeg/libass
- Google Fonts via Fontsource

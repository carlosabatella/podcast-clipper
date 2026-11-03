---
name: podcast-clipper
description: Clip podcast episodes into styled vertical shorts - scout trends, pick the best moments, cut, reframe and caption in a client's saved style, then deliver for review. For clipping clients or topic scouting.
---

# Podcast Clipper

Turn long podcast episodes into short vertical clips (TikTok, Reels, Shorts) in a saved per-client style, and run it as a repeatable service: client profiles, style presets, trend-informed picks, QA, an approval step and a clip log.

All editing runs in the workspace with `clipkit.py`, which is kept on the user's Clip Studio Toolkit page. It handles speech-to-text with word timestamps, tightening pauses and fillers, face-aware vertical framing (follow, split-screen for two-shots, blurred full frame, audiogram for audio-only shows), animated captions, a hook title, punch-in zooms, a progress bar, a logo, color and loudness.

**Two modes**
- **Client mode** (default): the episode comes from the client or the user.
- **Scout mode**: the user gives a topic. Find what is working in short-form and which shows or episodes fit, then clip only footage you are cleared to use.

## Ground rules

1. **Rights.** Only clip footage the user owns, a client supplied, a show that explicitly invites clippers (a clipping program or written permission), or openly licensed content. Never download from YouTube, Spotify, TikTok or similar platforms. If a strong show is only available there, list it as a lead and ask the user to get the files or permission.
2. **Approval before posting.** Never post or schedule anything until the user has approved the finished clips.
3. **No virality promises.** Picks are judgment calls backed by the trend brief. Say so.
4. **Keep clients separate.** Never mix one client's assets, notes or clips into another's.
5. **Present or away.** If the user is present, confirm the shortlist before rendering. If the run is unattended, make reasonable calls, state them at the top of the summary and carry on.

## 0. Set up the toolkit (every session)

1. **Fetch the toolkit.** Use the Artifact tool with `action: "read"`, `url: "https://claude.ai/artifact/3PhP3mDjwyBnGmjsLbonA4"` and `path: "clipkit.txt"`. The result gives the saved file's path and its sha256.
2. **Check the fingerprint.** The sha256 must be `fa49bb8de73e8c2fef1cc688c2ff03333fd66762cdc21ee4122dd5842653e91e`. If it differs, stop: tell the user the toolkit page changed after this skill was saved, and run nothing until they confirm the new version.
3. **Install and check:**

```bash
mkdir -p ~/clipstudio && cp "<saved path>" ~/clipstudio/clipkit.py && cd ~/clipstudio
python3 clipkit.py setup   # installs sherpa-onnx, fetches models (~0.5 GB, about a minute), checks ffmpeg + libass
```

If the Artifact tool or the page isn't available, ask the user to attach their copy of `clipkit.py` and check it with `sha256sum`. Run every later command from `~/clipstudio`. `python3 clipkit.py -h` lists every command.

**Timing.** Transcription runs about 6x faster than real time on 2 CPUs, so a 60-minute episode takes about 10 minutes. Rendering takes about 2x the clip length (4K sources take about 4x). Shell calls time out at 10 minutes, so run long jobs in the background and poll them:

```bash
nohup python3 clipkit.py transcribe EP.mp4 -o runs/EP > runs/EP.log 2>&1 &
sleep 120; tail -2 runs/EP.log     # repeat until the JSON summary line appears
```

## 1. Studio home (where client data lives)

Use a `Clip Studio` folder in a folder the user has connected from their computer. Ideally it sits inside their Google Drive or Dropbox, so deliveries sync to clients. If no folder is connected, ask for the client's `profile.json` and style files as attachments, or create new ones, and hand back the updated files with the delivery.

**Moving files between the studio and the workspace**
- **Small text files** (`profile.json`, `clip-log.csv`): read and edit them in place on the user's computer with the device shell.
- **What rendering needs:** stage the style JSON and its assets (fonts, logo, cover, music) plus the episode into the workspace. Copy them to `~/clipstudio/clients/<slug>/` with the same layout (`styles/house.json`, `styles/fonts/...`) so the style's relative paths still work.
- **Finished clips:** send them, then commit them into the client's `deliveries/` folder.
- **New or changed style files:** write them back the same way.

```
Clip Studio/
  clients/<client-slug>/
    profile.json
    styles/house.json          (+ fonts/, logo.png, cover.jpg, music files the style points to)
    inbox/                     raw episodes from the client
    deliveries/YYYY-MM-DD_<episode>/   clips, posting.csv, qa/ sheets
    clip-log.csv
  trends/<niche>_YYYY-MM-DD.md trend briefs, reusable within a niche
```

`profile.json` (keep it short and current):

```json
{
  "client": "Creative Money Talk", "slug": "creative-money-talk",
  "shows": ["Creative Money Talk"], "niche": "freelance creatives, pricing",
  "audience": "designers and video editors, 22-35",
  "platforms": ["tiktok", "reels", "shorts"], "clips_per_episode": 6, "length_s": [20, 60],
  "style": "styles/house.json",
  "vocab_fixes": {"neuron visuals": "Neuron Visuals"},
  "avoid": ["sponsor reads", "anything about the lawsuit"],
  "cta": "Full episode on Spotify", "hashtags": ["#freelance"],
  "rights": "client-owned footage, supplied via Drive",
  "approver": "Maya (host)", "delivery": "clients/creative-money-talk/deliveries",
  "notes": ["what performs: contrarian money takes, 25-40s, split-screen debates"]
}
```

## 2. Intake (every job)

Collect the following: which client, the episode file or files, how many clips, platforms, deadline, and any moments the client wants included. Check the client's `inbox/` first.

**Getting the episode into the workspace**
- **Attached to the chat:** use it directly.
- **In a connected folder and under the staging limit** (about 400 MB): stage it.
- **Bigger files on the user's computer:** split the work.
  1. On their computer (device shell), extract the audio: `ffmpeg -i EP.mp4 -vn -ac 1 -ar 16000 EP.wav`. Stage it, then transcribe and pick in the workspace.
  2. For each pick, cut a padded segment there: `ffmpeg -ss <start-3> -i EP.mp4 -t <len+6> -c:v libx264 -preset veryfast -crf 16 -c:a aac seg_NN.mp4`. Stage the segments.
  3. In `picks.json`, set `"media": "<staged seg path>"` and `"media_start": <start-3>` on that clip.
  - If their computer lacks ffmpeg, run `pip install imageio-ffmpeg` there; `python3 -c "import imageio_ffmpeg as f; print(f.get_ffmpeg_exe())"` gives a binary.
  - Device shell calls time out at about 45 s, so background long ffmpeg runs (`nohup ... &`) and poll.
- **A direct download link** (Dropbox, Drive, S3): fetch it with curl if the host is allowed. If it's blocked, tell the user which domain to allow, or ask them to put the file in the connected folder.

**New client?** Ask for 2–3 reference clips they love, their logo and brand fonts and colors, show name, platforms and cadence. Then create `profile.json` and build their style (section 3). Get the user's OK on a sample clip before running the first full batch.

## 3. Style presets

A style is a partial JSON file merged over the defaults (`python3 clipkit.py style` prints them all). Asset paths are relative to the style file.

| Key | What it controls |
|---|---|
| `layout` | `auto` (split for two faces, follow for one, blur for none), `follow`, `split`, `blur`, `audiogram` |
| `follow.zoom`, `follow.face_y` | Tighter crop, and where the face sits when zoomed |
| `split.*`, `blur_bg.*` | `enabled`, `divider_color`, `divider_px` · `darken`, `fg_y` (where the full frame sits) |
| `captions.*` | `font`, `weight`, `size`, `color`, `highlight` (active word), `emphasis` (key phrases), `outline_color`, `outline`, `shadow`, `uppercase`, `punctuation` (`drop`/`keep`), `max_words`, `max_chars`, `y` / `y_split` / `y_audiogram` (0–1 of height), `animation` (`pop`, `highlight`, `karaoke`, `word`, `static`), `linger`, `mask_words` |
| `hook.*` | `enabled`, `duration` (seconds; ≤0 = whole clip), `font`, `weight`, `size`, `color`, `box_color`, `pad`, `y`, `max_chars_line`, `uppercase` |
| `punch_in.*` | `enabled`, `scale`, `auto` (toggle at sentence starts), `min_interval` |
| `progress_bar.*` | `enabled`, `color`, `height`, `position` (`top`/`bottom`) |
| `logo.*` | `path`, `width`, `position` (`top-right` etc.), `margin`, `opacity` |
| `grade.*` | `contrast`, `saturation`, `brightness`, `lut` (.cube file) |
| `audio.*` | `loudness` (LUFS), `music` (file), `music_volume` |
| `cut.*` | `max_gap` (longest pause kept), `keep_gap`, `remove_fillers`, `pad_start`, `pad_end` |
| `audiogram.*` | `background`, `wave_color`, `cover` (image) for audio-only shows |
| other | `fonts_dir`, `fixes` (word replacements), `canvas.width/height/fps`, `encode.crf/preset` |

**Matching a reference style**
1. Run `python3 clipkit.py ref ref1.mp4 ref2.mp4 -o refs/`. It prints the cut rate, loudness and shot length, and saves a `*_first6s.jpg` (every 0.5 s) and `*_overview.jpg` sheet per clip. Look at both sheets.
2. Map what you see to keys:
   - **Captions:** words on screen at once → `max_words`. All caps → `uppercase`. Color of the spoken word → `highlight`. Active word grows → `pop`; color sweeps across → `karaoke`; one word at a time → `word`.
   - **Framing:** two people stacked → `split`. Landscape frame over a blurred background → `blur`. Single face full-bleed → `follow`.
   - **Overlays:** text box early at the top → `hook`. Thin bar → `progress_bar`. Many zoom jumps → `punch_in` with a lower `min_interval`.
   - **Audio:** `loudness_lufs` → `audio.loudness`.
3. Fonts: put client font files in `styles/fonts/` and set `"fonts_dir": "fonts"`. For a Google Font, run `python3 clipkit.py font "Montserrat" --weights 800,900 -o styles/fonts`, then set `captions.font` to the family it prints.
4. Render one test clip, make a sheet, and compare it side by side with the reference sheet. Adjust and repeat until it matches, then show the user.

## 4. Scout: the trend brief (both modes)

Write a short brief that steers picking. Reuse one under 2 weeks old for the same niche.
- Search the web for what is working now in the niche on TikTok, Reels and Shorts:
  - recurring angles and hook phrasing
  - formats (split-screen debate, single talking head, captions style)
  - typical lengths
  - topics people are arguing about this week
- Check how comparable shows clip their episodes: titles of their top clips and their hooks.
- If trend or social-data skills or connectors are available (TikTok, Reels or YouTube trend finders, viral-video analyzers, podcast finders, scrapers), use them.
- Save the brief as `trends/<niche>_<date>.md` with 5–10 bullets (angles, hook patterns, formats and lengths, timely topics) and sources.

**Scout mode, additionally:** shortlist shows and episodes on the topic. For each, give the show, episode, why it fits, rights status (own, client, clipping program, permission needed, openly licensed) and how to get the files. Present the list and proceed only with cleared footage. For clipping programs, record their terms (required tags, links, credit) in a profile and follow them.

## 5. Transcribe

```bash
python3 clipkit.py transcribe EP.mp4 -o runs/<client>/<ep>        # --lang eu for 25 European languages
python3 clipkit.py fix runs/<client>/<ep>/words.json "neuron visuals=>Neuron Visuals"   # profile vocab_fixes
```

This writes `words.json` (word timestamps) and `transcript.txt` (one sentence per line, `[hh:mm:ss.s]`). There are no speaker labels, so infer who's talking from the content. For other languages, use a transcription connector or service the user has, then convert its output to the `words.json` format (`{"media":..., "words":[{"w","s","e"}]}`).

## 6. Pick the moments (the judgment call)

Read the full `transcript.txt`. Over about 90 minutes, read it in parts and keep a running shortlist. Judge each candidate on these points:
- **Hook in the first 2–3 seconds:** a bold claim, a number, a question, a conflict or a confession. Start on it, not on "So, um, yeah".
- **Stands alone:** someone with zero context gets it. No "like I said", no unresolved "he" or "that".
- **Payoff:** it lands a point, punchline, story resolution or strong opinion. End right after it.
- **Energy:** laughter, disagreement, emotion, a vivid story.
- **Trend fit:** it matches an angle in the brief. Note which.
- **Length and variety:** within the profile's range (default 20–60 s), with a mix of topics and formats across the batch.
- **Avoid:** the profile's `avoid` list, ad reads, and claims that turn misleading or defamatory out of context. Also avoid medical, legal or financial advice stripped of its caveats, and anything the client would wince at.

Shortlist about 1.5× the clips needed and keep the best. **Timing convention:** `start` is the timestamp of the first line you want, and `end` is the timestamp of the first line you don't want. To start or stop mid-line, `python3 clipkit.py find runs/.../words.json "exact phrase"` gives exact times.

Write `runs/<client>/<ep>/picks.json`:

```json
{
  "media": "/abs/path/EP.mp4", "words": "runs/acme/ep12/words.json", "prefix": "acme_ep12_",
  "clips": [
    {"title": "Stop charging by the hour", "start": "00:41:05.2", "end": "00:41:49.8",
     "hook": "Hourly pricing punishes you for getting better",
     "emphasis": ["half as much money"],
     "caption": "Get twice as good, make half as much. Full episode on Spotify.",
     "hashtags": ["#freelance", "#pricing"],
     "why": "contrarian claim, clean payoff", "trend": "pricing transparency"}
  ]
}
```

- `hook`: on-screen text of 8 words or fewer. It must be specific and honest, never a promise the clip doesn't keep.
- `emphasis`: 1–3 phrases exactly as spoken. They get colored and punched in.
- `caption`/`hashtags`: the platform post text. Add the profile's `cta`.
- Optional per clip: `layout`, `cut` (for example `{"max_gap": 0.8, "remove_fillers": false}` when pauses or laughs matter), and `media` + `media_start` for a pre-cut segment.

If the user is present, show the shortlist (title, timestamps, hook, why) before rendering.

## 7. Cut and render

```bash
python3 clipkit.py batch runs/<client>/<ep>/picks.json --style clients/<slug>/styles/house.json -o runs/<client>/<ep>/out
python3 clipkit.py batch ... --only 2,5          # redo or render a subset
```

Leave out `--style` for the default look. An audio-only episode automatically becomes an audiogram; pass `--cover cover.jpg` or set `audiogram.cover` in the style.

Do about 5 clips per call, or run it in the background. Outputs:
- `out/<prefix>NN_<title>.mp4`
- `out/qa/*.jpg` contact sheets
- `out/posting.csv` and `out/manifest.json`
- `out/work/<clip>/` containing `cut.mp4`, `clip.json` and `captions.ass`

Single-clip commands are `cut` and `render` (see `-h`).

## 8. QA every clip before delivery

1. **Look at each `out/qa/<clip>.jpg`.** Check that:
   - faces are framed and not cut off, and two-shots got split-screen
   - captions are readable and clear of faces, and the hook box is legible
   - the logo and progress bar sit where the style says
   - there are no black or frozen frames
2. **Proofread captions.** Read the words in `out/work/<clip>/clip.json`. Fix misheard names and terms with `python3 clipkit.py fix out/work/<clip> "wrong words=>right words"`. Then re-render just that clip: `python3 clipkit.py render out/work/<clip> -o out/<clip>.mp4 --style ... --hook "..." --emphasis "a|b"`.
3. **Check the edit.** The first words should be the hook, the ending should land, and the duration should be in range.
4. **Framing problems:** set `"layout"` on that clip in picks.json (`follow` tracks the larger face in a two-shot; `blur` keeps everyone in frame) and re-run with `--only`.

## 9. Deliver and log

1. **Files:** copy the clips, `posting.csv` and the qa sheets to `/mnt/user-data/outputs/<client>_<ep>/` and send them to the user. If a studio folder is connected, also commit them (using the file ids from sending) into `clients/<slug>/deliveries/<date>_<ep>/`. Name files `<client>_<ep>_<NN>_<title>.mp4`, which is what `prefix` produces.
2. **Summary:** give one line per clip (title, length, hook), plus anything that needs the user's call.
3. **Log:** append to `clients/<slug>/clip-log.csv` one row per delivered clip with these columns: `date, episode, file, title, hook, duration, src_start, src_end, status, posted_url, views, notes`. Take the values from `manifest.json`.
4. **Posting:** only after explicit approval. Use the scheduler the user has (for example a Postiz or OmniSocials skill or connector) with the `posting.csv` captions, then update `status` and `posted_url` in the log.

## 10. Get better over time

When the user shares performance numbers per clip (views, watch time, shares) as a paste, a CSV or from an analytics connector:
- Add them to the clip log.
- Write 2–3 concrete lessons into `profile.notes`, for example "debate clips under 35 s beat solo takes 2:1".
- Apply those lessons to the next picks.
- Refresh the trend brief every couple of weeks.

## Running it as a service

- **Onboarding:** profile, reference clips, a style approved on a sample clip, brand assets, vocab list, avoid list, platforms, cadence, delivery folder and approver.
- **Per episode:** intake, trend brief (reuse), transcribe, shortlist, batch, QA, deliver and log.
- **Revisions are cheap.**
  - New hook or emphasis: re-render.
  - New trims: edit start/end and re-run `--only N`.
  - New look: edit the style and re-run.
- **Monthly report:** summarize the clip log for the period (clips delivered and posted, top performers, lessons) as a short doc.
- **Teammates:** to hand the workflow to someone else, share both this skill and the Clip Studio Toolkit page with them.

## Troubleshooting

- **Fingerprint mismatch:** someone republished `clipkit.txt`. Read the page's `version.txt`, tell the user what changed, and when they approve, update the sha256 in this skill.
- **Model download blocked:** ask the user to allow `github.com` and `*.githubusercontent.com` (Settings > Capabilities, or their admin), or transcribe on their computer.
- **Wrong caption font:** the family name must match the font file. Use the `font` command, or check `fc-list : family`, and confirm `fonts_dir` is relative to the style file.
- **Slow:** use `encode.preset: "ultrafast"` for drafts. Clips render one at a time on 2 CPUs.
- **Disk full:** after delivery, delete `runs/*/out/work` and staged episodes.

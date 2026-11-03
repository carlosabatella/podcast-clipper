#!/usr/bin/env python3
"""clipkit - turn podcast episodes into styled vertical clips (Python + ffmpeg).

Commands
  setup [--lang en|eu]                 install deps, fetch models, check ffmpeg/libass
  transcribe MEDIA -o DIR              words.json (word timestamps) + transcript.txt
  cut MEDIA WORDS --start S --end E -o CLIPDIR   tightened clip + clip.json
  render CLIPDIR -o OUT.mp4            reframe, captions, hook, punch-ins, bar, logo, loudness
  batch PICKS.json -o OUTDIR           cut + render every pick, QA sheets, posting.csv
  sheet VIDEO -o SHEET.jpg             contact sheet of frames for QA
  ref VIDEO [VIDEO...] -o DIR          study reference clips (cut rate, loudness, frame sheets)
  font FAMILY -o DIR [--weights 800]   fetch a Google Font as .ttf (via npm @fontsource)
  style [-o FILE]                      print or save the default style preset
  find WORDS "phrase"                  exact start/end times of a phrase in words.json
  fix CLIPDIR "old=>new" [...]         proofread caption words (also works on words.json)
"""
import argparse, bisect, csv, json, os, re, shutil, subprocess, sys, tarfile, tempfile, time
from pathlib import Path

os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")
HOME = Path(os.environ.get("CLIPKIT_HOME", str(Path.home() / ".cache" / "clipkit")))
REL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
ASR = {"en": "sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8",   # English, punctuation + casing
       "eu": "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8"}   # 25 European languages
YUNET = ("https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/"
         "face_detection_yunet/face_detection_yunet_2023mar.onnx")
FILLERS = {"um", "uh", "uhm", "umm", "erm", "er", "ah", "hmm", "mm", "mhm"}

DEFAULT_STYLE = {
    "name": "bold-pop",
    "canvas": {"width": 1080, "height": 1920, "fps": 30},
    "layout": "auto",                      # auto | follow | split | blur | audiogram
    "follow": {"zoom": 1.0, "face_y": 0.38},
    "split": {"enabled": True, "divider_color": "#111111", "divider_px": 8},
    "blur_bg": {"darken": 0.35, "fg_y": 0.5},
    "captions": {"font": "Inter", "weight": 900, "size": 86, "color": "#FFFFFF", "highlight": "#FFD400",
                 "emphasis": "#FFD400", "outline_color": "#000000", "outline": 7, "shadow": 2,
                 "uppercase": True, "punctuation": "drop", "max_words": 3, "max_chars": 16,
                 "y": 0.70, "y_split": 0.50, "y_audiogram": 0.83, "animation": "pop", "linger": 0.25,
                 "mask_words": []},
    "hook": {"enabled": True, "duration": 3.2, "font": "Inter", "weight": 800, "size": 62,
             "color": "#111111", "box_color": "#FFFFFF", "pad": 20, "y": 0.15, "max_chars_line": 22,
             "uppercase": False},
    "punch_in": {"enabled": True, "scale": 1.12, "auto": True, "min_interval": 3.5},
    "progress_bar": {"enabled": True, "color": "#FFD400", "height": 10, "position": "bottom"},
    "logo": {"path": None, "width": 150, "position": "top-right", "margin": 50, "opacity": 0.9},
    "grade": {"contrast": 1.04, "saturation": 1.08, "brightness": 0.0, "lut": None},
    "audio": {"loudness": -14, "music": None, "music_volume": 0.07},
    "cut": {"max_gap": 0.45, "keep_gap": 0.12, "remove_fillers": True, "pad_start": 0.10, "pad_end": 0.35},
    "audiogram": {"background": "#0E0E12", "wave_color": "#FFD400", "cover": None},
    "fonts_dir": None,
    "fixes": {},
    "encode": {"crf": 20, "preset": "veryfast"},
}


# ---------------------------------------------------------------- helpers
def log(*a):
    print(*a, file=sys.stderr, flush=True)


def sh(cmd, **kw):
    r = subprocess.run([str(c) for c in cmd], **kw)
    if r.returncode != 0:
        err = r.stderr if isinstance(r.stderr, str) else (r.stderr or b"").decode(errors="ignore")
        sys.exit(f"[clipkit] command failed: {' '.join(map(str, cmd[:8]))} ...\n{err[-1500:]}")
    return r


def merge(a, b):
    out = dict(a)
    for k, v in (b or {}).items():
        out[k] = merge(a[k], v) if isinstance(v, dict) and isinstance(a.get(k), dict) else v
    return out


def load_style(path):
    if not path:
        return json.loads(json.dumps(DEFAULT_STYLE))
    p = Path(path)
    st = merge(DEFAULT_STYLE, json.loads(p.read_text()))
    st["_dir"] = str(p.resolve().parent)
    return st


def asset(st, value):
    """Resolve an asset path from a style (relative paths are relative to the style file)."""
    if not value:
        return None
    p = Path(value)
    if not p.is_absolute() and st.get("_dir"):
        p = Path(st["_dir"]) / p
    return p if p.exists() else None


def parse_time(v):
    if isinstance(v, (int, float)):
        return float(v)
    parts = [float(x) for x in str(v).strip().split(":")]
    t = 0.0
    for x in parts:
        t = t * 60 + x
    return t


def fmt_ts(t):
    return f"{int(t // 3600):02d}:{int(t % 3600 // 60):02d}:{t % 60:04.1f}"


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-") or "clip"


def norm(w):
    return re.sub(r"[^\w']", "", w.lower())


def hex_bgr(h):
    h = h.lstrip("#")
    return (int(h[4:6], 16), int(h[2:4], 16), int(h[0:2], 16))


def probe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format",
                        str(path)], capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"[clipkit] can't read {path}: {r.stderr.strip()[:300]}")
    j = json.loads(r.stdout)
    streams = j.get("streams", [])
    vs = [s for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")]
    info = {"duration": float(j.get("format", {}).get("duration") or 0), "has_video": bool(vs),
            "has_audio": any(s.get("codec_type") == "audio" for s in streams)}
    if vs:
        v = vs[0]
        w, h = int(v["width"]), int(v["height"])
        rot = 0
        for sd in v.get("side_data_list") or []:
            if "rotation" in sd:
                rot = int(float(sd["rotation"]))
        try:
            rot = int(v.get("tags", {}).get("rotate", rot))
        except ValueError:
            pass
        if abs(rot) % 180 == 90:
            w, h = h, w
        n, d = ((v.get("avg_frame_rate") or "30/1").split("/") + ["1"])[:2]
        fps = float(n) / float(d) if float(d or 0) and float(n or 0) else 30.0
        info.update(width=w, height=h, fps=fps)
    return info


def fetch(url, dest):
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = Path(str(dest) + ".part")
    r = subprocess.run(["curl", "-fsSL", "--retry", "3", "-o", str(part), url])
    if r.returncode or not part.exists():
        sys.exit(f"[clipkit] download failed: {url}\nIf this host is blocked, allow github.com and "
                 "*.githubusercontent.com in Settings > Capabilities (or your admin's settings).")
    part.rename(dest)
    return dest


def ensure_pkgs(extra=()):
    need = []
    for mod, pkg in [("numpy", "numpy"), ("cv2", "opencv-python-headless"), ("sherpa_onnx", "sherpa-onnx==2.0.1"), *extra]:
        try:
            __import__(mod)
        except ImportError:
            need.append(pkg)
    if need:
        log("[clipkit] installing", *need)
        for pkgs in (need, [n.split("==")[0] for n in need]):        # tested versions first, then latest
            cmd = [sys.executable, "-m", "pip", "install", "-q", *pkgs]
            if subprocess.run(cmd + ["--break-system-packages"]).returncode == 0 or subprocess.run(cmd).returncode == 0:
                return
        sys.exit("[clipkit] pip install failed for: " + " ".join(need))


def extract(arc, dest):
    with tarfile.open(arc) as t:
        try:
            t.extractall(dest, filter="data")
        except TypeError:
            t.extractall(dest)


def ensure_asr(lang):
    name = ASR[lang]
    d = HOME / name
    if not (d / "tokens.txt").exists():
        log(f"[clipkit] downloading speech model {name} (~0.5 GB, about a minute)")
        arc = fetch(REL + name + ".tar.bz2", HOME / (name + ".tar.bz2"))
        extract(arc, HOME)
        arc.unlink()
    vad = HOME / "silero_vad.onnx"
    if not vad.exists():
        fetch(REL + "silero_vad.onnx", vad)
    return d, vad


class Faces:
    """YuNet face detector (downloaded once), Haar cascade fallback. Returns (x, y, w, h, score)."""

    def __init__(self, w, h):
        import cv2
        self.cv2, self.yn = cv2, None
        p = HOME / "yunet.onnx"
        if not p.exists():
            try:
                fetch(YUNET, p)
            except SystemExit:
                p = None
        if p and hasattr(cv2, "FaceDetectorYN"):
            try:
                self.yn = cv2.FaceDetectorYN.create(str(p), "", (w, h), 0.6, 0.3, 50)
            except Exception:
                self.yn = None
        if self.yn is None:
            self.hc = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")

    def __call__(self, img):
        if self.yn is not None:
            _, f = self.yn.detect(img)
            return [] if f is None else [(float(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[14])) for r in f]
        g = self.cv2.cvtColor(img, self.cv2.COLOR_BGR2GRAY)
        return [(float(x), float(y), float(w), float(h), 0.9)
                for (x, y, w, h) in self.hc.detectMultiScale(g, 1.1, 5, minSize=(24, 24))]


# ---------------------------------------------------------------- transcribe
def tok2words(r, off):
    toks, ts = list(r.tokens), list(r.timestamps)
    du = list(getattr(r, "durations", []) or [])
    if len(du) != len(toks):
        du = [0.0] * len(toks)
    out = []
    for tok, t, d in zip(toks, ts, du):
        piece = tok.replace("\u2581", " ")
        core = piece.strip()
        if not core:
            continue
        is_punct = not any(ch.isalnum() for ch in core)
        end = off + t + (d if d > 0 else 0.08)
        starts = piece.startswith(" ") or not out or (core[0].isdigit() and out[-1]["w"][-1:].isalpha())
        if starts and not is_punct:
            out.append({"w": core, "s": off + t, "e": end})
        elif out:
            out[-1]["w"] += core
            if not is_punct:
                out[-1]["e"] = max(out[-1]["e"], end)
    return out


def transcript_lines(words):
    lines, cur = [], []
    for w in words:
        if cur and (w["s"] - cur[-1]["e"] > 1.2 or len(cur) >= 30 or cur[-1]["w"][-1:] in ".?!"):
            lines.append(cur)
            cur = []
        cur.append(w)
    if cur:
        lines.append(cur)
    return "\n".join(f"[{fmt_ts(l[0]['s'])}] " + " ".join(w["w"] for w in l) for l in lines) + "\n"


def cmd_transcribe(a):
    ensure_pkgs()
    import numpy as np, sherpa_onnx
    mdir, vad_path = ensure_asr(a.lang)
    info = probe(a.media)
    if not info["has_audio"]:
        sys.exit("[clipkit] that file has no audio")
    t0s = parse_time(a.start) if a.start is not None else 0.0
    t1s = parse_time(a.end) if a.end is not None else info["duration"]
    SR, WIN = 16000, 512
    raw = sh(["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{t0s:.3f}", "-i", a.media, "-t", f"{t1s - t0s:.3f}",
              "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"], capture_output=True).stdout
    x = np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0
    cfg = sherpa_onnx.VadModelConfig()
    s = cfg.silero_vad
    s.model, s.threshold, s.min_silence_duration = str(vad_path), 0.4, 0.3
    s.min_speech_duration, s.max_speech_duration, s.window_size = 0.15, 15.0, WIN
    cfg.sample_rate, cfg.num_threads = SR, 1
    vad = sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=60)
    segs = []

    def drain():
        while not vad.empty():
            f = vad.front
            segs.append((f.start, f.start + len(f.samples)))
            vad.pop()

    for i in range(0, len(x), WIN):          # must be fed one window at a time
        vad.accept_waveform(x[i:i + WIN])
        drain()
    vad.flush()
    drain()
    pad, spans = int(0.15 * SR), []
    for k, (b0, b1) in enumerate(segs):
        lo = (segs[k - 1][1] + b0) // 2 if k else 0
        hi = (b1 + segs[k + 1][0]) // 2 if k + 1 < len(segs) else len(x)
        spans.append((max(lo, b0 - pad), min(hi, b1 + pad)))
    rec = sherpa_onnx.OfflineRecognizer.from_transducer(
        encoder=str(mdir / "encoder.int8.onnx"), decoder=str(mdir / "decoder.int8.onnx"),
        joiner=str(mdir / "joiner.int8.onnx"), tokens=str(mdir / "tokens.txt"),
        model_type="nemo_transducer", num_threads=max(1, os.cpu_count() or 2))
    words, t_start, B = [], time.time(), 6
    for i in range(0, len(spans), B):
        batch, streams = spans[i:i + B], []
        for b0, b1 in batch:
            stream = rec.create_stream()
            stream.accept_waveform(SR, x[b0:b1])
            streams.append(stream)
        rec.decode_streams(streams)
        for (b0, _), stream in zip(batch, streams):
            words += tok2words(stream.result, t0s + b0 / SR)
        if (i // B) % 10 == 0:
            log(f"[clipkit] {batch[-1][1] / SR / 60:.1f} of {len(x) / SR / 60:.1f} min ({time.time() - t_start:.0f}s)")
    words.sort(key=lambda w: w["s"])
    clean = []
    for w in words:
        if clean and w["w"] == clean[-1]["w"] and w["s"] - clean[-1]["s"] < 0.05:
            continue
        clean.append(w)
    for k in range(len(clean) - 1):
        clean[k]["e"] = min(clean[k]["e"], clean[k + 1]["s"])
    for w in clean:
        w["e"] = round(max(w["e"], w["s"] + 0.05), 3)
        w["s"] = round(w["s"], 3)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "words.json").write_text(json.dumps({"media": str(Path(a.media).resolve()), "lang": a.lang,
                                                "start": t0s, "end": t1s, "duration": info["duration"],
                                                "words": clean}))
    (out / "transcript.txt").write_text(transcript_lines(clean))
    print(json.dumps({"words": len(clean), "minutes": round((t1s - t0s) / 60, 1),
                      "seconds_taken": round(time.time() - t_start), "transcript": str(out / "transcript.txt"),
                      "words_json": str(out / "words.json")}))


# ---------------------------------------------------------------- cut
def plan_cut(words, start, end, dur, c, lo=0.0):
    idx = [k for k, w in enumerate(words) if w["e"] > start + 0.02 and w["s"] < end - 0.02]
    keep = [k for k in idx if not (c["remove_fillers"] and norm(words[k]["w"]) in FILLERS)]
    if not keep:
        sys.exit(f"[clipkit] no words between {start:.1f}s and {end:.1f}s")
    i0, i1 = keep[0], keep[-1]
    prev_e = words[i0 - 1]["e"] if i0 > 0 else 0.0
    next_s = words[i1 + 1]["s"] if i1 + 1 < len(words) else dur
    a = max(lo, words[i0]["s"] - c["pad_start"], (prev_e + words[i0]["s"]) / 2 if i0 > 0 else 0.0)
    b = min(dur, words[i1]["e"] + c["pad_end"], (words[i1]["e"] + next_s) / 2 if i1 + 1 < len(words) else dur)
    h, ranges, r0 = c["keep_gap"] / 2, [], a
    for p, q in zip(keep, keep[1:]):
        if words[q]["s"] - words[p]["e"] > c["max_gap"] or q != p + 1:   # long pause, or a filler removed
            ranges.append([r0, words[p]["e"] + h])
            r0 = words[q]["s"] - h
    ranges.append([r0, b])
    merged = []
    for r in ranges:
        if merged and r[0] - merged[-1][1] < 0.04:
            merged[-1][1] = max(merged[-1][1], r[1])
        else:
            merged.append(list(r))
    cw, off = [], 0.0
    for r in merged:
        for k in keep:
            w = words[k]
            if r[0] - 0.001 <= w["s"] < r[1]:
                cw.append({"w": w["w"], "s": round(w["s"] - r[0] + off, 3), "e": round(min(w["e"], r[1]) - r[0] + off, 3)})
        off += r[1] - r[0]
    return merged, cw, off


def render_cut(media, info, ranges, out_file, fps, max_h=1920):
    seek = max(0.0, ranges[0][0] - 3.0)
    v = info["has_video"]
    parts, labels = [], ""
    for k, (r0, r1) in enumerate(ranges):
        s0, s1, d = r0 - seek, r1 - seek, r1 - r0
        if v:
            parts.append(f"[0:v:0]trim=start={s0:.4f}:end={s1:.4f},setpts=PTS-STARTPTS[v{k}]")
        parts.append(f"[0:a:0]atrim=start={s0:.4f}:end={s1:.4f},asetpts=PTS-STARTPTS,"
                     f"afade=t=in:d=0.01,afade=t=out:st={max(0.0, d - 0.01):.4f}:d=0.01[a{k}]")
        labels += (f"[v{k}]" if v else "") + f"[a{k}]"
    parts.append(f"{labels}concat=n={len(ranges)}:v={int(v)}:a=1" + ("[vc][ac]" if v else "[ac]"))
    maps = ["-map", "[ac]"]
    if v:
        sc = f",scale=-2:{max_h}" if info["height"] > max_h else ""      # 4K -> output height: faster, same sharpness
        parts.append(f"[vc]fps={fps}{sc},format=yuv420p[vo]")
        maps = ["-map", "[vo]", "-map", "[ac]"]
    tmp = out_file.with_name("tmp_" + out_file.name)
    cmd = ["ffmpeg", "-v", "error", "-y", "-nostdin", "-ss", f"{seek:.3f}", "-t", f"{ranges[-1][1] - seek + 0.5:.3f}",
           "-i", str(media), "-filter_complex", ";".join(parts), *maps]
    cmd += (["-c:v", "libx264", "-preset", "veryfast", "-crf", "16"] if v else []) + ["-c:a", "aac", "-b:a", "192k", str(tmp)]
    sh(cmd, capture_output=True)
    tmp.replace(out_file)


def do_cut(media, words, start, end, cdir, st, c, media_start=0.0):
    """media_start: source time at which `media` begins (for a pre-cut segment of a long episode)."""
    info = probe(media)
    cdir = Path(cdir)
    cdir.mkdir(parents=True, exist_ok=True)
    ranges, cw, total = plan_cut(words, start, end, media_start + info["duration"], c, lo=media_start)
    f = cdir / ("cut.mp4" if info["has_video"] else "cut.m4a")
    render_cut(media, info, [[r0 - media_start, r1 - media_start] for r0, r1 in ranges], f, st["canvas"]["fps"],
               st["canvas"]["height"])
    meta = {"media": str(Path(media).resolve()), "file": f.name, "src_start": round(ranges[0][0], 3),
            "src_end": round(ranges[-1][1], 3), "ranges": [[round(x, 3) for x in r] for r in ranges],
            "duration": round(total, 3), "words": cw}
    (cdir / "clip.json").write_text(json.dumps(meta, indent=1))
    return meta


def cmd_cut(a):
    st = load_style(a.style)
    c = dict(st["cut"])
    if a.max_gap is not None:
        c["max_gap"] = a.max_gap
    if a.keep_fillers:
        c["remove_fillers"] = False
    if a.no_tighten:
        c["max_gap"] = 1e9
    W = json.loads(Path(a.words).read_text())
    meta = do_cut(a.media or W["media"], W["words"], parse_time(a.start), parse_time(a.end), a.out, st, c,
                  media_start=parse_time(a.media_start or 0))
    print(json.dumps({k: meta[k] for k in ("file", "src_start", "src_end", "duration")} |
                     {"cuts": len(meta["ranges"]) - 1, "text": " ".join(w["w"] for w in meta["words"])}))


# ---------------------------------------------------------------- analysis (cuts + faces -> layout per scene)
def analyze(media, info, st, layout):
    import numpy as np, cv2
    W, H, fps = info["width"], info["height"], st["canvas"]["fps"]
    aw = 640 if W >= H else 360
    ah = int(round(H * aw / W / 2)) * 2
    sc = W / aw
    det, step = Faces(aw, ah), max(1, round(fps / 8))
    p = subprocess.Popen(["ffmpeg", "-v", "error", "-nostdin", "-i", str(media), "-vf", f"fps={fps},scale={aw}:{ah}",
                          "-f", "rawvideo", "-pix_fmt", "bgr24", "-"], stdout=subprocess.PIPE)
    n, prev, cuts, dets, last_cut, fsz = 0, None, [0], {}, 0, aw * ah * 3
    while True:
        buf = p.stdout.read(fsz)
        if len(buf) < fsz:
            break
        f = np.frombuffer(buf, np.uint8).reshape(ah, aw, 3)
        sm = cv2.resize(f, (64, 36), interpolation=cv2.INTER_AREA)
        g = cv2.cvtColor(sm, cv2.COLOR_BGR2GRAY).astype(np.float32)
        hist = cv2.calcHist([cv2.cvtColor(sm, cv2.COLOR_BGR2HSV)], [0, 1], None, [16, 8], [0, 180, 0, 256])
        cv2.normalize(hist, hist, 1, 0, cv2.NORM_L1)
        is_cut = False
        if prev is not None:
            d_pix = float(np.mean(np.abs(g - prev[0]))) / 255.0
            d_hist = float(cv2.compareHist(hist, prev[1], cv2.HISTCMP_BHATTACHARYYA))
            is_cut = (d_pix > 0.08 and d_hist > 0.15) or d_pix > 0.20 or d_hist > 0.40
            if is_cut and n - last_cut >= int(0.4 * fps):
                cuts.append(n)
                last_cut = n
        prev = (g, hist)
        if n % step == 0 or is_cut:
            dets[n] = [(x * sc, y * sc, w * sc, h * sc, s) for (x, y, w, h, s) in det(f)]
        n += 1
    p.wait()
    scenes, bounds = [], cuts + [max(n, 1)]
    for f0, f1 in zip(bounds, bounds[1:]):
        if f1 <= f0:
            continue
        samp = [(k, [d for d in dets[k] if d[4] >= 0.6 and d[3] >= 0.07 * H]) for k in range(f0, f1) if k in dets]
        nf = int(np.median([len(fs) for _, fs in samp]) + 0.5) if samp else 0
        if layout in ("follow", "split", "blur"):
            lay = layout
        else:
            lay = "blur" if nf == 0 else "follow" if nf == 1 else ("split" if nf == 2 and st["split"]["enabled"] else
                                                                    "follow" if nf == 2 else "blur")
        if lay == "split" and nf < 2:
            lay = "follow" if nf == 1 else "blur"
        if lay == "follow" and nf == 0:
            lay = "blur"
        s_ = {"f0": f0, "f1": f1, "layout": lay, "faces": nf}
        if lay == "follow":
            s_["track"] = follow_track(samp, f0, f1, W, H, st)
        elif lay == "split":
            s_["panels"] = split_panels(samp, W, H, st)
            if not s_["panels"]:
                s_["layout"], s_["track"] = "follow", follow_track(samp, f0, f1, W, H, st)
        scenes.append(s_)
    return scenes, n


def follow_track(samp, f0, f1, W, H, st):
    import numpy as np
    keys, xs, ys, hs, last = [], [], [], [], None
    for k, fs in samp:
        if not fs:
            continue
        big = max(fs, key=lambda d: d[3])
        if last is not None:            # stay on the same person unless another face is much bigger
            near = min(fs, key=lambda d: abs(d[0] + d[2] / 2 - last))
            if near[3] >= 0.75 * big[3]:
                big = near
        last = big[0] + big[2] / 2
        keys.append(k), xs.append(last), ys.append(big[1] + big[3] / 2), hs.append(big[3])
    if not keys:
        return {"static": True, "cx": W / 2, "cy": H / 2}
    xs = np.array(xs)
    cw = H * 9 / 16 / st["follow"]["zoom"]
    if np.percentile(xs, 90) - np.percentile(xs, 10) < 0.15 * cw:   # subject barely moves: lock the frame
        return {"static": True, "cx": float(np.median(xs)), "cy": float(np.median(ys))}
    fr = np.arange(f0, f1)
    cx, cy = np.interp(fr, keys, xs), np.interp(fr, keys, ys)
    for arr in (cx, cy):                 # zero-phase smoothing (~0.5 s)
        for i in range(1, len(arr)):
            arr[i] = arr[i - 1] + 0.06 * (arr[i] - arr[i - 1])
        for i in range(len(arr) - 2, -1, -1):
            arr[i] = arr[i + 1] + 0.06 * (arr[i] - arr[i + 1])
    return {"static": False, "f0": f0, "cx": cx.tolist(), "cy": cy.tolist()}


def split_panels(samp, W, H, st):
    import numpy as np
    L, R = [], []
    for _, fs in samp:
        if len(fs) >= 2:
            two = sorted(sorted(fs, key=lambda d: -d[3])[:2], key=lambda d: d[0])
            L.append(two[0])
            R.append(two[1])
    if not L:
        return []
    OW, OH = st["canvas"]["width"], st["canvas"]["height"]
    asp, panels = OW / (OH / 2), []
    for side in (L, R):
        cx = float(np.median([d[0] + d[2] / 2 for d in side]))
        cy = float(np.median([d[1] + d[3] / 2 for d in side]))
        fh = float(np.median([d[3] for d in side]))
        ph = min(H, max(0.35 * H, 2.8 * fh))
        pw = ph * asp
        if pw > W:
            pw, ph = W, W / asp
        x0, y0 = min(max(0, cx - pw / 2), W - pw), min(max(0, cy - 0.45 * ph), H - ph)
        panels.append((x0, y0, pw, ph))
    return panels


# ---------------------------------------------------------------- captions (ASS)
def ass_color(hexc, alpha=0):
    b, g, r = hex_bgr(hexc)
    return f"&H{alpha:02X}{b:02X}{g:02X}{r:02X}"


def ass_t(t):
    cs = int(round(max(0.0, t) * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def paginate(words, c):
    pages, cur = [], []
    for k, w in enumerate(words):
        if cur:
            chars = sum(len(x["w"]) + 1 for x in cur) + len(w["w"])
            end = cur[-1]["w"][-1:]
            too_long = len(cur) >= c["max_words"] or chars > c["max_chars"]
            orphan = w["w"][-1:] in ".?!" and len(cur) <= c["max_words"] and chars <= c["max_chars"] + 6
            if ((too_long and not orphan) or w["s"] - cur[-1]["e"] > 0.6
                    or end in ".?!" or (end == "," and len(cur) >= 2)):
                pages.append(cur)
                cur = []
        cur.append(dict(w, i=k))
    if cur:
        pages.append(cur)
    return pages


def wrap(text, n):
    lines, cur = [], ""
    for w in text.split():
        if cur and len(cur) + 1 + len(w) > n:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        lines.append(cur)
    return "\\N".join(lines)


def build_ass(words, dur, st, hook, lay_at, emph):
    c, hk = st["captions"], st["hook"]
    OW, OH = st["canvas"]["width"], st["canvas"]["height"]
    mask = {norm(m) for m in c.get("mask_words") or []}

    def disp(w):
        t = w["w"].replace("{", "").replace("}", "").replace("\\", "")
        if c.get("punctuation", "drop") == "drop":
            t = re.sub(r"[.,;:]+$", "", t)
        if norm(t) in mask and len(t) > 1:
            t = t[0] + "*" * (len(t) - 1)
        return t.upper() if c.get("uppercase") else t

    prim, hl, em = ass_color(c["color"]), ass_color(c["highlight"]), ass_color(c.get("emphasis") or c["highlight"])

    def plain(w):
        return ("{\\c" + em + "}" + disp(w) + "{\\c" + prim + "}") if w["i"] in emph["idx"] else disp(w)

    def active(w):
        if c["animation"] == "pop":
            return ("{\\c" + hl + "\\fscx125\\fscy125\\t(0,90,\\fscx112\\fscy112)}" + disp(w) +
                    "{\\c" + prim + "\\fscx100\\fscy100}")
        if c["animation"] == "word":
            return "{\\fscx118\\fscy118\\t(0,80,\\fscx100\\fscy100)}" + plain(w)
        return "{\\c" + hl + "}" + disp(w) + "{\\c" + prim + "}"

    def sline(name, font, size, p, s, o, b, outline, shadow, border):
        return (f"Style: {name},{font},{size},{p},{s},{o},{b},-1,0,0,0,100,100,0,0,{border},{outline},{shadow},"
                f"5,40,40,40,1")

    hdr = ["[Script Info]", "ScriptType: v4.00+", f"PlayResX: {OW}", f"PlayResY: {OH}", "WrapStyle: 0",
           "ScaledBorderAndShadow: yes", "YCbCr Matrix: TV.709", "", "[V4+ Styles]",
           "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
           "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
           "MarginL, MarginR, MarginV, Encoding",
           sline("Cap", c["font"], c["size"], prim, prim, ass_color(c["outline_color"]),
                 ass_color(c["outline_color"], 0x70), c["outline"], c["shadow"], 1),
           sline("CapK", c["font"], c["size"], hl, prim, ass_color(c["outline_color"]),
                 ass_color(c["outline_color"], 0x70), c["outline"], c["shadow"], 1),
           sline("Hook", hk["font"], hk["size"], ass_color(hk["color"]), ass_color(hk["color"]),
                 ass_color(hk["box_color"]), ass_color(hk["box_color"]), hk.get("pad", 18), 0, 3),
           "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    ev, wt = [], "\\b" + str(int(c.get("weight", 700)))
    pages = paginate(words, c)
    for pi, pg in enumerate(pages):
        p0 = pg[0]["s"]
        nxt = pages[pi + 1][0]["s"] if pi + 1 < len(pages) else dur
        p1 = min(pg[-1]["e"] + c["linger"], nxt, dur)
        lay = lay_at(p0)
        y = OH * (c["y_split"] if lay == "split" else c.get("y_audiogram", c["y"]) if lay == "audiogram" else c["y"])
        pos = "{\\an5\\pos(" + f"{OW // 2},{int(y)}" + ")" + wt + "}"
        if c["animation"] == "karaoke":
            text, prev = "", p0
            for w in pg:
                gap = int(round((w["s"] - prev) * 100))
                text += ("{\\k" + str(gap) + "}" if gap > 0 else "") + "{\\kf" + str(max(1, int(round((w["e"] - w["s"]) * 100)))) + "}" + disp(w) + " "
                prev = w["e"]
            ev.append((p0, p1, "CapK", pos + text.strip()))
        elif c["animation"] == "static":
            ev.append((p0, p1, "Cap", pos + " ".join(plain(w) for w in pg)))
        else:
            for j, w in enumerate(pg):
                t0 = p0 if j == 0 else w["s"]
                t1 = pg[j + 1]["s"] if j + 1 < len(pg) else p1
                text = active(w) if c["animation"] == "word" else " ".join(active(x) if x is w else plain(x) for x in pg)
                ev.append((t0, t1, "Cap", pos + text))
    if hook and hk.get("enabled", True):
        hd = dur if hk["duration"] <= 0 else min(hk["duration"], dur)
        txt = re.sub(r"[{}\\]", "", hook)
        txt = wrap(txt.upper() if hk.get("uppercase") else txt, hk["max_chars_line"])
        ev.append((0.0, hd, "Hook", "{\\an5\\pos(" + f"{OW // 2},{int(OH * hk['y'])}" + ")\\b" +
                   str(int(hk.get("weight", 800))) + "\\fad(120,180)}" + txt))
    body = [f"Dialogue: {1 if s == 'Hook' else 0},{ass_t(t0)},{ass_t(t1)},{s},,0,0,0,,{txt}" for t0, t1, s, txt in ev if t1 > t0]
    return "\n".join(hdr + body) + "\n"


def find_emphasis(words, phrases):
    nw, idx, starts = [norm(w["w"]) for w in words], set(), []
    for ph in phrases or []:
        toks = [norm(t) for t in ph.split() if norm(t)]
        for i in range(len(nw) - len(toks) + 1):
            if toks and nw[i:i + len(toks)] == toks:
                idx.update(range(i, i + len(toks)))
                starts.append(words[i]["s"])
    return {"idx": idx, "starts": sorted(starts)}


def apply_fixes(words, fixes):
    if not fixes:
        return words
    low = {k.lower(): v for k, v in fixes.items()}
    out = []
    for w in words:
        m = re.match(r"^(.*?)([.,!?;:]*)$", w["w"])
        core, tail = m.group(1), m.group(2)
        out.append(dict(w, w=low.get(core.lower(), core) + tail))
    return out


def zoom_fn(words, dur, st, emph):
    P = st["punch_in"]
    if not P["enabled"] or not words:
        return lambda t: 1.0
    sents = [words[k]["s"] for k in range(1, len(words)) if words[k - 1]["w"][-1:] in ".?!"]
    ch = []
    if emph["starts"]:
        for t in emph["starts"]:
            after = [s for s in sents if s > t + 1.0]
            ch += [(t, P["scale"]), (after[0] if after else min(dur, t + 2.5), 1.0)]
    elif P.get("auto", True):
        last, state = 0.0, 1.0
        for t in sents:
            if t - last >= P["min_interval"]:
                state = P["scale"] if state == 1.0 else 1.0
                ch.append((t, state))
                last = t
    ch.sort()
    ts, vs = [x[0] for x in ch], [x[1] for x in ch]

    def f(t):
        i = bisect.bisect_right(ts, t) - 1
        return vs[i] if i >= 0 else 1.0
    return f


# ---------------------------------------------------------------- render
def prepare_fonts(st, cdir):
    fd = asset(st, st.get("fonts_dir")) or (HOME / "fonts" if (HOME / "fonts").is_dir() else None)
    if fd and fd.is_dir():
        dst = Path(cdir) / "fonts"
        dst.mkdir(exist_ok=True)
        for f in fd.iterdir():
            if f.suffix.lower() in (".ttf", ".otf"):
                shutil.copy(f, dst / f.name)
        return dst
    return None


def frame_follow(fr, s_, fi, z, k, st, OW, OH):
    import cv2
    dh, dw = fr.shape[:2]
    tr = s_["track"]
    if tr["static"]:
        cx, cy = tr["cx"] * k, tr["cy"] * k
    else:
        j = min(max(fi - tr["f0"], 0), len(tr["cx"]) - 1)
        cx, cy = tr["cx"][j] * k, tr["cy"][j] * k
    ch = dh / (st["follow"]["zoom"] * z)
    cw = ch * OW / OH
    if cw > dw:
        cw, ch = dw, dw * OH / OW
    x0 = min(max(0.0, cx - cw / 2), dw - cw)
    y0 = 0.0 if ch >= dh - 1 else min(max(0.0, cy - st["follow"]["face_y"] * ch), dh - ch)
    crop = fr[int(y0):int(y0 + ch), int(x0):int(x0 + cw)]
    return cv2.resize(crop, (OW, OH), interpolation=cv2.INTER_CUBIC if crop.shape[0] < OH else cv2.INTER_AREA)


def frame_split(fr, s_, k, st, OW, OH):
    import numpy as np, cv2
    ph_out, outs = OH // 2, []
    for (x0, y0, pw, ph) in s_["panels"]:
        x0, y0, pw, ph = int(x0 * k), int(y0 * k), int(pw * k), int(ph * k)
        outs.append(cv2.resize(fr[y0:y0 + ph, x0:x0 + pw], (OW, ph_out), interpolation=cv2.INTER_CUBIC))
    img = np.vstack(outs)
    if img.shape[0] < OH:
        img = np.vstack([img, np.zeros((OH - img.shape[0], OW, 3), np.uint8)])
    d = st["split"]["divider_px"]
    if d:
        img[ph_out - d // 2: ph_out + d - d // 2] = hex_bgr(st["split"]["divider_color"])
    return img


def frame_blur(fr, z, st, OW, OH):
    import numpy as np, cv2
    dh, dw = fr.shape[:2]
    tw, th = OW // 8, OH // 8
    s = max(tw / dw, th / dh)
    small = cv2.resize(fr, (max(tw, int(dw * s) + 1), max(th, int(dh * s) + 1)), interpolation=cv2.INTER_AREA)
    oy, ox = (small.shape[0] - th) // 2, (small.shape[1] - tw) // 2
    bg = cv2.resize(cv2.GaussianBlur(small[oy:oy + th, ox:ox + tw], (0, 0), 4), (OW, OH))
    bg = (bg.astype(np.float32) * (1 - st["blur_bg"]["darken"])).astype(np.uint8)
    if z > 1.001:
        ch, cw = dh / z, dw / z
        fr = fr[int((dh - ch) / 2):int((dh + ch) / 2), int((dw - cw) / 2):int((dw + cw) / 2)]
    fit = min(OW / fr.shape[1], OH / fr.shape[0])
    fw, fh = int(fr.shape[1] * fit) // 2 * 2, int(fr.shape[0] * fit) // 2 * 2
    fg = cv2.resize(fr, (fw, fh), interpolation=cv2.INTER_CUBIC if fit > 1 else cv2.INTER_AREA)
    y = min(max(0, int(st["blur_bg"]["fg_y"] * OH - fh / 2)), OH - fh)
    x = (OW - fw) // 2
    bg[y:y + fh, x:x + fw] = fg
    return bg


def load_logo(st, OW, OH):
    import numpy as np, cv2
    L = st["logo"]
    p = asset(st, L.get("path"))
    if not p:
        return None
    im = cv2.imread(str(p), cv2.IMREAD_UNCHANGED)
    if im is None:
        return None
    if im.ndim == 2:
        im = cv2.cvtColor(im, cv2.COLOR_GRAY2BGR)
    if im.shape[2] == 3:
        im = np.dstack([im, np.full(im.shape[:2], 255, np.uint8)])
    w = int(L["width"])
    h = int(im.shape[0] * w / im.shape[1])
    im = cv2.resize(im, (w, h), interpolation=cv2.INTER_AREA)
    m, pos = int(L["margin"]), L["position"]
    x = m if "left" in pos else OW - w - m
    y = m if "top" in pos else OH - h - m
    return x, y, im[:, :, :3].astype(np.float32), im[:, :, 3:4].astype(np.float32) / 255.0 * float(L["opacity"])


def loudnorm(src, A, cwd=None):
    """Two-pass loudness: measure, then normalise linearly to the target (falls back to one pass)."""
    base = f"loudnorm=I={A['loudness']}:TP=-1.5:LRA=11"
    r = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-i", str(src), "-vn", "-af", base + ":print_format=json",
                        "-f", "null", "-"], capture_output=True, text=True, cwd=cwd)
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", r.stderr, re.S)
    try:
        j = json.loads(m.group(0))
        if float(j["input_i"]) < -60:
            return base
        return (base + f":measured_I={j['input_i']}:measured_TP={j['input_tp']}:measured_LRA={j['input_lra']}"
                f":measured_thresh={j['input_thresh']}:offset={j['target_offset']}:linear=true")
    except (AttributeError, ValueError, KeyError):
        return base


def encoder_cmd(st, clipfile, fontsdir, tmp, OW, OH, fps, cdir):
    g, A, E = st["grade"] or {}, st["audio"], st["encode"]
    vf = []
    if g and (g.get("contrast", 1) != 1 or g.get("saturation", 1) != 1 or g.get("brightness", 0) != 0):
        vf.append(f"eq=contrast={g.get('contrast', 1)}:saturation={g.get('saturation', 1)}:brightness={g.get('brightness', 0)}")
    lut = asset(st, g.get("lut")) if g else None
    if lut:
        shutil.copy(lut, Path(cdir) / "grade.cube")
        vf.append("lut3d=file=grade.cube")
    vf += ["ass=captions.ass" + (":fontsdir=fonts" if fontsdir else ""), "format=yuv420p"]
    music = asset(st, A.get("music"))
    cmd = ["ffmpeg", "-v", "error", "-y", "-nostdin", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{OW}x{OH}",
           "-r", str(fps), "-i", "-", "-i", clipfile]
    if music:
        cmd += ["-stream_loop", "-1", "-i", str(music)]
    fc = [f"[0:v]{','.join(vf)}[v]",
          f"[1:a]{loudnorm(clipfile, A, cwd=str(cdir))},aresample=48000" + ("[vo]" if music else "[a]")]
    if music:
        fc += [f"[2:a]volume={A['music_volume']},aresample=48000[m]",
               "[vo][m]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]"]
    return cmd + ["-filter_complex", ";".join(fc), "-map", "[v]", "-map", "[a]", "-c:v", "libx264",
                  "-preset", E["preset"], "-crf", str(E["crf"]), "-c:a", "aac", "-b:a", "192k",
                  "-movflags", "+faststart", "-shortest", str(tmp)]


def render_audiogram(cdir, clipfile, dur, st, out, fontsdir, cover):
    A, bar, E = st["audiogram"], st["progress_bar"], st["encode"]
    OW, OH, fps = st["canvas"]["width"], st["canvas"]["height"], st["canvas"]["fps"]
    cov = Path(cover).resolve() if cover else asset(st, A.get("cover"))
    hx = lambda h: "0x" + h.lstrip("#")
    inputs = ["-f", "lavfi", "-i", f"color=c={hx(A['background'])}:s={OW}x{OH}:r={fps}:d={dur:.3f}"]
    fc, base = [], "[0:v]"
    if cov:
        inputs += ["-loop", "1", "-framerate", str(fps), "-t", f"{dur:.3f}", "-i", str(cov)]
        art = int(OW * 0.7)
        fc += [f"[1:v]scale={art}:{art}:force_original_aspect_ratio=increase,crop={art}:{art},format=rgba[art]",
               f"[0:v][art]overlay=(W-w)/2:{int(OH * 0.2)}[b1]"]
        base = "[b1]"
    inputs += ["-i", clipfile]
    ai = 2 if cov else 1
    fc += [f"[{ai}:a]asplit=2[aw][aa]",
           f"[aw]showwaves=s={OW}x{int(OH * 0.12)}:mode=cline:rate={fps}:colors={hx(A['wave_color'])}"
           f":draw=full:scale=sqrt[wv]",
           f"{base}[wv]overlay=0:{int(OH * 0.6)}[b2]"]
    base = "[b2]"
    if bar["enabled"]:
        by = OH - bar["height"] if bar["position"] == "bottom" else 0
        fc += [f"color=c={hx(bar['color'])}:s={OW}x{bar['height']}:r={fps}[bar]",
               f"[b2][bar]overlay=x='-w+w*t/{dur:.3f}':y={by}:shortest=1[b3]"]
        base = "[b3]"
    fc += [f"{base}ass=captions.ass" + (":fontsdir=fonts" if fontsdir else "") + ",format=yuv420p[v]",
           f"[aa]{loudnorm(clipfile, st['audio'], cwd=str(cdir))},aresample=48000[a]"]
    tmp = out.with_name("tmp_" + out.name)
    sh(["ffmpeg", "-v", "error", "-y", "-nostdin", *inputs, "-filter_complex", ";".join(fc), "-map", "[v]",
        "-map", "[a]", "-c:v", "libx264", "-preset", E["preset"], "-crf", str(E["crf"]), "-c:a", "aac",
        "-b:a", "192k", "-movflags", "+faststart", "-shortest", str(tmp)], cwd=str(cdir), capture_output=True)
    tmp.replace(out)
    return {"layouts": ["audiogram"]}


def do_render(cdir, st, out, hook=None, emphasis=None, layout=None, cover=None):
    import numpy as np, cv2
    cdir = Path(cdir).resolve()
    C = json.loads((cdir / "clip.json").read_text())
    media = cdir / C["file"]
    info = probe(media)
    dur = C["duration"]
    OW, OH, fps = st["canvas"]["width"], st["canvas"]["height"], st["canvas"]["fps"]
    words = apply_fixes(C["words"], st.get("fixes"))
    emph = find_emphasis(words, emphasis)
    layout = layout or st["layout"]
    out = Path(out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    fontsdir = prepare_fonts(st, cdir)
    if not info["has_video"] or layout == "audiogram":
        (cdir / "captions.ass").write_text(build_ass(words, dur, st, hook, lambda t: "audiogram", emph))
        return render_audiogram(cdir, C["file"], dur, st, out, fontsdir, cover)
    scenes, _ = analyze(media, info, st, layout)

    def lay_at(t):
        fi = int(t * fps)
        for s_ in scenes:
            if s_["f0"] <= fi < s_["f1"]:
                return s_["layout"]
        return scenes[-1]["layout"]

    (cdir / "captions.ass").write_text(build_ass(words, dur, st, hook, lay_at, emph))
    zoom = zoom_fn(words, dur, st, emph)
    W, H = info["width"], info["height"]
    if H > OH:                                   # decode big sources at output height (keeps crops sharp, saves time)
        dh, dw = OH, int(round(W * OH / H / 2)) * 2
    else:
        dh, dw = H, W
    k = dh / H
    vf = f"fps={fps}" + (f",scale={dw}:{dh}" if dh != H else "")
    dec = subprocess.Popen(["ffmpeg", "-v", "error", "-nostdin", "-i", str(media), "-vf", vf, "-f", "rawvideo",
                            "-pix_fmt", "bgr24", "-"], stdout=subprocess.PIPE)
    tmp = out.with_name("tmp_" + out.name)
    enc = subprocess.Popen(encoder_cmd(st, C["file"], fontsdir, tmp, OW, OH, fps, cdir), stdin=subprocess.PIPE,
                           cwd=str(cdir))
    logo, bar = load_logo(st, OW, OH), st["progress_bar"]
    total, fi, si, fsz = max(1, int(round(dur * fps))), 0, 0, dw * dh * 3
    try:
        while True:
            buf = dec.stdout.read(fsz)
            if len(buf) < fsz:
                break
            fr = np.frombuffer(buf, np.uint8).reshape(dh, dw, 3)
            while si + 1 < len(scenes) and fi >= scenes[si]["f1"]:
                si += 1
            s_ = scenes[si]
            z = zoom(fi / fps)
            if s_["layout"] == "follow":
                img = frame_follow(fr, s_, fi, z, k, st, OW, OH)
            elif s_["layout"] == "split":
                img = frame_split(fr, s_, k, st, OW, OH)
            else:
                img = frame_blur(fr, z, st, OW, OH)
            if bar["enabled"]:
                y0 = OH - bar["height"] if bar["position"] == "bottom" else 0
                img[y0:y0 + bar["height"], :int(OW * min(1.0, (fi + 1) / total))] = hex_bgr(bar["color"])
            if logo:
                x, y, rgb, al = logo
                h_, w_ = rgb.shape[:2]
                roi = img[y:y + h_, x:x + w_].astype(np.float32)
                img[y:y + h_, x:x + w_] = (roi * (1 - al) + rgb * al).astype(np.uint8)
            enc.stdin.write(np.ascontiguousarray(img).tobytes())
            fi += 1
    finally:
        enc.stdin.close()
        dec.wait()
        rc = enc.wait()
    if rc != 0 or not tmp.exists():
        sys.exit(f"[clipkit] encoder failed for {out.name}")
    tmp.replace(out)
    return {"layouts": sorted({s_["layout"] for s_ in scenes}), "scenes": len(scenes), "frames": fi}


def cmd_render(a):
    st = load_style(a.style)
    emphasis = [e.strip() for e in (a.emphasis or "").split("|") if e.strip()]
    res = do_render(a.clip, st, a.out, hook=a.hook, emphasis=emphasis, layout=a.layout, cover=a.cover)
    print(json.dumps(dict(res, out=str(Path(a.out).resolve()))))


# ---------------------------------------------------------------- QA, batch, reference, fonts
def make_sheet(video, out, n=6, cols=3, width=360, times=None):
    import numpy as np, cv2
    d = probe(video)["duration"]
    times = times or [d * (j + 0.5) / n for j in range(n)]
    ims = []
    for t in times:
        r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1",
                            "-vf", f"scale={width}:-2", "-f", "image2pipe", "-vcodec", "png", "-"], capture_output=True)
        im = cv2.imdecode(np.frombuffer(r.stdout, np.uint8), cv2.IMREAD_COLOR) if r.stdout else None
        if im is None:
            continue
        for col, th in (((0, 0, 0), 4), ((255, 255, 255), 2)):
            cv2.putText(im, f"{t:.1f}s", (8, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, col, th)
        ims.append(im)
    if not ims:
        sys.exit(f"[clipkit] no frames from {video}")
    hmax = max(i.shape[0] for i in ims)
    ims = [np.vstack([i, np.zeros((hmax - i.shape[0], i.shape[1], 3), np.uint8)]) if i.shape[0] < hmax else i for i in ims]
    blank = np.zeros_like(ims[0])
    rows = [np.hstack(ims[i:i + cols] + [blank] * (cols - len(ims[i:i + cols]))) for i in range(0, len(ims), cols)]
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), np.vstack(rows), [cv2.IMWRITE_JPEG_QUALITY, 85])
    return str(out)


def cmd_sheet(a):
    times = [parse_time(t) for t in a.times.split(",")] if a.times else None
    print(make_sheet(a.video, a.out, n=a.n, cols=a.cols, width=a.width, times=times))


def cmd_batch(a):
    P = json.loads(Path(a.picks).read_text())
    clips = P["clips"] if isinstance(P, dict) else P
    meta = P if isinstance(P, dict) else {}
    W = json.loads(Path(a.words or meta["words"]).read_text())
    media = a.media or meta.get("media") or W["media"]
    st = load_style(a.style or meta.get("style"))
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    prefix = a.prefix if a.prefix is not None else meta.get("prefix", "")
    only = {int(x) for x in a.only.split(",")} if a.only else None
    rows = []
    for n, c in enumerate(clips, 1):
        if only and n not in only:
            continue
        name = f"{prefix}{n:02d}_{slugify(c.get('title') or f'clip-{n}')[:40]}"
        t0 = time.time()
        try:
            cc = merge(st["cut"], c.get("cut") or {})
            m = do_cut(c.get("media") or media, W["words"], parse_time(c["start"]), parse_time(c["end"]),
                       out / "work" / name, st, cc, media_start=parse_time(c.get("media_start", 0)))
            final = out / f"{name}.mp4"
            res = do_render(out / "work" / name, st, final, hook=c.get("hook"), emphasis=c.get("emphasis"),
                            layout=c.get("layout"), cover=a.cover)
            sheet = make_sheet(final, out / "qa" / f"{name}.jpg")
            rows.append({"n": n, "file": final.name, "title": c.get("title", ""), "hook": c.get("hook", ""),
                         "caption": c.get("caption", ""), "hashtags": " ".join(c.get("hashtags", [])),
                         "duration": round(probe(final)["duration"], 1), "src_start": fmt_ts(m["src_start"]),
                         "src_end": fmt_ts(m["src_end"]), "layouts": "+".join(res.get("layouts", [])),
                         "qa_sheet": sheet, "status": "ok"})
            log(f"[clipkit] {final.name} ({rows[-1]['duration']}s, {rows[-1]['layouts']}) in {time.time() - t0:.0f}s")
        except (SystemExit, Exception) as e:
            rows.append({"n": n, "file": "", "title": c.get("title", ""), "status": f"failed: {e}"})
            log(f"[clipkit] clip {n} failed: {e}")
    man = out / "manifest.json"
    if only and man.exists():                       # partial re-run: keep the other clips' rows
        done = {r["n"] for r in rows}
        rows = sorted([r for r in json.loads(man.read_text()) if r.get("n") not in done] + rows, key=lambda r: r["n"])
    keys = ["n", "file", "title", "hook", "caption", "hashtags", "duration", "src_start", "src_end", "layouts",
            "qa_sheet", "status"]
    with open(out / "posting.csv", "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=keys)
        wr.writeheader()
        wr.writerows([{k: r.get(k, "") for k in keys} for r in rows])
    man.write_text(json.dumps(rows, indent=1))
    print(json.dumps(rows, indent=1))


def cmd_ref(a):
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    res = []
    for v in a.videos:
        info = probe(v)
        d = info["duration"]
        r = subprocess.run(["ffmpeg", "-nostdin", "-i", v, "-vf", "select='gt(scene,0.3)',showinfo", "-an", "-f",
                            "null", "-"], capture_output=True, text=True)
        cuts = [round(float(x), 2) for x in re.findall(r"pts_time:([0-9.]+)", r.stderr)]
        r2 = subprocess.run(["ffmpeg", "-nostdin", "-i", v, "-af", "ebur128", "-vn", "-f", "null", "-"],
                            capture_output=True, text=True)
        lu = re.findall(r"I:\s+(-?[0-9.]+) LUFS", r2.stderr)
        stem = slugify(Path(v).stem)
        hook_t = [0.25 + 0.5 * j for j in range(12) if 0.25 + 0.5 * j < d]
        res.append({"file": v, "duration": round(d, 1), "size": f"{info.get('width')}x{info.get('height')}",
                    "fps": round(info.get("fps", 0), 2), "cuts": cuts,
                    "cuts_per_min": round(len(cuts) / max(d / 60, 1e-6), 1),
                    "avg_shot_s": round(d / (len(cuts) + 1), 2), "loudness_lufs": float(lu[-1]) if lu else None,
                    "hook_sheet": make_sheet(v, out / f"{stem}_first6s.jpg", cols=4, width=300, times=hook_t),
                    "overview_sheet": make_sheet(v, out / f"{stem}_overview.jpg", n=12, cols=4, width=300)})
    print(json.dumps(res, indent=1))


def cmd_font(a):
    ensure_pkgs([("fontTools", "fonttools"), ("brotli", "brotli")])
    from fontTools.ttLib import TTFont
    slug = slugify(a.family)
    r = subprocess.run(["curl", "-fsSL", f"https://registry.npmjs.org/@fontsource/{slug}/latest"],
                       capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"[clipkit] '{a.family}' isn't on Fontsource (Google Fonts). Ask the client for the .ttf/.otf files.")
    tmp = Path(tempfile.mkdtemp())
    extract(fetch(json.loads(r.stdout)["dist"]["tarball"], tmp / "f.tgz"), tmp)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    made = []
    for wt in a.weights.split(","):
        cands = sorted((tmp / "package" / "files").glob(f"{slug}-{a.subset}-{wt.strip()}-normal.woff2"))
        if not cands:
            have = sorted({p.name.split("-")[-2] for p in (tmp / "package" / "files").glob(f"{slug}-{a.subset}-*-normal.woff2")})
            log(f"[clipkit] weight {wt} not available for {a.family}; available: {', '.join(have)}")
            continue
        f = TTFont(str(cands[0]))
        f.flavor = None
        w = int(wt)
        sub = {100: "Thin", 200: "ExtraLight", 300: "Light", 400: "Regular", 500: "Medium", 600: "SemiBold",
               700: "Bold", 800: "ExtraBold", 900: "Black"}.get(w, str(w))
        name = f["name"]                     # clean names so libass finds it as "<Family>" + weight
        name.names = [r for r in name.names if r.nameID not in (1, 2, 4, 6, 16, 17)]
        for nid, val in ((1, a.family), (2, "Regular"), (4, f"{a.family} {sub}"),
                         (6, f"{a.family.replace(' ', '')}-{sub}"), (16, a.family), (17, sub)):
            name.setName(val, nid, 3, 1, 0x409)
        f["OS/2"].usWeightClass = w
        dst = out / f"{slug}-{w}.ttf"
        f.save(str(dst))
        made.append({"file": str(dst), "family": a.family, "weight": w})
    shutil.rmtree(tmp, ignore_errors=True)
    print(json.dumps(made, indent=1))


def cmd_find(a):
    W = json.loads(Path(a.words).read_text())["words"]
    nw, hits = [norm(w["w"]) for w in W], []
    toks = [norm(t) for t in a.phrase.split() if norm(t)]
    for i in range(len(nw) - len(toks) + 1):
        if toks and nw[i:i + len(toks)] == toks:
            j = i + len(toks) - 1
            hits.append({"start": W[i]["s"], "end": W[j]["e"], "at": fmt_ts(W[i]["s"]),
                         "context": " ".join(w["w"] for w in W[max(0, i - 6):j + 7])})
    print(json.dumps(hits, indent=1))


def cmd_fix(a):
    """Proofread captions: replace word sequences ("old words=>new words") in clip.json or words.json."""
    p = Path(a.target)
    p = p / "clip.json" if p.is_dir() else p
    J = json.loads(p.read_text())
    W = J["words"]
    bak = p.with_name(p.stem + ".orig.json")
    if not bak.exists():
        bak.write_text(json.dumps(J))
    for rule in a.rules:
        if "=>" not in rule:
            sys.exit(f'[clipkit] rule must look like "old words=>new words": {rule}')
        old, new = rule.split("=>", 1)
        ot, nt, i, count = [norm(t) for t in old.split() if norm(t)], new.split(), 0, 0
        while ot and i <= len(W) - len(ot):
            if [norm(w["w"]) for w in W[i:i + len(ot)]] == ot:
                s0, e0, t, rep = W[i]["s"], W[i + len(ot) - 1]["e"], W[i]["s"], []
                tot = sum(len(x) for x in nt) or 1
                for tok in nt:
                    d = (e0 - s0) * len(tok) / tot
                    rep.append({"w": tok, "s": round(t, 3), "e": round(t + d, 3)})
                    t += d
                W[i:i + len(ot)] = rep
                i += max(1, len(rep))
                count += 1
            else:
                i += 1
        log(f"[clipkit] {old.strip()!r} -> {new.strip()!r}: {count} replaced")
    J["words"] = W
    p.write_text(json.dumps(J, indent=1 if "ranges" in J else None))
    if p.name == "words.json":
        (p.parent / "transcript.txt").write_text(transcript_lines(W))
    print(" ".join(w["w"] for w in W) if len(W) <= 300 else f"{len(W)} words saved")


def cmd_setup(a):
    ensure_pkgs()
    ok = {"ffmpeg": bool(shutil.which("ffmpeg")), "ffprobe": bool(shutil.which("ffprobe"))}
    if ok["ffmpeg"]:
        flt = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True).stdout
        ok["libass"] = " ass " in flt
    d, vad = ensure_asr(a.lang)
    Faces(640, 360)
    fams = subprocess.run(["fc-list", ":", "family"], capture_output=True, text=True).stdout
    if not re.search(r"^Inter\b", fams, re.M) and not (HOME / "fonts").is_dir():
        log("[clipkit] Inter font not installed; fetching it for the default style")
        cmd_font(argparse.Namespace(family="Inter", out=str(HOME / "fonts"), weights="800,900", subset="latin"))
    ok.update(asr_model=str(d), vad=str(vad), face_model=(HOME / "yunet.onnx").exists(), home=str(HOME))
    print(json.dumps(ok, indent=1))
    if not ok.get("libass"):
        sys.exit("[clipkit] ffmpeg lacks libass (captions). Install an ffmpeg build with libass.")


def cmd_style(a):
    txt = json.dumps(DEFAULT_STYLE, indent=2)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(txt + "\n")
        print(a.out)
    else:
        print(txt)


def main():
    ap = argparse.ArgumentParser(prog="clipkit", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("setup"); p.add_argument("--lang", default="en", choices=list(ASR)); p.set_defaults(fn=cmd_setup)
    p = sp.add_parser("transcribe"); p.add_argument("media"); p.add_argument("-o", "--out", required=True)
    p.add_argument("--lang", default="en", choices=list(ASR)); p.add_argument("--start"); p.add_argument("--end")
    p.set_defaults(fn=cmd_transcribe)
    p = sp.add_parser("cut"); p.add_argument("media", nargs="?"); p.add_argument("words")
    p.add_argument("--start", required=True); p.add_argument("--end", required=True); p.add_argument("-o", "--out", required=True)
    p.add_argument("--style"); p.add_argument("--max-gap", type=float); p.add_argument("--keep-fillers", action="store_true")
    p.add_argument("--no-tighten", action="store_true")
    p.add_argument("--media-start", help="source time where MEDIA begins (pre-cut segment)"); p.set_defaults(fn=cmd_cut)
    p = sp.add_parser("render"); p.add_argument("clip"); p.add_argument("-o", "--out", required=True); p.add_argument("--style")
    p.add_argument("--hook"); p.add_argument("--emphasis", help='phrases separated by "|"')
    p.add_argument("--layout", choices=["auto", "follow", "split", "blur", "audiogram"]); p.add_argument("--cover")
    p.set_defaults(fn=cmd_render)
    p = sp.add_parser("batch"); p.add_argument("picks"); p.add_argument("-o", "--out", required=True)
    p.add_argument("--words"); p.add_argument("--media"); p.add_argument("--style"); p.add_argument("--prefix")
    p.add_argument("--only", help="comma-separated clip numbers"); p.add_argument("--cover"); p.set_defaults(fn=cmd_batch)
    p = sp.add_parser("sheet"); p.add_argument("video"); p.add_argument("-o", "--out", required=True)
    p.add_argument("--n", type=int, default=6); p.add_argument("--cols", type=int, default=3)
    p.add_argument("--width", type=int, default=360); p.add_argument("--times"); p.set_defaults(fn=cmd_sheet)
    p = sp.add_parser("ref"); p.add_argument("videos", nargs="+"); p.add_argument("-o", "--out", required=True)
    p.set_defaults(fn=cmd_ref)
    p = sp.add_parser("font"); p.add_argument("family"); p.add_argument("-o", "--out", required=True)
    p.add_argument("--weights", default="800"); p.add_argument("--subset", default="latin"); p.set_defaults(fn=cmd_font)
    p = sp.add_parser("style"); p.add_argument("-o", "--out"); p.set_defaults(fn=cmd_style)
    p = sp.add_parser("find"); p.add_argument("words"); p.add_argument("phrase"); p.set_defaults(fn=cmd_find)
    p = sp.add_parser("fix"); p.add_argument("target", help="clip dir, clip.json or words.json")
    p.add_argument("rules", nargs="+", help='"old words=>new words"'); p.set_defaults(fn=cmd_fix)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()

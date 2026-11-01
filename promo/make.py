#!/usr/bin/env python3
"""TikTok product-promo editor.  Usage: python make.py config.json

Everything product-specific (cut times, texts, price, features, captions, contact)
lives in config.json.  Pipeline:
  1. cut/reorder/zoom/colour/flash each segment with ffmpeg
  2. render animated HTML overlay -> transparent PNG frames (Playwright/Chromium)
  3. synthesize beat + whoosh + pop sounds (numpy), mix with loudnorm'd voice
  4. composite, write caption.txt and check frames
"""
import json, math, os, shutil, subprocess, sys, wave
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parent
SR = 44100


def sh(cmd, **kw):
    r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, **kw)
    if r.returncode:
        sys.exit("FAILED: " + " ".join(map(str, cmd))[:300] + "\n" + r.stderr[-1500:])
    return r


def probe(path, entry="format=duration"):
    out = sh(["ffprobe", "-v", "error", "-show_entries", entry, "-of", "csv=p=0", path]).stdout.strip()
    return float(out.split(",")[0])


# --------------------------------------------------------------------------- 1. segments
def plan_segments(cfg, dur):
    segs, pos = [], 0.0
    for a, b in sorted(cfg["cuts"], key=lambda c: c[0]):
        if a > pos + 0.01:
            segs.append((pos, a))
        pos = dur if b is None else b
    if pos < dur - 0.05:
        segs.append((pos, dur))
    h = cfg.get("hook_segment")
    if h is not None and 0 <= h < len(segs):
        segs.insert(0, segs.pop(h))
    return segs


def render_segment(cfg, idx, s, e, zoom, first, out_v, out_a):
    W, H, fps = cfg["width"], cfg["height"], cfg["fps"]
    c = cfg["color"]
    zexpr = f"{zoom}*(1+0.05*min(on/15,1))"
    amp = 16
    sx = f"if(lt(on,5),{amp}*(1-on/5)*sin(on*3),0)"
    sy = f"if(lt(on,5),{amp}*0.7*(1-on/5)*cos(on*3.3),0)"
    vf = [
        f"trim=start={s:.3f}:end={e:.3f}", "setpts=PTS-STARTPTS", f"fps={fps}",
        f"scale={W*2}:{H*2}:flags=lanczos",
        f"zoompan=z='{zexpr}':x='(iw-iw/zoom)/2+{sx}':y='(ih-ih/zoom)*0.40+{sy}':d=1:s={W}x{H}:fps={fps}",
        f"eq=brightness={c['brightness']}:contrast={c['contrast']}:saturation={c['saturation']}",
        f"curves=all='0/0 0.25/{0.25 + c['shadow_lift']:.3f} 0.6/0.62 1/1'",
        "unsharp=5:5:0.6:5:5:0.0",
    ]
    if not first and cfg["flash_frames"]:
        vf.append(f"fade=t=in:st=0:d={cfg['flash_frames']/fps:.4f}:color=white")
    vf.append("format=yuv420p")
    sh(["ffmpeg", "-v", "error", "-y", "-i", ROOT / cfg["input"], "-an", "-vf", ",".join(vf),
        "-c:v", "libx264", "-crf", "12", "-preset", "fast", "-r", fps, out_v])
    d = e - s
    af = (f"atrim=start={s:.3f}:end={e:.3f},asetpts=PTS-STARTPTS,aformat=sample_rates={SR}:channel_layouts=stereo,"
          f"afade=t=in:d=0.01,afade=t=out:st={d-0.01:.3f}:d=0.01")
    sh(["ffmpeg", "-v", "error", "-y", "-i", ROOT / cfg["input"], "-vn", "-af", af, out_a])


# --------------------------------------------------------------------------- 2. overlay
def resolve_times(cfg, total):
    def tv(x):
        return total + x if x < 0 else min(x, total)
    tl = cfg["timeline"]
    t = {k: [tv(v[0]), tv(v[1])] for k, v in tl.items() if k != "features"}
    t["features"] = [[tv(a), tv(b)] for a, b in tl["features"]]
    return t


def render_overlay(cfg, total, times, work):
    from playwright.sync_api import sync_playwright
    fps, W, H = cfg["fps"], cfg["width"], cfg["height"]
    sf = cfg["safe"]
    cfgjs = {k: cfg[k] for k in ["hook_text", "title", "offer", "price", "features", "cta", "caption_lines"]}
    cfgjs["times"] = times
    (work / "cfg.js").write_text("window.CFG = " + json.dumps(cfgjs, ensure_ascii=False) + ";", encoding="utf-8")
    frames = work / "overlay"
    shutil.rmtree(frames, ignore_errors=True)
    frames.mkdir(parents=True)
    n = int(math.ceil(total * fps))
    issues, blank = [], frames / "blank.png"
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH', '/opt/pw-browsers/chromium'), args=['--no-sandbox'])
        pg = b.new_page(viewport={"width": W, "height": H})
        pg.goto((ROOT / "overlay.html").as_uri())
        pg.wait_for_function("window.READY === true")
        pg.evaluate("render(-1)")
        pg.screenshot(path=str(blank), omit_background=True)
        for i in range(n):
            t = i / fps
            res = pg.evaluate(f"render({t})")
            dst = frames / f"{i:05d}.png"
            if res["visible"]:
                pg.screenshot(path=str(dst), omit_background=True)
            else:
                shutil.copy(blank, dst)
            rs = [r for r in res["rects"] if r["settled"]]
            for r in rs:
                if (r["left"] < sf["left"] - 2 or r["right"] > W - sf["right"] + 2 or
                        r["top"] < sf["top"] - 2 or r["bottom"] > H - sf["bottom"] + 2):
                    issues.append(f"t={t:.2f} {r['name']} outside safe area "
                                  f"[{r['left']:.0f},{r['top']:.0f},{r['right']:.0f},{r['bottom']:.0f}]")
            for a in range(len(rs)):
                for c in range(a + 1, len(rs)):
                    A, B = rs[a], rs[c]
                    ox = min(A["right"], B["right"]) - max(A["left"], B["left"])
                    oy = min(A["bottom"], B["bottom"]) - max(A["top"], B["top"])
                    if ox > 4 and oy > 4:
                        issues.append(f"t={t:.2f} overlap {A['name']} x {B['name']} ({ox:.0f}x{oy:.0f}px)")
        b.close()
    return frames, issues


# --------------------------------------------------------------------------- 3. audio synth
def env_exp(n, tau):
    return np.exp(-np.arange(n) / (tau * SR))


def kick(dur=0.22):
    n = int(dur * SR); t = np.arange(n) / SR
    f = 45 + 95 * np.exp(-t * 30)
    return np.sin(2 * np.pi * np.cumsum(f) / SR) * env_exp(n, 0.07)


def noise_hit(dur, tau, hp=True):
    n = int(dur * SR); x = np.random.randn(n)
    if hp:
        x = np.diff(x, prepend=0)
    return x * env_exp(n, tau)


def pluck(freq, dur=0.22):
    n = int(dur * SR); t = np.arange(n) / SR
    w = np.sin(2 * np.pi * freq * t) + 0.35 * np.sin(4 * np.pi * freq * t) + 0.15 * np.sin(6 * np.pi * freq * t)
    return w * env_exp(n, 0.09)


def put(buf, sig, at):
    i = int(at * SR)
    if i >= len(buf):
        return
    j = min(len(buf), i + len(sig))
    buf[i:j] += sig[: j - i]


def make_music(total, bpm=118):
    np.random.seed(7)
    buf = np.zeros(int((total + 0.5) * SR))
    beat = 60 / bpm
    roots = [55.0, 43.65, 65.41, 49.0]  # A1 F1 C2 G1
    arps = [[220, 261.6, 329.6, 261.6], [174.6, 220, 261.6, 220], [261.6, 329.6, 392, 329.6], [196, 246.9, 293.7, 246.9]]
    k = kick(); sn = noise_hit(0.16, 0.05) * 0.6; hat = noise_hit(0.05, 0.012) * 0.35
    nb = int((total + 0.5) / beat) + 1
    for b in range(nb):
        t0 = b * beat; bar = (b // 4) % 4
        put(buf, k * 0.9, t0)
        if b % 4 in (1, 3):
            put(buf, sn, t0)
        put(buf, hat, t0 + beat / 2)
        put(buf, pluck(roots[bar], 0.4) * 0.55, t0)
        put(buf, pluck(roots[bar], 0.3) * 0.40, t0 + beat * 0.75)
        for j in range(2):
            put(buf, pluck(arps[bar][(b * 2 + j) % 4], 0.2) * 0.22, t0 + j * beat / 2)
    return buf / (np.max(np.abs(buf)) + 1e-9) * 0.8


def whoosh(dur=0.38):
    n = int(dur * SR); x = np.random.randn(n)
    a = np.linspace(0.01, 0.55, n) ** 1.5
    y = np.zeros(n); s = 0.0
    for i in range(n):
        s += a[i] * (x[i] - s); y[i] = s
    env = np.sin(np.linspace(0, np.pi, n)) ** 2
    y = y * env
    return y / (np.max(np.abs(y)) + 1e-9) * 0.9


def pop(dur=0.12):
    n = int(dur * SR); t = np.arange(n) / SR
    f = 320 + 700 * np.exp(-t * 55)
    return np.sin(2 * np.pi * np.cumsum(f) / SR) * env_exp(n, 0.03)


def write_wav(path, mono):
    st = np.stack([mono, mono], axis=1)
    st = np.clip(st, -1, 1)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes((st * 32767).astype("<i2").tobytes())


def make_sfx(total, cut_times, pop_times, soft_pop_times):
    np.random.seed(3)
    buf = np.zeros(int((total + 0.5) * SR))
    wh = whoosh()
    for t in cut_times:
        put(buf, wh * 0.55, max(0.0, t - 0.30))
    pp = pop()
    for t in pop_times:
        put(buf, pp * 0.60, t)
    for t in soft_pop_times:
        put(buf, pp * 0.28, t)
    return buf


# --------------------------------------------------------------------------- main
def main(cfg_path):
    cfg = json.loads(Path(cfg_path).read_text(encoding="utf-8"))
    fps = cfg["fps"]
    out = ROOT / cfg["output"]
    outdir = out.parent
    work = ROOT / "work"
    (outdir / "checks").mkdir(parents=True, exist_ok=True)
    work.mkdir(exist_ok=True)

    src = ROOT / cfg["input"]
    sdur = probe(src)
    segs = plan_segments(cfg, sdur)
    zooms = cfg["zooms"]
    print("segments (source s):", [(round(a, 2), round(b, 2)) for a, b in segs])

    vlist, alist, bounds, pos = [], [], [], 0.0
    for i, (s, e) in enumerate(segs):
        v, a = work / f"seg{i}.mp4", work / f"seg{i}.wav"
        render_segment(cfg, i, s, e, zooms[i % len(zooms)], i == 0, v, a)
        vlist.append(v); alist.append(a)
        if i:
            bounds.append(pos)
        pos += probe(v)
    (work / "v.txt").write_text("".join(f"file '{p.name}'\n" for p in vlist))
    (work / "a.txt").write_text("".join(f"file '{p.name}'\n" for p in alist))
    sh(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", work / "v.txt", "-c", "copy", work / "video_raw.mp4"])
    sh(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", work / "a.txt", "-c", "copy", work / "voice_raw.wav"])
    total = probe(work / "video_raw.mp4")
    print(f"timeline: {total:.2f}s, cuts at {[round(b, 2) for b in bounds]}")

    # overlay
    times = resolve_times(cfg, total)
    frames, issues = render_overlay(cfg, total, times, work)

    # audio
    pops = [times["hook"][0], times["title"][0], times["offer"][0], times["price"][0], times["cta"][0]]
    pops += [f[0] for f in times["features"]]
    soft = [l["start"] for l in cfg["caption_lines"] if l["start"] < times["cta"][0]]
    write_wav(work / "music.wav", make_music(total))
    write_wav(work / "sfx.wav", make_sfx(total, bounds, pops, soft))
    sh(["ffmpeg", "-v", "error", "-y", "-i", work / "voice_raw.wav", "-i", work / "music.wav", "-i", work / "sfx.wav",
        "-filter_complex",
        "[0:a]loudnorm=I=-16:TP=-1.5:LRA=11[v];[1:a]volume=0.12[m];[2:a]volume=1.0[s];"
        "[v][m][s]amix=inputs=3:duration=first:normalize=0,alimiter=limit=0.95[a]",
        "-map", "[a]", "-t", f"{total:.3f}", work / "audio.wav"])

    # composite
    sh(["ffmpeg", "-v", "error", "-y", "-i", work / "video_raw.mp4", "-framerate", fps, "-i", frames / "%05d.png",
        "-i", work / "audio.wav",
        "-filter_complex", "[0:v][1:v]overlay=format=auto:eof_action=pass,format=yuv420p[v]",
        "-map", "[v]", "-map", "2:a", "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-r", fps,
        "-c:a", "aac", "-b:a", "192k", "-t", f"{total:.3f}", "-movflags", "+faststart", out])

    # caption.txt + checks
    (outdir / "caption.txt").write_text(cfg["caption_text"] + " " + cfg["hashtags"] + "\n", encoding="utf-8")
    for name, t in cfg["check_times"].items():
        sh(["ffmpeg", "-v", "error", "-y", "-ss", f"{t}", "-i", out, "-frames:v", "1", outdir / "checks" / f"{name}.png"])

    # report
    loud = sh(["ffmpeg", "-hide_banner", "-i", out, "-af", "loudnorm=I=-16:print_format=json", "-f", "null", "-"]).stderr
    lufs = [l for l in loud.splitlines() if "input_i" in l]
    print("\nlayout issues:", "none" if not issues else f"{len(issues)}")
    for s in sorted(set(issues))[:25]:
        print("  ", s)
    print("integrated loudness:", lufs[0].strip() if lufs else "n/a")
    print(f"\nDURATION: {probe(out):.2f} s")
    print(f"SIZE: {out.stat().st_size/1e6:.2f} MB")
    print(f"PATH: {out}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "config.json")

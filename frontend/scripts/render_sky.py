"""Render the sky loops the room plays behind the glass: one per theme in src/lib/theme.ts.

Usage: python render_sky.py [id ...] [--footage src/videos] [--use id=file ...]
                            [--frame [id=]x,y,w,h ...] [--flip id ...] [--out public/sky]
                            [--footage-size 3840x2160] [--size 960x540] [--seconds 20]
                            [--fps 24] [--crf 20]

Footage is cut at 3840x2160 by default, which is what ships. Know what that buys: a 16:9 band
of a 4K clip holds about 1600x900 real pixels, so the cut is an upscale of those, at four times
the bytes of 1080p and a 4K decode behind the interface, which a television board may or may
not manage. --footage-size 1920x1080 is the stage's own size and the cheaper choice.

A sky that has footage is cut from it: a file in the footage folder named after the theme
("Golden hour.mp4"), after its id ("golden.mp4"), or given with --use id=file. The clip is
scaled and cropped to 16:9 and its last second is cross-faded into its first, so it loops
without a cut. --frame keeps only part of each clip, as fractions of its width and height
(--frame 0,0.58,1,0.42 is the bottom band), for footage that carries a caption or a logo;
--frame golden=0,0,1,0.4 sets a different part for one sky. --flip golden mirrors a clip
left to right, for footage composed the wrong way round for the panels in front of it.
A sky with no footage is synthesised instead: slow drifting bands of its own four stops,
periodic in time so it loops the same way.

Either way the loop is checked against the sky's contrast grading. theme.ts gives each sky a
`shade`, the strength of the scrim over the room, solved so white ink stays legible over the
still gradient. A loop that is lighter than the still where it matters needs a heavier scrim,
and the script prints the value to set in theme.ts.

Needs numpy and an ffmpeg built with libx264: one on PATH, or `pip install imageio-ffmpeg`.
"""
import argparse
import math
import os
import re
import shutil
import subprocess
import sys
import zlib

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
THEME_TS = os.path.join(HERE, "..", "src", "lib", "theme.ts")
STOP_AT = [0.125, 0.375, 0.625, 0.875]  # where the CSS gradient puts its four stops

# The room as theme.ts and styles.css paint it, for the contrast check. Keep in step with tokensOf().
WHITE = (255.0, 255.0, 255.0)
BLACK = (0.0, 0.0, 0.0)
SCRIM_MIX, GLASS_MIX, GLASS_ALPHA = 0.75, 0.7, 0.6  # the deepest stop, mixed toward black
LIGHT_ALPHA = 0.28  # the brightest of the room's four lights
INK3_ALPHA = 0.6
RING_MIN, INK3_MIN = 3.0, 4.17  # what the original grey room measured, or better


def ffmpeg_exe():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        sys.exit("ffmpeg not found: install it, or `pip install imageio-ffmpeg`")


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, **kw)


# ---------- theme.ts ----------

def read_themes():
    """dicts of id, name, stops and shade for every entry of THEMES, straight out of the TypeScript."""
    src = open(THEME_TS, encoding="utf-8").read()
    pat = re.compile(r"\{\s*id:\s*'(\w+)',\s*name:\s*'([^']+)'[^}]*?stops:\s*\[([^\]]+)\],\s*shade:\s*([\d.]+)")
    found = pat.findall(src)
    if not found:
        sys.exit(f"no themes found in {THEME_TS}")
    return [dict(id=i, name=n, stops=re.findall(r"#([0-9a-fA-F]{6})", s), shade=float(sh)) for i, n, s, sh in found]


def hex_rgb(h):
    return tuple(float(int(h[i:i + 2], 16)) for i in (0, 2, 4))


# ---------- contrast ----------

def _lin(c):
    c /= 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def luminance(rgb):
    r, g, b = (_lin(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    la, lb = luminance(a), luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def over(top, alpha, under):
    return tuple(top[i] * alpha + under[i] * (1 - alpha) for i in range(3))


def mix(a, b, t):
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(3))


def shade_for(stops, lightest):
    """The lightest scrim that keeps white ink legible over this sky.

    Two places matter: the focus ring at the lightest patch under the brightest light, since a
    poster can be anywhere; and ink-3 on the glass over that same patch. Nothing else writes
    directly on the room. Scrim and glass are the sky's deepest stop mixed toward black, as
    tokensOf() in theme.ts makes them.
    """
    s3 = hex_rgb(stops[3])
    scrim, glass = mix(s3, BLACK, SCRIM_MIX), mix(s3, BLACK, GLASS_MIX)
    room = over(WHITE, LIGHT_ALPHA, lightest)
    for step in range(0, 100):
        alpha = step / 100
        bg = over(scrim, alpha, room)
        pane = over(glass, GLASS_ALPHA, bg)
        if contrast(WHITE, bg) >= RING_MIN and contrast(over(WHITE, INK3_ALPHA, pane), pane) >= INK3_MIN:
            return alpha
    return 1.0


def lightest_patch(exe, path):
    """RGB of the lightest ring-sized patch in any frame: each sample is ~20px of the 1920 stage."""
    w, h = 96, 54
    raw = run([exe, "-hide_banner", "-loglevel", "error", "-i", path,
               "-vf", f"scale={w}:{h}:flags=area", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
              capture_output=True).stdout
    fr = np.frombuffer(raw, np.uint8).reshape(-1, 3).astype(np.float32)
    lin = np.where(fr / 255 <= 0.04045, fr / 255 / 12.92, ((fr / 255 + 0.055) / 1.055) ** 2.4)
    y = lin @ np.array([0.2126, 0.7152, 0.0722], np.float32)
    return tuple(float(c) for c in fr[int(np.argmax(y))])


# ---------- synthesised sky ----------

def palette(stops, n=1024):
    """A lookup table along the CSS gradient: index 0 is the lightest stop, n-1 the darkest."""
    rgb = np.array([hex_rgb(h) for h in stops], dtype=np.float32)
    v = np.linspace(0, 1, n, dtype=np.float32)
    return np.stack([np.interp(v, STOP_AT, rgb[:, c]) for c in range(3)], axis=1).round().astype(np.uint8)


def fields(w, h, n, seed):
    """Yield n float32 fields in [0, 1]: where along the gradient each pixel sits, over one loop."""
    rng = np.random.default_rng(seed)
    x = np.linspace(0, 1, w, dtype=np.float32)[None, :]
    y = np.linspace(0, 1, h, dtype=np.float32)[:, None]
    base = (x * w + y * h) / (w + h)  # the 135deg gradient's own position, as CSS computes it

    # Slow plane waves. Each makes a whole number of cycles per loop, so frame n wraps to
    # frame 0, and each drifts a different way so the bands undulate rather than march.
    waves = []
    for _ in range(4):
        ax = rng.uniform(0.4, 1.0) * rng.choice([-1, 1])
        ay = rng.uniform(0.3, 0.8) * rng.choice([-1, 1])
        waves.append((ax, ay, int(rng.choice([-1, 1])), rng.uniform(0, 2 * np.pi), rng.uniform(0.6, 1.0)))
    total = sum(wk for *_, wk in waves)

    for i in range(n):
        t = i / n
        noise = np.zeros((h, w), np.float32)
        for ax, ay, c, ph, wk in waves:
            noise += wk * np.sin(2 * np.pi * (ax * x + ay * y + c * t) + ph)
        noise /= total
        # A soft light that circles once per loop, pulling its patch toward the lighter stops.
        cx, cy = 0.28 + 0.14 * np.cos(2 * np.pi * t), 0.22 + 0.10 * np.sin(2 * np.pi * t)
        glow = np.exp(-(((x - cx) ** 2) / 0.12 + ((y - cy) ** 2) / 0.08))
        yield np.clip(base + 0.11 * noise - 0.16 * glow, 0, 1)


# BT.709 for the RGB->YUV step and matching tags, or the conversion falls back to BT.601 and
# the loop lands a shade off the still sky it fades in over. Main profile, level 4.0: what every
# TV's decoder handles. aq-mode 3 spends bits on the flat dark areas, where a gradient bands.
def encode_args(crf, gop, w, h):
    # Level 4.0 covers up to 1080p30 and is what every decoder handles; 4K needs 5.1.
    level = "5.1" if w * h > 1920 * 1080 else "4.0"
    return ["-an", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
            "-c:v", "libx264", "-profile:v", "main", "-level", level, "-preset", "slow",
            "-crf", str(crf), "-g", str(gop), "-x264-params", "aq-mode=3", "-movflags", "+faststart"]


def synthesise(exe, theme, out, w, h, fps, seconds, crf):
    lut = palette(theme["stops"])
    n = fps * seconds
    cmd = [exe, "-y", "-hide_banner", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", str(fps), "-i", "pipe:0",
           "-vf", "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p",
           *encode_args(crf, fps * 2, w, h), out]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    seed = zlib.crc32(theme["id"].encode())  # the same sky renders the same loop every time
    for field in fields(w, h, n, seed):
        proc.stdin.write(lut[(field * (len(lut) - 1)).astype(np.int32)].tobytes())
    proc.stdin.close()
    if proc.wait() != 0:
        sys.exit(f"ffmpeg failed on {theme['id']}")


# ---------- footage ----------

def duration_of(exe, path):
    info = subprocess.run([exe, "-hide_banner", "-i", path], capture_output=True, text=True).stderr
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", info)
    if not m:
        sys.exit(f"cannot read the length of {path}")
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def cut_loop(exe, src, out, w, h, crf, frame=None, flip=False, fade=1.0):
    """Scale and crop to the frame, then cross-fade the last `fade` seconds into the first."""
    length = duration_of(exe, src)
    if length < 3 * fade:
        sys.exit(f"{src} is too short to loop: {length:.1f}s")
    body_end = length - fade
    pre = "hflip," if flip else ""
    if frame:
        x, y, fw, fh = frame
        pre += f"crop=iw*{fw}:ih*{fh}:iw*{x}:ih*{y},"
    graph = (
        f"[0:v]{pre}scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,crop={w}:{h},"
        f"setsar=1,fps=30,format=yuv420p,split=3[a][b][c];"
        # A trim drops the constant-rate flag xfade insists on, so each branch restates it.
        f"[a]trim=start={fade}:end={body_end},setpts=PTS-STARTPTS,fps=30[body];"
        f"[b]trim=start={body_end},setpts=PTS-STARTPTS,fps=30[tail];"
        f"[c]trim=end={fade},setpts=PTS-STARTPTS,fps=30[head];"
        f"[tail][head]xfade=transition=fade:duration={fade}:offset=0[seam];"
        f"[body][seam]concat=n=2:v=1:a=0[out]"
    )
    run([exe, "-y", "-hide_banner", "-loglevel", "error", "-i", src,
         "-filter_complex", graph, "-map", "[out]", *encode_args(crf, 60, w, h), out])


def find_footage(theme, folder, overrides):
    if theme["id"] in overrides:
        path = overrides[theme["id"]]
        return path if os.path.isabs(path) else os.path.join(folder, path)
    if not os.path.isdir(folder):
        return None
    wanted = {theme["name"].lower(), theme["id"].lower()}
    for f in os.listdir(folder):
        stem, ext = os.path.splitext(f)
        if stem.lower() in wanted and ext.lower() in (".mp4", ".mov", ".m4v", ".webm"):
            return os.path.join(folder, f)
    return None


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("ids", nargs="*", help="theme ids to render; default all")
    ap.add_argument("--footage", default=os.path.join(HERE, "..", "src", "videos"),
                    help="folder of source clips named after a theme or its id")
    ap.add_argument("--use", action="append", default=[], metavar="ID=FILE",
                    help="use this clip for this theme, whatever it is called")
    ap.add_argument("--frame", action="append", default=[], metavar="[ID=]X,Y,W,H",
                    help="the part of each clip to use, as fractions; id= limits it to one sky")
    ap.add_argument("--flip", action="append", default=[], metavar="ID",
                    help="mirror this sky's clip left to right")
    ap.add_argument("--out", default=os.path.join(HERE, "..", "public", "sky"))
    ap.add_argument("--footage-size", default="3840x2160",
                    help="what ships; 1920x1080 is the stage's own size and all a band of a 4K clip really holds")
    ap.add_argument("--size", default="960x540", help="a synthesised sky needs no more; the stage scales it")
    ap.add_argument("--seconds", type=int, default=20, help="length of a synthesised loop")
    ap.add_argument("--fps", type=int, default=24, help="frame rate of a synthesised loop")
    ap.add_argument("--crf", type=int, default=20, help="x264 quality; soft gradients block up above ~22")
    args = ap.parse_args()

    overrides = dict(u.split("=", 1) for u in args.use)
    frames = {}
    for spec in args.frame:
        key, _, box = spec.rpartition("=")
        try:
            box = tuple(float(v) for v in box.split(","))
        except ValueError:
            box = ()
        if len(box) != 4 or any(v < 0 or v > 1 for v in box) or box[2] <= 0 or box[3] <= 0:
            sys.exit(f"--frame wants four fractions x,y,w,h: {spec}")
        frames[key or "*"] = box
    fw, fh = (int(v) for v in args.footage_size.lower().split("x"))
    sw, sh = (int(v) for v in args.size.lower().split("x"))
    exe = ffmpeg_exe()
    os.makedirs(args.out, exist_ok=True)
    themes = read_themes()
    known = {t["id"] for t in themes}
    wanted = set(args.ids) or known
    if wanted - known:
        sys.exit(f"unknown theme id(s): {', '.join(sorted(wanted - known))}")

    for theme in themes:
        if theme["id"] not in wanted:
            continue
        out = os.path.join(args.out, f"{theme['id']}.mp4")
        clip = find_footage(theme, args.footage, overrides)
        if clip:
            cut_loop(exe, clip, out, fw, fh, args.crf, frames.get(theme["id"], frames.get("*")),
                     theme["id"] in args.flip)
            source = os.path.basename(clip)
        else:
            synthesise(exe, theme, out, sw, sh, args.fps, args.seconds, args.crf)
            source = "synthesised"

        need = math.ceil(shade_for(theme["stops"], lightest_patch(exe, out)) * 50) / 50  # up to the next 0.02
        verdict = "ok" if need <= theme["shade"] else f"set shade: {need:.2f} in theme.ts (has {theme['shade']:.2f})"
        print(f"{theme['id']:8s} {os.path.getsize(out) / 1024:6.0f} KB  {source:20s} {verdict}")


if __name__ == "__main__":
    main()

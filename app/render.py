"""Render a storyboard Short to a vertical (9:16) MP4 with narration and captions.

Frames are drawn with Pillow and piped to ffmpeg as raw RGB. Static pieces of a
scene (cards, text blocks, caption chips) are drawn once and composited per frame
with simple entrance animations, which keeps rendering fast enough for a laptop.
"""
from __future__ import annotations

import math
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .storyboard import Scene, Short, spoken
from .tts import Narrator, concat_wavs

W, H = 720, 1280
FPS = int(os.environ.get("SHORTS_FPS", "30"))
MARGIN_L, MARGIN_R = 48, 96  # right margin leaves room for the player's action rail
CONTENT_W = W - MARGIN_L - MARGIN_R
FONT_DIR = Path(__file__).parent / "assets" / "fonts"

RISK_COLORS = {
    "High": (255, 77, 94),
    "Moderate": (255, 176, 32),
    "Low": (46, 204, 143),
    "Unrated": (148, 163, 184),
    "Info": (91, 140, 255),
}
STANCE_STYLE = {
    "Agrees": ((46, 204, 143), "✓"),
    "Agrees with reservations": ((255, 176, 32), "!"),
    "Disagrees": ((255, 77, 94), "✕"),
}
WHITE = (255, 255, 255)
MUTED = (203, 213, 225)
BG_TOP, BG_BOTTOM = (13, 18, 38), (5, 7, 16)


# ----------------------------------------------------------------- fonts

@lru_cache(maxsize=64)
def font(weight: str, size: int) -> ImageFont.FreeTypeFont:
    files = {
        "regular": ["Inter-Regular.ttf", "DejaVuSans.ttf"],
        "semibold": ["Inter-SemiBold.ttf", "DejaVuSans-Bold.ttf"],
        "black": ["InterDisplay-ExtraBold.ttf", "DejaVuSans-Bold.ttf"],
        "italic": ["Inter-Italic.ttf", "DejaVuSans-Oblique.ttf"],
        "symbol": ["DejaVuSans-Bold.ttf", "Inter-SemiBold.ttf"],
    }[weight]
    for name in files:
        for base in (FONT_DIR, Path("/usr/share/fonts/truetype/dejavu")):
            p = base / name
            if p.exists():
                return ImageFont.truetype(str(p), size)
    return ImageFont.load_default(size)


def wrap(text: str, f: ImageFont.FreeTypeFont, width: int) -> list[str]:
    lines: list[str] = []
    for para in text.split("\n"):
        line = ""
        for word in para.split():
            trial = f"{line} {word}".strip()
            if f.getlength(trial) <= width or not line:
                line = trial
            else:
                lines.append(line)
                line = word
        if line:
            lines.append(line)
    return lines


def fit_text(text: str, weight: str, sizes: list[int], width: int, max_lines: int) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    """Largest font size at which the text fits in max_lines."""
    for size in sizes:
        f = font(weight, size)
        lines = wrap(text, f, width)
        if len(lines) <= max_lines and all(f.getlength(ln) <= width for ln in lines):
            return f, lines
    f = font(weight, sizes[-1])
    lines = wrap(text, f, width)[:max_lines]
    lines[-1] = lines[-1].rstrip(".,;: ") + "…"
    return f, lines


# ----------------------------------------------------------------- layers

@dataclass
class Layer:
    image: Image.Image
    x: int
    y: int
    delay: float = 0.0


def text_block(text: str, weight: str, sizes: list[int], width: int, max_lines: int,
               color=WHITE, line_gap: float = 1.18) -> Image.Image:
    f, lines = fit_text(text, weight, sizes, width, max_lines)
    lh = int(f.size * line_gap)
    img = Image.new("RGBA", (width, lh * len(lines) + int(f.size * 0.3)), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for i, ln in enumerate(lines):
        d.text((0, i * lh), ln, font=f, fill=color)
    return img


def pill(text: str, color, size: int = 24, fg=WHITE, fill_alpha: int = 255, pad=(18, 10)) -> Image.Image:
    f = font("semibold", size)
    tw = int(f.getlength(text))
    img = Image.new("RGBA", (tw + pad[0] * 2, size + pad[1] * 2 + 4), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, img.width - 1, img.height - 1], radius=img.height // 2, fill=(*color, fill_alpha))
    d.text((pad[0], pad[1] - 1), text, font=f, fill=fg)
    return img


def label(text: str, accent) -> Image.Image:
    f = font("semibold", 24)
    spaced = " ".join(text) if len(text) < 14 else text
    img = Image.new("RGBA", (CONTENT_W, 40), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 6, 8, 34], radius=4, fill=accent)
    d.text((22, 6), spaced if f.getlength(spaced) < CONTENT_W - 30 else text, font=f, fill=accent)
    return img


def card(content: Image.Image, pad: int = 28) -> Image.Image:
    """Frosted panel behind a text block."""
    img = Image.new("RGBA", (content.width + pad * 2, content.height + pad * 2), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, img.width - 1, img.height - 1], radius=28, fill=(15, 20, 40, 150),
                        outline=(255, 255, 255, 40), width=2)
    img.alpha_composite(content, (pad, pad))
    return img


def bullet_rows(bullets: list[str], accent) -> list[Image.Image]:
    rows = []
    n = len(bullets)
    size_sizes = [34, 30, 28] if n <= 3 else [28, 26, 24]
    max_lines = 4 if n <= 2 else 3 if n <= 3 else 2
    for b in bullets:
        m = re.match(r"^#(\d+)\s+(.*)$", b)
        chip = m.group(1) if m else "›"
        body = m.group(2) if m else b
        text = text_block(body, "semibold" if m else "regular", size_sizes, CONTENT_W - 110, max_lines)
        h = max(text.height + 30, 76)
        row = Image.new("RGBA", (CONTENT_W, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(row)
        d.rounded_rectangle([0, 0, CONTENT_W - 1, h - 1], radius=22, fill=(255, 255, 255, 22), outline=(255, 255, 255, 36), width=2)
        d.ellipse([18, h // 2 - 22, 62, h // 2 + 22], fill=accent)
        cf = font("semibold", 22 if len(chip) > 1 else 24)
        d.text((40, h // 2), chip, font=cf, fill=WHITE, anchor="mm")
        row.alpha_composite(text, (84, (h - text.height) // 2 + 4))
        rows.append(row)
    return rows


def arrow(accent) -> Image.Image:
    img = Image.new("RGBA", (90, 90), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([0, 0, 89, 89], fill=(255, 255, 255, 30), outline=(*accent, 255), width=3)
    d.line([(26, 54), (45, 35), (64, 54)], fill=WHITE, width=7, joint="curve")
    return img


def stance_icon(stance: str) -> Image.Image:
    color, glyph = STANCE_STYLE.get(stance, (RISK_COLORS["Info"], "i"))
    img = Image.new("RGBA", (150, 150), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([0, 0, 149, 149], fill=(*color, 60))
    d.ellipse([18, 18, 131, 131], fill=color)
    d.text((75, 76), glyph, font=font("symbol", 70), fill=WHITE, anchor="mm")
    return img


# ----------------------------------------------------------------- scenes

def scene_layers(scene: Scene, short: Short, accent) -> tuple[list[Layer], dict | None]:
    """Static layers for a scene plus an optional animated stat description."""
    layers: list[Layer] = []
    y = 250
    x = MARGIN_L
    stat_anim = None

    def add(img: Image.Image, delay: float, gap: int = 26, dx: int = 0) -> None:
        nonlocal y
        layers.append(Layer(img, x + dx, y, delay))
        y += img.height + gap

    if scene.kind == "title":
        y = 330
        add(label(scene.label, accent), 0.0, 30)
        add(text_block(scene.headline, "black", [76, 68, 60, 52, 46], CONTENT_W, 5), 0.15, 34)
        if scene.body:
            add(pill(scene.body.upper(), accent, 26), 0.45)
    elif scene.kind == "stat":
        y = 300
        add(label(scene.label, accent), 0.0, 20)
        stat_anim = {"text": scene.stat, "x": x, "y": y, "delay": 0.1}
        y += 230
        if scene.body:
            add(card(text_block(scene.body, "regular", [36, 32, 30], CONTENT_W - 56, 6, MUTED)), 0.6)
    elif scene.kind == "text":
        add(label(scene.label, accent), 0.0, 22)
        add(text_block(scene.headline, "black", [60, 54], CONTENT_W, 2), 0.1, 30)
        add(card(text_block(scene.body, "regular", [38, 35, 32, 30, 28], CONTENT_W - 56, 11)), 0.35)
    elif scene.kind == "list":
        add(label(scene.label, accent), 0.0, 22)
        add(text_block(scene.headline, "black", [56, 50], CONTENT_W, 2), 0.1, 28)
        rows = bullet_rows(scene.bullets, accent)
        gap = 14 if len(rows) > 4 else 18
        for i, row in enumerate(rows):
            add(row, 0.35 + i * 0.35, gap)
    elif scene.kind == "stance":
        y = 270
        add(label(scene.label, accent), 0.0, 30)
        add(stance_icon(scene.headline), 0.1, 26)
        add(text_block(scene.headline, "black", [56, 50, 44], CONTENT_W, 2), 0.25, 26)
        if scene.body:
            add(card(text_block(f"“{scene.body}”", "italic", [32, 30, 28, 26], CONTENT_W - 56, 8, MUTED)), 0.5)
    elif scene.kind == "outro":
        y = 380
        add(label(scene.label, accent), 0.0, 30)
        add(text_block(scene.headline, "black", [70, 62, 54], CONTENT_W, 3), 0.1, 30)
        if scene.body:
            add(text_block(scene.body, "regular", [36, 32], CONTENT_W, 3, MUTED), 0.35, 40)
        layers.append(Layer(arrow(accent), x, y, 0.6))

    # Centre the block between the header and the caption band.
    if layers:
        top = min([l.y for l in layers] + ([stat_anim["y"]] if stat_anim else []))
        bottom = max(l.y + l.image.height for l in layers)
        if stat_anim:
            bottom = max(bottom, stat_anim["y"] + 220)
        shift = int((CONTENT_TOP + CAPTION_TOP - 30) / 2 - (top + bottom) / 2)
        shift = max(shift, CONTENT_TOP - top)
        for l in layers:
            l.y += shift
        if stat_anim:
            stat_anim["y"] += shift
    return layers, stat_anim


# ----------------------------------------------------------------- captions

CAPTION_TOP = 1010
CONTENT_TOP = 160


def caption_chunks(text: str, max_words: int = 7) -> list[str]:
    chunks: list[str] = []
    for part in re.split(r"(?<=[.,;:!?])\s+", text):
        words = part.split()
        while words:
            n = len(words) if len(words) <= max_words + 2 else max_words
            chunks.append(" ".join(words[:n]))
            words = words[n:]
    return [c for c in chunks if c]


def caption_image(text: str) -> Image.Image:
    f = font("semibold", 34)
    lines = wrap(text, f, CONTENT_W - 30)[:2]
    lh = 44
    w = int(max(f.getlength(l) for l in lines)) + 36
    img = Image.new("RGBA", (w, lh * len(lines) + 22), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, w - 1, img.height - 1], radius=16, fill=(0, 0, 0, 165))
    for i, ln in enumerate(lines):
        d.text((w // 2, 11 + i * lh + lh // 2), ln, font=f, fill=WHITE, anchor="mm")
    return img


def caption_timeline(narration: str, start: float, speech: float) -> list[tuple[float, float, Image.Image]]:
    chunks = caption_chunks(narration)
    total = sum(len(c) + 6 for c in chunks) or 1
    out, t = [], start
    for c in chunks:
        dur = speech * (len(c) + 6) / total
        out.append((t, t + dur, caption_image(c)))
        t += dur
    return out


# ----------------------------------------------------------------- background

def background(accent, seed: int) -> Image.Image:
    bw, bh = W + 160, H + 200
    bg = Image.new("RGB", (bw, bh))
    d = ImageDraw.Draw(bg)
    for yy in range(bh):
        t = yy / bh
        d.line([(0, yy), (bw, yy)], fill=tuple(int(BG_TOP[i] * (1 - t) + BG_BOTTOM[i] * t) for i in range(3)))
    glow = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    g = ImageDraw.Draw(glow)
    blobs = [(accent, 0.15, 0.18, 330, 120), ((139, 92, 246), 0.9, 0.55, 360, 90), ((56, 189, 248), 0.2, 0.92, 300, 70)]
    for i, (color, fx, fy, r, a) in enumerate(blobs):
        fx = (fx + 0.13 * math.sin(seed * 1.7 + i)) % 1
        cx, cy = int(bw * fx), int(bh * fy)
        g.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(*color, a))
    glow = glow.filter(ImageFilter.GaussianBlur(110))
    out = bg.convert("RGBA")
    out.alpha_composite(glow)
    # Subtle dot grid for texture
    dots = ImageDraw.Draw(out)
    for yy in range(0, bh, 40):
        for xx in range(0, bw, 40):
            dots.point((xx, yy), fill=(255, 255, 255, 18))
    return out


# ----------------------------------------------------------------- compositing

def ease(p: float) -> float:
    p = min(1.0, max(0.0, p))
    return 1 - (1 - p) ** 3


def faded(img: Image.Image, alpha: float, cache: dict) -> Image.Image:
    """`img` with its opacity scaled by alpha (quantised to 5% steps and cached per render)."""
    if alpha >= 0.999:
        return img
    key = (id(img), int(alpha * 20))
    if key not in cache:
        a = img.getchannel("A").point(lambda v: int(v * key[1] / 20))
        c = img.copy()
        c.putalpha(a)
        cache[key] = c
    return cache[key]


def split_stat(stat: str) -> tuple[str, float | None, str, int]:
    m = re.match(r"^([^\d]*)([\d,]*\.?\d+)(.*)$", stat)
    if not m:
        return stat, None, "", 0
    num = m.group(2)
    decimals = len(num.split(".")[1]) if "." in num else 0
    return m.group(1), float(num.replace(",", "")), m.group(3), decimals


def stat_image(text: str, accent) -> Image.Image:
    f, _ = fit_text(text, "black", [190, 170, 150, 130, 110, 96, 84, 72], CONTENT_W, 1)
    tw = int(f.getlength(text)) + 10
    img = Image.new("RGBA", (max(tw, 10), int(f.size * 1.2)), (0, 0, 0, 0))
    ImageDraw.Draw(img).text((0, 0), text, font=f, fill=accent)
    return img


def header(short: Short, report_label: str, accent) -> Image.Image:
    img = Image.new("RGBA", (W, 130), (0, 0, 0, 0))
    left = pill(report_label, (255, 255, 255), 22, fg=WHITE, fill_alpha=30, pad=(16, 8))
    img.alpha_composite(left, (MARGIN_L - 8, 58))
    right_text = {"Info": "OVERVIEW"}.get(short.risk_level, f"{short.risk_level.upper()} RISK")
    if short.kind == "actions":
        right_text = "ACTIONS"
    right = pill(right_text, accent, 22, pad=(16, 8))
    img.alpha_composite(right, (W - 40 - right.width, 58))
    return img


def draw_progress(frame: Image.Image, durations: list[float], idx: int, local_t: float) -> None:
    d = ImageDraw.Draw(frame)
    n = len(durations)
    gap, x0, x1, y = 6, 32, W - 32, 30
    seg = (x1 - x0 - gap * (n - 1)) / n
    for i in range(n):
        sx = x0 + i * (seg + gap)
        d.rounded_rectangle([sx, y, sx + seg, y + 5], radius=3, fill=(255, 255, 255, 70))
        p = 1.0 if i < idx else (min(1.0, local_t / durations[i]) if i == idx else 0.0)
        if p > 0:
            d.rounded_rectangle([sx, y, sx + max(6, seg * p), y + 5], radius=3, fill=(255, 255, 255, 255))


def render_short(short: Short, report_label: str, out_mp4: Path, poster: Path, narrator: Narrator,
                 work_dir: Path | None = None) -> dict:
    """Render one short. Returns metadata (duration, narration engine, captions)."""
    accent = RISK_COLORS.get(short.risk_level, RISK_COLORS["Info"])
    tmp = Path(tempfile.mkdtemp(dir=work_dir))
    lead_in, tail = 0.35, 0.55

    # 1. narration per scene → scene durations
    clips: list[tuple[Path | None, float]] = []
    durations: list[float] = []
    speech: list[float] = []
    for i, sc in enumerate(short.scenes):
        wav = tmp / f"scene{i}.wav"
        dur = narrator.synthesize(spoken(sc.narration), wav)
        if dur <= 0:  # silent mode: give readers time for the words on screen
            wav = None
            dur = max(2.5, len(sc.narration.split()) / 2.6)
        speech.append(dur)
        durations.append(lead_in + dur + tail)
    clips = [(None, lead_in)]
    for i, d in enumerate(durations):
        wav = tmp / f"scene{i}.wav"
        clips.append((wav if wav.exists() else None, speech[i] + tail + (lead_in if i < len(durations) - 1 else 0)))
    audio = tmp / "narration.wav"
    concat_wavs(clips, audio)

    # 2. static layers
    bg = background(accent, short.index)
    head = header(short, report_label, accent)
    watermark = text_block("AUDIT SHORTS", "semibold", [18], 300, 1, (255, 255, 255, 110))
    scenes = []
    start = 0.0
    captions = []
    for i, sc in enumerate(short.scenes):
        layers, stat = scene_layers(sc, short, accent)
        caps = caption_timeline(sc.narration, start + lead_in, speech[i])
        captions.extend((round(a, 2), round(b, 2), txt) for (a, b, _), txt in zip(caps, caption_chunks(sc.narration)))
        scenes.append((start, durations[i], layers, stat, caps))
        start += durations[i]
    total = start

    # 3. frames → ffmpeg
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
           "-r", str(FPS), "-i", "-", "-i", str(audio), "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-shortest", "-movflags", "+faststart", str(out_mp4)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    stat_cache: dict[str, Image.Image] = {}
    fade_cache: dict[tuple[int, int], Image.Image] = {}
    poster_saved = False
    n_frames = int(math.ceil(total * FPS))
    try:
        for fi in range(n_frames):
            t = fi / FPS
            idx = next(i for i, s in enumerate(scenes) if t < s[0] + s[1] or i == len(scenes) - 1)
            s_start, s_dur, layers, stat, caps = scenes[idx]
            lt = t - s_start
            ox = int(80 + 60 * math.sin(t * 0.35 + short.index))
            oy = int(100 + 70 * math.cos(t * 0.27))
            frame = bg.crop((ox, oy, ox + W, oy + H))
            draw_progress(frame, [s[1] for s in scenes], idx, lt)
            frame.alpha_composite(head)
            # exit fade in the last 0.2 s of a scene
            exit_p = ease((s_start + s_dur - t) / 0.2) if idx < len(scenes) - 1 else 1.0
            for layer in layers:
                p = ease((lt - layer.delay) / 0.45)
                if p <= 0:
                    continue
                a = p * exit_p
                frame.alpha_composite(faded(layer.image, a, fade_cache), (layer.x, int(layer.y + (1 - p) * 40)))
            if stat:
                p = ease((lt - stat["delay"]) / 1.1)
                if p > 0:
                    prefix, value, suffix, dec = split_stat(stat["text"])
                    if value is None or p >= 1:
                        txt = stat["text"]
                    else:
                        v = value * p
                        txt = f"{prefix}{v:,.{dec}f}{suffix}" if "," in stat["text"] else f"{prefix}{v:.{dec}f}{suffix}"
                    if txt not in stat_cache:
                        stat_cache[txt] = stat_image(txt, accent)
                    img = stat_cache[txt]
                    scale_p = ease((lt - stat["delay"]) / 0.35)
                    frame.alpha_composite(faded(img, scale_p * exit_p, fade_cache), (stat["x"], int(stat["y"] + (1 - scale_p) * 30)))
            for (a, b, img) in caps:
                if a <= t < b:
                    frame.alpha_composite(img, ((W - img.width) // 2, CAPTION_TOP))
                    break
            frame.alpha_composite(watermark, (MARGIN_L, H - 60))
            if not poster_saved and idx == 0 and lt >= 1.2:
                frame.convert("RGB").save(poster, quality=85)
                poster_saved = True
            proc.stdin.write(frame.convert("RGB").tobytes())
    finally:
        proc.stdin.close()
        rc = proc.wait()
    if rc != 0:
        raise RuntimeError(f"ffmpeg failed with exit code {rc}")
    if not poster_saved:
        frame.convert("RGB").save(poster, quality=85)
    for f in tmp.iterdir():
        f.unlink()
    tmp.rmdir()
    return {"duration": round(total, 2), "narration": narrator.used or "silent", "captions": captions,
            "scene_starts": [round(s[0], 2) for s in scenes]}

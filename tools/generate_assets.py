"""Generate VaultSprite placeholder assets: an animated pixel-art mascot + chiptune SFX.

Run once from the repo root::

    .venv/bin/python tools/generate_assets.py

Produces:
  assets/sprites/<state>.gif   transparent GIFs (96x96, 3-4 frames each)
  assets/sounds/{step,chirp,yawn}.wav   short 8-bit style mono WAVs
  assets/config.yaml           sprite FSM matrix (states/frames/transitions)

The generator is deterministic (seeded RNG). Swap in your own sheets later by
repointing entries in ``assets/config.yaml`` — no code changes needed.
"""
from __future__ import annotations

import math
import random
import struct
import wave
from pathlib import Path

import yaml
from PIL import Image, ImageDraw, ImageFont

REPO_ROOT = Path(__file__).resolve().parent.parent
ASSETS = REPO_ROOT / "assets"
SS = 4            # supersampling factor for smooth antialiased edges
SIZE = 96         # logical sprite size in px (== window size)
CANVAS = SIZE * SS

# palette
BODY_TOP = (150, 208, 255)
BODY_MID = (92, 164, 250)
BODY_LOW = (54, 110, 214)
OUTLINE = (30, 47, 88)
WHITE = (255, 255, 255)
PUPIL = (32, 42, 64)
CHEEK = (255, 140, 165)
MOUTH_IN = (74, 34, 58)
TONGUE = (255, 122, 142)
SHADOW = (20, 30, 55)
PUFF = (215, 232, 255)

RNG = random.Random(42)


# --------------------------------------------------------------------------
# low-level drawing helpers (all at supersample resolution)
# --------------------------------------------------------------------------
def layer() -> Image.Image:
    return Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))


def composite(*parts: Image.Image) -> Image.Image:
    out = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    for p in parts:
        out.alpha_composite(p)
    return out


def vgradient(top: tuple, mid: tuple, low: tuple) -> Image.Image:
    """Full-canvas vertical three-stop gradient."""
    img = Image.new("RGBA", (1, CANVAS), (0, 0, 0, 0))
    px = img.load()
    for y in range(CANVAS):
        t = y / (CANVAS - 1)
        if t < 0.55:
            k = t / 0.55
            c = [int(a + (b - a) * k) for a, b in zip(top, mid)]
        else:
            k = (t - 0.55) / 0.45
            c = [int(a + (b - a) * k) for a, b in zip(mid, low)]
        px[0, y] = (*c, 255)
    return img.resize((CANVAS, CANVAS))


def body_mask(cx: int, cy: int, rx: int, ry: int) -> Image.Image:
    """Blob body mask — rounded dome with a slightly flattened bottom."""
    m = Image.new("L", (CANVAS, CANVAS), 0)
    d = ImageDraw.Draw(m)
    d.ellipse([cx - rx, cy - int(ry * 1.06), cx + rx, cy + ry], fill=255)
    return m


def base_body(cx: int, cy: int, squash_y: float = 1.0):
    """Gradient blob with outline ring and a soft top-left highlight."""
    ry = int(96 * squash_y)
    rx = 84
    grad = vgradient(BODY_TOP, BODY_MID, BODY_LOW)
    grad.putalpha(body_mask(cx, cy, rx, ry))
    outline = layer()
    od = ImageDraw.Draw(outline)
    od.ellipse([cx - rx + 10, cy - int(ry * 1.06) + 12, cx + rx - 10, cy + ry],
               outline=OUTLINE, width=14)
    hi = layer()
    hd = ImageDraw.Draw(hi)
    hd.ellipse([cx - rx // 2 - 30, cy - int(ry * 0.95), cx - 8, cy - int(ry * 0.35)],
               fill=(255, 255, 255, 64))
    return composite(outline, grad, hi)


def draw_eyes(d: ImageDraw.ImageDraw, cx: int, cy: int, open_: bool = True,
              look_dx: int = 18):
    for side in (-1, 1):
        ex = cx + side * 62
        if open_:
            d.ellipse([ex - 46, cy - 58, ex + 46, cy + 58], fill=WHITE,
                      outline=OUTLINE, width=8)
            px_, py_ = ex + look_dx, cy + 12
            d.ellipse([px_ - 26, py_ - 34, px_ + 26, py_ + 10], fill=PUPIL)
            g = 9   # glint (upper-left of the pupil)
            gx, gy = px_ - 8, py_ - 16
            d.ellipse([gx - g, gy - g, gx + g, gy + g], fill=WHITE)
        else:  # contented closed eye — downward curve
            d.arc([ex - 46, cy - 58, ex + 46, cy + 58], start=15, end=165,
                  fill=OUTLINE, width=12)


def draw_cheeks(d: ImageDraw.ImageDraw, cx: int, cy: int):
    for side in (-1, 1):
        d.ellipse([cx + side * 96 - 20, cy + 8, cx + side * 96 + 20, cy + 34],
                  fill=(*CHEEK, 150))


def draw_mouth(d: ImageDraw.ImageDraw, cx: int, cy: int, kind: str):
    if kind == "smile":
        d.arc([cx - 34, cy - 20, cx + 34, cy + 26], start=15, end=165,
              fill=OUTLINE, width=9)
    elif kind == "open_small":
        d.ellipse([cx - 18, cy - 18, cx + 18, cy + 14], fill=MOUTH_IN,
                  outline=OUTLINE, width=7)
    elif kind == "open_big":
        d.ellipse([cx - 36, cy - 28, cx + 36, cy + 36], fill=MOUTH_IN,
                  outline=OUTLINE, width=8)
        d.ellipse([cx - 16, cy + 4, cx + 16, cy + 28], fill=TONGUE)
    elif kind == "o":
        d.ellipse([cx - 13, cy - 17, cx + 13, cy + 13], fill=MOUTH_IN,
                  outline=OUTLINE, width=7)


def draw_feet(d: ImageDraw.ImageDraw, cx: int, cy: int, phase: float = 0.0):
    """Two stubby feet; ``phase`` in [0..1] drives the step cycle."""
    for side in (-1, 1):
        lift = max(math.sin((phase + (0.5 if side > 0 else 0.0)) * 2 * math.pi), 0) * 16
        fx = cx + side * 58
        fy = cy - int(lift)
        d.rounded_rectangle([fx - 34, fy - 18, fx + 34, fy + 24], radius=24,
                            fill=(54, 110, 214), outline=OUTLINE, width=8)


def draw_arms_up(d: ImageDraw.ImageDraw, cx: int, cy: int):
    for side in (-1, 1):
        bx = cx + side * 96
        by = cy - 30
        ex = bx + side * 44
        ey = by - 78
        d.line([bx, by, ex, ey], fill=BODY_MID, width=36, joint="curve")
        d.ellipse([ex - 24, ey - 24, ex + 24, ey + 24], fill=BODY_TOP,
                  outline=OUTLINE, width=8)


def ground_shadow(d: ImageDraw.ImageDraw, cx: int, squash: float):
    w = int(96 * squash)
    d.ellipse([cx - w, CANVAS - 40, cx + w, CANVAS], fill=(*SHADOW, 72))


def draw_sparkles(d: ImageDraw.ImageDraw, t: float):
    """Twinkling plus-shaped glints for the stretch pose."""
    font = _font(30)
    d.text((CANVAS // 2 + 150, CANVAS // 2 - 260), "*", font=font,
           fill=(255, 244, 178, int(255 * (0.5 + 0.5 * math.sin(t * 6)))))
    d.text((CANVAS // 2 - 230, CANVAS // 2 - 190), "+", font=font,
           fill=(190, 255, 240, int(255 * (0.5 + 0.5 * math.cos(t * 6)))))


def z_puffs(d: ImageDraw.ImageDraw, t: float):
    """Rising 'z Z' above the sleeping head; ``t`` in [0..1)."""
    f1 = _font(30)
    f2 = _font(44)
    cx = CANVAS // 2 + 30          # follow the shifted body center
    y1 = int(CANVAS * 0.45 - 70 * t)
    d.text((cx + 96, y1), "z", font=f1, fill=PUFF)
    if t > 0.3:
        k = (t - 0.3) / 0.7
        d.text((cx + 40, y1 - int(80 * k)), "Z", font=f2, fill=(240, 248, 255))


_FONT_CACHE: dict[int, ImageFont.ImageFont] = {}


def _font(size: int):
    if size not in _FONT_CACHE:
        try:
            _FONT_CACHE[size] = ImageFont.load_default(size=size)
        except TypeError:
            _FONT_CACHE[size] = ImageFont.load_default()
    return _FONT_CACHE[size]


# --------------------------------------------------------------------------
# frame renderers — each state lists 3-4 frames (list index = frame number)
# --------------------------------------------------------------------------
def frame_idle(fi: int) -> Image.Image:
    bob = [0, -16, -6][fi % 3]          # gentle breathing
    cx, cy = CANVAS // 2, 210 + bob
    canvas = layer()
    ground_shadow(ImageDraw.Draw(canvas), cx, squash=0.94 - 0.05 * (bob / 16))
    canvas.alpha_composite(base_body(cx, cy))
    d = ImageDraw.Draw(canvas)
    draw_eyes(d, cx, cy - 42, open_=(fi != 1), look_dx=14 + fi * 6)   # blink on frame 1
    draw_cheeks(d, cx, cy + 30)
    draw_mouth(d, cx, cy + 46, "smile")
    return _finalize(canvas)


def frame_walking(fi: int) -> Image.Image:
    sway = [-8, 8, -6, 6][fi % 4]       # body rocks with the gait
    bob = [0, -12, 0, -12][fi % 4]
    cx = CANVAS // 2 + sway
    cy = 210 + bob
    canvas = layer()
    ground_shadow(ImageDraw.Draw(canvas), CANVAS // 2, squash=0.9)
    canvas.alpha_composite(base_body(cx, cy))
    d = ImageDraw.Draw(canvas)
    draw_feet(d, cx, 348, phase=fi / 4)
    draw_eyes(d, cx, cy - 42, open_=True, look_dx=60)   # looks where it walks
    draw_mouth(d, cx, cy + 46, "smile")
    return _finalize(canvas)


def frame_sleeping(fi: int) -> Image.Image:
    t = fi / 3
    squash = [1.0, 0.92, 0.86][fi % 3]   # slow breath while prone
    cx = CANVAS // 2 + 30                # shifted right so puffs fit on the left
    cy = 238
    canvas = layer()
    ground_shadow(ImageDraw.Draw(canvas), CANVAS // 2, squash=1.05)
    canvas.alpha_composite(base_body(cx, cy, squash_y=squash))
    d = ImageDraw.Draw(canvas)
    draw_eyes(d, cx, cy - 34, open_=False)
    draw_cheeks(d, cx, cy + 36)
    z_puffs(d, t)
    return _finalize(canvas)


def frame_talking(fi: int) -> Image.Image:
    bob = [0, -8, 0, -12][fi % 4]        # excited bounces
    cx, cy = CANVAS // 2, 206 + bob
    mouths = ["smile", "open_small", "o", "open_big"]   # word cadence
    canvas = layer()
    ground_shadow(ImageDraw.Draw(canvas), cx, squash=0.9)
    canvas.alpha_composite(base_body(cx, cy))
    d = ImageDraw.Draw(canvas)
    draw_eyes(d, cx, cy - 42, open_=True, look_dx=-16)
    draw_mouth(d, cx, cy + 46, mouths[fi % 4])
    return _finalize(canvas)


def frame_stretch(fi: int) -> Image.Image:
    stretch = [0, 14, 26, 14][fi % 4]    # torso reaches up and settles
    cx = CANVAS // 2
    cy = 198 - stretch
    canvas = layer()
    ground_shadow(ImageDraw.Draw(canvas), cx, squash=0.88)
    canvas.alpha_composite(base_body(cx, cy))
    d = ImageDraw.Draw(canvas)
    draw_arms_up(d, cx, cy + 30)
    draw_eyes(d, cx, cy - 46, open_=True, look_dx=0)
    draw_cheeks(d, cx, cy + 24)
    draw_mouth(d, cx, cy + 42, "open_small" if fi % 2 else "smile")
    draw_sparkles(d, fi / 4)
    return _finalize(canvas)


def frame_falling(fi: int) -> Image.Image:
    squash = [0.8, 1.15, 0.9][fi % 3]    # pancake-squash tumble
    cx = CANVAS // 2 + [-14, 10, -6][fi % 3]
    cy = 224
    canvas = layer()
    ground_shadow(ImageDraw.Draw(canvas), CANVAS // 2, squash=0.7)
    canvas.alpha_composite(base_body(cx, cy, squash_y=squash))
    d = ImageDraw.Draw(canvas)
    draw_arms_up(d, cx + (fi - 1) * 18, cy + 40)      # flailing
    draw_eyes(d, cx, cy - 36, open_=True, look_dx=[-40, 40, 0][fi % 3])
    draw_mouth(d, cx, cy + 52, "open_big" if fi else "o")
    rot = [-10, 8, -4][fi % 3]
    return _finalize(canvas, rotate_deg=rot)


def _finalize(canvas: Image.Image, rotate_deg: float = 0.0) -> Image.Image:
    if rotate_deg:
        canvas = canvas.rotate(rotate_deg, resample=Image.BICUBIC, expand=False,
                               center=(CANVAS // 2, CANVAS // 2))
    return canvas.resize((SIZE, SIZE), Image.LANCZOS)


# --------------------------------------------------------------------------
# GIF writer (RGBA frames -> transparent animated GIF)
# --------------------------------------------------------------------------
def save_gif(path: Path, frames: list[Image.Image], frame_ms: int):
    path.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(
        path,
        save_all=True,
        append_images=frames[1:],
        duration=frame_ms,
        loop=0,
        disposal=2,
        optimize=False,
    )


# --------------------------------------------------------------------------
# chiptune SFX synthesis (mono 8-bit style WAVs)
# --------------------------------------------------------------------------
def _tone(freq: float, dur_s: float, kind: str = "square",
          volume: int = 90, sr: int = 22050) -> bytes:
    """Render a single envelope-shaped tone to signed 16-bit samples."""
    n = int(dur_s * sr)
    out = bytearray()
    for i in range(n):
        t = i / sr
        env = min(1.0, i / (0.008 * sr)) * max(0.0, 1.0 - i / n ** 2 * dur_s * 4)
        # short exponential-ish decay: sharp attack, fast fade
        env *= math.exp(-t * (6.0 if kind == "noise" else 3.5))
        phase = 2 * math.pi * freq * t
        if kind == "square":
            s = 1.0 if math.sin(phase) >= 0 else -1.0
        elif kind == "sine":
            s = math.sin(phase)
        elif kind == "noise":
            s = RNG.uniform(-1, 1)
        out += struct.pack("<h", int(volume * env * s))
    return bytes(out)


def _arp(freqs: list[float], step_s: float, sr: int = 22050) -> bytes:
    return b"".join(_tone(f, step_s * 1.9) for f in freqs)


def make_wav(path: Path, samples: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(22050)
        w.writeframes(samples)


# --------------------------------------------------------------------------
# FSM asset matrix (assets/config.yaml)
# --------------------------------------------------------------------------
FRAME_MS = 96

ASSET_STATES: dict[str, dict] = {
    "idle": {
        "sprite": "sprites/idle.gif", "duration_ms": 2400,
        "transitions_to": [
            {"name": "walking", "probability": 0.35},
            {"name": "talking", "probability": 0.15},
            {"name": "sleeping", "probability": 0.10},
            {"name": "idle", "probability": 0.40},
        ],
    },
    "walking": {
        "sprite": "sprites/walking.gif", "duration_ms": 2600, "move": [2, 0],
        "transitions_to": [
            {"name": "idle", "probability": 0.55},
            {"name": "talking", "probability": 0.15},
            {"name": "sleeping", "probability": 0.05},
            {"name": "walking", "probability": 0.25},
        ],
    },
    "sleeping": {
        "sprite": "sprites/sleeping.gif", "duration_ms": 4800,
        "transitions_to": [
            {"name": "sleeping", "probability": 0.60},
            {"name": "idle", "probability": 0.40},
        ],
    },
    "talking": {
        "sprite": "sprites/talking.gif", "duration_ms": 2800,
        "transitions_to": [
            {"name": "idle", "probability": 0.95},
            {"name": "walking", "probability": 0.05},
        ],
    },
    # forced/exception states (reached via FSM.force_state, not weighted picks)
    "falling": {
        "sprite": "sprites/falling.gif", "duration_ms": 3600, "one_shot": False,
        "transitions_to": [{"name": "idle", "probability": 1.0}],
    },
    "stretch_nudge": {
        "sprite": "sprites/stretch_nudge.gif", "duration_ms": 4200, "one_shot": True,
        "transitions_to": [{"name": "idle", "probability": 1.0}],
    },
}


def build_fsm_config() -> dict:
    return {
        "pet": "vaultsprite",
        "size": [SIZE, SIZE],
        "default_frame_ms": FRAME_MS,
        "initial_state": "idle",
        "states": ASSET_STATES,
    }


# --------------------------------------------------------------------------
def main():
    global ASSETS  # noqa: PLW0603
    out_sprites = ASSETS / "sprites"
    out_sounds = ASSETS / "sounds"

    renderers = {
        "idle": frame_idle,
        "walking": frame_walking,
        "sleeping": frame_sleeping,
        "talking": frame_talking,
        "falling": frame_falling,
        "stretch_nudge": frame_stretch,
    }

    # sanity render: verify transparency survives the GIF round-trip
    for name, fn in renderers.items():
        frames = [fn(i) for i in range(4)]
        save_gif(out_sprites / f"{name}.gif", frames, FRAME_MS)
        check = Image.open(out_sprites / f"{name}.gif").convert("RGBA")
        corner_a = check.getpixel((2, 2))[3]
        center_a = check.getpixel((SIZE // 2, SIZE // 2))[3]
        print(f"sprite {name:14s} -> GIF {out_sprites / f'{name}.gif'} "
              f"(corner alpha={corner_a}, center alpha={center_a})")

    make_wav(out_sounds / "step.wav", _tone(720, 0.05) + _tone(640, 0.04))
    make_wav(out_sounds / "chirp.wav",
             _arp([1318.5, 1760.0], 0.07) + _tone(2093.0, 0.09))
    make_wav(out_sounds / "yawn.wav",
             _tone(420, 0.28, kind="sine") + _tone(300, 0.34, kind="sine"))

    (ASSETS / "config.yaml").write_text(
        yaml.safe_dump(build_fsm_config(), sort_keys=False), encoding="utf-8"
    )
    print(f"wrote assets/config.yaml with {len(ASSET_STATES)} states")
    print("asset generation complete.")


if __name__ == "__main__":
    main()

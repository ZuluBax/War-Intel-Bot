"""Draws the two animated alert emoji for the target board (own artwork, no licence issues).

    uv run --no-project --with pillow python scripts/make_alert_emoji.py app/discord/assets

Changing a GIF? Also delete the old emoji in the Discord Developer Portal (your app ->
Emojis): the bot only uploads an emoji whose name it does not already own.

GIF only has on/off transparency, so each frame is drawn 4x large, shrunk (smooth edges),
then alpha is cut at 50%. Discord shows emoji at 22-48 px, where that reads cleanly.
"""

import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw

S, K = 128, 4  # final size, supersample factor
B = S * K


def finish(big: Image.Image) -> Image.Image:
    small = big.resize((S, S), Image.LANCZOS)
    a = small.getchannel("A").point(lambda v: 255 if v >= 128 else 0)
    small.putalpha(a)
    return small


def save(frames: list[Image.Image], out: Path, name: str, ms: int) -> None:
    # Palette with a reserved transparent index, same for all frames.
    conv = []
    for f in frames:
        p = f.convert("RGB").quantize(colors=255, method=Image.MEDIANCUT, dither=Image.NONE)
        p.putpalette([*(p.getpalette() or [])[:765], 0, 0, 0])
        mask = f.getchannel("A").point(lambda v: 255 if v == 0 else 0)
        p.paste(255, mask=mask)
        conv.append(p)
    conv[0].save(
        out / name,
        save_all=True,
        append_images=conv[1:],
        duration=ms,
        loop=0,
        transparency=255,
        disposal=2,
        optimize=False,
    )
    print(name, (out / name).stat().st_size, "bytes")


def lerp(
    c1: tuple[int, int, int], c2: tuple[int, int, int], t: float, alpha: int = 255
) -> tuple[int, int, int, int]:
    r, g, b = (round(a + (b - a) * t) for a, b in zip(c1, c2, strict=True))
    return (r, g, b, alpha)


def triangle(cx: float, cy: float, r: float) -> list[tuple[float, float]]:
    return [(cx, cy - r), (cx + r * 0.98, cy + r * 0.78), (cx - r * 0.98, cy + r * 0.78)]


# --- pulsing warning triangle ---------------------------------------------------------
def warning(out: Path, n: int = 14) -> None:
    frames = []
    for i in range(n):
        t = (1 - math.cos(2 * math.pi * i / n)) / 2  # 0 -> 1 -> 0
        img = Image.new("RGBA", (B, B), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        scale = 0.86 + 0.14 * t
        cx, cy = B / 2, B / 2 + B * 0.04
        d.polygon(triangle(cx, cy, B * 0.5 * scale), fill=(40, 20, 0, 255))  # dark outline
        d.polygon(
            triangle(cx, cy, B * 0.5 * scale - B * 0.05),
            fill=lerp((255, 196, 0), (255, 120, 0), t),
        )
        # exclamation mark
        w = B * 0.075 * scale
        top, bot = cy - B * 0.22 * scale, cy + B * 0.12 * scale
        d.rounded_rectangle([cx - w, top, cx + w, bot], radius=w, fill=(30, 15, 0, 255))
        r = B * 0.07 * scale
        dy = cy + B * 0.25 * scale
        d.ellipse([cx - r, dy - r, cx + r, dy + r], fill=(30, 15, 0, 255))
        frames.append(finish(img))
    save(frames, out, "twi_warning_pulse.gif", 70)


# --- spinning siren -------------------------------------------------------------------
def siren(out: Path, n: int = 16) -> None:
    frames = []
    for i in range(n):
        ang = 2 * math.pi * i / n
        img = Image.new("RGBA", (B, B), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        cx, dome_cy = B / 2, B * 0.5
        # rotating light beam: two opposite wedges, drawn behind the dome
        span = 26
        deg = math.degrees(ang)
        for off in (0, 180):
            a0 = deg + off
            br = B * 0.46  # stays inside the square so the beam is never clipped
            d.pieslice(
                [cx - br, dome_cy - br, cx + br, dome_cy + br],
                a0 - span,
                a0 + span,
                fill=(255, 225, 60, 255),
            )
        # base
        d.rounded_rectangle(
            [B * 0.16, B * 0.74, B * 0.84, B * 0.92],
            radius=B * 0.04,
            fill=(70, 70, 80, 255),
            outline=(20, 20, 25, 255),
            width=int(B * 0.02),
        )
        # dome (brightness flickers as the lamp faces us)
        face = (1 + math.cos(ang)) / 2
        dome = lerp((170, 0, 10), (255, 40, 40), face)
        r = B * 0.3
        box = [cx - r, dome_cy - r * 1.05, cx + r, dome_cy + r * 1.05 + B * 0.2]
        d.pieslice(box, 180, 360, fill=dome)
        d.arc(box, 180, 360, fill=(60, 0, 0, 255), width=int(B * 0.02))  # no chord line
        lw = int(B * 0.02)
        d.rectangle([cx - r, dome_cy + B * 0.1, cx + r, B * 0.745], fill=dome)
        d.line(
            [cx - r + lw / 2, dome_cy + B * 0.1, cx - r + lw / 2, B * 0.745],
            fill=(60, 0, 0, 255),
            width=lw,
        )
        d.line(
            [cx + r - lw / 2, dome_cy + B * 0.1, cx + r - lw / 2, B * 0.745],
            fill=(60, 0, 0, 255),
            width=lw,
        )
        # the lamp inside, moving side to side as it spins
        lx = cx + math.sin(ang) * r * 0.55
        lr = B * 0.07
        d.ellipse(
            [lx - lr, dome_cy + B * 0.05 - lr, lx + lr, dome_cy + B * 0.05 + lr],
            fill=lerp((255, 150, 120), (255, 255, 220), face),
        )
        # shine
        d.ellipse(
            [cx - r * 0.6, dome_cy - r * 0.65, cx - r * 0.3, dome_cy - r * 0.2],
            fill=(255, 255, 255, 170),
        )
        frames.append(finish(img))
    save(frames, out, "twi_siren.gif", 55)


if __name__ == "__main__":
    target = Path(sys.argv[1])
    warning(target)
    siren(target)

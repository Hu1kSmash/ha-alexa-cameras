#!/usr/bin/env python3
"""Brand-asset generator for the Alexa Cameras (HLS) add-on.

Everything about the visual identity is drawn from code here, so it can be
recreated or tweaked later without any binary-editing:

  - alexa_cameras/icon.png          256x256 app / store / sidebar icon
  - alexa_cameras/logo.png          icon + "Alexa Cameras" wordmark (transparent)
  - docs/images/social-preview.png  1280x640 GitHub "social preview" banner
  - alexa_cameras/ui.py             the base64 header icon embedded in the Web UI

Design: a ceiling-mounted white bullet security camera inside a glowing
Alexa-style cyan->blue ring, on a dark-navy rounded tile. The camera geometry
is defined in a 360x360 "design space" (see make_icon) so the numbers are easy
to reason about; everything scales from there.

Usage:
    python3 branding/generate_branding.py            # regenerate every asset
    python3 branding/generate_branding.py --icon-only # just icon.png

Dependencies:
    - Pillow (PIL)
    - A bold variable sans for the wordmark/banner text (Ubuntu Sans by default).
      Only the logo and social banner use it; the icon itself is font-free.
      Override with --font /path/to/font.ttf if yours lives elsewhere.
"""
import argparse
import base64
import io
import math
import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter

REPO = Path(__file__).resolve().parent.parent

# ---- palette -----------------------------------------------------------------
CYAN  = (70, 228, 238)     # lens rim / accents
WHITE = (250, 253, 255)    # camera barrel
LIGHT = (222, 237, 255)    # mount + sunshield (slightly cooler than the barrel)
RING_A = (46, 238, 222)    # ring gradient, teal end
RING_B = (26, 100, 250)    # ring gradient, blue end
OUTLINE = (10, 14, 22)     # thin edge line around the camera (keeps it visible on light backgrounds)

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/ubuntu/UbuntuSans[wdth,wght].ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def gradient(w, h, c0, c1, diag=True):
    """Cheap smooth 2-stop gradient (rendered small, then scaled up)."""
    g = Image.new("RGB", (48, 48))
    p = g.load()
    for y in range(48):
        for x in range(48):
            t = ((x + y) / 94.0) if diag else y / 47.0
            p[x, y] = lerp(c0, c1, t)
    return g.resize((w, h), Image.BILINEAR)


def make_icon(px):
    """Render the app icon at px x px.

    A glowing Alexa ring with a ceiling-mounted bullet camera, on a TRANSPARENT
    full-bleed background — the ring reaches almost to the edges (no square tile).
    A thin dark outline traces the camera+mount silhouette so it stays visible on
    light backgrounds. Drawn at 768px for clean anti-aliasing, then downscaled.

    Coordinates are in a 360-unit design space centered on the ring; `f` scales the
    camera group and `box()`/`r()` map design units to pixels.
    """
    w = 768
    u = w / 360.0
    def U(v): return v * u
    img = Image.new("RGBA", (w, w), (0, 0, 0, 0))

    # --- glowing Alexa ring (gradient sweep + two blur passes for the glow) ---
    # The glow is kept tight and dimmed so it fully fades *inside* the canvas — a
    # bigger ring/looser glow would clip at the image edge and show a square halo.
    cx, cy = w / 2, w / 2
    R, th = U(148), U(26)
    ring = Image.new("RGBA", (w, w), (0, 0, 0, 0))
    rd = ImageDraw.Draw(ring)
    for a in range(0, 360, 2):
        t = (1 - math.cos(math.radians(a - 25))) / 2
        rd.arc([cx - R, cy - R, cx + R, cy + R], a, a + 3, fill=(*lerp(RING_A, RING_B, t), 255), width=int(th))
    def dim(layer, k):
        r, g, b, a = layer.split()
        return Image.merge("RGBA", (r, g, b, a.point(lambda v: int(v * k))))
    img = Image.alpha_composite(img, dim(ring.filter(ImageFilter.GaussianBlur(int(U(9)))), 0.55))
    img = Image.alpha_composite(img, dim(ring.filter(ImageFilter.GaussianBlur(int(U(4)))), 0.80))
    img = Image.alpha_composite(img, ring)

    # --- ceiling-mounted bullet camera, centered in the ring ---
    cam = Image.new("RGBA", (w, w), (0, 0, 0, 0))
    c = ImageDraw.Draw(cam)
    f = 0.87
    ox, oy = cx, cy + 41 * u * f     # design-space origin (barrel center); +41 auto-centers the camera group in the ring
    def box(x0, y0, x1, y1): return [ox + x0 * u * f, oy + y0 * u * f, ox + x1 * u * f, oy + y1 * u * f]
    r = lambda v: int(v * u * f)

    # ceiling mount: flange disc (with screw dots) -> short arm -> slim knuckle
    c.ellipse(box(-42, -124, 42, -104), fill=LIGHT + (255,))               # flange
    c.ellipse(box(-27, -117, -19, -111), fill=(150, 170, 198, 255))        # screw
    c.ellipse(box(19, -117, 27, -111), fill=(150, 170, 198, 255))          # screw
    c.rounded_rectangle(box(-11, -110, 11, -70), radius=r(10), fill=LIGHT + (255,))   # arm
    c.rounded_rectangle(box(-16, -79, 16, -55), radius=r(10), fill=LIGHT + (255,))    # knuckle
    # camera body
    c.rounded_rectangle(box(-84, -42, 78, 42), radius=r(41), fill=WHITE + (255,))     # barrel
    c.rounded_rectangle(box(-50, -61, 50, -33), radius=r(11), fill=LIGHT + (255,))    # sunshield (symmetric)
    # front lens
    c.ellipse(box(42, -40, 92, 40), fill=(13, 38, 84, 255))
    c.ellipse(box(50, -30, 86, 30), outline=(*CYAN, 255), width=r(9))
    c.ellipse(box(58, -19, 78, 19), fill=(7, 24, 60, 255))

    # thin dark outline traced around the whole camera+mount silhouette so it
    # stays legible on light backgrounds: dilate the silhouette, paint it OUTLINE,
    # and place it *behind* the camera.
    solid = cam.split()[3].point(lambda a: 255 if a > 70 else 0)
    stroke = solid.filter(ImageFilter.MaxFilter(9))
    outline = Image.new("RGBA", (w, w), OUTLINE + (255,))
    outline.putalpha(stroke)
    img = Image.alpha_composite(img, outline)
    img = Image.alpha_composite(img, cam)

    return img.resize((px, px), Image.LANCZOS)


# ---- text helpers (wordmark + banner) ---------------------------------------
def load_font(size, weight=800, font_path=None):
    path = font_path or next((p for p in FONT_CANDIDATES if Path(p).exists()), None)
    if not path:
        print("WARNING: no bold sans font found; wordmark/banner text will use PIL default.", file=sys.stderr)
        return ImageFont.load_default()
    f = ImageFont.truetype(path, size)
    try:
        f.set_variation_by_axes([100, weight])   # [wdth, wght] for variable fonts; ignored otherwise
    except Exception:
        pass
    return f


def gradient_wordmark(text, cap_px, c0, c1, font_path=None, weight=820, outline=False):
    """'Alexa Cameras' filled with a horizontal gradient, tight-cropped, transparent.

    With outline=True, a very thin, anti-aliased OUTLINE-coloured edge is stroked
    around the letters (rendered natively via the font's stroke, not a dilation, so
    it stays smooth and hairline-thin).
    """
    probe = load_font(300, weight, font_path)
    m = Image.new("L", (4000, 700), 0)
    ImageDraw.Draw(m).text((10, 10), "A", font=probe, fill=255)
    b = m.getbbox()
    f = load_font(max(1, int(300 * cap_px / (b[3] - b[1]))), weight, font_path)
    sw = max(1, round(cap_px * 0.014)) if outline else 0     # very thin stroke
    pad = sw + 6
    # fill = letter interiors; full = interiors + stroke (only differ when outline)
    fill = Image.new("L", (7000, 1000), 0)
    ImageDraw.Draw(fill).text((pad, pad), text, font=f, fill=255)
    full = fill
    if outline:
        full = Image.new("L", (7000, 1000), 0)
        ImageDraw.Draw(full).text((pad, pad), text, font=f, fill=255, stroke_width=sw, stroke_fill=255)
    bb = full.getbbox()
    fill, full = fill.crop(bb), full.crop(bb)
    tw, tht = full.size
    fp = fill.load()
    grd = Image.new("RGBA", (tw, tht), (0, 0, 0, 0))
    gp = grd.load()
    for x in range(tw):
        col = lerp(c0, c1, x / max(tw - 1, 1))
        for y in range(tht):
            a = fp[x, y]
            if a:
                gp[x, y] = (*col, a)
    if not outline:
        return grd
    ol = Image.new("RGBA", (tw, tht), OUTLINE + (255,))
    ol.putalpha(full)                       # black under the whole silhouette; gradient on top
    return Image.alpha_composite(ol, grd)


def make_logo(font_path=None):
    """icon + 'Alexa Cameras' wordmark on a transparent canvas."""
    isz = 210
    ic = make_icon(isz)
    wm = gradient_wordmark("Alexa Cameras", int(isz * 0.82), (44, 150, 240), (52, 214, 236), font_path, outline=True)
    pad, gap = 24, int(isz * 0.26)
    H = isz + pad * 2
    W = pad + isz + gap + wm.width + pad
    logo = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    logo.alpha_composite(ic, (pad, pad))
    logo.alpha_composite(wm, (pad + isz + gap, pad + (isz - wm.height) // 2))
    return logo


def make_social(font_path=None):
    """1280x640 GitHub social-preview banner (rendered at 2x, downscaled)."""
    S = 2
    W, H = 1280 * S, 640 * S
    img = gradient(W, H, (8, 16, 30), (12, 34, 58), diag=False).convert("RGBA")
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gx, gy = int(W * 0.5), int(H * 0.36)
    ImageDraw.Draw(glow).ellipse([gx - 560 * S, gy - 320 * S, gx + 560 * S, gy + 320 * S], fill=(34, 120, 220, 110))
    img = Image.alpha_composite(img, glow.filter(ImageFilter.GaussianBlur(90 * S)))
    draw = ImageDraw.Draw(img)

    def width_of(s, f): b = draw.textbbox((0, 0), s, font=f); return b[2] - b[0]
    def centered(y, s, f, fill):
        b = draw.textbbox((0, 0), s, font=f)
        draw.text(((W - (b[2] - b[0])) // 2 - b[0], y - b[1]), s, font=f, fill=fill)

    # kicker (letter-tracked, baseline-aligned)
    kick, kf, tr = "HOME  ASSISTANT  ADD-ON", load_font(30 * S, 650, font_path), int(10 * S)
    ws = [kf.getlength(ch) for ch in kick]
    x = (W - (sum(ws) + tr * (len(kick) - 1))) / 2
    for ch, wch in zip(kick, ws):
        draw.text((x, int(92 * S)), ch, font=kf, fill=(96, 200, 240, 255), anchor="ls")
        x += wch + tr

    # icon (transparent, full-bleed — its ring glow separates it from the dark bg)
    isz = int(196 * S)
    icon = make_icon(isz)
    ix, iy = (W - isz) // 2, int(104 * S)
    img.alpha_composite(icon, (ix, iy))
    draw = ImageDraw.Draw(img)

    # wordmark (fit to ~70% width)
    wf = load_font(300, 820, font_path)
    wf = load_font(int(300 * int(W * 0.70) / width_of("Alexa Cameras", wf)), 820, font_path)
    ab = draw.textbbox((0, 0), "A", font=wf)
    wm = gradient_wordmark("Alexa Cameras", ab[3] - ab[1], (120, 185, 255), (52, 214, 236), font_path)
    img.alpha_composite(wm, ((W - wm.width) // 2, int(322 * S)))
    draw = ImageDraw.Draw(img)

    # tagline
    tag = "RTSP cameras on your Echo Show — the stream Alexa actually plays"
    tf = load_font(38 * S, 500, font_path)
    if width_of(tag, tf) > W * 0.86:
        tf = load_font(int(38 * S * (W * 0.86) / width_of(tag, tf)), 500, font_path)
    centered(int(452 * S), tag, tf, (168, 188, 212, 255))

    # feature pills
    pills = ["Home Assistant", "MPEG-TS HLS", "Echo Show", "Self-hosted"]
    pf = load_font(28 * S, 640, font_path)
    ph, gp, padx = int(58 * S), int(22 * S), int(30 * S)
    ws = [width_of(p, pf) + padx * 2 for p in pills]
    x, y = (W - (sum(ws) + gp * (len(pills) - 1))) // 2, int(524 * S)
    for p, wpill in zip(pills, ws):
        draw.rounded_rectangle([x, y, x + wpill, y + ph], radius=ph // 2, fill=(60, 120, 200, 46), outline=(96, 168, 230, 160), width=2 * S)
        bb = draw.textbbox((0, 0), p, font=pf)
        draw.text((x + (wpill - (bb[2] - bb[0])) // 2 - bb[0], y + ph // 2 - (bb[3] + bb[1]) // 2), p, font=pf, fill=(206, 224, 244, 255))
        x += wpill + gp

    # repo url + inner frame
    uf = load_font(24 * S, 500, font_path)
    url = "github.com/Hu1kSmash/ha-alexa-cameras"
    draw.text((W - width_of(url, uf) - int(46 * S), H - int(56 * S)), url, font=uf, fill=(122, 152, 182, 255))
    draw.rounded_rectangle([int(10 * S), int(10 * S), W - int(10 * S), H - int(10 * S)], radius=int(22 * S), outline=(70, 110, 160, 70), width=2 * S)
    return img.convert("RGB").resize((1280, 640), Image.LANCZOS)


def update_ui_header():
    """Replace the base64 header logo (icon + wordmark) embedded in the Web UI (ui.py)."""
    logo = make_logo()                                  # icon + "Alexa Cameras" wordmark
    h = 72                                              # compact embed; shown ~30px tall (2x for retina)
    logo = logo.resize((round(logo.width * h / logo.height), h), Image.LANCZOS)
    buf = io.BytesIO()
    logo.save(buf, "PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    ui = REPO / "alexa_cameras" / "ui.py"
    s = ui.read_text()
    s, n = re.subn(r"data:image/png;base64,[A-Za-z0-9+/=]+", "data:image/png;base64," + b64, s, count=1)
    if n != 1:
        raise SystemExit(f"ui.py: expected exactly one data:image to replace, found {n}")
    ui.write_text(s)
    return n


def main():
    ap = argparse.ArgumentParser(description="Regenerate Alexa Cameras brand assets.")
    ap.add_argument("--font", help="path to a bold (variable) sans .ttf for the wordmark/banner text")
    ap.add_argument("--icon-only", action="store_true", help="only write alexa_cameras/icon.png")
    args = ap.parse_args()

    make_icon(256).save(REPO / "alexa_cameras" / "icon.png")
    print("wrote alexa_cameras/icon.png (256x256)")
    if args.icon_only:
        return
    make_logo(args.font).save(REPO / "alexa_cameras" / "logo.png")
    print("wrote alexa_cameras/logo.png")
    (REPO / "docs" / "images").mkdir(parents=True, exist_ok=True)
    make_social(args.font).save(REPO / "docs" / "images" / "social-preview.png")
    print("wrote docs/images/social-preview.png (1280x640)")
    update_ui_header()
    print("updated Web UI header logo in alexa_cameras/ui.py")


if __name__ == "__main__":
    main()

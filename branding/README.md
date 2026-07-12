# Branding

All Alexa Cameras brand art is **generated from code** ([`generate_branding.py`](generate_branding.py))
so it can be recreated or tweaked later without any binary image editing.

## Regenerate every asset

```bash
pip install pillow
python3 branding/generate_branding.py
```

This (re)writes:

| Output | What it is |
|---|---|
| `../alexa_cameras/icon.png` | 256×256 app / store / sidebar icon |
| `../alexa_cameras/logo.png` | icon + "Alexa Cameras" wordmark (transparent) |
| `../docs/images/social-preview.png` | 1280×640 GitHub social-preview banner |
| `../alexa_cameras/ui.py` | the base64 header icon embedded in the Web UI |

Options:

- `--icon-only` — only rewrite `icon.png`
- `--font /path/to/font.ttf` — use a specific bold/variable sans for the wordmark
  and banner text (defaults to Ubuntu Sans; the icon itself is font-free)

## Design

A **ceiling-mounted white bullet security camera inside a glowing Alexa-style
cyan→blue ring**, on a **transparent, full-bleed** background — the ring runs
almost to the edges (no square tile). A thin dark outline (`OUTLINE`) traces the
camera so it stays visible on light backgrounds.

The camera and its mount are drawn in a **360-unit "design space" centered on the
ring** (see `make_icon()`), so the coordinates are easy to reason about and tweak:

- **Ring** — `R` (radius) and `th` (thickness); the colour sweep is `RING_A`→`RING_B`.
- **Mount** — flange disc → short arm → slim knuckle (all in `LIGHT`).
- **Camera** — `barrel` (widest, `WHITE`), `sunshield` (symmetric cap), and the
  three-ellipse `lens` (dark face, `CYAN` rim, dark pupil).
- **`f`** scales the whole camera group inside the ring.

The palette is defined once at the top of `generate_branding.py`.

## Notes

- After changing the icon, **bump the add-on `version`** in
  `alexa_cameras/config.yaml` so Home Assistant refreshes its cached icon.
- Updating the GitHub **social preview** image itself is a manual step: upload the
  regenerated `docs/images/social-preview.png` under **repo Settings → General →
  Social preview** (GitHub's API can't set it).

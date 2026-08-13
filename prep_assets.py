"""One-time asset prep for the GUI skin.

Reads the UXIF studio logo and emits:
  assets/logo.png             — full-strength RGBA copy (used at runtime)
  assets/logo_watermark.png   — faded copy for the panel full-bleed bg
  assets/icon.ico             — Windows multi-res icon (16/32/48/64/128/256)
  assets/icon.icns            — macOS icon (best-effort via Pillow)

It then refreshes the two inline base64 images inside figma-plugin/ui.html
(the corner logo and the banner backdrop) *in place*.

Note the difference from the PSD extractor's version of this script, which
regenerates ui.html wholesale from a template held in this file. That would
mean two copies of the plugin markup, and running the script would silently
revert any UI change made in ui.html itself. Here the markup lives only in
ui.html and this script just swaps the data URIs.

Run after pulling a new logo source or banner:
  python prep_assets.py
"""
from PIL import Image
import base64
import io
import os
import re
import sys

SRC = r"C:/Users/matt.laverty/Documents/UXIF_Process_Portfolio/images/Logo.png"
OUT_DIR = "assets"
BANNER_SRC = "assets/BannerBg.png"
FIGMA_PLUGIN_HTML = "figma-plugin/ui.html"
WATERMARK_ALPHA = 0.06   # how visible the bg-watermark logo is on the dark panel
ICO_SIZES = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
ICNS_SIZES = [(16, 16), (32, 32), (64, 64), (128, 128), (256, 256), (512, 512), (1024, 1024)]

if not os.path.exists(SRC):
    print(f"error: logo source not found at {SRC}", file=sys.stderr)
    print("Edit SRC at the top of this script to point at the UXIF logo.", file=sys.stderr)
    sys.exit(1)

os.makedirs(OUT_DIR, exist_ok=True)
src = Image.open(SRC).convert("RGBA")
print(f"Source: {SRC}  ({src.size[0]}x{src.size[1]})")

# 1. Full-strength copy
src.save(os.path.join(OUT_DIR, "logo.png"), optimize=True)
print(f"  wrote {OUT_DIR}/logo.png")

# 2. Watermark — multiply alpha by WATERMARK_ALPHA
r, g, b, a = src.split()
faded_a = a.point(lambda v: int(v * WATERMARK_ALPHA))
watermark = Image.merge("RGBA", (r, g, b, faded_a))
watermark.save(os.path.join(OUT_DIR, "logo_watermark.png"), optimize=True)
print(f"  wrote {OUT_DIR}/logo_watermark.png  (alpha x {WATERMARK_ALPHA})")

# 3. Square-pad source so icons aren't squished, then write multi-res .ico
side = max(src.size)
pad = int(side * 0.04)
canvas_side = side + pad * 2
icon_canvas = Image.new("RGBA", (canvas_side, canvas_side), (0, 0, 0, 0))
ox = (canvas_side - src.width) // 2
oy = (canvas_side - src.height) // 2
icon_canvas.paste(src, (ox, oy), src)

ico_path = os.path.join(OUT_DIR, "icon.ico")
icon_canvas.save(ico_path, sizes=ICO_SIZES)
print(f"  wrote {ico_path}  ({', '.join(f'{s[0]}x{s[1]}' for s in ICO_SIZES)})")

# 4. macOS .icns — Pillow can write ICNS but requires explicit sizes
icns_path = os.path.join(OUT_DIR, "icon.icns")
try:
    icon_canvas.save(icns_path, format="ICNS", sizes=ICNS_SIZES)
    print(f"  wrote {icns_path}")
except Exception as e:
    print(f"  skipped {icns_path}: {e}")


# 5. Refresh the inline images in the Figma plugin UI, leaving its markup alone.
def _data_uri(img_bytes, mime):
    return f"data:{mime};base64,{base64.b64encode(img_bytes).decode('ascii')}"


if not os.path.exists(FIGMA_PLUGIN_HTML):
    print(f"  skipped {FIGMA_PLUGIN_HTML}: not found")
    sys.exit(0)

# Downsample banner to ~640px wide JPEG (q60) — invisible at 5% opacity, tiny payload
banner = Image.open(BANNER_SRC).convert("RGB")
b_w = 640
b_h = int(banner.size[1] * (b_w / banner.size[0]))
buf = io.BytesIO()
banner.resize((b_w, b_h), Image.LANCZOS).save(buf, "JPEG", quality=60, optimize=True, progressive=True)
banner_uri = _data_uri(buf.getvalue(), "image/jpeg")
print(f"  banner data-uri: {len(banner_uri)//1024} KB")

# Logo small + transparent for the corner mark (96px tall preserves crisp detail)
logo_h = 96
lw = int(src.size[0] * (logo_h / src.size[1]))
buf = io.BytesIO()
src.resize((lw, logo_h), Image.LANCZOS).save(buf, "PNG", optimize=True)
logo_uri = _data_uri(buf.getvalue(), "image/png")
print(f"  logo data-uri:   {len(logo_uri)//1024} KB")

with open(FIGMA_PLUGIN_HTML, "r", encoding="utf-8") as fh:
    html = fh.read()

html, n_banner = re.subn(
    r'(background-image:\s*url\(")data:[^"]*(")',
    lambda m: m.group(1) + banner_uri + m.group(2),
    html,
    count=1,
)
html, n_logo = re.subn(
    r'(<img class="corner-logo" src=")data:[^"]*(")',
    lambda m: m.group(1) + logo_uri + m.group(2),
    html,
    count=1,
)

if not n_banner or not n_logo:
    print(
        f"  warning: patched banner={n_banner} logo={n_logo} in {FIGMA_PLUGIN_HTML} — "
        "expected 1 each. Check that the selectors in this script still match the markup.",
        file=sys.stderr,
    )

with open(FIGMA_PLUGIN_HTML, "w", encoding="utf-8") as fh:
    fh.write(html)
size_kb = os.path.getsize(FIGMA_PLUGIN_HTML) / 1024
print(f"  wrote {FIGMA_PLUGIN_HTML}  ({size_kb:.0f} KB total)")

print("\nDone.")

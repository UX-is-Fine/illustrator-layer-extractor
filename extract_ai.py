"""
Extract Illustrator layers into per-layer PNG/JPG + SVG files + a layers.json manifest.

No Illustrator install required. `.ai` files saved with "Create PDF Compatible File"
(Illustrator's default) are readable as PDF, where each Illustrator *layer* is stored
as a PDF Optional Content Group (OCG). This script isolates one OCG at a time and
renders it, which gives one image per layer with the rest of the artwork removed.

Convention:
  - Each top-level Illustrator layer = one exported plane. Sublayers and groups
    inside a layer are flattened into that layer's output.
  - Each artboard = one page in the manifest; artboards keep their own size.
  - Layer panel order is preserved; bottom-of-panel = back of z-order (index 0).
  - Name prefix "_" or "hidden_" skips the layer (reference/scratch material).
  - Layers hidden in Illustrator are skipped unless --include-hidden is passed.
  - Optional hints encoded in the layer name (pass-through into the manifest):
      "fg_tree@parallax=0.6@opaque@blend=screen"
      -> {"parallax": 0.6, "opaque": True, "blend": "screen"}
    Flag-style hints (no =value) become {"key": True}.

Because Illustrator art is vector, every layer is exported twice:
  - a trimmed raster (PNG, or JPEG when fully opaque)  -> Unity / Unreal / web
  - an artboard-sized SVG                              -> Figma, as real vectors

Output:
  out/
    00_<name>.png (or .jpg)     — trimmed raster, one per layer
    00_<name>.svg               — vector, artboard-sized so it drops in at 0,0
    ...
    composite.jpg               — flat render of the whole artboard
    layers.json                 — manifest with artboards, per-plane bounds,
                                  visibility, hints, and file references

  With more than one artboard, filenames gain an `aNN_` prefix.

Install:
  pip install -r requirements.txt

Usage:
  python extract_ai.py input.ai --out ./out
  python extract_ai.py input.ai --max-width 4000      # larger raster target
  python extract_ai.py input.ai --no-svg              # rasters only
  python extract_ai.py input.ai --artboard 2          # just the 2nd artboard
  python extract_ai.py input.ai --include-hidden
  python extract_ai.py input.ai --zip                 # also write <name>.figma.zip
"""

import argparse
import json
import re
import sys
import zipfile
from pathlib import Path

import pymupdf
from PIL import Image


SKIP_PREFIXES = ("_", "hidden_", "hidden-")

# PyMuPDF optional-content actions for set_layer_ui_config().
OC_ON, OC_TOGGLE, OC_OFF = 0, 1, 2


def parse_node_name(raw_name: str):
    """'fg_tree@parallax=0.6@opaque' -> ('fg_tree', {'parallax': 0.6, 'opaque': True})"""
    parts = [p.strip() for p in raw_name.split("@")]
    name = parts[0]
    hints = {}
    for part in parts[1:]:
        if "=" not in part:
            key = part.strip()
            if key:
                hints[key] = True
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        value = value.strip()
        try:
            hints[key] = int(value)
            continue
        except ValueError:
            pass
        try:
            hints[key] = float(value)
            continue
        except ValueError:
            pass
        hints[key] = value
    return name, hints


def sanitize_filename(name: str) -> str:
    safe = re.sub(r"[^a-z0-9_\-]+", "_", name.lower())
    safe = re.sub(r"_+", "_", safe).strip("_")
    return safe or "layer"


def strip_empty_groups(svg: str) -> str:
    """Drop the empty <g> shells MuPDF emits for the layers we switched off.

    Isolating one layer still writes a group per layer in the document, so a
    per-layer SVG arrives with one populated group and N empty ones. Left in,
    every layer imports into Figma with a pile of empty sibling groups.
    Looped, because removing an inner group can empty its parent.
    """
    empty = re.compile(r"<g\b[^>]*>\s*</g>\s*")
    while True:
        stripped = empty.sub("", svg)
        if stripped == svg:
            return svg
        svg = stripped


def count_top_level_objects(doc, pno) -> int:
    """Rough count of drawable objects at the top of a page's content stream.

    Used only to detect the under-separation case: Illustrator *groups* do not
    survive into an .ai file's PDF data (saving flattens the group tree into a
    flat run of Form XObject invocations), so a document with everything grouped
    under one layer exports as a single flat plane. Comparing this count against
    the number of planes we produced lets us say so out loud instead of
    reporting a useless success.
    """
    try:
        page = doc.load_page(pno)
        raw = b""
        for xref in page.get_contents():
            raw += doc.xref_stream(xref)
        text = raw.decode("latin-1", "replace")
        invocations = len(re.findall(r"/[A-Za-z0-9_.]+\s+Do\b", text))
        paints = len(re.findall(r"\b(?:f|f\*|B|B\*|b|b\*|S|s)\s", text))
        return invocations + paints
    except Exception:
        return 0


def probe_under_separated(ai_path):
    """Cheaply decide whether a file is grouped-but-unlayered, without rendering.

    Opening the document and comparing its layer count against the number of
    drawable objects costs a fraction of a second even on a 674 MB file, whereas
    a full extraction pass costs tens of seconds (and can waste a ~30s SVG
    generation). Lets the auto workflow skip straight to restructuring.

    Returns (under_separated, exportable_layers, top_level_objects).
    """
    doc = None
    try:
        doc = open_document(Path(ai_path))
        groups, _ = layer_groups(doc)
        exportable = [
            g for g in groups
            if not g["name"].lower().startswith(SKIP_PREFIXES) and g["visible"]
        ]
        worst = 0
        for pno in range(doc.page_count):
            worst = max(worst, count_top_level_objects(doc, pno))
        # Mirrors the post-extraction check: few planes, plenty of art.
        return (len(exportable) <= 2 and worst >= 8), len(exportable), worst
    except Exception:
        return False, 0, 0
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:
                pass


def open_document(ai_path: Path):
    """Open an .ai (or .pdf) with PyMuPDF, with a targeted error for the one
    failure mode artists actually hit: PDF compatibility switched off on save."""
    try:
        return pymupdf.open(ai_path)
    except Exception as err:
        head = b""
        try:
            with open(ai_path, "rb") as fh:
                head = fh.read(512)
        except OSError:
            pass
        if head.startswith(b"%!PS"):
            raise ValueError(
                f"{ai_path.name} has no PDF-compatible data, so its layers can't be read "
                "without Illustrator. Re-save it from Illustrator with "
                '"Create PDF Compatible File" checked (File > Save As > Illustrator '
                "Options), then run this again."
            ) from err
        raise ValueError(f"Could not open {ai_path.name} as an Illustrator/PDF file: {err}") from err


def layer_groups(doc):
    """Top-level Illustrator layers, each with the UI-config numbers that must be
    switched on together to render it.

    PyMuPDF exposes optional content as a flat list with a `depth` column. An
    Illustrator layer containing sublayers therefore appears as a depth-0 entry
    followed by its descendants. Isolating a layer means switching on the parent
    *and* every descendant — switching on the parent alone renders nothing,
    because the sublayers holding the actual art stay off.
    """
    configs = doc.layer_ui_configs()
    groups = []
    for i, cfg in enumerate(configs):
        if cfg.get("depth", 0) != 0:
            continue
        members = [cfg["number"]]
        for nxt in configs[i + 1:]:
            if nxt.get("depth", 0) <= 0:
                break
            members.append(nxt["number"])
        groups.append({
            "name": cfg.get("text") or f"layer_{cfg['number']}",
            "members": members,
            # Captured before any toggling, so it reflects how the artist saved the file.
            "visible": bool(cfg.get("on", 1)),
        })
    return groups, configs


def draw_order_names(doc, pno):
    """OCG names in the order their content first appears on the page.

    This is the real z-order: PDF paints in stream order, so first-drawn is
    backmost. It has to be read from the stream rather than taken from
    `layer_ui_configs()`, because that array is the *layer panel* order and its
    direction is producer-dependent — Illustrator writes it top-layer-first
    (i.e. reversed relative to painting), while PyMuPDF-generated files write it
    bottom-first. Trusting it inverted the z-order on real Illustrator files,
    which put the background in front and hid the whole comp on Figma import.

    Returns [] if the stream can't be parsed, in which case the caller keeps the
    panel order.
    """
    try:
        page = doc.load_page(pno)
        ocgs = doc.get_ocgs()
        marked = {name: xref for (name, xref, _kind) in page.get_oc_items()}
        raw = b""
        for xref in page.get_contents():
            raw += doc.xref_stream(xref)
        text = raw.decode("latin-1", "replace")

        order = []
        for match in re.finditer(r"/OC\s*/([A-Za-z0-9_.]+)\s*BDC", text):
            xref = marked.get(match.group(1))
            if xref in ocgs:
                name = ocgs[xref]["name"]
                if name not in order:
                    order.append(name)
        return order
    except Exception:
        return []


def order_groups_by_z(groups, order):
    """Sort top-level layers back-to-front so manifest index 0 is the backmost.

    Layers missing from `order` (no content on this artboard, or a name that
    collides) keep their relative panel position after the ones we could place.
    """
    if not order:
        return list(groups)
    rank = {}
    for i, name in enumerate(order):
        rank.setdefault(name, i)
    fallback_base = len(rank)
    return sorted(
        groups,
        key=lambda g: (rank[g["name"]], 0) if g["name"] in rank
        else (fallback_base + groups.index(g), 1),
    )


def layer_svg(doc, pno, bbox_px, scale, text_as_path):
    """SVG for the currently-isolated layer, trimmed to its own content.

    Emitting artboard-sized SVGs (the obvious approach, since the geometry is
    already positioned) means every vector layer imports into Figma as a
    full-canvas frame with a small piece of art somewhere inside. On a 53-layer
    comp that is 43 stacked full-canvas frames — technically positioned right,
    unusable in practice.

    Narrowing the page's cropbox to the layer's content box makes MuPDF emit a
    content-sized SVG: it sets width/height to the box and translates the
    geometry into it (adding a clip path), so the file stands alone and the
    consumer positions it at `bounds` exactly like the raster twin.

    The cropbox is restored before returning — it would otherwise leak into the
    next layer's render and the composite.
    """
    page = doc.load_page(pno)
    saved = pymupdf.Rect(page.cropbox)
    try:
        left, top, right, bottom = bbox_px
        # Back to unscaled page units; the render was scaled, the cropbox isn't.
        box = pymupdf.Rect(left / scale, top / scale, right / scale, bottom / scale)
        box = box & pymupdf.Rect(page.mediabox)
        # PDF rejects a degenerate cropbox, and sub-point layers do occur.
        if box.width < 1 or box.height < 1:
            box = pymupdf.Rect(
                box.x0, box.y0,
                box.x0 + max(1.0, box.width), box.y0 + max(1.0, box.height),
            )
        page.set_cropbox(box)
        page = doc.load_page(pno)  # reload so the narrowed box takes effect
        return strip_empty_groups(page.get_svg_image(
            matrix=pymupdf.Matrix(scale, scale),
            text_as_path=text_as_path,
        ))
    finally:
        try:
            doc.load_page(pno).set_cropbox(saved)
        except Exception:
            pass


def set_isolation(doc, all_numbers, on_numbers):
    on = set(on_numbers)
    for number in all_numbers:
        doc.set_layer_ui_config(number, OC_ON if number in on else OC_OFF)


def render_page(doc, pno, scale):
    """Render a page at `scale` with alpha, as a PIL RGBA image."""
    matrix = pymupdf.Matrix(scale, scale)
    pix = doc.load_page(pno).get_pixmap(matrix=matrix, alpha=True)
    return Image.frombytes("RGBA", (pix.width, pix.height), pix.samples)


def extract(
    ai_path,
    out_dir="./out",
    max_width=2400,
    clean=False,
    make_zip=False,
    jpeg_quality=88,
    include_hidden=False,
    emit_svg=True,
    svg_text_as_path=True,
    max_svg_mb=24.0,
    artboard=None,
    progress=None,
):
    """Core extraction. progress is an optional callback:
        progress(message: str, pct: float | None) -> None
    pct is a 0.0-1.0 fraction for a progress bar, or None for indeterminate.
    Returns a dict with manifest_path, zip_path (or None), out_dir, layer_count.
    """
    def _log(msg, pct=None):
        if progress:
            progress(msg, pct)
        elif sys.stdout is not None:  # sys.stdout can be None in PyInstaller --windowed builds
            print(msg)

    ai_path = Path(ai_path)
    out_dir = Path(out_dir)

    if not ai_path.exists():
        raise FileNotFoundError(f"Illustrator file not found at {ai_path}")

    out_dir.mkdir(parents=True, exist_ok=True)

    if clean:
        removed = 0
        for pattern in ("*.png", "*.jpg", "*.svg", "*.json"):
            for f in out_dir.glob(pattern):
                f.unlink()
                removed += 1
        _log(f"Cleaned {removed} existing file(s) from {out_dir}", 0.02)

    _log(f"Reading {ai_path.name}...", 0.05)
    doc = open_document(ai_path)

    groups, configs = layer_groups(doc)
    all_numbers = [c["number"] for c in configs]

    # Artboard selection. Illustrator writes one PDF page per artboard.
    page_indices = list(range(doc.page_count))
    if artboard is not None:
        if not 1 <= artboard <= doc.page_count:
            raise ValueError(
                f"--artboard {artboard} is out of range; {ai_path.name} has "
                f"{doc.page_count} artboard(s)."
            )
        page_indices = [artboard - 1]

    multi = len(page_indices) > 1
    if not groups:
        _log(
            "  [warning] No Illustrator layers found - the file may be flattened, or "
            "saved without PDF compatibility. Exporting each artboard as one plane.",
            0.08,
        )

    artboards_out = []
    total_files = 0
    used_filenames = set()
    warnings = []
    # Set when an artboard yields almost no planes despite holding lots of art —
    # i.e. the file is grouped but not layered. Callers use this to offer or run
    # the Illustrator restructuring pre-pass.
    under_separated = False

    # Illustrator art that is mostly placed bitmaps produces enormous SVGs:
    # MuPDF embeds every image as a base64 data URI, so one layer of a
    # raster-heavy comp measured 259 MB across 3286 embedded bitmaps — with no
    # vector benefit, since there are barely any real paths in it. Cap the size,
    # and once a layer trips the cap stop attempting SVG for the rest of the
    # document rather than burning ~30s per layer to throw each one away.
    svg_enabled = emit_svg
    max_svg_bytes = int(max_svg_mb * 1_000_000) if max_svg_mb and max_svg_mb > 0 else 0
    n_steps = max(1, len(page_indices) * max(1, len(groups)))
    step = 0

    for slot, pno in enumerate(page_indices):
        page_rect = doc.load_page(pno).rect
        src_w, src_h = page_rect.width, page_rect.height

        if max_width > 0 and src_w > max_width:
            scale = max_width / src_w
        else:
            scale = 1.0
        out_w = max(1, round(src_w * scale))
        out_h = max(1, round(src_h * scale))

        prefix = f"a{slot + 1:02d}_" if multi else ""
        layers_out = []

        # Order back-to-front per artboard, so index 0 is the backmost plane and
        # a consumer can paint in manifest order.
        ordered_groups = order_groups_by_z(groups, draw_order_names(doc, pno))

        # --- per-layer isolation ------------------------------------------------
        for group in ordered_groups:
            step += 1
            base_pct = 0.1 + 0.7 * (step / n_steps)
            raw_name = group["name"]

            if raw_name.lower().startswith(SKIP_PREFIXES):
                _log(f"  [skip prefix]  {raw_name!r}", base_pct)
                continue
            if not group["visible"] and not include_hidden:
                _log(f"  [skip hidden] {raw_name!r}", base_pct)
                continue

            name, hints = parse_node_name(raw_name)

            set_isolation(doc, all_numbers, group["members"])
            image = render_page(doc, pno, scale)

            # A layer that exists in the document but holds no art on *this*
            # artboard renders fully transparent — that's how we detect it.
            bbox = image.split()[-1].getbbox()
            if bbox is None:
                _log(f"  [skip empty]  {name!r} (nothing on artboard {slot + 1})", base_pct)
                continue

            _log(f"Extracting {name} (artboard {slot + 1})...", base_pct)

            left, top, right, bottom = bbox
            image = image.crop(bbox)

            sbase = sanitize_filename(name)
            stem = f"{prefix}{len(layers_out):02d}_{sbase}"
            if stem in used_filenames:
                i = 2
                while f"{stem}_{i}" in used_filenames:
                    i += 1
                stem = f"{stem}_{i}"
            used_filenames.add(stem)

            truly_opaque = image.split()[-1].getextrema()[0] >= 250
            hint_opaque = bool(hints.get("opaque")) or hints.get("encoding") in ("jpg", "jpeg")

            if truly_opaque or hint_opaque:
                flat = Image.new("RGB", image.size, (255, 255, 255))
                flat.paste(image, mask=image.split()[-1])
                filename = f"{stem}.jpg"
                flat.save(out_dir / filename, "JPEG", quality=jpeg_quality, optimize=True)
            else:
                filename = f"{stem}.png"
                image.save(out_dir / filename, "PNG", optimize=True)
            total_files += 1

            # Vector twin, trimmed to the same box as the raster so both are
            # placed identically at `bounds`.
            svg_filename = None
            if svg_enabled:
                svg = layer_svg(doc, pno, bbox, scale, svg_text_as_path)
                svg_bytes = len(svg.encode("utf-8"))
                if max_svg_bytes and svg_bytes > max_svg_bytes:
                    svg_enabled = False
                    msg = (
                        f"SVG for {name!r} would be {svg_bytes / 1e6:.0f} MB (cap "
                        f"{max_svg_mb:g} MB) - it is mostly embedded bitmaps, not vectors. "
                        "Skipping SVG for the rest of this document; rasters are unaffected. "
                        "Raise --max-svg-mb to override, or pass --no-svg to skip silently."
                    )
                    warnings.append(msg)
                    _log(f"  [skip svg] {msg}", base_pct)
                else:
                    svg_filename = f"{stem}.svg"
                    (out_dir / svg_filename).write_text(svg, encoding="utf-8")
                    total_files += 1

            layers_out.append({
                "id": name,
                "index": len(layers_out),
                "file": filename,
                "svg": svg_filename,
                "bounds": {"x": left, "y": top, "width": right - left, "height": bottom - top},
                # Illustrator bakes layer opacity and blend mode into the rendered
                # artwork; PDF optional content carries neither. These are kept for
                # manifest parity with the PSD extractor.
                "opacity": 1.0,
                "blend_mode": "normal",
                "visible": group["visible"],
                "hints": hints,
            })

        # --- flat composite for this artboard ----------------------------------
        _log(f"Compositing artboard {slot + 1}...", 0.85)
        set_isolation(doc, all_numbers, all_numbers)
        composite_filename = None
        if not groups or layers_out or include_hidden:
            comp = render_page(doc, pno, scale)
            flat = Image.new("RGB", comp.size, (255, 255, 255))
            flat.paste(comp, mask=comp.split()[-1])
            composite_filename = f"{prefix}composite.jpg" if multi else "composite.jpg"
            flat.save(out_dir / composite_filename, "JPEG", quality=jpeg_quality, optimize=True)
            total_files += 1

        # Flattened files have no layers to isolate; the composite *is* the plane.
        if not groups and composite_filename:
            layers_out.append({
                "id": ai_path.stem,
                "index": 0,
                "file": composite_filename,
                "svg": None,
                "bounds": {"x": 0, "y": 0, "width": out_w, "height": out_h},
                "opacity": 1.0,
                "blend_mode": "normal",
                "visible": True,
                "hints": {},
            })

        # Under-separation check: lots of art, almost no planes.
        if len(layers_out) <= 2:
            n_objects = count_top_level_objects(doc, pno)
            if n_objects >= 8:
                under_separated = True
                msg = (
                    f"artboard {slot + 1} exported only {len(layers_out)} plane(s) but holds "
                    f"~{n_objects} top-level objects. Illustrator groups do NOT survive into "
                    ".ai PDF data, so grouped-but-unlayered art collapses into one flat image. "
                    "To get one export per element, put each element on its own top-level "
                    "layer, or run restructure.py to do that automatically (needs Illustrator)."
                )
                warnings.append(msg)
                _log(f"  [WARNING] {msg}", 0.9)

        artboards_out.append({
            "index": slot,
            "source_index": pno + 1,
            "name": f"Artboard {pno + 1}",
            "canvas": {"width": out_w, "height": out_h},
            "source_canvas": {"width": round(src_w), "height": round(src_h)},
            "scale": round(scale, 6),
            "composite": composite_filename,
            "layers": layers_out,
        })

    _log("Writing manifest...", 0.93)
    first = artboards_out[0] if artboards_out else {
        "canvas": {"width": 0, "height": 0},
        "source_canvas": {"width": 0, "height": 0},
        "scale": 1.0,
        "composite": None,
        "layers": [],
    }
    manifest = {
        "format": "uxif-layers/1",
        "source": ai_path.name,
        "source_kind": "illustrator",
        # Top-level mirrors of the first artboard, so consumers written against the
        # PSD extractor's manifest keep working without knowing about artboards.
        "canvas": first["canvas"],
        "source_canvas": first["source_canvas"],
        "scale": first["scale"],
        "composite": first["composite"],
        "layers": first["layers"],
        "artboards": artboards_out,
        "warnings": warnings,
        "under_separated": under_separated,
    }
    manifest_path = out_dir / "layers.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    layer_count = sum(len(ab["layers"]) for ab in artboards_out)

    zip_path = None
    if make_zip:
        _log("Writing zip...", 0.97)
        zip_path = out_dir.parent / f"{ai_path.stem}.figma.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(manifest_path, "layers.json")
            written = set()
            for ab in artboards_out:
                for layer in ab["layers"]:
                    for key in ("file", "svg"):
                        fn = layer.get(key)
                        if fn and fn not in written and (out_dir / fn).exists():
                            z.write(out_dir / fn, fn)
                            written.add(fn)
                if ab["composite"] and ab["composite"] not in written:
                    z.write(out_dir / ab["composite"], ab["composite"])
                    written.add(ab["composite"])

    doc.close()
    _log(f"Done - {layer_count} layer(s) across {len(artboards_out)} artboard(s)", 1.0)

    return {
        "manifest_path": manifest_path,
        "zip_path": zip_path,
        "out_dir": out_dir,
        "layer_count": layer_count,
        "artboard_count": len(artboards_out),
        "file_count": total_files,
        "warnings": warnings,
        "under_separated": under_separated,
        "canvas": (first["canvas"]["width"], first["canvas"]["height"]),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("ai_path", help="Input .ai file (or PDF-compatible .pdf)")
    parser.add_argument("--out", default="./out", help="Output directory (default: ./out)")
    parser.add_argument("--include-hidden", action="store_true", help="Export layers that are hidden in Illustrator")
    parser.add_argument("--max-width", type=int, default=2400, help="Rasterize so artboard width <= this (default: 2400). Use 0 for 1:1 with the artboard's point size.")
    parser.add_argument("--artboard", type=int, default=None, help="Export only this artboard (1-based). Default: all artboards.")
    parser.add_argument("--no-svg", dest="emit_svg", action="store_false", help="Skip the per-layer SVG export (rasters only)")
    parser.add_argument("--svg-text", dest="svg_text_as_path", action="store_false", help="Keep text as text in SVG instead of converting to paths (needs the fonts on the consuming end)")
    parser.add_argument("--max-svg-mb", type=float, default=24.0, help="Skip SVG output once a layer's SVG exceeds this size in MB (default: 24). Raster-heavy art embeds bitmaps as base64 and can reach hundreds of MB with no vector benefit. Use 0 to disable the cap.")
    parser.add_argument("--clean", action="store_true", help="Delete existing PNG/JPG/SVG/JSON in the output dir before writing")
    parser.add_argument("--zip", dest="make_zip", action="store_true", help="After extraction, also write <name>.figma.zip containing the manifest + all assets (drop into the Figma plugin)")
    parser.add_argument("--jpeg-quality", type=int, default=88, help="Quality for JPEG encoding (default: 88)")
    parser.add_argument(
        "--auto",
        action="store_true",
        help="If the file is grouped but not layered (which otherwise exports as one flat plane), "
             "run the Illustrator restructuring pre-pass automatically and re-extract. Needs Illustrator.",
    )
    parser.add_argument(
        "--restructure",
        action="store_true",
        help="Always run the Illustrator restructuring pre-pass first, promoting every top-level group "
             "onto its own layer. Needs Illustrator. Implies --auto.",
    )
    args = parser.parse_args()

    common = dict(
        out_dir=args.out,
        max_width=args.max_width,
        clean=args.clean,
        make_zip=args.make_zip,
        jpeg_quality=args.jpeg_quality,
        include_hidden=args.include_hidden,
        emit_svg=args.emit_svg,
        svg_text_as_path=args.svg_text_as_path,
        max_svg_mb=args.max_svg_mb,
        artboard=args.artboard,
    )

    if args.auto or args.restructure:
        # Imported lazily so the default path never touches the Adobe-dependent
        # module — extract_ai.py on its own requires no Illustrator install.
        try:
            from restructure import extract_auto
        except ImportError as e:
            print(f"error: --auto/--restructure needs restructure.py alongside this script ({e})", file=sys.stderr)
            sys.exit(1)
        try:
            result = extract_auto(
                args.ai_path,
                progress=lambda m, p: print(m),
                force_restructure=args.restructure,
                **common,
            )
        except (FileNotFoundError, ValueError, RuntimeError, TimeoutError) as e:
            print(f"error: {e}", file=sys.stderr)
            sys.exit(1)
        print(
            f"\nWrote {result['manifest_path']} - {result['layer_count']} plane(s) "
            f"across {result['artboard_count']} artboard(s)."
        )
        if result.get("restructured_from"):
            print("Groups were split onto their own layers via Illustrator first.")
        if result.get("zip_path"):
            size_kb = result["zip_path"].stat().st_size / 1024
            print(f"Wrote {result['zip_path']} ({size_kb:.0f}KB) - drop into the Figma plugin.")
        for warning in result.get("warnings", []):
            print(f"\nWARNING: {warning}", file=sys.stderr)
        return

    try:
        result = extract(
            ai_path=args.ai_path,
            out_dir=args.out,
            max_width=args.max_width,
            clean=args.clean,
            make_zip=args.make_zip,
            jpeg_quality=args.jpeg_quality,
            include_hidden=args.include_hidden,
            emit_svg=args.emit_svg,
            svg_text_as_path=args.svg_text_as_path,
            max_svg_mb=args.max_svg_mb,
            artboard=args.artboard,
        )
    except (FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)

    print(
        f"\nWrote {result['manifest_path']} - {result['layer_count']} plane(s) "
        f"across {result['artboard_count']} artboard(s)."
    )
    # Repeat warnings at the end; the per-layer log scrolls past on big files.
    for warning in result.get("warnings", []):
        print(f"\nWARNING: {warning}", file=sys.stderr)
    if result["zip_path"]:
        size_kb = result["zip_path"].stat().st_size / 1024
        print(f"Wrote {result['zip_path']} ({size_kb:.0f}KB) - drop into the Figma plugin.")


if __name__ == "__main__":
    main()

# UXIF Illustrator Layer Extractor

Convert a layered Illustrator file into per-layer images **and per-layer SVGs**
plus a JSON manifest you can feed to any consumer — Figma, Unity, Unreal, web —
to reconstruct the comp with independent control over each plane.

**Illustrator does not need to be installed.** `.ai` files saved with
"Create PDF Compatible File" (Illustrator's default) carry PDF data, where each
Illustrator layer is stored as a PDF Optional Content Group. The extractor
switches one group on at a time and renders it, which yields one clean image
per layer with the rest of the artwork removed.

Sibling project to the [PSD Layer Extractor](https://github.com/UX-is-Fine/psd-layer-extractor),
sharing its manifest shape and workflow.

## What it does

Given an `.ai`, for each artboard, for each top-level Illustrator layer:

- Writes a **trimmed raster** (PNG if it has real transparency, JPEG if opaque
  or marked `@opaque`) — for Unity, Unreal, and the web.
- Writes an **artboard-sized SVG** — for Figma, as real editable vectors.
- Records canvas-space bounds, visibility, and any `@key=value` hints from the
  layer name into `layers.json`.
- Optionally bundles everything into a `.figma.zip` ready to drop into the
  included Figma plugin.

Also emits `composite.jpg` per artboard — a flat render of the whole artboard,
handy as a fallback image or for previewing before the manifest loads.

### Why the SVG matters

This is the main thing the PSD extractor can't do. Illustrator art is vector, so
each layer can be exported as geometry rather than pixels. The Figma plugin
imports those SVGs as editable vector nodes — paths stay paths, at any zoom —
while game engines take the rasters. One extraction serves both.

## Two ways to use it

### I'm an artist / I just want to extract layers

Download the latest **`Illustrator-Layer-Extractor-windows.exe`** from the
[Releases page](https://github.com/UX-is-Fine/illustrator-layer-extractor/releases)
(macOS `.zip` also attached), run it, drop an `.ai` onto the window. Output lands
in `<name>_layers/` next to your file and the folder opens automatically. A
`.figma.zip` is produced in the same spot for the
[Figma plugin](figma-plugin/README.md).

No Python or Illustrator install required.

### I'm a dev / I want the CLI or to contribute

Python 3.10+.

```bash
pip install -r requirements.txt
python gui.py                              # same GUI, from source
python extract_ai.py input.ai --out ./out  # CLI
```

## CLI usage

```bash
python extract_ai.py input.ai --out ./out
```

Common flags:

| Flag | Purpose |
|---|---|
| `--out DIR` | Output directory (default `./out`) |
| `--max-width N` | Rasterize so artboard width ≤ N (default 2400; use 0 for 1:1 with the artboard's point size) |
| `--artboard N` | Export only artboard N (1-based). Default: all artboards |
| `--no-svg` | Skip the per-layer SVG export (rasters only) |
| `--svg-text` | Keep text as text in SVG instead of converting to paths |
| `--clean` | Wipe existing PNG/JPG/SVG/JSON in the output dir before writing |
| `--zip` | Also write `<name>.figma.zip` for the Figma plugin |
| `--include-hidden` | Export layers that are hidden in Illustrator |
| `--jpeg-quality N` | JPEG quality 1–100 (default 88) |

Because the source is vector, `--max-width` is a free choice — raise it for
print-resolution rasters without any quality loss from the original art.

## Illustrator naming convention

The extractor treats each **top-level layer** as one export unit. Nest freely:
sublayers and groups inside a layer are flattened into that layer's output. Your
top-level layer structure is the spec.

- **Name layers semantically** (`bg_forest`, `fg_tree`, `mid_smoke`). Layer
  panel order = manifest z-order; bottom of the panel is index 0 (back).
- **Skip a layer** by prefixing its name with `_` or `hidden_` (reference/
  scratch material stays in the file without being exported).
- **Pass metadata into the manifest** with `@key=value` suffixes:
  `fg_tree@parallax=0.6@anchor=bottom`. Flag-style (no `=value`) also works:
  `bg@opaque`. The extractor has no opinion on what the keys mean — they're
  pass-through metadata that consumers interpret.
- **Built-in meaning for one hint:** `@opaque` (or `@encoding=jpg`) forces JPEG
  encoding and flattens any remaining transparent pixels over white.

## Manifest shape

```jsonc
{
  "format":        "uxif-layers/1",
  "source":        "hero.ai",
  "source_kind":   "illustrator",

  // Mirrors of the first artboard, so consumers written against the PSD
  // extractor's manifest keep working without knowing about artboards.
  "canvas":        { "width": 2400, "height": 1350 },
  "source_canvas": { "width": 1920, "height": 1080 },
  "scale":         1.25,
  "composite":     "composite.jpg",
  "layers":        [ /* same as artboards[0].layers */ ],

  "artboards": [
    {
      "index":         0,
      "source_index":  1,
      "name":          "Artboard 1",
      "canvas":        { "width": 2400, "height": 1350 },
      "source_canvas": { "width": 1920, "height": 1080 },
      "scale":         1.25,
      "composite":     "composite.jpg",
      "layers": [
        {
          "id":         "bg_forest",
          "index":      0,
          "file":       "00_bg_forest.jpg",
          "svg":        "00_bg_forest.svg",
          "bounds":     { "x": 0, "y": 0, "width": 2400, "height": 1350 },
          "opacity":    1.0,
          "blend_mode": "normal",
          "visible":    true,
          "hints":      { "opaque": true }
        }
      ]
    }
  ]
}
```

Coordinates are in canvas pixels, at the extraction scale. With more than one
artboard, filenames gain an `aNN_` prefix (`a02_00_bg_forest.png`).

**On `opacity` and `blend_mode`:** Illustrator bakes layer opacity and blend
mode into the artwork, and PDF optional content carries neither, so these are
always `1.0` / `"normal"`. The rendered appearance is still correct — the blend
is already in the pixels and geometry — there's just no separate value to
re-apply downstream. The fields exist for manifest parity with the PSD
extractor.

## Figma plugin

See `figma-plugin/README.md`. One Frame per artboard, laid out left to right,
each layer imported as an editable vector node (or a flat raster, your choice).
Use `extract_ai.py --zip` (or the GUI, which always produces a zip) to create
the file the plugin accepts.

## Unity / Unreal

The rasters and manifest are engine-agnostic. `bounds` gives each plane's
position in canvas pixels, and `index` gives z-order back-to-front — enough to
rebuild the comp as sprites or UI widgets. The `@hints` mechanism is there to
carry whatever your runtime needs (parallax factors, anchors, sorting groups)
without the extractor needing to know about it.

## Building the desktop app from source

`build.spec` is a PyInstaller spec producing a windowed GUI executable plus a
CLI binary. Artists get prebuilt binaries from GitHub Releases; devs can build
locally:

```bash
pip install -r requirements.txt pyinstaller
pyinstaller build.spec --clean --noconfirm
# output: dist/Illustrator Layer Extractor.exe  (Windows GUI)
#         dist/Illustrator Layer Extractor.app  (Mac GUI)
#         dist/ai-extract[.exe]                 (CLI)
```

Releases are tag-driven: push a `vX.Y.Z` tag and CI builds Windows + macOS
binaries and attaches them to the Release. Binaries are unsigned — on macOS,
first launch needs right-click → Open to get past Gatekeeper.

## Known caveats

- **`.ai` saved without PDF compatibility cannot be read.** The file is then
  pure PostScript with no layer data available outside Illustrator. The
  extractor detects this and tells you to re-save with "Create PDF Compatible
  File" checked (File → Save As → Illustrator Options). It's on by default, so
  this is rare.
- A **flattened** file (no layers) still works — each artboard exports as a
  single plane, with a warning.
- Gradient meshes, complex effects (glows, feathers), and some transparency
  groups may approximate in the SVG. The raster twin is always exact, and the
  Figma plugin falls back to it if an SVG fails to parse.
- Text is converted to paths by default; `--svg-text` keeps it as text but the
  consuming end then needs the fonts.
- Clipping masks inside a layer are applied as rendered.

When an Illustrator feature doesn't export cleanly, flatten or expand the
problem layer in Illustrator before export.

## License

MIT.

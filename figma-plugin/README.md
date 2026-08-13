# UXIF Illustrator Layer Importer (Figma plugin)

Imports the output of `extract_ai.py` into Figma as one Frame per artboard,
with each Illustrator layer rebuilt inside it.

By default layers come in as **editable vector nodes**, built from the
per-layer SVGs the extractor writes. That's the main reason to go through
Illustrator rather than Photoshop: the geometry survives the trip, so paths
stay editable in Figma instead of arriving as flat images.

## Install (development mode)

1. Download **Illustrator-Layer-Extractor-Figma-Plugin.zip** from the
   [latest release](https://github.com/UX-is-Fine/illustrator-layer-extractor/releases/latest)
   and unzip it. (Or, if you have the repo cloned, skip this step — the
   `figma-plugin/` folder is already on disk.)
2. Open Figma → **Plugins** → **Development** → **Import plugin from manifest…**
3. Select `figma-plugin/manifest.json` from the unzipped folder.

The plugin shows up under *Plugins → Development → UXIF Illustrator Layer Importer*.

## Use

1. Run `python extract_ai.py your.ai --zip` (or use the GUI, which always
   writes a zip). This produces `your.figma.zip`.
2. In Figma, open the plugin.
3. Leave **Import as editable vectors** checked for vector output, or uncheck
   it to import flat rasters instead.
4. Drag the `.zip` into the drop zone (or click to pick one).
5. One Frame per artboard is created at your viewport center, laid out left to
   right, each containing its named layers.

## What it imports

For each layer in each artboard of `layers.json`:

- **Name:** the layer's `id` (the Illustrator layer name, with `@hints` stripped).
- **Geometry:** in vector mode, the per-layer SVG, as real Figma vector nodes.
  The SVG is artboard-sized with art already positioned, so it lands at the
  frame origin.
- **Position/size:** in raster mode, `bounds.x/y/width/height` in canvas pixels.
- **Fill:** in raster mode, the layer's PNG or JPG as an `IMAGE` fill.

## Vector vs raster

| | Vector (default) | Raster |
|---|---|---|
| Editable paths in Figma | yes | no |
| Resolution-independent | yes | no (fixed at extraction `--max-width`) |
| Gradient meshes, complex effects | may approximate | exact as rendered |
| Import speed on huge files | slower | faster |

If a layer's SVG is missing or fails to parse, the plugin silently falls back
to that layer's raster rather than dropping it — so a partial vector import
still gives you the complete comp.

## Known limits

- Illustrator bakes layer opacity and blend mode into the artwork itself; PDF
  optional content carries neither, so the manifest reports `opacity: 1.0` and
  `blend_mode: "normal"`. The *appearance* is still correct — it's already in
  the rendered geometry — there's just no separate blend mode to re-apply in
  Figma.
- Text is converted to paths by default. Run the extractor with `--svg-text`
  to keep it as text, but the consuming Figma file then needs the fonts.
- Hints (`@parallax`, `@anchor`, `@opaque`, etc.) are NOT applied in Figma —
  they're inert metadata for downstream runtimes.
- Layers hidden in Illustrator aren't included unless you ran the extractor
  with `--include-hidden`.

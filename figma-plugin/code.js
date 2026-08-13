// Runs in the Figma plugin context. Receives { manifest, artboards, images,
// svgs, wantVector } from ui.html and reconstructs the Illustrator document as
// one Frame per artboard, laid out left to right.
//
// Each layer imports either as an editable vector node (from its per-layer SVG)
// or as an image-filled Rectangle. Vector is preferred for Illustrator art —
// the whole point of coming from .ai rather than .psd is that the geometry
// survives — with the raster kept as an automatic fallback.

const BLEND_MAP = {
  normal: "NORMAL",
  darken: "DARKEN",
  multiply: "MULTIPLY",
  color_burn: "COLOR_BURN",
  linear_burn: "LINEAR_BURN",
  lighten: "LIGHTEN",
  screen: "SCREEN",
  color_dodge: "COLOR_DODGE",
  linear_dodge: "LINEAR_DODGE",
  overlay: "OVERLAY",
  soft_light: "SOFT_LIGHT",
  hard_light: "HARD_LIGHT",
  difference: "DIFFERENCE",
  exclusion: "EXCLUSION",
  hue: "HUE",
  saturation: "SATURATION",
  color: "COLOR",
  luminosity: "LUMINOSITY",
};

// Gap between artboard frames on the Figma canvas.
const ARTBOARD_GAP = 80;

figma.showUI(__html__, { width: 400, height: 320 });

function applyLayerProps(node, layer) {
  node.name = layer.id;
  if (typeof layer.opacity === "number") {
    node.opacity = Math.max(0, Math.min(1, layer.opacity));
  }
  const bm = BLEND_MAP[(layer.blend_mode || "normal").toLowerCase()];
  if (bm && bm !== "NORMAL") node.blendMode = bm;
}

// Vector path: the per-layer SVG is artboard-sized with the art already in
// place, so the node drops in at the frame origin rather than at layer bounds.
function importVector(svgText, layer) {
  const node = figma.createNodeFromSvg(svgText);
  applyLayerProps(node, layer);
  node.x = 0;
  node.y = 0;
  return node;
}

function importRaster(bytes, layer) {
  const figImage = figma.createImage(new Uint8Array(bytes));
  const rect = figma.createRectangle();
  applyLayerProps(rect, layer);
  rect.x = layer.bounds.x;
  rect.y = layer.bounds.y;
  rect.resize(Math.max(1, layer.bounds.width), Math.max(1, layer.bounds.height));
  rect.fills = [{ type: "IMAGE", scaleMode: "FILL", imageHash: figImage.hash }];
  return rect;
}

figma.ui.onmessage = async (msg) => {
  if (msg.type !== "import") return;
  try {
    const { manifest, artboards, images, svgs, wantVector } = msg;

    const frames = [];
    let imported = 0;
    let vectorCount = 0;
    let rasterCount = 0;

    // Lay artboards out in a row starting at the viewport centre.
    const totalWidth =
      artboards.reduce((sum, ab) => sum + ab.canvas.width, 0) +
      ARTBOARD_GAP * Math.max(0, artboards.length - 1);
    const vp = figma.viewport.center;
    let cursorX = Math.round(vp.x - totalWidth / 2);
    const tallest = Math.max.apply(null, artboards.map((ab) => ab.canvas.height));

    for (const ab of artboards) {
      const frame = figma.createFrame();
      frame.name =
        artboards.length > 1
          ? `${manifest.source || "Illustrator"} — ${ab.name}`
          : manifest.source || "Illustrator Import";
      frame.resize(ab.canvas.width, ab.canvas.height);
      frame.fills = []; // transparent background
      frame.clipsContent = false;
      frame.x = cursorX;
      frame.y = Math.round(vp.y - tallest / 2);
      cursorX += ab.canvas.width + ARTBOARD_GAP;

      figma.currentPage.appendChild(frame);
      frames.push(frame);

      for (const layer of ab.layers || []) {
        let node = null;

        if (wantVector && layer.svg && svgs[layer.svg]) {
          try {
            node = importVector(svgs[layer.svg], layer);
            vectorCount += 1;
          } catch (err) {
            // Malformed or unsupported SVG — fall through to the raster twin
            // rather than dropping the layer entirely.
            console.warn(`Vector import failed for ${layer.id}, using raster`, err);
            node = null;
          }
        }

        if (!node) {
          const bytes = layer.file && images[layer.file];
          if (!bytes) {
            console.warn(`No usable asset for ${layer.id} (${layer.file})`);
            continue;
          }
          node = importRaster(bytes, layer);
          rasterCount += 1;
        }

        frame.appendChild(node);
        imported += 1;
      }
    }

    figma.currentPage.selection = frames;
    figma.viewport.scrollAndZoomIntoView(frames);

    const detail = [];
    if (vectorCount) detail.push(`${vectorCount} vector`);
    if (rasterCount) detail.push(`${rasterCount} raster`);
    figma.notify(
      `Imported ${imported} layer${imported === 1 ? "" : "s"}` +
        (detail.length ? ` (${detail.join(", ")})` : "") +
        ` from ${manifest.source || "Illustrator"}`
    );
    figma.closePlugin();
  } catch (err) {
    console.error(err);
    figma.notify("Import failed: " + (err && err.message ? err.message : String(err)), { error: true });
    figma.closePlugin();
  }
};

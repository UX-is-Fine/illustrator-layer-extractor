/*
 * UXIF Illustrator Layer Extractor — group-to-layer restructurer
 *
 * WHY THIS EXISTS
 * ---------------
 * The extractor's export unit is the top-level Illustrator *layer*, because
 * layers are the only structure that survives into an .ai file's PDF-compatible
 * data (as PDF Optional Content Groups). Illustrator *groups* do not survive:
 * saving flattens the group tree into a long, flat sequence of PDF Form XObject
 * invocations. On a real test file, ~20 panel groups became 1040 flat
 * invocations at uniform nesting depth.
 *
 * So a document with everything grouped under one "Layer 1" extracts to a
 * single flat plane, no matter how well organised the group tree is. The only
 * place the group tree still exists is Illustrator's own DOM — hence this
 * script.
 *
 * WHAT IT DOES
 * ------------
 * Promotes each top-level group (or path, text frame, image, ...) to its own
 * named layer, then saves a *copy* with PDF compatibility on. The extractor
 * then works on that copy unchanged, producing one trimmed export per element
 * plus positional JSON — matching the PSD extractor's behaviour.
 *
 * The source document is never saved over. Output goes to a new file.
 *
 * HOW TO RUN
 * ----------
 * From Illustrator:  File > Scripts > Other Script...  (prompts for a file)
 * From the CLI:      python restructure.py your.ai     (see that script)
 *
 * ExtendScript is ES3-era: no let/const, no arrow functions, no native JSON.
 * Keep it that way or Illustrator will throw a syntax error.
 */

// UXIF_OPTIONS may be pre-set by a generated bootstrap (see restructure.py).
// When absent, we fall back to prompting — which is what an artist running this
// from the Scripts menu gets.
if (typeof UXIF_OPTIONS === "undefined") {
    var UXIF_OPTIONS = null;
}

(function () {
    var MAX_NAME_LEN = 48;

    // Illustrator reports unnamed items as "", but users and older files can
    // also leave these placeholder-ish names behind; treat them as unnamed so
    // we fall through to deriving something meaningful from the content.
    var GENERIC_NAMES = ["", "<group>", "<path>", "<compound path>", "<clip group>",
                         "<rectangle>", "<ellipse>", "<line>", "<text>", "group",
                         "path", "layer", "layer 1"];

    var report = {
        ok: false,
        error: null,
        source: null,
        output: null,
        artboards: 0,
        original_layers: [],
        created: [],
        warnings: []
    };

    // ---------------------------------------------------------------- helpers

    function warn(msg) {
        report.warnings.push(msg);
    }

    function isGenericName(name) {
        if (name === null || name === undefined) return true;
        var n = String(name).toLowerCase();
        for (var i = 0; i < GENERIC_NAMES.length; i++) {
            if (n === GENERIC_NAMES[i]) return true;
        }
        return false;
    }

    function sanitize(name) {
        var s = String(name).toLowerCase();
        s = s.replace(/[^a-z0-9]+/g, "_");
        s = s.replace(/_+/g, "_");
        s = s.replace(/^_+|_+$/g, "");
        if (s.length > MAX_NAME_LEN) s = s.substring(0, MAX_NAME_LEN);
        return s;
    }

    /* Illustrator's letter-spacing/tracking is a formatting attribute, so
     * textFrame.contents returns the clean string ("NIGHT VISION"). Reading the
     * same text out of the PDF instead yields "N I G H T V I S I O N", because
     * tracking renders as individually positioned glyphs. That difference is a
     * concrete reason to derive names here rather than downstream. */
    function collectText(item, out, depth) {
        if (depth > 6 || out.length >= 12) return;
        try {
            if (item.typename === "TextFrame") {
                var c = String(item.contents).replace(/\s+/g, " ");
                c = c.replace(/^\s+|\s+$/g, "");
                if (c.length) out.push(c);
                return;
            }
            if (item.textFrames && item.textFrames.length) {
                for (var i = 0; i < item.textFrames.length; i++) {
                    collectText(item.textFrames[i], out, depth + 1);
                }
            }
            if (item.groupItems && item.groupItems.length) {
                for (var g = 0; g < item.groupItems.length; g++) {
                    collectText(item.groupItems[g], out, depth + 1);
                }
            }
        } catch (e) {
            // Some item types throw on property access; not worth failing over.
        }
    }

    /* Only accept text as a name when it reads like a label, not a value.
     * Naming by first-text-found produced actively misleading results on a real
     * game-UI file: a panel of six character cards came out as "60" because a
     * level readout sat inside it, and others became "x", "13_60", "37_300".
     * Requiring three consecutive letters keeps "upgrade" and "night_vision"
     * while rejecting counters, scores, and single glyphs — a generic
     * "group_04" beats a confidently wrong "60". */
    function looksLikeLabel(slug) {
        return /[a-z]{3,}/.test(slug);
    }

    function deriveName(item, index) {
        if (!isGenericName(item.name)) {
            var fromName = sanitize(item.name);
            if (fromName.length) return fromName;
        }

        var candidates = [];
        collectText(item, candidates, 0);
        for (var i = 0; i < candidates.length; i++) {
            var slug = sanitize(candidates[i]);
            if (slug.length && looksLikeLabel(slug)) return slug;
        }

        /* A text frame's own contents ARE its identity, so a numeric one is
         * informative rather than misleading — unlike a number scavenged from
         * inside a group. Keep it, but prefix it so the layer name can't be a
         * bare number. */
        if (item.typename === "TextFrame" && candidates.length) {
            var own = sanitize(candidates[0]);
            if (own.length) return "text_" + own;
        }

        // Fall back to the item's kind so at least the type is legible.
        var kind = String(item.typename || "item")
            .replace(/Item$|Frame$/, "")
            .toLowerCase();
        return sanitize(kind + "_" + pad(index));
    }

    function pad(n) {
        return (n < 10 ? "0" : "") + n;
    }

    function uniqueName(base, used) {
        var name = base.length ? base : "element";
        if (!used[name]) {
            used[name] = true;
            return name;
        }
        var i = 2;
        while (used[name + "_" + i]) i++;
        used[name + "_" + i] = true;
        return name + "_" + i;
    }

    // Direct children only. `pageItems` can surface nested descendants depending
    // on version, so filter on parent identity rather than trusting it. Works for
    // both Layer and GroupItem containers.
    function directChildren(container) {
        var out = [];
        var items = container.pageItems;
        for (var i = 0; i < items.length; i++) {
            var it = items[i];
            var isDirect = true;
            try {
                isDirect = (it.parent === container);
            } catch (e) {
                isDirect = true;
            }
            if (isDirect) out.push(it);
        }
        return out;
    }

    /* Find the container actually worth splitting.
     *
     * Artists commonly wrap an entire comp in one group, so the layer has a
     * single child and there is nothing to spread across layers. Two versions of
     * the same test file differed exactly this way: one exported to 21 planes,
     * the other to a single flat plane because everything sat inside one wrapper
     * group. Descend through solitary wrappers until a container with two or
     * more children turns up.
     *
     * Clipped groups are never entered: moving children out of a clip group
     * discards the mask and changes the artwork. Compound paths aren't entered
     * either — their children are sub-paths of one shape, not separate elements.
     */
    function splitTarget(layer) {
        var container = layer;
        var kids = directChildren(container);
        var depth = 0;

        while (kids.length === 1 && depth < 12) {
            var only = kids[0];
            if (only.typename !== "GroupItem") break;
            var isClipped = false;
            try {
                isClipped = (only.clipped === true);
            } catch (e) {
                isClipped = false;
            }
            if (isClipped) break;

            var inner = directChildren(only);
            if (inner.length === 0) break;

            container = only;
            kids = inner;
            depth++;
            if (inner.length > 1) break;
        }

        return { container: container, kids: kids, depth: depth };
    }

    function jsonEscape(s) {
        s = String(s);
        s = s.replace(/\\/g, "\\\\").replace(/"/g, '\\"');
        s = s.replace(/[\r]/g, "\\r").replace(/[\n]/g, "\\n").replace(/[\t]/g, "\\t");
        return s;
    }

    // No native JSON in ExtendScript — serialize by hand.
    function toJson(o, indent) {
        var pre = indent || "";
        if (o === null || o === undefined) return "null";
        var t = typeof o;
        if (t === "number") return isFinite(o) ? String(o) : "null";
        if (t === "boolean") return o ? "true" : "false";
        if (t === "string") return '"' + jsonEscape(o) + '"';
        if (o instanceof Array) {
            if (!o.length) return "[]";
            var parts = [];
            for (var i = 0; i < o.length; i++) {
                parts.push(pre + "  " + toJson(o[i], pre + "  "));
            }
            return "[\n" + parts.join(",\n") + "\n" + pre + "]";
        }
        var keys = [];
        for (var k in o) {
            if (o.hasOwnProperty(k)) keys.push(k);
        }
        if (!keys.length) return "{}";
        var kv = [];
        for (var j = 0; j < keys.length; j++) {
            kv.push(pre + '  "' + jsonEscape(keys[j]) + '": ' + toJson(o[keys[j]], pre + "  "));
        }
        return "{\n" + kv.join(",\n") + "\n" + pre + "}";
    }

    function writeFile(path, text) {
        var f = new File(path);
        f.encoding = "UTF-8";
        if (!f.open("w")) return false;
        f.write(text);
        f.close();
        return true;
    }

    // ------------------------------------------------------------------- main

    var doc = null;
    var prevInteraction = app.userInteractionLevel;

    try {
        // Suppress modal dialogs — an unattended run must never block on one.
        app.userInteractionLevel = UserInteractionLevel.DONTDISPLAYALERTS;

        var inPath, outPath, reportPath, deleteEmpty;

        if (UXIF_OPTIONS) {
            inPath = UXIF_OPTIONS.inPath;
            outPath = UXIF_OPTIONS.outPath;
            reportPath = UXIF_OPTIONS.reportPath;
            deleteEmpty = UXIF_OPTIONS.deleteEmpty !== false;
        } else {
            var picked = File.openDialog("Choose an Illustrator file to restructure");
            if (!picked) return;
            inPath = picked.fsName;
            outPath = inPath.replace(/\.ai$/i, "") + "_restructured.ai";
            reportPath = inPath.replace(/\.ai$/i, "") + "_restructure_report.json";
            deleteEmpty = true;
        }

        report.source = inPath;
        report.output = outPath;

        var inFile = new File(inPath);
        if (!inFile.exists) throw new Error("Input file not found: " + inPath);

        doc = app.open(inFile);
        report.artboards = doc.artboards.length;

        // Snapshot the original top-level layers before we start adding new ones.
        var originals = [];
        for (var li = 0; li < doc.layers.length; li++) {
            originals.push(doc.layers[li]);
        }

        var used = {};
        var createdCount = 0;

        for (var oi = 0; oi < originals.length; oi++) {
            var layer = originals[oi];
            var layerName = layer.name;
            var wasLocked = layer.locked;
            var wasHidden = !layer.visible;

            // Locked or hidden layers reject move operations; unlock/show to
            // work, then restore the flags onto the layers we create so the
            // artist's intent survives.
            if (layer.locked) layer.locked = false;
            if (!layer.visible) layer.visible = true;

            if (layer.layers && layer.layers.length) {
                warn("Layer '" + layerName + "' has " + layer.layers.length +
                     " sublayer(s); sublayers are left as-is and still export as part of their parent.");
            }

            var target = splitTarget(layer);
            var container = target.container;
            var kids = target.kids;

            if (target.depth > 0) {
                warn("Layer '" + layerName + "' held everything inside " + target.depth +
                     " wrapper group(s); split the " + kids.length + " item(s) inside instead. " +
                     "Opacity, blend modes and effects applied to the wrapper group itself are not preserved.");
            }

            report.original_layers.push({
                name: layerName,
                children: kids.length,
                unwrapped: target.depth,
                locked: wasLocked,
                hidden: wasHidden
            });

            if (kids.length === 0) continue;

            // A layer holding a single element is already the right shape;
            // splitting it would just rename it for no gain.
            if (kids.length === 1) {
                if (isGenericName(layerName)) {
                    var solo = uniqueName(deriveName(kids[0], 0), used);
                    layer.name = solo;
                    report.created.push({ layer: solo, from: layerName, items: 1, renamed_only: true });
                } else {
                    used[sanitize(layerName)] = true;
                }
                layer.locked = wasLocked;
                layer.visible = !wasHidden;
                continue;
            }

            /* Z-order: layers.add() inserts at the TOP of the stack, and
             * index 0 of pageItems is the FRONTMOST item. Walking children
             * back-to-front (last index first) therefore rebuilds the stack in
             * the right order — the frontmost item's layer ends up on top. */
            for (var ki = kids.length - 1; ki >= 0; ki--) {
                var item = kids[ki];
                var name = uniqueName(deriveName(item, ki), used);

                var target = doc.layers.add();
                target.name = name;

                try {
                    item.move(target, ElementPlacement.PLACEATEND);
                } catch (moveErr) {
                    warn("Could not move item " + ki + " out of '" + layerName +
                         "': " + moveErr.message);
                    target.remove();
                    continue;
                }

                if (wasHidden) target.visible = false;
                if (wasLocked) target.locked = true;

                report.created.push({
                    layer: name,
                    from: layerName,
                    items: 1,
                    typename: String(item.typename)
                });
                createdCount++;
            }

            // Drop the wrapper group we emptied, so it doesn't linger as an
            // empty container in the output.
            if (container !== layer) {
                try {
                    if (directChildren(container).length === 0) container.remove();
                } catch (unwrapErr) {
                    warn("Could not remove the emptied wrapper group in '" + layerName +
                         "': " + unwrapErr.message);
                }
            }

            // The original layer should now be empty.
            var leftover = directChildren(layer);
            if (leftover.length === 0 && (!layer.layers || layer.layers.length === 0)) {
                if (deleteEmpty) {
                    try {
                        layer.remove();
                    } catch (rmErr) {
                        warn("Emptied '" + layerName + "' but could not remove it: " + rmErr.message);
                    }
                }
            } else {
                layer.locked = wasLocked;
                layer.visible = !wasHidden;
                warn("Layer '" + layerName + "' still holds " + leftover.length +
                     " item(s) after restructuring.");
            }
        }

        if (createdCount === 0) {
            warn("No groups were promoted — the document may already be one element per layer.");
        }

        // Layer-level appearance can't follow items onto new layers.
        warn("Layer-level opacity, blend modes, and layer clipping masks are not " +
             "transferred to the new layers. Check the output if the source used them.");

        var saveOpts = new IllustratorSaveOptions();
        // Non-negotiable: without PDF compatibility the extractor cannot read
        // the result at all, which would defeat the entire purpose.
        saveOpts.pdfCompatible = true;
        doc.saveAs(new File(outPath), saveOpts);

        report.ok = true;
        report.created_count = createdCount;
    } catch (err) {
        report.ok = false;
        report.error = (err && err.message) ? err.message : String(err);
    } finally {
        try {
            if (doc) doc.close(SaveOptions.DONOTSAVECHANGES);
        } catch (e) {}
        try {
            app.userInteractionLevel = prevInteraction;
        } catch (e) {}

        if (reportPath) {
            writeFile(reportPath, toJson(report, ""));
        }
    }
})();

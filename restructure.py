"""
Drive illustrator/restructure.jsx to promote Illustrator groups onto their own
layers, so the extractor can export one plane per element.

WHY THIS IS A SEPARATE, OPTIONAL STEP
-------------------------------------
extract_ai.py deliberately needs no Adobe software: it reads the PDF-compatible
data inside an .ai and treats each Illustrator *layer* as one export plane.
Layers survive into that PDF data as Optional Content Groups; *groups* do not —
saving flattens the group tree into a flat run of PDF Form XObject invocations.
So a document with 20 groups under one "Layer 1" extracts as a single flat plane
however tidy the group tree is.

Illustrator's DOM is the only place that group tree still exists. This script is
therefore a one-time cleanup pass on a messy handoff, not a runtime dependency:

    messy.ai --[this script + Illustrator]--> clean.ai --[extract_ai.py]--> layers + SVG + JSON

Everything downstream keeps working with no Adobe install.

Illustrator has no headless mode, so this launches the full application. It needs
an interactive desktop session and will not work in CI.

Usage:
  python restructure.py messy.ai
  python restructure.py messy.ai --out clean.ai
  python restructure.py messy.ai --timeout 600
  python restructure.py messy.ai --keep-empty-layers
"""

import argparse
import glob
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def resource_path(rel) -> Path:
    """Resolve a bundled resource for both source runs and PyInstaller builds.

    A frozen build unpacks data files to sys._MEIPASS, so __file__-relative
    lookups miss the .jsx entirely and the GUI's auto-restructure silently
    degrades to a one-plane export.
    """
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / rel
    return Path(__file__).parent / rel


def find_illustrator():
    """Locate the Illustrator executable, newest version first."""
    system = platform.system()
    if system == "Windows":
        pattern = r"C:\Program Files\Adobe\Adobe Illustrator*\Support Files\Contents\Windows\Illustrator.exe"
        hits = glob.glob(pattern)
        if not hits:
            hits = glob.glob(pattern.replace(r"C:\Program Files", r"C:\Program Files (x86)"))
        return sorted(hits)[-1] if hits else None
    if system == "Darwin":
        hits = glob.glob("/Applications/Adobe Illustrator*/Adobe Illustrator.app")
        return sorted(hits)[-1] if hits else None
    return None


def jsx_string(path) -> str:
    """Escape a path for embedding in an ExtendScript string literal."""
    return str(path).replace("\\", "\\\\").replace('"', '\\"')


def restructure(ai_path, out_path=None, timeout=600, delete_empty=True, verbose=True):
    ai_path = Path(ai_path).resolve()
    if not ai_path.exists():
        raise FileNotFoundError(f"Illustrator file not found at {ai_path}")

    illustrator = find_illustrator()
    if not illustrator:
        raise RuntimeError(
            "Could not find Adobe Illustrator. This step needs Illustrator installed "
            "(it is the only source of the group structure). The extractor itself "
            "does not — run extract_ai.py directly if you don't need restructuring."
        )

    out_path = Path(out_path).resolve() if out_path else ai_path.with_name(f"{ai_path.stem}_restructured.ai")

    main_jsx = resource_path("illustrator/restructure.jsx")
    if not main_jsx.exists():
        raise FileNotFoundError(
            f"Missing {main_jsx}. The .jsx must ship alongside this script "
            "(and be listed in build.spec's datas for frozen builds)."
        )

    # The report is how we learn what happened: Illustrator gives no useful exit
    # code, and the .jsx writes this file as its last act either way.
    tmp_dir = Path(tempfile.mkdtemp(prefix="uxif_restructure_"))
    report_path = tmp_dir / "report.json"

    # ExtendScript can't take argv, so generate a tiny bootstrap that sets the
    # options and includes the real script. Keeps restructure.jsx independently
    # runnable from Illustrator's Scripts menu (where it prompts instead).
    bootstrap = tmp_dir / "bootstrap.jsx"
    bootstrap.write_text(
        "var UXIF_OPTIONS = {\n"
        f'    inPath: "{jsx_string(ai_path)}",\n'
        f'    outPath: "{jsx_string(out_path)}",\n'
        f'    reportPath: "{jsx_string(report_path)}",\n'
        f"    deleteEmpty: {'true' if delete_empty else 'false'}\n"
        "};\n"
        f'$.evalFile(new File("{jsx_string(main_jsx)}"));\n',
        encoding="utf-8",
    )

    if report_path.exists():
        report_path.unlink()

    if verbose:
        print(f"Illustrator: {illustrator}")
        print(f"Restructuring {ai_path.name} -> {out_path.name}")
        print("Launching Illustrator (no headless mode exists; a window will open)...")

    if platform.system() == "Darwin":
        cmd = ["open", "-a", illustrator, str(bootstrap)]
    else:
        cmd = [illustrator, str(bootstrap)]
    subprocess.Popen(cmd)

    deadline = time.time() + timeout
    spinner = 0
    while time.time() < deadline:
        if report_path.exists():
            # Guard against reading a half-written file.
            time.sleep(0.4)
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                time.sleep(0.6)
                report = json.loads(report_path.read_text(encoding="utf-8"))
            break
        if verbose and spinner % 10 == 0:
            waited = int(time.time() - (deadline - timeout))
            print(f"  waiting for Illustrator... {waited}s", end="\r")
        spinner += 1
        time.sleep(0.5)
    else:
        raise TimeoutError(
            f"Illustrator did not finish within {timeout}s. It may be showing a dialog "
            "(a font-substitution or missing-link prompt will block an unattended run) "
            "or still opening a large document. Check the Illustrator window."
        )

    if verbose:
        print(" " * 50, end="\r")

    if not report.get("ok"):
        raise RuntimeError(f"Restructuring failed in Illustrator: {report.get('error')}")

    return report, out_path


def extract_auto(
    ai_path,
    out_dir,
    progress=None,
    force_restructure=False,
    timeout=2400,
    **extract_kwargs,
):
    """Extract, and transparently restructure first when the file needs it.

    This exists because the two-step workflow is a trap: extract_ai.py alone
    cannot split Illustrator groups (they don't survive into .ai PDF data), so
    running it on a grouped-but-unlayered file silently yields one flat plane.
    Callers get one call that does the right thing.

    Restructuring needs Illustrator. If it isn't installed we keep the
    single-plane result and leave the warning in place rather than failing —
    the extractor's no-Adobe-required guarantee still holds.

    Returns the extract() result dict, with `restructured_from` added when a
    pre-pass ran.
    """
    from extract_ai import extract, probe_under_separated

    def _log(msg, pct=None):
        if progress:
            progress(msg, pct)

    ai_path = Path(ai_path).resolve()
    result = None

    if not force_restructure:
        # Probe rather than extracting first: a full pass on a large file costs
        # tens of seconds and would be thrown away whenever restructuring is
        # needed. The probe only opens the document and counts.
        _log("Checking layer structure...", 0.01)
        needs_split, n_layers, n_objects = probe_under_separated(ai_path)
        if not needs_split:
            return extract(ai_path, out_dir=out_dir, progress=progress, **extract_kwargs)
        _log(
            f"Grouped but unlayered ({n_layers} layer(s), ~{n_objects} objects) - "
            "splitting groups via Illustrator...",
            0.05,
        )
    else:
        _log("Splitting groups via Illustrator...", 0.02)

    if not find_illustrator():
        if force_restructure:
            raise RuntimeError(
                "Restructuring needs Illustrator installed, and it was not found."
            )
        # No Illustrator: still produce the honest single-plane export. The
        # extractor's own warning tells the user why it came out flat.
        _log("Illustrator not found - exporting without splitting groups.", 0.1)
        return extract(ai_path, out_dir=out_dir, progress=progress, **extract_kwargs)

    tmp_dir = Path(tempfile.mkdtemp(prefix="uxif_auto_"))
    clean_ai = tmp_dir / f"{ai_path.stem}_restructured.ai"
    try:
        report, clean_ai = restructure(
            ai_path, out_path=clean_ai, timeout=timeout, verbose=False
        )
    except (RuntimeError, TimeoutError) as err:
        if force_restructure:
            raise
        _log(f"Restructuring failed ({err}) - exporting without splitting groups.", 0.1)
        return extract(ai_path, out_dir=out_dir, progress=progress, **extract_kwargs)

    created = len(report.get("created", []))
    _log(f"Split into {created} layer(s); re-extracting...", 0.1)

    result = extract(clean_ai, out_dir=out_dir, progress=progress, **extract_kwargs)
    result["restructured_from"] = str(ai_path)
    result["restructure_report"] = report
    return result


def print_report(report, out_path):
    originals = report.get("original_layers", [])
    created = report.get("created", [])

    print()
    print(f"Source layers: {len(originals)}")
    for layer in originals:
        flags = []
        if layer.get("locked"):
            flags.append("locked")
        if layer.get("hidden"):
            flags.append("hidden")
        suffix = f" [{', '.join(flags)}]" if flags else ""
        print(f"  {layer['name']!r}: {layer['children']} top-level item(s){suffix}")

    print(f"\nLayers now in the restructured file: {len(created)}")
    for entry in created[:40]:
        note = " (renamed only)" if entry.get("renamed_only") else ""
        print(f"  {entry['layer']}{note}")
    if len(created) > 40:
        print(f"  ... and {len(created) - 40} more")

    warnings = report.get("warnings", [])
    if warnings:
        print("\nWarnings:")
        for w in warnings:
            print(f"  - {w}")

    print(f"\nWrote {out_path}")
    print(f"Next: python extract_ai.py \"{out_path.name}\" --zip")


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("ai_path", help="Input .ai file (never modified)")
    parser.add_argument("--out", default=None, help="Output .ai (default: <name>_restructured.ai)")
    parser.add_argument("--timeout", type=int, default=600, help="Seconds to wait for Illustrator (default: 600)")
    parser.add_argument(
        "--keep-empty-layers",
        dest="delete_empty",
        action="store_false",
        help="Leave the emptied original layers in place instead of removing them",
    )
    args = parser.parse_args()

    try:
        report, out_path = restructure(
            args.ai_path,
            out_path=args.out,
            timeout=args.timeout,
            delete_empty=args.delete_empty,
        )
    except (FileNotFoundError, RuntimeError, TimeoutError) as err:
        print(f"error: {err}", file=sys.stderr)
        sys.exit(1)

    print_report(report, out_path)


if __name__ == "__main__":
    main()

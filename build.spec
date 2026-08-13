# PyInstaller spec — produces both the artist GUI and a CLI binary from
# the same source tree.
#
# Build:   pyinstaller build.spec --clean --noconfirm
# Output:
#   dist/Illustrator Layer Extractor.exe   (Windows GUI)
#   dist/Illustrator Layer Extractor.app   (Mac GUI bundle)
#   dist/ai-extract.exe                    (Windows CLI)
#   dist/ai-extract                        (Mac CLI binary)

from PyInstaller.utils.hooks import collect_all
import sys

# pymupdf is shared by GUI + CLI; tkinterdnd2 is GUI-only (drag-drop).
mu_datas, mu_binaries, mu_hiddenimports = collect_all("pymupdf")
tk_datas, tk_binaries, tk_hiddenimports = collect_all("tkinterdnd2")

# Per-platform icon for the GUI EXE/.app. The CLI doesn't ship an icon.
_icon = "assets/icon.icns" if sys.platform == "darwin" else "assets/icon.ico"

# Modules we never want bundled — they bloat the binary by hundreds of MB
# if the dev machine happens to have them installed for other projects.
_excludes_common = [
    "torch", "torchvision", "torchaudio",
    "transformers", "tokenizers", "safetensors", "huggingface_hub",
    "cv2", "scipy", "matplotlib", "IPython",
    "notebook", "jupyter", "jupyter_client", "jupyter_core",
    "pandas", "sympy", "sklearn", "tensorboard", "networkx",
    "simple_lama_inpainting",
    # This tool reads .ai via PDF, not PSD — psd-tools may be present from the
    # sibling PSD extractor project but is never imported here.
    "psd_tools",
]

# ---------------------------------------------------------------------------
# GUI variant — windowed, includes tkinter + tkinterdnd2 + asset images.
# ---------------------------------------------------------------------------

gui_datas = [
    ("assets/icon.ico", "assets"),
    ("assets/icon.icns", "assets"),
    ("assets/logo.png", "assets"),
    ("assets/logo_watermark.png", "assets"),
] + mu_datas + tk_datas

gui_a = Analysis(
    ["gui.py"],
    pathex=[],
    binaries=mu_binaries + tk_binaries,
    datas=gui_datas,
    hiddenimports=mu_hiddenimports + tk_hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=_excludes_common,
    noarchive=False,
)
gui_pyz = PYZ(gui_a.pure)
gui_exe = EXE(
    gui_pyz,
    gui_a.scripts,
    gui_a.binaries,
    gui_a.datas,
    [],
    name="Illustrator Layer Extractor",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,        # windowed GUI — no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=_icon,
)

# On macOS, wrap the GUI EXE in a .app bundle so the output is a draggable
# Mac application instead of a bare Unix binary. Windows ignores BUNDLE.
if sys.platform == "darwin":
    gui_app = BUNDLE(
        gui_exe,
        name="Illustrator Layer Extractor.app",
        icon=_icon,
        bundle_identifier="com.uxisfine.illustrator-layer-extractor",
    )

# ---------------------------------------------------------------------------
# CLI variant — console, pymupdf only. No tkinter or image assets.
# Smaller binary; suitable for terminal/scripting use.
# ---------------------------------------------------------------------------

cli_a = Analysis(
    ["extract_ai.py"],
    pathex=[],
    binaries=mu_binaries,
    datas=mu_datas,
    hiddenimports=mu_hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # Strip GUI-only deps so the CLI binary stays lean.
    excludes=_excludes_common + ["tkinter", "_tkinter", "tkinterdnd2", "PIL.ImageTk"],
    noarchive=False,
)
cli_pyz = PYZ(cli_a.pure)
cli_exe = EXE(
    cli_pyz,
    cli_a.scripts,
    cli_a.binaries,
    cli_a.datas,
    [],
    name="ai-extract",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,         # CLI needs stdout/stderr
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

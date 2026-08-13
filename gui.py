"""
Tkinter GUI wrapper around extract_ai.extract().

Artist workflow:
  1. Launch gui.py (or the PyInstaller-built exe)
  2. Drop an .ai onto the window (or click to browse)
  3. Progress bar fills, status updates; output lands next to the file in
     <name>_layers/ and the folder is opened automatically
  4. A .figma.zip is also produced for the Figma plugin

Illustrator does not need to be installed or running.

Install for dev:
  pip install -r requirements.txt
  python gui.py

Drag-drop requires tkinterdnd2; click-to-pick works without it.
"""

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk

from PIL import Image, ImageTk

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    HAS_DND = True
except ImportError:
    HAS_DND = False

from extract_ai import extract


ACCEPTED_SUFFIXES = (".ai", ".pdf")


# --- theme -------------------------------------------------------------------
PANEL_BG    = "#0A0D11"
ACCENT      = "#FF6F20"
TEXT_HI     = "#FFFFFF"   # main readable text on dark
TEXT_LO     = "#FFFFFF"   # secondary/smaller text — also white per design
ERR         = "#E5544A"
BORDER      = "#2A2F38"   # button outline + neutral chrome
TROUGH      = "#050709"   # near-black empty progress-bar fill

FONT_HEAD   = ("Segoe UI", 20, "bold")
FONT_SUB    = ("Segoe UI", 10)
FONT_DROP   = ("Segoe UI", 14, "bold")
FONT_DROP_SUB = ("Segoe UI", 9)
FONT_STATUS = ("Segoe UI", 9)


def resource_path(rel):
    """Resolve an asset path that works for both source runs and PyInstaller bundles."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def open_in_file_manager(path):
    path = str(Path(path))
    try:
        if sys.platform == "win32":
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("UXIF Illustrator Layer Extractor")
        self.root.geometry("620x460")
        self.root.minsize(540, 400)
        self.root.configure(bg=PANEL_BG)

        # Window/taskbar icon (Windows: .ico; Linux/Mac fall back silently)
        try:
            self.root.iconbitmap(resource_path("assets/icon.ico"))
        except Exception:
            pass

        # Orange title bar — Windows 11 DWM. No-op on older Windows / Mac / Linux.
        self.root.after(50, self._tint_title_bar_windows)

        self.queue = queue.Queue()
        self.last_out_dir = None
        self.running = False

        # Corner logo (top-left). Full-strength, not faded — it's a brand mark, not a backdrop.
        self._logo_src = Image.open(resource_path("assets/logo.png")).convert("RGBA")
        self._logo_photo = None  # ImageTk.PhotoImage

        # Big bottom-left logo at 2% opacity — pre-bake the alpha multiply once.
        big = Image.open(resource_path("assets/logo.png")).convert("RGBA")
        r, g, b, a = big.split()
        self._big_logo_src = Image.merge("RGBA", (r, g, b, a.point(lambda v: int(v * 0.02))))
        self._big_logo_photo = None
        self._big_logo_last_h = None

        # Vertical gradient backdrop (gray on top -> black on bottom). Re-rendered on resize.
        self._gradient_photo = None
        self._gradient_last_size = None

        self._configure_ttk()
        self._build_ui()
        self._wire_events()

    # ----- styling -----
    def _configure_ttk(self):
        style = ttk.Style()
        # 'clam' theme honors color overrides on Windows; the default Win theme ignores most
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "UXIF.Horizontal.TProgressbar",
            troughcolor=TROUGH,
            background=ACCENT,
            bordercolor=PANEL_BG,
            lightcolor=ACCENT,
            darkcolor=ACCENT,
            thickness=10,
        )

    # ----- layout -----
    def _build_ui(self):
        # Single canvas covers the whole window; watermark + canvas text sit on it,
        # the few real widgets (progressbar, button) get embedded via create_window.
        self.canvas = tk.Canvas(
            self.root, bg=PANEL_BG, highlightthickness=0, bd=0,
        )
        self.canvas.pack(fill="both", expand=True)

        # Created first => bottom-of-stack. Order: gradient -> big bg-logo -> corner logo -> UI
        self._bg_id = self.canvas.create_image(0, 0, anchor="nw")
        self._big_logo_id = self.canvas.create_image(0, 0, anchor="sw")
        self._logo_id = self.canvas.create_image(0, 0, anchor="nw")

        # Headline
        self._head_id = self.canvas.create_text(
            0, 0, text="Illustrator Layer Extractor",
            font=FONT_HEAD, fill=ACCENT, anchor="n",
        )
        sub = "Drop an .ai below, or click to choose." if HAS_DND else "Click below to choose an .ai file."
        self._sub_id = self.canvas.create_text(
            0, 0, text=sub, font=FONT_SUB, fill=TEXT_LO, anchor="n",
        )

        # Drop area — dashed orange rectangle (lets the watermark show through)
        self._drop_rect = self.canvas.create_rectangle(
            0, 0, 0, 0, outline=ACCENT, width=2, dash=(6, 4),
        )
        self._drop_main = self.canvas.create_text(
            0, 0,
            text="Drop .ai here" if HAS_DND else "Click to choose .ai",
            font=FONT_DROP, fill=TEXT_HI, anchor="center",
        )
        self._drop_sub = self.canvas.create_text(
            0, 0,
            text="Output goes to <name>_layers/ next to your file.",
            font=FONT_DROP_SUB, fill=TEXT_LO, anchor="center",
        )

        # Status row
        self._status_id = self.canvas.create_text(
            0, 0, text="Ready.", font=FONT_STATUS, fill=TEXT_LO, anchor="w",
        )

        # Progressbar — embed an actual ttk widget so it can animate
        self.progress = ttk.Progressbar(
            self.canvas, mode="determinate", maximum=100,
            style="UXIF.Horizontal.TProgressbar",
        )
        self._progress_win = self.canvas.create_window(0, 0, window=self.progress, anchor="nw")

        # Custom canvas button (rounded rect + text). tk.Button can't do rounded corners.
        self._btn_text = "Open output folder"
        self._btn_font = ("Segoe UI", 10, "bold")
        self._btn_enabled = False
        # Two items — rounded-rect bg (polygon) and the label. Coords set in _relayout.
        self._btn_bg = self.canvas.create_polygon(0, 0, 0, 0, smooth=True, fill="#3A1F08", outline="")
        self._btn_label = self.canvas.create_text(0, 0, text=self._btn_text, font=self._btn_font, fill="#000000", anchor="center")
        for tag in (self._btn_bg, self._btn_label):
            self.canvas.tag_bind(tag, "<Button-1>", self._on_button_click)
            self.canvas.tag_bind(tag, "<Enter>", self._on_button_enter)
            self.canvas.tag_bind(tag, "<Leave>", self._on_button_leave)

    def _wire_events(self):
        self.canvas.bind("<Configure>", self._relayout)
        # Click anywhere in the drop rect (or on the labels inside it) opens the picker
        for tag in (self._drop_rect, self._drop_main, self._drop_sub):
            self.canvas.tag_bind(tag, "<Button-1>", self._pick_file)
        # Whole-canvas drag-drop target
        if HAS_DND:
            self.canvas.drop_target_register(DND_FILES)
            self.canvas.dnd_bind("<<Drop>>", self._on_drop)

    def _relayout(self, _event=None):
        c = self.canvas
        w = c.winfo_width()
        h = c.winfo_height()
        if w <= 1 or h <= 1:
            return

        # Gradient backdrop — gray (PANEL_BG) at top to pure black at bottom.
        if self._gradient_last_size != (w, h):
            self._gradient_photo = ImageTk.PhotoImage(self._make_gradient(w, h))
            c.itemconfigure(self._bg_id, image=self._gradient_photo)
            self._gradient_last_size = (w, h)
        c.coords(self._bg_id, 0, 0)

        # Big bottom-left logo — anchored sw, bleeds off the bottom-left corner.
        # Top of the logo lands at roughly h/2; bottom + left extend off the frame.
        BIG_OFF_X = 90   # px the logo's left edge is past the canvas's left edge
        BIG_OFF_Y = 100  # px the logo's bottom edge is past the canvas's bottom edge
        big_h = int(h * 0.5) + BIG_OFF_Y
        big_sw, big_sh = self._big_logo_src.size
        big_w = max(1, int(big_sw * (big_h / big_sh)))
        if self._big_logo_last_h != big_h:
            big = self._big_logo_src.resize((big_w, big_h), Image.LANCZOS)
            self._big_logo_photo = ImageTk.PhotoImage(big)
            c.itemconfigure(self._big_logo_id, image=self._big_logo_photo)
            self._big_logo_last_h = big_h
        c.coords(self._big_logo_id, -BIG_OFF_X, h + BIG_OFF_Y)

        # Corner logo — fixed 64px height, top-left with margin. Built lazily.
        logo_h = 64
        sw, sh = self._logo_src.size
        logo_w = max(1, int(sw * (logo_h / sh)))
        if self._logo_photo is None:
            logo = self._logo_src.resize((logo_w, logo_h), Image.LANCZOS)
            self._logo_photo = ImageTk.PhotoImage(logo)
            c.itemconfigure(self._logo_id, image=self._logo_photo)
        c.coords(self._logo_id, 18, 14)

        # Top stack
        c.coords(self._head_id, w / 2, 22)
        c.coords(self._sub_id, w / 2, 60)

        # Drop area
        margin = 28
        drop_top = 92
        drop_bottom = h - 90
        c.coords(self._drop_rect, margin, drop_top, w - margin, drop_bottom)
        c.coords(self._drop_main, w / 2, (drop_top + drop_bottom) / 2 - 12)
        c.coords(self._drop_sub, w / 2, (drop_top + drop_bottom) / 2 + 18)

        # Bottom row — status, progress, button. Button vertically centered on progress-bar mid-line.
        status_y = h - 64
        progress_y = h - 44
        progress_thickness = 10
        c.coords(self._status_id, margin, status_y)
        c.coords(self._progress_win, margin, progress_y)
        self.canvas.itemconfigure(self._progress_win, width=w - margin * 2 - 160)

        # Button: measure text and build a rounded rect with 20% radius.
        text_w = c.bbox(self._btn_label)
        if text_w:
            tw_px = text_w[2] - text_w[0]
        else:
            tw_px = 110  # fallback before first measure
        btn_h = 28
        btn_w = int(tw_px) + 30  # padding
        btn_r = int(btn_h * 0.20)  # 20% rounded corners
        btn_cy = progress_y + progress_thickness / 2
        btn_x2 = w - margin
        btn_x1 = btn_x2 - btn_w
        btn_y1 = btn_cy - btn_h / 2
        btn_y2 = btn_cy + btn_h / 2
        c.coords(self._btn_bg, *self._rounded_rect_points(btn_x1, btn_y1, btn_x2, btn_y2, btn_r))
        c.coords(self._btn_label, (btn_x1 + btn_x2) / 2, btn_cy)

    # ----- actions -----
    def _pick_file(self, _event=None):
        if self.running:
            return
        path = filedialog.askopenfilename(
            filetypes=[
                ("Illustrator Artwork", "*.ai"),
                ("PDF", "*.pdf"),
                ("All files", "*.*"),
            ],
        )
        if path:
            self._start_extract(path)

    def _on_drop(self, event):
        if self.running:
            return
        raw = event.data.strip()
        if raw.startswith("{") and raw.endswith("}"):
            raw = raw[1:-1]
        path = raw.split("} {")[0].strip("{}")
        if not path.lower().endswith(ACCEPTED_SUFFIXES):
            self._set_status("That's not an .ai file.", error=True)
            return
        self._start_extract(path)

    def _start_extract(self, ai_path):
        ai_path = Path(ai_path)
        out_dir = ai_path.parent / f"{ai_path.stem}_layers"

        self.running = True
        self._set_button_enabled(False)
        self.progress["value"] = 0
        self._set_status("Starting...")

        def cb(msg, pct):
            self.queue.put(("progress", msg, pct))

        def work():
            try:
                # extract_auto handles the case that surprises everyone: a file
                # whose art is grouped but not layered exports as one flat plane,
                # because Illustrator groups don't survive into .ai PDF data. It
                # runs the Illustrator restructuring pre-pass only when needed,
                # and falls back to the plain result if Illustrator is absent.
                try:
                    from restructure import extract_auto
                except ImportError:
                    extract_auto = None

                if extract_auto is not None:
                    result = extract_auto(
                        ai_path,
                        out_dir=out_dir,
                        clean=True,
                        make_zip=True,
                        progress=cb,
                    )
                else:
                    result = extract(
                        ai_path=ai_path,
                        out_dir=out_dir,
                        clean=True,
                        make_zip=True,
                        progress=cb,
                    )
                self.queue.put(("done", result, None))
            except Exception as err:
                self.queue.put(("error", str(err), None))

        threading.Thread(target=work, daemon=True).start()
        self.root.after(80, self._poll)

    def _poll(self):
        try:
            while True:
                kind, a, b = self.queue.get_nowait()
                if kind == "done":
                    result = a
                    self.running = False
                    self.last_out_dir = result["out_dir"]
                    self.progress["value"] = 100
                    n = result["layer_count"]
                    boards = result["artboard_count"]
                    msg = f"Done. Extracted {n} layer{'s' if n != 1 else ''}"
                    if boards > 1:
                        msg += f" across {boards} artboards"
                    msg += "."
                    if result.get("restructured_from"):
                        msg += "  (groups split via Illustrator)"
                    if result.get("zip_path"):
                        msg += f"  Figma zip: {Path(result['zip_path']).name}"
                    self._set_status(msg)
                    # A single plane out of a busy file means groups needed
                    # splitting and we couldn't do it — say so instead of
                    # letting it read as success.
                    if result.get("under_separated") and not result.get("restructured_from"):
                        self._set_status(
                            f"{msg}  This file is grouped but not layered - install "
                            "Illustrator to split it into separate layers.",
                            error=True,
                        )
                    self._set_button_enabled(True)
                    open_in_file_manager(self.last_out_dir)
                    return
                if kind == "error":
                    self.running = False
                    self._set_status(f"Error: {a}", error=True)
                    return
                msg, pct = a, b
                self._set_status(msg)
                if isinstance(pct, (int, float)):
                    self.progress["value"] = float(pct) * 100.0
        except queue.Empty:
            pass
        if self.running:
            self.root.after(80, self._poll)

    @staticmethod
    def _make_gradient(w, h):
        """Vertical gradient: PANEL_BG at the top -> pure black at the bottom."""
        top = (int(PANEL_BG[1:3], 16), int(PANEL_BG[3:5], 16), int(PANEL_BG[5:7], 16))
        bot = (0, 0, 0)
        col = Image.new("RGB", (1, h))
        px = col.load()
        denom = max(1, h - 1)
        for y in range(h):
            t = y / denom
            px[0, y] = (
                int(top[0] * (1 - t) + bot[0] * t),
                int(top[1] * (1 - t) + bot[1] * t),
                int(top[2] * (1 - t) + bot[2] * t),
            )
        return col.resize((w, h))

    def _tint_title_bar_windows(self):
        """Paint the OS title bar orange via DWM. Windows 11 build 22000+ only;
        silently no-ops elsewhere. Tk has no portable way to do this."""
        if sys.platform != "win32":
            return
        try:
            import ctypes
            self.root.update_idletasks()
            # Tk's winfo_id() is the inner HWND; the actual Toplevel HWND is its parent
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            if not hwnd:
                return
            DWMWA_CAPTION_COLOR = 35   # title-bar fill
            DWMWA_TEXT_COLOR    = 36   # title-bar text
            DWMWA_BORDER_COLOR  = 34   # window border

            def colorref(hex_rgb):
                # COLORREF is 0x00BBGGRR
                r = int(hex_rgb[1:3], 16)
                g = int(hex_rgb[3:5], 16)
                b = int(hex_rgb[5:7], 16)
                return (b << 16) | (g << 8) | r

            cap = ctypes.c_uint(colorref(ACCENT))
            txt = ctypes.c_uint(colorref("#000000"))
            brd = ctypes.c_uint(colorref(ACCENT))
            dwm = ctypes.windll.dwmapi
            for attr, val in ((DWMWA_CAPTION_COLOR, cap),
                              (DWMWA_TEXT_COLOR, txt),
                              (DWMWA_BORDER_COLOR, brd)):
                dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val), ctypes.sizeof(val))
        except Exception:
            pass

    @staticmethod
    def _rounded_rect_points(x1, y1, x2, y2, r):
        """Control points for a smooth-polygon rounded rectangle. Repeated points on the
        straight edges hold the polygon flat; corners get the Bezier interpolation."""
        return [
            x1 + r, y1, x1 + r, y1, x2 - r, y1, x2 - r, y1,
            x2, y1,
            x2, y1 + r, x2, y1 + r, x2, y2 - r, x2, y2 - r,
            x2, y2,
            x2 - r, y2, x2 - r, y2, x1 + r, y2, x1 + r, y2,
            x1, y2,
            x1, y2 - r, x1, y2 - r, x1, y1 + r, x1, y1 + r,
            x1, y1,
        ]

    def _set_button_enabled(self, enabled):
        self._btn_enabled = enabled
        c = self.canvas
        if enabled:
            c.itemconfigure(self._btn_bg, fill=ACCENT)
            c.itemconfigure(self._btn_label, fill="#000000")
            c.tag_raise(self._btn_bg)
            c.tag_raise(self._btn_label)
        else:
            c.itemconfigure(self._btn_bg, fill="#3A1F08")
            c.itemconfigure(self._btn_label, fill="#7A4A20")

    def _on_button_click(self, _event=None):
        if self._btn_enabled:
            self._open_output()

    def _on_button_enter(self, _event=None):
        if self._btn_enabled:
            self.canvas.itemconfigure(self._btn_bg, fill="#E55D10")
            self.canvas.configure(cursor="hand2")

    def _on_button_leave(self, _event=None):
        if self._btn_enabled:
            self.canvas.itemconfigure(self._btn_bg, fill=ACCENT)
        self.canvas.configure(cursor="")

    def _set_status(self, msg, error=False):
        self.canvas.itemconfigure(self._status_id, text=msg, fill=ERR if error else TEXT_LO)

    def _open_output(self):
        if self.last_out_dir:
            open_in_file_manager(self.last_out_dir)


def main():
    root = TkinterDnD.Tk() if HAS_DND else tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()

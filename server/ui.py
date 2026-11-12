"""
Subtle's window (Tkinter, part of Python: ~15 MB, no browser engine).

It runs inside the supervisor's process: the window lives on the main thread, the supervisor's
asyncio loop on a background thread. They talk through run_coroutine_threadsafe (window -> loop)
and a queue the window polls (loop -> window). Closing the window hides it; Subtle keeps running
in the tray. Nothing is polled while the window is hidden.

Layout: a sidebar (pages + live status) and a content area with a fixed page header and a
scrolling body. Text wraps to the window width. Settings are "label on the left, control on the
right" rows, with on/off switches.
"""

import asyncio
import ctypes
import os
import queue
import sys
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import messagebox, ttk

import modelstore

ASSETS = Path(__file__).resolve().parent / "assets"

PALETTES = {
    "dark": dict(bg="#16181d", panel="#1e2128", panel2="#252932", side="#121418", text="#e8eaef", muted="#9aa1ad",
                 faint="#6d7480", line="#2d313a", accent="#3cc497", accent_hover="#52d3a8", accent_soft="#183428",
                 warn="#f0a640", warn_soft="#3a2c12", bad="#ef6b6b", bad_soft="#3c1d1d", other="#465060",
                 field="#13151a", on_accent="#0b1c16", knob="#ffffff", off="#4a505c"),
    "light": dict(bg="#f4f5f7", panel="#ffffff", panel2="#f0f2f5", side="#e9ebef", text="#1b1e24", muted="#5d6470",
                  faint="#8a909b", line="#dde0e6", accent="#1a9670", accent_hover="#14805f", accent_soft="#dff3eb",
                  warn="#a86400", warn_soft="#fff1d9", bad="#c43838", bad_soft="#fde8e8", other="#c3c9d3",
                  field="#ffffff", on_accent="#ffffff", knob="#ffffff", off="#b7bdc7"),
}

ICONS = {"home": "", "models": "", "translator": "", "settings": "", "setup": ""}
PAGES = (("home", "Home"), ("models", "Models"), ("translator", "Translator"), ("settings", "Settings"),
         ("setup", "Setup guide"))
SUBTITLES = {
    "home": "What Subtle is doing right now, and how much memory it uses.",
    "models": "Speech and translator models on this PC. Download what you need; delete what you don't.",
    "translator": "Used by the Best engine and for subtitles in any language other than English.",
    "settings": "How Subtle uses memory, and how it starts.",
    "setup": "Three steps to your first subtitles.",
}

LANGS = [("en", "English"), ("es", "Spanish"), ("fr", "French"), ("de", "German"), ("pt", "Portuguese"),
         ("ar", "Arabic"), ("hi", "Hindi"), ("ur", "Urdu"), ("id", "Indonesian"), ("ja", "Japanese")]
LANG_TAGS = {"ja": "Japanese", "zh": "Chinese", "ko": "Korean", "yue": "Cantonese"}
IDLE_CHOICES = [(1, "1 minute"), (2, "2 minutes"), (5, "5 minutes"), (10, "10 minutes"), (30, "30 minutes"), (0, "Never")]
PREVIEW_CHOICES = [("normal", "Every 0.5 s (smoothest)"), ("slow", "Every second (less GPU work)"),
                   ("off", "Off (least GPU work)")]
LAYER_CHOICES = [(999, "Everything (fastest)"), (24, "Most (saves some VRAM)"), (12, "Half (saves more, slower)"),
                 (0, "None - processor only (slow)")]
CTX_CHOICES = [(1024, "1024 tokens (least VRAM)"), (2048, "2048 tokens (recommended)"), (4096, "4096 tokens")]
SOURCES = {"folder": "Subtle folder", "hf-cache": "Hugging Face cache", "lmstudio": "LM Studio", "custom": "your folder"}
KIND_TABS = (("whisper", "Speech"), ("llm", "Translator"), ("speaker", "Speakers"))


def windows_dark() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


def gb(n, d=1):
    return f"{n / 1e9:.{d}f} GB"           # file sizes


def gib(n, d=1):
    return f"{n / 1073741824:.{d}f} GB"    # memory, as Windows shows it


def mb(n):
    return gib(n) if n >= 1073741824 else f"{round(n / 1048576)} MB"


# ---------------------------------------------------------------------------------------------
# Small widgets
# ---------------------------------------------------------------------------------------------
class Toggle(tk.Canvas):
    """An on/off switch (easier to see and hit than Tk's tiny checkbox)."""

    W, H = 44, 24

    def __init__(self, parent, c, command=None):
        super().__init__(parent, width=self.W, height=self.H, bg=parent["bg"], highlightthickness=0, bd=0,
                         cursor="hand2", takefocus=1)
        self.c, self.command, self.value = c, command, False
        self.bind("<Button-1>", lambda e: self.flip())
        self.bind("<space>", lambda e: self.flip())
        self.draw()

    def set(self, value: bool):
        self.value = bool(value)
        self.draw()

    def flip(self):
        self.set(not self.value)
        if self.command:
            self.command(self.value)

    def draw(self):
        self.delete("all")
        c, W, H, r = self.c, self.W, self.H, self.H // 2
        fill = c["accent"] if self.value else c["off"]
        self.create_oval(0, 0, H, H, fill=fill, outline=fill)
        self.create_oval(W - H, 0, W, H, fill=fill, outline=fill)
        self.create_rectangle(r, 0, W - r, H, fill=fill, outline=fill)
        x = W - r if self.value else r
        self.create_oval(x - r + 3, 3, x + r - 3, H - 3, fill=c["knob"], outline=c["knob"])


class Bar(tk.Canvas):
    """A thin bar made of coloured parts: [(fraction, colour), ...]."""

    def __init__(self, parent, c, height=10):
        super().__init__(parent, height=height, bg=c["line"], highlightthickness=0, bd=0)
        self.parts, self.h = [], height
        self.bind("<Configure>", lambda e: self.draw())

    def set(self, parts):
        parts = [(round(f, 3), col) for f, col in parts]
        if parts != self.parts:               # redraw only when something changed
            self.parts = parts
            self.draw()

    def draw(self):
        self.delete("all")
        w, x = max(self.winfo_width(), 1), 0.0
        for frac, colour in self.parts:
            dx = max(0.0, min(1.0, frac)) * w
            if dx > 0:
                self.create_rectangle(x, 0, x + dx, self.h, fill=colour, width=0)
            x += dx


class Scroll(tk.Frame):
    """A vertically scrolling area; the scrollbar shows only when the content is taller."""

    def __init__(self, parent, c):
        super().__init__(parent, bg=c["bg"])
        self.canvas = tk.Canvas(self, bg=c["bg"], highlightthickness=0, bd=0)
        self.bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = tk.Frame(self.canvas, bg=c["bg"])
        self.win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self._on_scroll)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.win, width=e.width))
        self.bind_all("<MouseWheel>", self._wheel, add="+")

    def _on_scroll(self, lo, hi):
        self.bar.set(lo, hi)
        if float(lo) <= 0.0 and float(hi) >= 1.0:
            self.bar.pack_forget()
        elif not self.bar.winfo_ismapped():
            self.bar.pack(side="right", fill="y")

    active = True        # pages are stacked; only the one in front scrolls

    def _wheel(self, e):
        if self.active and self.winfo_ismapped() and self.inner.winfo_height() > self.canvas.winfo_height():
            self.canvas.yview_scroll(int(-e.delta / 120), "units")

    def top(self):
        self.canvas.yview_moveto(0)


# ---------------------------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------------------------
class ControlPanel:
    CONTENT_MAX = 900          # body text never runs wider than this (px), even in a maximised window

    def __init__(self, sup, show: bool):
        self.sup = sup
        self.q: queue.Queue = queue.Queue()
        self.st: dict = {}
        self.page = ""
        self.visible = False
        self.models_sig = None
        self.model_tab = "whisper"
        self.dl_rows: dict[str, dict] = {}
        self.prev_dl: dict[str, str] = {}
        self.toast_job = None
        self._setup_shown = False
        self.wraps: list[tuple] = []          # (label, its container, pixels used beside it)
        self._wrap_job = None

        if sys.platform == "win32":
            try:   # own taskbar button and icon instead of Python's; sharp text on high-DPI screens
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Subtle.App")
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                pass
        theme = os.environ.get("SUBTLE_UI_THEME")          # "light" / "dark" to override Windows (tests)
        self.dark = windows_dark() if theme not in ("light", "dark") else theme == "dark"
        self.c = c = PALETTES["dark" if self.dark else "light"]
        self.root = root = tk.Tk()
        root.withdraw()
        port = getattr(sup, "port", 8765)
        root.title("Subtle" if port == 8765 else f"Subtle (port {port})")
        root.geometry(os.environ.get("SUBTLE_UI_SIZE") or "1080x740")
        root.minsize(820, 560)
        root.configure(bg=c["bg"])
        try:
            root.iconbitmap(default=str(ASSETS / "subtle.ico"))
        except tk.TclError:
            pass
        root.protocol("WM_DELETE_WINDOW", self.hide)
        fams = set(tkfont.families(root))
        ui = "Segoe UI"            # static fonts: the "Variable" ones draw ~3x slower in Tk (laggy resizing)
        disp = "Segoe UI Semibold"
        self.icon_font = ("Segoe Fluent Icons", 12) if "Segoe Fluent Icons" in fams else \
            ("Segoe MDL2 Assets", 12) if "Segoe MDL2 Assets" in fams else None
        self.f = {
            "body": (ui, 10), "small": (ui, 9), "bold": (ui, 10, "bold"), "title": (disp, 20, "bold"),
            "h2": (disp, 13, "bold"), "big": (disp, 16, "bold"), "nav": (ui, 11), "mono": ("Consolas", 9),
        }
        self._styles()
        self._layout()
        root.after(50, self._pump)
        start_page = os.environ.get("SUBTLE_UI_PAGE")      # for screenshots / tests
        if start_page in dict(PAGES):
            self._setup_shown = True
            self.go(start_page)
        if show:
            self.show()

    # ---------------------------------------------------------------- plumbing
    def post(self, fn, *args):
        """Run fn(*args) on the window's thread (safe to call from any thread)."""
        self.q.put((fn, args))

    def _pump(self):
        try:
            while True:
                fn, args = self.q.get_nowait()
                try:
                    fn(*args)
                except Exception as e:      # a window hiccup must never take Subtle down
                    print(f"[ui] {type(e).__name__}: {e}", flush=True)
        except queue.Empty:
            pass
        self.root.after(50, self._pump)

    def _run(self, coro, cb):
        loop = self.sup.loop
        if loop is None:
            return
        fut = asyncio.run_coroutine_threadsafe(coro, loop)

        def done(f):
            try:
                res = f.result()
            except Exception as e:
                res = {"error": f"{type(e).__name__}: {e}"}
            self.post(cb, res)
        fut.add_done_callback(done)

    def cmd(self, name: str, cb=None, **kw):
        def after(res):
            if res and res.get("error"):
                self.toast(res["error"], 7000, bad=True)
            if cb:
                cb(res or {})
        self._run(self.sup.panel_command(dict(cmd=name, **kw)), after)

    def save(self, **changes):
        self.cmd("settings", lambda r: self.refresh(True), changes=changes)

    def refresh(self, full=False):
        self._run(self.sup.state(full), lambda st: self.on_state(st, full))

    def _tick(self):
        if not self.visible:
            return
        self.refresh(False)
        self.root.after(1500, self._tick)

    def show(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self._dark_titlebar()
        if not self.visible:
            self.visible = True
            self.refresh(True)
            self.root.after(1500, self._tick)

    def hide(self):
        self.visible = False
        self.root.withdraw()

    def quit(self):
        self.visible = False
        self.root.quit()

    def mainloop(self):
        self.root.mainloop()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _dark_titlebar(self):
        if sys.platform != "win32" or not self.dark:
            return
        try:
            self.root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            val = ctypes.c_int(1)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(val), ctypes.sizeof(val))
        except Exception:
            pass

    # ---------------------------------------------------------------- building blocks
    def _styles(self):
        c = self.c
        s = ttk.Style(self.root)
        s.theme_use("clam")
        s.configure("TCombobox", fieldbackground=c["field"], background=c["panel2"], foreground=c["text"],
                    arrowcolor=c["muted"], bordercolor=c["line"], lightcolor=c["field"], darkcolor=c["field"],
                    padding=(8, 5), arrowsize=14)
        s.map("TCombobox", fieldbackground=[("readonly", c["field"])], foreground=[("readonly", c["text"])],
              selectbackground=[("readonly", c["field"])], selectforeground=[("readonly", c["text"])],
              bordercolor=[("focus", c["accent"]), ("hover", c["faint"])])
        self.root.option_add("*TCombobox*Listbox.background", c["panel"])
        self.root.option_add("*TCombobox*Listbox.foreground", c["text"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", c["accent"])
        self.root.option_add("*TCombobox*Listbox.selectForeground", c["on_accent"])
        self.root.option_add("*TCombobox*Listbox.font", self.f["body"])
        s.configure("Vertical.TScrollbar", troughcolor=c["bg"], background=c["line"], bordercolor=c["bg"],
                    arrowcolor=c["faint"], lightcolor=c["line"], darkcolor=c["line"], gripcount=0)
        s.map("Vertical.TScrollbar", background=[("active", c["other"])])

    def label(self, parent, text="", kind="body", fg=None, wrap=False, **kw):
        """wrap: True, or a number of pixels used by things beside the label in the same row. The text
        then wraps to whatever width its container really has, and re-wraps when the window is resized."""
        c = self.c
        font = self.f.get({"muted": "body", "faint": "small"}.get(kind, kind), self.f["body"])
        color = fg or {"muted": c["muted"], "small": c["muted"], "faint": c["faint"]}.get(kind, c["text"])
        lbl = tk.Label(parent, text=text, font=font, fg=color, bg=parent["bg"], anchor="w", justify="left", **kw)
        if wrap is not False:
            self.wraps.append((lbl, parent, 0 if wrap is True else int(wrap)))
            lbl.configure(wraplength=600)
            self.schedule_rewrap()
        return lbl

    # Wrapping text to the window width. One pass over all visible wrapping labels, run a moment
    # after the size stops changing: re-wrapping each label on every <Configure> made every size
    # change cascade through the whole layout (~80,000 layout events at start, 370 ms per resize).
    def schedule_rewrap(self, event=None):
        if self._wrap_job is None:
            self._wrap_job = self.root.after(40, self._rewrap_all)

    def _rewrap_all(self, passes=2):
        self._wrap_job = None
        changed, alive = False, []
        for lbl, parent, extra in self.wraps:
            try:
                if not lbl.winfo_exists():
                    continue
                alive.append((lbl, parent, extra))
                if not lbl.winfo_ismapped():
                    continue                          # hidden pages wrap when they're shown
                pad = 2 * int(str(parent.cget("padx") or 0))
                w = parent.winfo_width() - pad - extra - 4
                if w > 50 and abs(int(lbl.cget("wraplength") or 0) - w) > 6:
                    lbl.configure(wraplength=w)
                    changed = True
            except (tk.TclError, ValueError):
                pass
        self.wraps = alive
        if changed and passes > 1:                    # a container may have resized itself: settle once more
            self.root.update_idletasks()
            self._rewrap_all(passes - 1)

    @staticmethod
    def upd(widget, **kw):
        """configure() only what actually changed: unchanged values would still make Tk redo the layout."""
        try:
            diff = {k: v for k, v in kw.items() if str(widget.cget(k)) != str(v)}
            if diff:
                widget.configure(**diff)
        except tk.TclError:
            pass

    def button(self, parent, text, command, kind="primary", small=False):
        c = self.c
        bg, fg, hover = {"primary": (c["accent"], c["on_accent"], c["accent_hover"]),
                         "secondary": (c["panel2"], c["text"], c["line"]),
                         "danger": (c["bad"], "#ffffff", c["bad"]),
                         "link": (parent["bg"], c["accent"], parent["bg"])}[kind]
        b = tk.Button(parent, text=text, command=command, font=self.f["small" if small else "body"], bg=bg, fg=fg,
                      activebackground=hover, activeforeground=fg, relief="flat", bd=0,
                      padx=10 if small else 14, pady=3 if small else 6, cursor="hand2", highlightthickness=0,
                      disabledforeground=c["faint"])
        if kind == "secondary":
            b.configure(highlightthickness=1, highlightbackground=c["line"], highlightcolor=c["line"])
        b.bind("<Enter>", lambda e: b.configure(bg=hover) if str(b["state"]) != "disabled" else None)
        b.bind("<Leave>", lambda e: b.configure(bg=bg))
        return b

    def card(self, parent, pad=(18, 14), bg=None):
        c = self.c
        return tk.Frame(parent, bg=bg or c["panel"], highlightthickness=1, highlightbackground=c["line"],
                        padx=pad[0], pady=pad[1])

    def badge(self, parent, text, tone="neutral"):
        c = self.c
        bg, fg = {"good": (c["accent_soft"], c["accent"]), "warn": (c["warn_soft"], c["warn"]),
                  "bad": (c["bad_soft"], c["bad"]), "neutral": (c["panel2"], c["muted"]),
                  "accent": (c["accent"], c["on_accent"])}[tone]
        return tk.Label(parent, text=text, font=self.f["small"], bg=bg, fg=fg, padx=7, pady=1)

    def dot(self, parent, tone="off"):
        c = self.c
        color = {"ok": c["accent"], "busy": c["warn"], "bad": c["bad"], "off": c["faint"]}[tone]
        return tk.Label(parent, text="●", font=self.f["small"], fg=color, bg=parent["bg"])

    def choice(self, parent, choices, on_pick, width=28):
        """Read-only dropdown of (value, label) pairs."""
        cb = ttk.Combobox(parent, state="readonly", font=self.f["body"], width=width, values=[lbl for _, lbl in choices])
        cb.choices = list(choices)
        cb.bind("<<ComboboxSelected>>", lambda e: (on_pick(cb.choices[cb.current()][0]), cb.selection_clear()))
        return cb

    def set_choice(self, cb, value):
        for i, (v, _) in enumerate(cb.choices):
            if v == value:
                cb.current(i)
                return

    def entry(self, parent, var, width=40):
        c = self.c
        return tk.Entry(parent, textvariable=var, font=self.f["body"], bg=c["field"], fg=c["text"],
                        insertbackground=c["text"], relief="flat", highlightthickness=1, highlightbackground=c["line"],
                        highlightcolor=c["accent"], width=width)

    def setting_row(self, card, title, desc, first=False):
        """A 'label on the left, control on the right' row inside a card. Returns the right-hand frame."""
        c = self.c
        if not first:
            tk.Frame(card, bg=c["line"], height=1).pack(fill="x", pady=0)
        row = tk.Frame(card, bg=card["bg"])
        row.pack(fill="x", pady=10)
        right = tk.Frame(row, bg=card["bg"])
        right.pack(side="right", padx=(16, 0))
        left = tk.Frame(row, bg=card["bg"])
        left.pack(side="left", fill="x", expand=True)
        self.label(left, title, "body").pack(anchor="w")
        if desc:
            self.label(left, desc, "small", wrap=True).pack(anchor="w", pady=(1, 0))
        return right

    def section_title(self, parent, text, pady=(22, 8)):
        self.label(parent, text, "h2").pack(anchor="w", pady=pady)

    def toast(self, text, ms=3500, bad=False):
        self.status.configure(text=text, fg=self.c["bad"] if bad else self.c["muted"])
        if self.toast_job:
            self.root.after_cancel(self.toast_job)
        self.toast_job = self.root.after(ms, lambda: self.status.configure(text=""))

    def copy(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.toast("Copied to the clipboard")

    # ---------------------------------------------------------------- layout
    def _layout(self):
        c = self.c
        side = tk.Frame(self.root, bg=c["side"], width=228)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)

        brand = tk.Frame(side, bg=c["side"], padx=18, pady=20)
        brand.pack(fill="x")
        try:
            self.logo = tk.PhotoImage(file=str(ASSETS / "subtle.png")).subsample(3, 3)
            tk.Label(brand, image=self.logo, bg=c["side"]).pack(side="left", padx=(0, 10))
        except tk.TclError:
            pass
        names = tk.Frame(brand, bg=c["side"])
        names.pack(side="left")
        self.label(names, "Subtle", "h2").pack(anchor="w")
        self.ver = self.label(names, "", "faint")
        self.ver.pack(anchor="w")

        self.nav: dict[str, dict] = {}
        for key, title in PAGES:
            row = tk.Frame(side, bg=c["side"], cursor="hand2")
            row.pack(fill="x", padx=10, pady=1)
            mark = tk.Frame(row, bg=c["side"], width=3)
            mark.pack(side="left", fill="y")
            icon = tk.Label(row, text=ICONS[key] if self.icon_font else "", font=self.icon_font or self.f["nav"],
                            bg=c["side"], fg=c["muted"], width=2)
            icon.pack(side="left", padx=(10, 6), pady=9)
            text = tk.Label(row, text=title, font=self.f["nav"], bg=c["side"], fg=c["text"], anchor="w")
            text.pack(side="left", fill="x", expand=True)
            parts = (row, icon, text)
            for w in parts:
                w.bind("<Button-1>", lambda e, k=key: self.go(k))
                w.bind("<Enter>", lambda e, k=key: self._nav_hover(k, True))
                w.bind("<Leave>", lambda e, k=key: self._nav_hover(k, False))
            self.nav[key] = {"row": row, "mark": mark, "icon": icon, "text": text}

        # live status at the bottom of the sidebar
        stat = tk.Frame(side, bg=c["side"], padx=20, pady=16)
        stat.pack(side="bottom", fill="x")
        tk.Frame(side, bg=c["line"], height=1).pack(side="bottom", fill="x", padx=16)
        self.side_status = {}
        for key, title in (("app", "Subtle"), ("engine", "Speech engine"), ("llm", "Translator")):
            r = tk.Frame(stat, bg=c["side"])
            r.pack(fill="x", pady=2)
            d = self.dot(r)
            d.pack(side="left")
            t = self.label(r, f"{title}: …", "small")
            t.pack(side="left", padx=(6, 0))
            self.side_status[key] = (d, t, title)

        # content: fixed header + scrolling body + status line
        self.content = content = tk.Frame(self.root, bg=c["bg"])
        content.pack(side="left", fill="both", expand=True)
        content.bind("<Configure>", self.schedule_rewrap)
        header = tk.Frame(content, bg=c["bg"], padx=36, pady=22)
        header.pack(fill="x")
        hrow = tk.Frame(header, bg=c["bg"])
        hrow.pack(fill="x")
        self.h_title = self.label(hrow, "", "title")
        self.h_title.pack(side="left")
        self.h_actions = tk.Frame(hrow, bg=c["bg"])
        self.h_actions.pack(side="right")
        self.h_sub = self.label(header, "", "muted", wrap=True)
        self.h_sub.pack(anchor="w", pady=(4, 0))
        tk.Frame(content, bg=c["line"], height=1).pack(fill="x", padx=36)
        self.status = self.label(content, "", "small")
        self.status.pack(side="bottom", fill="x", padx=36, pady=(4, 10))

        self.pages: dict[str, Scroll] = {}
        self.page_actions: dict[str, tk.Frame] = {}
        for key, _ in PAGES:
            sc = Scroll(content, c)
            self.pages[key] = sc
            body = tk.Frame(sc.inner, bg=c["bg"], padx=36, pady=8)
            body.pack(fill="x", anchor="w")
            sc.body = body
            self.page_actions[key] = tk.Frame(self.h_actions, bg=c["bg"])

        self._home(self.pages["home"].body)
        self._models(self.pages["models"].body)
        self._translator(self.pages["translator"].body)
        self._settings(self.pages["settings"].body)
        self._setup(self.pages["setup"].body)
        self.go("home")

    def _nav_hover(self, key, on):
        if key == self.page:
            return
        c = self.c
        bg = c["panel2"] if on else c["side"]
        for w in ("row", "icon", "text"):
            self.nav[key][w].configure(bg=bg)
        self.nav[key]["mark"].configure(bg=bg)

    def go(self, key):
        c = self.c
        for k, n in self.nav.items():
            on = k == key
            bg = c["accent_soft"] if on else c["side"]
            for w in ("row", "icon", "text"):
                n[w].configure(bg=bg)
            n["mark"].configure(bg=c["accent"] if on else bg)
            n["icon"].configure(fg=c["accent"] if on else c["muted"])
            n["text"].configure(fg=c["accent"] if on else c["text"], font=self.f["nav"])
        new, old = self.pages[key], self.pages.get(self.page)
        for k, p in self.pages.items():
            p.active = k == key
        if old is not new:
            new.top()
            new.pack(fill="both", expand=True)
            if old is not None:
                old.pack_forget()
            self.schedule_rewrap()
        for k, a in self.page_actions.items():
            if k == key:
                a.pack(side="right")
            else:
                a.pack_forget()
        self.h_title.configure(text=dict(PAGES)[key])
        self.h_sub.configure(text=SUBTITLES[key])
        self.page = key
        if self.visible and key in ("models", "setup"):    # the pages whose lists may have changed
            self.refresh(True)

    # ================================================================ HOME
    def _home(self, p):
        c = self.c
        # one-line summary of what's going on
        self.hero = self.card(p, pad=(22, 18))
        self.hero.pack(fill="x", pady=(8, 0))
        top = tk.Frame(self.hero, bg=c["panel"])
        top.pack(fill="x")
        self.hero_dot = self.dot(top, "off")
        self.hero_dot.configure(font=self.f["h2"])
        self.hero_dot.pack(side="left", padx=(0, 10))
        self.hero_title = self.label(top, "Starting…", "big")
        self.hero_title.pack(side="left")
        self.hero_sub = self.label(self.hero, "", "muted", wrap=True)
        self.hero_sub.pack(anchor="w", pady=(6, 0))

        # three tiles
        tiles = tk.Frame(p, bg=c["bg"])
        tiles.pack(fill="x", pady=12)
        self.tiles = {}
        for i, (key, title) in enumerate((("engine", "Speech engine"), ("llm", "Translator"), ("vram", "Subtle's video memory"))):
            t = self.card(tiles)
            t.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 6, 0 if i == 2 else 6))
            tiles.columnconfigure(i, weight=1, uniform="tiles")
            self.label(t, title, "small", wrap=True).pack(anchor="w")
            v = self.label(t, "-", "big")
            v.pack(anchor="w", pady=(4, 2))
            d = self.label(t, "", "small", wrap=True)
            d.pack(anchor="w")
            self.tiles[key] = (v, d)

        # memory details
        mem = self.card(p, pad=(22, 16))
        mem.pack(fill="x")
        head = tk.Frame(mem, bg=c["panel"])
        head.pack(fill="x")
        self.label(head, "Memory", "h2").pack(side="left")
        self.button(head, "Free memory now", lambda: self.cmd(
            "unload", lambda r: self.toast("Models unloaded - their memory is free again")), "secondary").pack(side="right")
        self.gpu_line = self.label(mem, "", "small")
        self.gpu_line.pack(anchor="w", pady=(8, 6))
        self.vbar = Bar(mem, c, 12)
        self.vbar.pack(fill="x")
        leg = tk.Frame(mem, bg=c["panel"])
        leg.pack(fill="x", pady=(6, 12))
        for color, text in ((c["accent"], "Subtle"), (c["other"], "Other programs"), (c["line"], "Free")):
            tk.Label(leg, text="■", font=self.f["small"], fg=color, bg=c["panel"]).pack(side="left")
            self.label(leg, text + "     ", "small").pack(side="left")
        self.vtext = self.label(leg, "", "small")
        self.vtext.pack(side="right")

        grid = tk.Frame(mem, bg=c["panel"])
        grid.pack(fill="x")
        for col, (t, anchor) in enumerate((("Part", "w"), ("Video memory", "e"), ("Memory (RAM)", "e"))):
            self.label(grid, t, "faint").grid(row=0, column=col, sticky=anchor, padx=4, pady=(0, 4))
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, minsize=150)
        grid.columnconfigure(2, minsize=150)
        self.usage_cells = {}
        for r, (key, name) in enumerate((("engine", "Speech engine"), ("translator", "Translator"),
                                          ("supervisor", "Subtle app (window + tray)")), start=1):
            tk.Frame(grid, bg=c["line"], height=1).grid(row=2 * r - 1, column=0, columnspan=3, sticky="ew")
            self.label(grid, name).grid(row=2 * r, column=0, sticky="w", padx=4, pady=7)
            v = self.label(grid, "-")
            v.grid(row=2 * r, column=1, sticky="e", padx=4)
            m = self.label(grid, "-")
            m.grid(row=2 * r, column=2, sticky="e", padx=4)
            self.usage_cells[key] = (v, m)
        self.idle_rule = self.label(mem, "", "small", wrap=True)
        self.idle_rule.pack(anchor="w", pady=(12, 0))

        self.notice = self.card(p, bg=c["warn_soft"])
        self.notice.configure(highlightbackground=c["warn_soft"])
        self.notice_text = self.label(self.notice, "", fg=c["warn"], wrap=200)
        self.notice_text.pack(side="left", fill="x", expand=True)
        self.button(self.notice, "Open Setup guide", lambda: self.go("setup")).pack(side="right")

    def render_home(self):
        st, c = self.st, self.c
        self.upd(self.ver, text="version " + st.get("version", ""))
        s = st.get("settings", {})
        e, t = st.get("engine", {}), st.get("translator", {})
        ext = s.get("llm_backend") == "external"
        tabs = st.get("tabs", 0)
        loaded = [m.split("/")[-1] for m in e.get("loaded", [])]
        u = st.get("usage") or {}
        subtle_v = (u.get("engine") or {}).get("vram", 0) + (u.get("translator") or {}).get("vram", 0)

        # summary
        if e.get("state") == "error" or t.get("state") == "error":
            tone, title = "bad", "Something needs attention"
            sub = e.get("error") or t.get("error") or ""
        elif e.get("state") == "starting" or t.get("state") == "starting":
            tone, title, sub = "busy", "Loading models…", "This takes a few seconds; subtitles start right after."
        elif tabs:
            tone, title = "ok", f"Subtitling {tabs} tab{'s' if tabs != 1 else ''}"
            sub = f"Using {', '.join(loaded) or 'the speech engine'}" + \
                  (f" and the translator {t.get('model')}" if t.get("state") == "ready" else "") + "."
        else:
            tone, title = "off", "Ready"
            sub = ("Nothing is loaded, so Subtle uses no video memory. "
                   "Start subtitles from the Subtle icon in your browser.") if not subtle_v else \
                  "No tab is using Subtle right now. Models are still loaded and will be freed automatically."
        self.upd(self.hero_dot, fg={"ok": c["accent"], "busy": c["warn"], "bad": c["bad"], "off": c["faint"]}[tone])
        self.upd(self.hero_title, text=title)
        self.upd(self.hero_sub, text=sub)

        # tiles
        es = e.get("state")
        v, d = self.tiles["engine"]
        self.upd(v, text={"ready": "Running", "starting": "Loading", "error": "Problem"}.get(es, "Idle"),
                    fg={"ready": c["accent"], "starting": c["warn"], "error": c["bad"]}.get(es, c["text"]))
        self.upd(d, text=", ".join(loaded) if es == "ready" and loaded else "Loads when a tab starts")
        v, d = self.tiles["llm"]
        ts = t.get("state")
        self.upd(v, text="External" if ext else {"ready": "Running", "starting": "Loading", "error": "Problem"}.get(ts, "Off"),
                    fg={"ready": c["accent"], "starting": c["warn"], "error": c["bad"]}.get(ts, c["text"]))
        self.upd(d, text=s.get("external_url", "") if ext else t.get("model") if ts == "ready" else "Only for the Best engine")
        v, d = self.tiles["vram"]
        self.upd(v, text=mb(subtle_v) if subtle_v else "0 GB", fg=c["text"])
        g = st.get("gpu") or {}
        self.upd(d, text=f"of {gib(g['total_mb'] * 1048576, 0)} in total" if g.get("total_mb") else "No NVIDIA graphics card")

        # memory card
        if g.get("total_mb"):
            total, used = g["total_mb"] * 1048576, g["used_mb"] * 1048576
            self.upd(self.gpu_line, text=f"{g['name']}  ·  driver {g['driver']}")
            self.vbar.set([(subtle_v / total, c["accent"]), (max(0, used - subtle_v) / total, c["other"])])
            self.upd(self.vtext, text=f"{gib(total - used)} free of {gib(total, 0)}")
        else:
            self.upd(self.gpu_line, text="No NVIDIA graphics card found.")
            self.vbar.set([])
        for key, (vc, mc) in self.usage_cells.items():
            x = u.get(key) or {}
            running = bool(x.get("pid"))
            self.upd(vc, text=mb(x.get("vram", 0)) if running else "not running", fg=c["text"] if running else c["faint"])
            self.upd(mc, text=mb(x.get("ram", 0)) if running else "", fg=c["text"])
        idle = s.get("idle_unload_min", 5)
        self.upd(self.idle_rule, 
            text="Models stay loaded (Settings → Keep models loaded)." if s.get("keep_loaded") or not idle else
            f"Everything is unloaded {idle} minute{'s' if idle != 1 else ''} after the last subtitle, "
            "so Subtle then uses no video memory.")
        models = st.get("models")
        if models is not None:
            if not models.get("whisper"):
                self.upd(self.notice_text, text="No speech model is installed yet, so subtitles can't start.")
                if not self.notice.winfo_ismapped():
                    self.notice.pack(fill="x", pady=(12, 0))
            elif self.notice.winfo_ismapped():
                self.notice.pack_forget()

    def render_side(self):
        """The live status at the bottom of the sidebar (always visible, so updated on every page)."""
        st = self.st
        s, e, t = st.get("settings", {}), st.get("engine", {}), st.get("translator", {})
        ext = s.get("llm_backend") == "external"
        es, ts = e.get("state"), t.get("state")
        self.upd(self.ver, text="version " + st.get("version", ""))
        self._side("app", "ok", "running")
        self._side("engine", {"ready": "ok", "starting": "busy", "error": "bad"}.get(es, "off"),
                   {"ready": "running", "starting": "loading", "error": "problem"}.get(es, "idle"))
        self._side("llm", "ok" if ext else {"ready": "ok", "starting": "busy", "error": "bad"}.get(ts, "off"),
                   "external" if ext else {"ready": "running", "starting": "loading", "error": "problem"}.get(ts, "off"))

    def _side(self, key, tone, text):
        c = self.c
        d, t, title = self.side_status[key]
        self.upd(d, fg={"ok": c["accent"], "busy": c["warn"], "bad": c["bad"], "off": c["faint"]}[tone])
        self.upd(t, text=f"{title}: {text}")

    # ================================================================ profile
    def gpu_gb(self):
        g = self.st.get("gpu") or {}
        return g.get("total_mb", 0) / 1024

    def profile(self):
        v = self.gpu_gb()
        if not v:
            return ("No NVIDIA graphics card found", True, ["whisper-small"],
                    "Subtle works without one, but slowly: use Fast with the small speech model.")
        n = f"{v:.0f} GB graphics card"
        if v < 5.5:
            return (n, True, ["whisper-medium", "whisper-large-v3-turbo", "Qwen3.5-2B-Q4_K_M"],
                    "Use Fast with Whisper medium. Best works with the smallest translator; close games first.")
        if v < 7.5:
            return (n, False, ["whisper-large-v3", "whisper-large-v3-turbo", "Qwen3.5-2B-Q4_K_M"],
                    "Fast with large-v3 fits well. For Best, the small 2B translator keeps you under 4.5 GB.")
        if v < 11.5:
            return (n, False, ["whisper-large-v3", "whisper-large-v3-turbo", "Qwen3-4B-Instruct-2507-Q4_K_M"],
                    "Large-v3 for Fast, turbo + Qwen3 4B for Best (about 5.7 GB).")
        if v < 15.5:
            return (n, False, ["whisper-large-v3", "whisper-large-v3-turbo", "Qwen3-4B-Instruct-2507-Q4_K_M", "Qwen3.5-9B-Q4_K_M"],
                    "Room for a bigger translator: Qwen3.5 9B gives more natural lines (about 9 GB for Best).")
        return (n, False, ["whisper-large-v3", "whisper-large-v3-turbo", "Qwen3-4B-Instruct-2507-Q4_K_M", "gemma-4-12B-it-QAT-Q4_0"],
                "Plenty of room: Gemma 4 12B for the best translations (about 10 GB for Best), Qwen3 4B when you want it light.")

    def fits(self, m):
        v = self.gpu_gb()
        if not v:
            return m["kind"] != "llm"
        need = m["vram_mb"] + 2600 if m["kind"] == "llm" else m["vram_mb"]
        return need <= v * 1024 * 0.85

    # ================================================================ MODELS
    def _models(self, p):
        c = self.c
        acts = self.page_actions["models"]
        self.button(acts, "Rescan folders", lambda: self.cmd(
            "rescan", lambda r: (self.toast("Folders scanned"), self.refresh(True))), "secondary").pack(side="left", padx=(0, 6))
        self.button(acts, "Open models folder", lambda: self.cmd("open", what="models"), "secondary").pack(side="left")

        self.rec = tk.Frame(p, bg=c["accent_soft"], padx=18, pady=14)
        self.rec.pack(fill="x", pady=(8, 0))
        rt = tk.Frame(self.rec, bg=c["accent_soft"])
        rt.pack(side="left", fill="x", expand=True)
        self.rec_title = self.label(rt, "", "bold", wrap=True)
        self.rec_title.pack(anchor="w")
        self.rec_text = self.label(rt, "", wrap=True)
        self.rec_text.pack(anchor="w", pady=(2, 0))
        self.rec_btn = self.button(self.rec, "Download recommended", self.download_set)
        self.rec_btn.pack(side="right", padx=(12, 0), before=rt)

        # Speech / Translator / Speakers tabs
        tabs = tk.Frame(p, bg=c["bg"])
        tabs.pack(fill="x", pady=(20, 0))
        self.kind_tabs = {}
        for key, title in KIND_TABS:
            t = tk.Label(tabs, text=title, font=self.f["bold"], bg=c["bg"], fg=c["muted"], padx=4, pady=6, cursor="hand2")
            t.pack(side="left", padx=(0, 18))
            line = tk.Frame(tabs, bg=c["bg"], height=2)
            t.bind("<Button-1>", lambda e, k=key: self.set_model_tab(k))
            self.kind_tabs[key] = (t, line)
        tk.Frame(p, bg=c["line"], height=1).pack(fill="x")
        self.kind_hint = self.label(p, "", "small", wrap=True)
        self.kind_hint.pack(anchor="w", pady=(10, 8))
        self.model_list = tk.Frame(p, bg=c["bg"])
        self.model_list.pack(fill="x")

        help_card = self.card(p)
        help_card.pack(fill="x", pady=(18, 12))
        self.label(help_card, "Add a model yourself", "bold").pack(anchor="w")
        self.help_t = self.label(help_card, "", "small", wrap=True)
        self.help_t.pack(anchor="w", pady=(4, 0))

    def set_model_tab(self, key):
        self.model_tab = key
        self.models_sig = None
        self.render_models()

    def render_models(self):
        c, st = self.c, self.st
        name, warn, ids, text = self.profile()
        bg = c["warn_soft"] if warn else c["accent_soft"]
        for w in (self.rec, self.rec_title, self.rec_text, self.rec_title.master):
            w.configure(bg=bg)
        cat = {m["id"]: m for m in st.get("catalog", [])}
        missing = [i for i in ids if i in cat and not cat[i]["installed"]]
        self.rec_title.configure(text=f"Recommended for your PC ({name})")
        self.rec_text.configure(text=text)
        if missing:
            self.rec_btn.configure(text=f"Download recommended ({gb(sum(cat[i]['size'] for i in missing))})", state="normal")
            self.rec_btn.pack(side="right", padx=(12, 0), before=self.rec_title.master)
        else:
            self.rec_btn.pack_forget()

        for key, (t, line) in self.kind_tabs.items():
            on = key == self.model_tab
            t.configure(fg=c["accent"] if on else c["muted"])
            line.place(in_=t, relx=0, rely=1.0, relwidth=1.0, height=2, anchor="sw") if on else line.place_forget()
            line.configure(bg=c["accent"])
        self.kind_hint.configure(text={
            "whisper": "Speech models turn what people say into text. Fast needs one that can translate (large-v3); "
                       "Best works with any. Language-specific ones are often more accurate for that language: pick them "
                       "in the extension popup under Speech model.",
            "llm": "Translator models turn the text into natural subtitles in any language (Best engine). "
                   "Bigger ones translate better but need more video memory.",
            "speaker": "Only needed for 'Identify speakers', which gives each voice its own colour.",
        }[self.model_tab])

        sig = (self.model_tab, str([(m["id"], m["installed"]) for m in st.get("catalog", [])]),
               str([(k, [(m["id"], m.get("source")) for m in v]) for k, v in st.get("models", {}).items()]),
               st.get("settings", {}).get("llm_model"))
        if sig != self.models_sig:
            self.models_sig = sig
            self._build_model_rows(ids)
        paths = st.get("paths", {})
        lms = paths.get("lmstudio")
        self.help_t.configure(text=(
            f"Speech: put a Whisper model folder (Transformers format, with config.json and model.safetensors) into "
            f"{paths.get('models', '')}\\whisper.   Translator: put any instruct .gguf file into "
            f"{paths.get('models', '')}\\llm (Q4_K_M is a good balance). "
            + ("Models downloaded with LM Studio are used too, without copying. " if lms else "")
            + "Then press Rescan folders."))
        self.render_downloads()

    def _build_model_rows(self, rec_ids):
        st = self.st
        for w in self.model_list.winfo_children():
            w.destroy()
        self.dl_rows = {}
        kind = self.model_tab
        installed = st.get("models", {}).get(kind, [])
        default = st.get("settings", {}).get("llm_model")
        catalog = [m for m in st.get("catalog", []) if m["kind"] == kind]
        known = {m["id"].lower() for m in catalog}

        def inst_for(m):
            for it in installed:
                if it["name"].lower() == m["id"].lower() or it["id"].lower() == m["id"].lower() or \
                        (kind == "speaker" and it):
                    return it
            return None

        box = self.card(self.model_list, pad=(0, 0))
        box.pack(fill="x")
        first = True
        # catalog models first (recommended ones on top), then anything else found on this PC
        order = sorted(catalog, key=lambda m: (m["id"] not in rec_ids, m.get("experimental", False), m["size"]))
        for m in order:
            self._model_row(box, m, inst_for(m), m["id"] in rec_ids, default, first)
            first = False
        extra = [it for it in installed if it["name"].lower() not in known and it["id"].lower() not in known]
        for it in extra:
            self._model_row(box, None, it, False, default, first)
            first = False
        if first:
            self.label(box, "  Nothing here yet.", "muted").pack(anchor="w", padx=16, pady=14)

    def _model_row(self, box, m, inst, recommended, default, first):
        c = self.c
        if not first:
            tk.Frame(box, bg=c["line"], height=1).pack(fill="x")
        row = tk.Frame(box, bg=c["panel"], padx=18, pady=12)
        row.pack(fill="x")
        right = tk.Frame(row, bg=c["panel"])
        right.pack(side="right", padx=(12, 0))
        left = tk.Frame(row, bg=c["panel"])
        left.pack(side="left", fill="x", expand=True)

        title = tk.Frame(left, bg=c["panel"])
        title.pack(anchor="w", fill="x")
        self.label(title, m["name"] if m else inst["name"], "bold").pack(side="left")
        if m and recommended:
            self.badge(title, "Recommended", "accent").pack(side="left", padx=(8, 0))
        if m and m.get("language"):
            self.badge(title, LANG_TAGS.get(m["language"], m["language"]) + " only", "neutral").pack(side="left", padx=(6, 0))
        if m and m.get("experimental"):
            self.badge(title, "Experimental", "warn").pack(side="left", padx=(6, 0))
        if m and not inst and not self.fits(m):
            self.badge(title, "Too big for your graphics card", "bad").pack(side="left", padx=(6, 0))
        if inst and inst.get("source") and inst["source"] != "folder":
            self.badge(title, "from " + SOURCES.get(inst["source"], inst["source"]), "neutral").pack(side="left", padx=(6, 0))
        if self.model_tab == "llm" and inst and inst["id"] == default:
            self.badge(title, "Default", "good").pack(side="left", padx=(6, 0))

        if m:
            facts = f"{gb(m['size'])} download  ·  about {m['vram_mb'] / 1024:.1f} GB video memory  ·  {m['license']}"
            self.label(left, facts, "small").pack(anchor="w", pady=(3, 0))
            self.label(left, m["note"], "muted", wrap=True).pack(anchor="w", pady=(3, 0))
        else:
            self.label(left, f"{mb(inst.get('size', 0))}  ·  {inst['path']}", "small", wrap=True).pack(anchor="w", pady=(3, 0))

        prog = Bar(left, c, 6)
        status = self.label(left, "", "small")
        r = {"prog": prog, "status": status, "right": right}
        if inst:
            self.label(right, "✓ Installed", fg=c["accent"]).pack(side="left", padx=(0, 10))
            if self.model_tab == "llm" and inst["id"] != default:
                self.button(right, "Make default", lambda i=inst: self.save(llm_model=i["id"], llm_backend="builtin"),
                            "secondary", small=True).pack(side="left", padx=(0, 6))
            if inst.get("source") == "folder":
                self.button(right, "Delete", lambda i=inst: self.delete(self.model_tab, i), "secondary",
                            small=True).pack(side="left")
        elif m:
            r["dl"] = self.button(right, "Download", lambda mid=m["id"]: self.cmd("download", lambda x: self.refresh(), id=mid))
            r["dl"].pack(side="left")
            r["cancel"] = self.button(right, "Cancel", lambda mid=m["id"]: self.cmd("cancel_download", id=mid), "secondary")
        if m:
            self.dl_rows[m["id"]] = r

    def delete(self, kind, it):
        if messagebox.askyesno("Delete model", f"Delete {it['name']} ({mb(it.get('size', 0))})?\n\n"
                                               "You can download it again later.", parent=self.root):
            self.cmd("delete_model", lambda r: (self.toast("Deleted"), self.refresh(True)), kind=kind, path=it["path"])

    def render_downloads(self):
        jobs = self.st.get("downloads", [])
        finished = False
        for j in jobs:
            if self.prev_dl.get(j["id"]) not in (None, "done") and j["state"] == "done":
                finished = True
            self.prev_dl[j["id"]] = j["state"]
            r = self.dl_rows.get(j["id"])
            if not r:
                continue
            active = j["state"] in ("queued", "downloading", "verifying")
            if active:
                if not r["prog"].winfo_ismapped():
                    r["prog"].pack(fill="x", pady=(8, 2))
                    r["status"].pack(anchor="w")
                r["prog"].set([(j["done"] / j["total"] if j["total"] else 0, self.c["accent"])])
            elif j["state"] in ("error", "cancelled"):
                r["prog"].pack_forget()
                if not r["status"].winfo_ismapped():
                    r["status"].pack(anchor="w", pady=(6, 0))
            if "dl" in r:
                if active:
                    r["dl"].pack_forget()
                    if not r["cancel"].winfo_ismapped():
                        r["cancel"].pack(side="left")
                else:
                    r["cancel"].pack_forget()
                    if j["state"] != "done" and not r["dl"].winfo_ismapped():
                        r["dl"].configure(text="Resume" if j["state"] == "cancelled" else "Try again")
                        r["dl"].pack(side="left")
            speed = f"  ·  {mb(j['speed'])}/s" if j.get("speed") else ""
            r["status"].configure(
                fg=self.c["bad"] if j["state"] == "error" else self.c["muted"],
                text={"downloading": f"Downloading {gb(j['done'])} of {gb(j['total'])}{speed}",
                      "verifying": "Checking the download…", "queued": "Waiting for the other downloads…",
                      "error": "Download failed: " + j.get("error", ""),
                      "cancelled": "Paused - press Resume to continue where it stopped",
                      "done": "Installed"}.get(j["state"], ""))
        active = [j for j in jobs if j["state"] in ("queued", "downloading", "verifying")]
        if hasattr(self, "setup_dl"):
            self.setup_dl.configure(text=f"Downloading {len(active)} model{'s' if len(active) != 1 else ''}: "
                                         f"{gb(sum(j['done'] for j in active))} of {gb(sum(j['total'] for j in active))}"
                                    if active else "")
        if finished:
            self.refresh(True)

    def download_set(self):
        name, warn, ids, text = self.profile()
        cat = {m["id"]: m for m in self.st.get("catalog", [])}
        for i in ids:
            if i in cat and not cat[i]["installed"]:
                self.cmd("download", id=i)
        llm = next((i for i in ids if cat.get(i, {}).get("kind") == "llm"), None)
        if llm and not self.st.get("settings", {}).get("llm_model"):
            self.save(llm_model=llm)
        self.toast("Downloading - you can keep using your PC. Progress shows on the Models page.")
        self.refresh(True)

    # ================================================================ TRANSLATOR
    def _translator(self, p):
        c = self.c
        self.section_title(p, "Where translations come from", pady=(8, 8))
        src = self.card(p, pad=(6, 6))
        src.pack(fill="x")
        self.src_rows = {}
        for i, (key, title, desc) in enumerate((
                ("builtin", "Built-in (recommended)",
                 "Subtle runs the translator itself, only while it's needed, and stops it when you're done."),
                ("external", "Another program",
                 "LM Studio, Ollama or any OpenAI-compatible server that you start yourself."))):
            if i:
                tk.Frame(src, bg=c["line"], height=1).pack(fill="x", padx=12)
            r = tk.Frame(src, bg=c["panel"], padx=14, pady=10, cursor="hand2")
            r.pack(fill="x")
            mark = tk.Label(r, text="○", font=self.f["h2"], bg=c["panel"], fg=c["muted"])
            mark.pack(side="left", padx=(0, 12))
            txt = tk.Frame(r, bg=c["panel"])
            txt.pack(side="left", fill="x", expand=True)
            t = self.label(txt, title, "bold")
            t.pack(anchor="w")
            d = self.label(txt, desc, "small", wrap=True)
            d.pack(anchor="w")
            for w in (r, mark, txt, t, d):
                w.bind("<Button-1>", lambda e, k=key: self.save(llm_backend=k))
            self.src_rows[key] = mark

        self.ext_card = self.card(p)
        self.ext_url, self.ext_model = tk.StringVar(), tk.StringVar()
        for var, title, key in ((self.ext_url, "Server address", "external_url"),
                                (self.ext_model, "Model name (as that server calls it; empty = choose in the popup)", "llm_model")):
            self.label(self.ext_card, title, "small").pack(anchor="w", pady=(4, 2))
            e = self.entry(self.ext_card, var, 56)
            e.pack(anchor="w", ipady=5, pady=(0, 6))
            e.bind("<FocusOut>", lambda ev, v=var, k=key: self.save(**{k: v.get().strip()}))
            e.bind("<Return>", lambda ev, v=var, k=key: self.save(**{k: v.get().strip()}))

        # built-in model + start/stop
        self.builtin_wrap = tk.Frame(p, bg=c["bg"])
        self.section_title(self.builtin_wrap, "Translator model")
        bc = self.card(self.builtin_wrap)
        bc.pack(fill="x")
        right = self.setting_row(bc, "Default model", "Used unless you pick another one in the extension popup.", first=True)
        self.llm_pick = self.choice(right, [], lambda v: self.save(llm_model=v, llm_backend="builtin"), 40)
        self.llm_pick.pack()
        right = self.setting_row(bc, "Status", "")
        self.llm_dot = self.dot(right)
        self.llm_dot.pack(side="left")
        self.llm_status = self.label(right, "", "body")
        self.llm_status.pack(side="left", padx=(6, 14))
        self.button(right, "Start now", self.llm_start, "primary", small=True).pack(side="left", padx=(0, 6))
        self.button(right, "Stop", lambda: self.cmd("llm_stop", lambda x: self.refresh()), "secondary", small=True).pack(side="left")

        adv_head = tk.Frame(self.builtin_wrap, bg=c["bg"], cursor="hand2")
        adv_head.pack(fill="x", pady=(18, 6))
        self.adv_arrow = self.label(adv_head, "▸  Advanced", "bold", fg=c["muted"])
        self.adv_arrow.pack(side="left")
        self.adv = self.card(self.builtin_wrap)
        for w in (adv_head, self.adv_arrow):
            w.bind("<Button-1>", lambda e: self.toggle_adv())
        right = self.setting_row(self.adv, "On the graphics card", "Less = less video memory, but slower translations.", first=True)
        self.layers = self.choice(right, LAYER_CHOICES, lambda v: self.save(llm_gpu_layers=v), 30)
        self.layers.pack()
        right = self.setting_row(self.adv, "Context size", "How much text the translator can look at. Subtitles need little.")
        self.ctx = self.choice(right, CTX_CHOICES, lambda v: self.save(llm_context=v), 30)
        self.ctx.pack()
        right = self.setting_row(self.adv, "Compact memory", "8-bit context memory: less video memory, same translations.")
        self.kv = Toggle(right, c, lambda v: self.save(llm_kv_quant=v))
        self.kv.pack()

        # try it
        self.section_title(p, "Try it")
        tc = self.card(p)
        tc.pack(fill="x", pady=(0, 12))
        row = tk.Frame(tc, bg=c["panel"])
        row.pack(fill="x")
        self.test_text = tk.StringVar(value="今日は本当にありがとうございました。")
        self.entry(row, self.test_text, 40).pack(side="left", ipady=5, fill="x", expand=True)
        self.label(row, "  into  ", "muted").pack(side="left")
        self.test_target = self.choice(row, LANGS, lambda v: None, 12)
        self.test_target.current(0)
        self.test_target.pack(side="left", padx=(0, 10))
        self.test_btn = self.button(row, "Translate", self.llm_test)
        self.test_btn.pack(side="left")
        self.test_out = self.label(tc, "Type something in any language and press Translate.", "muted", wrap=True)
        self.test_out.pack(anchor="w", pady=(12, 0))

    def toggle_adv(self):
        if self.adv.winfo_ismapped():
            self.adv.pack_forget()
            self.adv_arrow.configure(text="▸  Advanced")
        else:
            self.adv.pack(fill="x")
            self.adv_arrow.configure(text="▾  Advanced")

    def llm_start(self):
        self.llm_status.configure(text="Starting…")
        self.cmd("llm_start", lambda r: self.refresh(), model=self.current_llm())

    def current_llm(self):
        i = self.llm_pick.current()
        return self.llm_pick.choices[i][0] if i >= 0 and self.llm_pick.choices else ""

    def llm_test(self):
        s = self.st.get("settings", {})
        self.test_out.configure(text="Translating… (the first time also loads the model)", fg=self.c["muted"],
                                font=self.f["body"])
        self.test_btn.configure(state="disabled")

        def done(r):
            self.test_btn.configure(state="normal")
            if r.get("error"):
                self.test_out.configure(text=r["error"], fg=self.c["bad"], font=self.f["body"])
            else:
                self.test_out.configure(text=f"{r['text']}      ({r['ms']} ms)", fg=self.c["text"], font=self.f["h2"])
            self.refresh()
        target = self.test_target.choices[max(0, self.test_target.current())][0]
        model = s.get("llm_model") if s.get("llm_backend") == "external" else self.current_llm()
        self._run(self.sup.panel_command({"cmd": "llm_test", "text": self.test_text.get(), "target": target,
                                          "model": model}), done)

    def render_translator(self):
        st, s, c = self.st, self.st.get("settings", {}), self.c
        ext = s.get("llm_backend") == "external"
        for key, mark in self.src_rows.items():
            on = (key == "external") == ext
            mark.configure(text="◉" if on else "○", fg=c["accent"] if on else c["muted"])
        if ext:
            self.ext_card.pack(fill="x", pady=(10, 0), after=self.src_rows["builtin"].master.master)
            self.builtin_wrap.pack_forget()
        else:
            self.ext_card.pack_forget()
            self.builtin_wrap.pack(fill="x", after=self.src_rows["builtin"].master.master)
        self.ext_url.set(s.get("external_url", ""))
        self.ext_model.set(s.get("llm_model", "") if ext else "")
        llms = st.get("models", {}).get("llm", [])
        choices = [(m["id"], f"{m['name']}   ({mb(m.get('size', 0))})") for m in llms] or \
                  [("", "No translator installed - see Models")]
        self.llm_pick.choices = choices
        self.llm_pick.configure(values=[lbl for _, lbl in choices])
        ids = [m["id"] for m in llms]
        self.set_choice(self.llm_pick, s.get("llm_model") if s.get("llm_model") in ids else (ids[0] if ids else ""))
        self.set_choice(self.layers, s.get("llm_gpu_layers", 999) if s.get("llm_gpu_layers") in (999, 24, 12, 0) else 999)
        self.set_choice(self.ctx, s.get("llm_context", 2048))
        self.kv.set(bool(s.get("llm_kv_quant", True)))
        self.render_llm_status()

    def render_llm_status(self):
        c = self.c
        t = self.st.get("translator", {})
        state = t.get("state")
        if not t.get("installed", True):
            text, tone = "The built-in translator is missing - run Install Subtle.bat again", "bad"
        else:
            text = {"ready": f"Running {t.get('model', '')}", "error": t.get("error", "Problem"),
                    "starting": "Starting…"}.get(state, "Not running (starts by itself when needed)")
            tone = {"ready": "ok", "error": "bad", "starting": "busy"}.get(state, "off")
        self.llm_status.configure(text=text, fg=c["bad"] if tone == "bad" else c["text"])
        self.llm_dot.configure(fg={"ok": c["accent"], "busy": c["warn"], "bad": c["bad"], "off": c["faint"]}[tone])

    # ================================================================ SETTINGS
    def _settings(self, p):
        c = self.c
        self.section_title(p, "Memory", pady=(8, 8))
        mc = self.card(p)
        mc.pack(fill="x")
        right = self.setting_row(mc, "Free memory after", "Unload everything when nothing was subtitled for this long.", first=True)
        self.idle = self.choice(right, IDLE_CHOICES, lambda v: self.save(idle_unload_min=v), 16)
        self.idle.pack()
        right = self.setting_row(mc, "Keep models loaded", "Subtitles start instantly, but the memory stays in use.")
        self.keep = Toggle(right, c, lambda v: self.save(keep_loaded=v))
        self.keep.pack()
        right = self.setting_row(mc, "Live previews", "Show the line while someone is still speaking. Less often = less GPU work.")
        self.prev = self.choice(right, PREVIEW_CHOICES, lambda v: self.save(previews=v), 26)
        self.prev.pack()

        self.section_title(p, "Speech model for each language")
        sc = self.card(p)
        sc.pack(fill="x")
        self.asr_defaults = {}
        for i, (key, title, desc) in enumerate((
                ("zh", "Chinese", "Automatic: SenseVoice with the Best engine if installed, otherwise Whisper."),
                ("ja", "Japanese", "Automatic: Parakeet with the Best engine if installed, otherwise Whisper."),
                ("ko", "Korean", "Automatic: Whisper."),
                ("other", "Other languages", "Automatic: Whisper large-v3 for Fast, turbo for Best."))):
            right = self.setting_row(sc, title, desc + " The popup's own choice still wins.", first=i == 0)
            cb = self.choice(right, [("auto", "Automatic (recommended)")], lambda v, k=key: self.save(**{f"asr_default_{k}": v}), 34)
            cb.pack()
            self.asr_defaults[key] = cb

        self.section_title(p, "Starting Subtle")
        ac = self.card(p)
        ac.pack(fill="x")
        right = self.setting_row(ac, "Start with Windows", "Waits in the tray and uses no video memory until you need it.", first=True)
        self.auto = Toggle(right, c, self.toggle_autostart)
        self.auto.pack()
        right = self.setting_row(ac, "Show this window when Subtle starts", "Otherwise it starts quietly in the tray.")
        self.open_start = Toggle(right, c, lambda v: self.save(open_panel_on_start=v))
        self.open_start.pack()
        right = self.setting_row(ac, "Use LM Studio's models", "Translator models you downloaded with LM Studio are used without copying.")
        self.scan_lms = Toggle(right, c, lambda v: self.save(scan_lmstudio=v))
        self.scan_lms.pack()

        self.section_title(p, "Files and logs")
        fc = self.card(p)
        fc.pack(fill="x")
        right = self.setting_row(fc, "Saved transcripts", "Only when 'Save transcripts' is on in the extension popup.", first=True)
        self.button(right, "Open folder", lambda: self.cmd("open", what="transcripts"), "secondary", small=True).pack()
        right = self.setting_row(fc, "Error log", "Errors only - never what was said.")
        self.button(right, "Open folder", lambda: self.cmd("open", what="logs"), "secondary", small=True).pack()
        right = self.setting_row(fc, "Activity log", "What Subtle did recently (kept in memory only).")
        self.log_btn = self.button(right, "Show", self.toggle_log, "secondary", small=True)
        self.log_btn.pack()
        self.log = tk.Text(fc, height=14, font=self.f["mono"], bg=c["field"], fg=c["text"], relief="flat",
                           highlightthickness=1, highlightbackground=c["line"], wrap="word", padx=8, pady=6)

        self.section_title(p, "Quit")
        qc = self.card(p)
        qc.pack(fill="x", pady=(0, 16))
        right = self.setting_row(qc, "Quit Subtle", "Closing this window keeps Subtle in the tray. Quit stops it completely.",
                                 first=True)
        self.button(right, "Quit Subtle", self.ask_quit, "danger", small=True).pack()

    def render_settings(self):
        s = self.st.get("settings", {})
        self.set_choice(self.idle, s.get("idle_unload_min", 5))
        self.keep.set(s.get("keep_loaded"))
        self.set_choice(self.prev, s.get("previews", "normal"))
        installed = (self.st.get("models") or {}).get("whisper", [])
        for key, cb in self.asr_defaults.items():        # installed speech models that suit the language
            fits = []
            for m in installed:
                langs = modelstore.model_languages(m["id"])
                if langs is None or key in langs:
                    fits.append((m["id"], m["name"]))
            cb.choices = [("auto", "Automatic (recommended)")] + fits
            cb.configure(values=[label for _, label in cb.choices])
            want = s.get(f"asr_default_{key}", "auto")
            self.set_choice(cb, want if any(v == want for v, _ in cb.choices) else "auto")
        self.open_start.set(s.get("open_panel_on_start", True))
        self.scan_lms.set(s.get("scan_lmstudio", True))
        self.auto.set(self.st.get("autostart"))

    def toggle_autostart(self, on):
        def done(r):
            if r.get("error"):
                self.auto.set(not on)
            else:
                self.toast("Subtle will start with Windows" if on else "Subtle won't start with Windows")
        self.cmd("autostart", done, on=on)

    def toggle_log(self):
        if self.log.winfo_ismapped():
            self.log.pack_forget()
            self.log_btn.configure(text="Show")
            return
        self.log.pack(fill="x", pady=(0, 12))
        self.log_btn.configure(text="Hide")

        def fill(r):
            text = "\n".join(r.get("lines", []))
            if r.get("llm"):
                text += "\n\n--- translator (llama-server) ---\n" + "\n".join(r["llm"])
            self.log.configure(state="normal")
            self.log.delete("1.0", "end")
            self.log.insert("end", text)
            self.log.see("end")
            self.log.configure(state="disabled")
        self.cmd("logs", fill)

    def ask_quit(self):
        if messagebox.askyesno("Quit Subtle", "Quit Subtle? Subtitles stop until you start it again.", parent=self.root):
            self.sup.request_stop()

    # ================================================================ SETUP
    def _step(self, p, n, title):
        c = self.c
        cd = self.card(p, pad=(20, 16))
        cd.pack(fill="x", pady=(8 if n == 1 else 12, 0))
        head = tk.Frame(cd, bg=c["panel"])
        head.pack(fill="x")
        num = tk.Label(head, text=str(n), font=self.f["bold"], bg=c["accent_soft"], fg=c["accent"], width=3, pady=2)
        num.pack(side="left", padx=(0, 12))
        self.label(head, title, "h2").pack(side="left")
        state = self.label(head, "", "small")
        state.pack(side="right")
        body = tk.Frame(cd, bg=c["panel"], padx=46)
        body.pack(fill="x", pady=(8, 0))
        return body, num, state

    def _setup(self, p):
        c = self.c
        b, self.step1_num, self.step1_state = self._step(p, 1, "Get the models for your PC")
        self.setup_gpu = self.label(b, "", "muted", wrap=True)
        self.setup_gpu.pack(anchor="w")
        self.setup_list = tk.Frame(b, bg=c["panel"])
        self.setup_list.pack(anchor="w", fill="x", pady=(8, 0))
        r = tk.Frame(b, bg=c["panel"])
        r.pack(anchor="w", pady=(10, 0))
        self.get_set = self.button(r, "Download the recommended models", self.download_set)
        self.get_set.pack(side="left")
        self.setup_dl = self.label(r, "", "small")
        self.setup_dl.pack(side="left", padx=10)

        b, _, _ = self._step(p, 2, "Add Subtle to your browser (Brave or Chrome)")
        steps = (
            ("Open a new browser tab and go to  brave://extensions   (Chrome: chrome://extensions)",
             ("Copy address", lambda: self.copy("brave://extensions"))),
            ("Turn on Developer mode (switch at the top right).", None),
            ("Click Load unpacked and choose Subtle's extension folder:", None),
            ("Optional: on the extension's Details page, turn on Allow in Private.", None),
        )
        for i, (text, action) in enumerate(steps, start=1):
            row = tk.Frame(b, bg=c["panel"])
            row.pack(anchor="w", fill="x", pady=3)
            self.label(row, f"{i}.", "muted", width=2).pack(side="left", anchor="n")
            self.label(row, text, wrap=170).pack(side="left", anchor="w")
            if action:
                self.button(row, action[0], action[1], "secondary", small=True).pack(side="left", padx=10)
            if i == 3:
                pr = tk.Frame(b, bg=c["panel"], padx=22)
                pr.pack(anchor="w", fill="x", pady=(2, 4))
                self.ext_path = tk.Label(pr, text="", font=self.f["mono"], bg=c["field"], fg=c["text"], padx=8, pady=4)
                self.ext_path.pack(side="left")
                self.button(pr, "Copy path", lambda: self.copy(self.st.get("paths", {}).get("extension", "")),
                            "secondary", small=True).pack(side="left", padx=(8, 4))
                self.button(pr, "Open folder", lambda: self.cmd("open", what="extension"), "secondary",
                            small=True).pack(side="left")

        b, _, _ = self._step(p, 3, "Watch something")
        for i, text in enumerate((
                "Open a video, click the Subtle icon in the browser toolbar, then Start subtitles on this tab "
                "(or press Alt+Shift+S).",
                "The first line takes a few seconds while the models load. After that, lines take about 0.1 s.",
                "Fast: English subtitles with the least memory.   Best: natural translations into any language."),
                start=1):
            row = tk.Frame(b, bg=c["panel"])
            row.pack(anchor="w", fill="x", pady=3)
            self.label(row, f"{i}.", "muted", width=2).pack(side="left", anchor="n")
            self.label(row, text, wrap=30).pack(side="left", anchor="w")
        self.button(b, "Done - go to Home", self.finish_setup).pack(anchor="w", pady=(12, 4))

    def render_setup(self):
        st, c = self.st, self.c
        name, warn, ids, text = self.profile()
        g = st.get("gpu") or {}
        self.setup_gpu.configure(text=(f"Your graphics card: {g['name']} ({self.gpu_gb():.0f} GB). " if g.get("name") else "")
                                 + text)
        for w in self.setup_list.winfo_children():
            w.destroy()
        cat = {m["id"]: m for m in st.get("catalog", [])}
        missing, size = 0, 0
        for i in ids:
            m = cat.get(i)
            if not m:
                continue
            row = tk.Frame(self.setup_list, bg=c["panel"])
            row.pack(anchor="w", fill="x", pady=2)
            tk.Label(row, text="✓" if m["installed"] else "•", font=self.f["bold"], bg=c["panel"],
                     fg=c["accent"] if m["installed"] else c["muted"], width=2).pack(side="left")
            self.label(row, f"{m['name']}  ").pack(side="left")
            self.label(row, gb(m["size"]) + ("  ·  installed" if m["installed"] else ""), "small").pack(side="left")
            if not m["installed"]:
                missing += 1
                size += m["size"]
        done = missing == 0
        self.get_set.configure(state="disabled" if done else "normal",
                               text="All recommended models are installed" if done else
                               f"Download the recommended models ({gb(size)})")
        self.step1_state.configure(text="✓ Done" if done else "", fg=c["accent"])
        self.step1_num.configure(text="✓" if done else "1")
        self.ext_path.configure(text=st.get("paths", {}).get("extension", ""))

    def finish_setup(self):
        self.save(first_run_done=True)
        self.go("home")

    # ================================================================ state
    def on_state(self, st, full):
        if not st:
            return
        if full:
            self.st = st
        else:
            self.st.update(st)
        self.render_side()
        if full or self.page == "home":       # pages you can't see aren't redrawn
            self.render_home()
        if full or (self.dl_rows and self.page in ("models", "setup")) or any(
                j["state"] in ("queued", "downloading", "verifying") for j in st.get("downloads", [])):
            self.render_downloads()
        if full:
            self.render_models()
            self.render_translator()
            self.render_settings()
            self.render_setup()
            if not st.get("settings", {}).get("first_run_done") and not self._setup_shown:
                self._setup_shown = True
                self.go("setup")
        elif self.page == "translator":
            self.render_llm_status()

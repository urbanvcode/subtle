"""
Subtle's window, drawn with Qt (PySide6).

Qt draws the whole window as one surface, so resizing and switching pages stay smooth (Tk made
every label its own Windows window: ~200 ms per resize step on a high-DPI screen, ~38 ms here),
for ~33 MB of RAM. ui.py (Tk) remains as a fallback when Qt can't be loaded.

Threads: the window owns the main thread; the supervisor's asyncio loop runs on a background
thread. Window -> loop: asyncio.run_coroutine_threadsafe. Loop/tray -> window: post(), which
goes through a queued Qt signal. Closing the window hides it (Subtle stays in the tray); nothing
is polled while it's hidden.
"""

import asyncio
import ctypes
import os
import sys
from pathlib import Path

from PySide6.QtCore import (QEasingCurve, QObject, QPropertyAnimation, QRectF, QSize, Qt, QTimer, QUrl, Signal,
                            Property)
from PySide6.QtGui import QColor, QDesktopServices, QFont, QFontDatabase, QIcon, QPainter
from PySide6.QtWidgets import (QAbstractButton, QApplication, QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel,
                               QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
                               QScrollArea, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget)

import modelstore

PC_SPOKEN = [("ja", "Japanese"), ("zh", "Chinese"), ("ko", "Korean"), ("en", "English"), ("es", "Spanish"),
             ("fr", "French"), ("de", "German"), ("it", "Italian"), ("pt", "Portuguese"), ("ru", "Russian"),
             ("th", "Thai"), ("vi", "Vietnamese"), ("id", "Indonesian"), ("hi", "Hindi"), ("ar", "Arabic"),
             ("tr", "Turkish"), ("ur", "Urdu"), ("auto", "Auto-detect")]
PC_TARGETS = [("en", "English"), ("ja", "Japanese"), ("zh", "Chinese"), ("ko", "Korean"), ("es", "Spanish"),
              ("fr", "French"), ("de", "German"), ("it", "Italian"), ("pt", "Portuguese"), ("ru", "Russian"),
              ("ar", "Arabic"), ("hi", "Hindi"), ("ur", "Urdu"), ("id", "Indonesian"), ("vi", "Vietnamese"),
              ("th", "Thai"), ("tr", "Turkish"), ("nl", "Dutch"), ("pl", "Polish")]

ASSETS = Path(__file__).resolve().parent / "assets"

PALETTES = {
    "dark": dict(bg="#16181d", panel="#1e2128", panel2="#262a32", side="#121418", text="#e8eaef", muted="#9aa1ad",
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
    "home": "What Subtle is doing right now, how fast subtitles arrive, and how much memory it uses.",
    "models": "Speech and translator models on this PC. Download what you need; delete what you don't.",
    "translator": "Used by the Best engine and for subtitles in any language other than English.",
    "settings": "How Subtle uses memory, which models it picks, and how it starts.",
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
ASR_DEFAULT_ROWS = (
    ("zh", "Chinese", "Automatic: SenseVoice with the Best engine if installed, otherwise Whisper."),
    ("ja", "Japanese", "Automatic: Parakeet with the Best engine if installed, otherwise Whisper."),
    ("ko", "Korean", "Automatic: Whisper."),
    ("other", "Other languages", "Automatic: Whisper large-v3 for Fast, turbo for Best."),
)


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


def clear(layout):
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None:
            w.deleteLater()
        elif item.layout() is not None:
            clear(item.layout())


# ---------------------------------------------------------------------------------------------
# Small widgets
# ---------------------------------------------------------------------------------------------
class Toggle(QAbstractButton):
    """An animated on/off switch."""

    def __init__(self, c, on_change=None):
        super().__init__()
        self.c = c
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(44, 24)
        self._x = 0.0
        self._anim = QPropertyAnimation(self, b"knob", self)
        self._anim.setDuration(140)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self.toggled.connect(self._animate)
        if on_change:
            self.clicked.connect(lambda checked: on_change(checked))     # user clicks only

    def _get(self):
        return self._x

    def _set(self, v):
        self._x = v
        self.update()

    knob = Property(float, _get, _set)

    def _animate(self, on):
        self._anim.stop()
        self._anim.setStartValue(self._x)
        self._anim.setEndValue(1.0 if on else 0.0)
        self._anim.start()

    def set_value(self, on):
        on = bool(on)
        if on != self.isChecked():
            self.blockSignals(True)
            self.setChecked(on)
            self.blockSignals(False)
            self._x = 1.0 if on else 0.0
            self.update()

    def sizeHint(self):
        return QSize(44, 24)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = self.c
        track = QColor(c["off"])
        on = QColor(c["accent"])
        mix = QColor(int(track.red() + (on.red() - track.red()) * self._x),
                     int(track.green() + (on.green() - track.green()) * self._x),
                     int(track.blue() + (on.blue() - track.blue()) * self._x))
        p.setPen(Qt.NoPen)
        p.setBrush(mix)
        p.drawRoundedRect(QRectF(0, 0, 44, 24), 12, 12)
        p.setBrush(QColor(c["knob"]))
        x = 3 + self._x * 20
        p.drawEllipse(QRectF(x, 3, 18, 18))


class Bar(QWidget):
    """A thin bar of coloured parts: [(fraction, colour), ...]."""

    def __init__(self, c, height=10):
        super().__init__()
        self.c, self.parts = c, []
        self.setFixedHeight(height)

    def set_parts(self, parts):
        parts = [(round(f, 3), col) for f, col in parts]
        if parts != self.parts:
            self.parts = parts
            self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(self.c["line"]))
        p.drawRoundedRect(QRectF(0, 0, w, h), h / 2, h / 2)
        p.setClipRect(QRectF(0, 0, w, h))
        x = 0.0
        for frac, col in self.parts:
            dx = max(0.0, min(1.0, frac)) * w
            if dx > 0:
                p.setBrush(QColor(col))
                p.drawRect(QRectF(x, 0, dx, h))
            x += dx


class Dot(QWidget):
    def __init__(self, c, size=10):
        super().__init__()
        self.c, self.color = c, QColor(c["faint"])
        self.setFixedSize(size, size)

    def set_tone(self, tone):
        col = QColor({"ok": self.c["accent"], "busy": self.c["warn"], "bad": self.c["bad"], "off": self.c["faint"]}[tone])
        if col != self.color:
            self.color = col
            self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(self.color)
        p.drawEllipse(QRectF(0, 0, self.width(), self.height()))


class Clickable(QFrame):
    clicked = Signal()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit()


class _Bridge(QObject):
    call = Signal(object, object)


# ---------------------------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------------------------
class Window(QMainWindow):
    def __init__(self, panel):
        super().__init__()
        self.panel = panel

    def closeEvent(self, e):
        if self.panel.quitting:
            e.accept()
        else:
            e.ignore()
            self.panel.hide()              # X = back to the tray; Subtle keeps running


class ControlPanel:
    COLUMN = 940            # page content never runs wider than this (px), even maximised

    def __init__(self, sup, show: bool):
        self.sup = sup
        self.st: dict = {}
        self.page = ""
        self.visible = False
        self.quitting = False
        self.models_sig = None
        self.model_tab = "whisper"
        self.dl_rows: dict[str, dict] = {}
        self.prev_dl: dict[str, str] = {}
        self._setup_shown = False

        if sys.platform == "win32":
            try:
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Subtle.App")
            except Exception:
                pass
        self.app = QApplication.instance() or QApplication(sys.argv[:1])
        self.app.setQuitOnLastWindowClosed(False)
        self.app.setApplicationName("Subtle")
        theme = os.environ.get("SUBTLE_UI_THEME")
        self.dark = windows_dark() if theme not in ("light", "dark") else theme == "dark"
        self.c = PALETTES["dark" if self.dark else "light"]
        fams = set(QFontDatabase.families())
        self.icon_family = "Segoe Fluent Icons" if "Segoe Fluent Icons" in fams else \
            "Segoe MDL2 Assets" if "Segoe MDL2 Assets" in fams else None
        base = QFont("Segoe UI", 10)
        base.setHintingPreference(QFont.PreferNoHinting)
        self.app.setFont(base)
        self.app.setStyleSheet(self._qss())

        self.bridge = _Bridge()
        self.bridge.call.connect(lambda fn, args: fn(*args), Qt.QueuedConnection)

        self.win = Window(self)
        port = getattr(sup, "port", 8765)
        self.win.setWindowTitle("Subtle" if port == 8765 else f"Subtle (port {port})")
        if (ASSETS / "subtle.ico").is_file():
            self.win.setWindowIcon(QIcon(str(ASSETS / "subtle.ico")))
        size = os.environ.get("SUBTLE_UI_SIZE", "1080x740").split("x")
        self.win.resize(int(size[0]), int(size[1]))
        self.win.setMinimumSize(820, 560)
        self._build()

        self.timer = QTimer()
        self.timer.setInterval(2000)
        self.timer.timeout.connect(lambda: self.refresh(False))

        start = os.environ.get("SUBTLE_UI_PAGE")
        if start in dict(PAGES):
            self._setup_shown = True
            self.go(start)
        else:
            self.go("home")
        if show:
            self.show()

    # ---------------------------------------------------------------- plumbing
    def post(self, fn, *args):
        """Run fn(*args) on the window's thread (safe from any thread)."""
        self.bridge.call.emit(fn, args)

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

    def cmd(self, name, cb=None, **kw):
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

    def show(self):
        self.win.show()
        self.win.setWindowState((self.win.windowState() & ~Qt.WindowMinimized) | Qt.WindowActive)
        self.win.raise_()
        self.win.activateWindow()
        self.park_focus()
        self._dark_titlebar()
        if not self.visible:
            self.visible = True
            self.refresh(True)
            self.timer.start()

    def hide(self):
        self.visible = False
        self.timer.stop()
        self.win.hide()

    def quit(self):
        self.visible = False
        self.quitting = True
        self.timer.stop()
        self.win.close()
        self.app.quit()

    def mainloop(self):
        self.app.exec()

    def park_focus(self):
        """Keep keyboard focus on the window itself: a focused button inside a page makes Qt scroll the
        page to it (pages then opened half-way down, or jumped when a list was rebuilt)."""
        root = self.win.centralWidget()
        if root is not None:
            root.setFocusPolicy(Qt.StrongFocus)
            root.setFocus(Qt.OtherFocusReason)

    def _dark_titlebar(self):
        if sys.platform != "win32" or not self.dark:
            return
        try:
            hwnd = int(self.win.winId())
            val = ctypes.c_int(1)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(val), ctypes.sizeof(val))
        except Exception:
            pass

    # ---------------------------------------------------------------- look
    def _qss(self):
        c = self.c
        return f"""
        QWidget {{ color: {c['text']}; font-family: 'Segoe UI'; font-size: 10pt; }}
        QMainWindow, #content, #page, QScrollArea, QScrollArea > QWidget > QWidget {{ background: {c['bg']}; }}
        #side {{ background: {c['side']}; }}
        #card {{ background: {c['panel']}; border: 1px solid {c['line']}; border-radius: 12px; }}
        #soft {{ background: {c['accent_soft']}; border-radius: 12px; }}
        #softwarn {{ background: {c['warn_soft']}; border-radius: 12px; }}
        #sep {{ background: {c['line']}; max-height: 1px; min-height: 1px; border: none; }}
        QLabel {{ background: transparent; }}
        QLabel[kind="title"] {{ font-size: 21pt; font-weight: 600; }}
        QLabel[kind="h2"] {{ font-size: 13pt; font-weight: 600; }}
        QLabel[kind="big"] {{ font-size: 17pt; font-weight: 600; }}
        QLabel[kind="huge"] {{ font-size: 22pt; font-weight: 600; }}
        QLabel[kind="bold"] {{ font-weight: 600; }}
        QLabel[kind="muted"] {{ color: {c['muted']}; }}
        QLabel[kind="small"] {{ color: {c['muted']}; font-size: 9pt; }}
        QLabel[kind="faint"] {{ color: {c['faint']}; font-size: 9pt; }}
        QLabel[tone="ok"] {{ color: {c['accent']}; }}
        QLabel[tone="busy"] {{ color: {c['warn']}; }}
        QLabel[tone="bad"] {{ color: {c['bad']}; }}
        QLabel[badge="accent"] {{ background: {c['accent']}; color: {c['on_accent']}; border-radius: 9px; padding: 1px 8px; font-size: 9pt; }}
        QLabel[badge="good"] {{ background: {c['accent_soft']}; color: {c['accent']}; border-radius: 9px; padding: 1px 8px; font-size: 9pt; }}
        QLabel[badge="warn"] {{ background: {c['warn_soft']}; color: {c['warn']}; border-radius: 9px; padding: 1px 8px; font-size: 9pt; }}
        QLabel[badge="bad"] {{ background: {c['bad_soft']}; color: {c['bad']}; border-radius: 9px; padding: 1px 8px; font-size: 9pt; }}
        QLabel[badge="neutral"] {{ background: {c['panel2']}; color: {c['muted']}; border-radius: 9px; padding: 1px 8px; font-size: 9pt; }}
        QPushButton {{ background: {c['panel2']}; color: {c['text']}; border: 1px solid {c['line']}; border-radius: 8px;
                      padding: 7px 14px; }}
        QPushButton:hover {{ background: {c['line']}; }}
        QPushButton:disabled {{ color: {c['faint']}; }}
        QPushButton[kind="primary"] {{ background: {c['accent']}; color: {c['on_accent']}; border: none; font-weight: 600; }}
        QPushButton[kind="primary"]:hover {{ background: {c['accent_hover']}; }}
        QPushButton[kind="primary"]:disabled {{ background: {c['panel2']}; color: {c['faint']}; }}
        QPushButton[kind="danger"] {{ background: {c['bad']}; color: #ffffff; border: none; font-weight: 600; }}
        QPushButton[size="small"] {{ padding: 4px 11px; font-size: 9pt; }}
        #nav {{ background: transparent; border-radius: 8px; }}
        #nav:hover {{ background: {c['panel2']}; }}
        #nav[active="true"] {{ background: {c['accent_soft']}; }}
        #nav QLabel {{ font-size: 11pt; }}
        #nav[active="true"] QLabel {{ color: {c['accent']}; }}
        #tab {{ background: transparent; border: none; border-bottom: 2px solid transparent; border-radius: 0;
                padding: 6px 2px; margin-right: 16px; color: {c['muted']}; font-weight: 600; }}
        #tab:hover {{ color: {c['text']}; background: transparent; }}
        #tab[active="true"] {{ color: {c['accent']}; border-bottom: 2px solid {c['accent']}; }}
        #choice {{ background: {c['panel']}; border: none; border-radius: 10px; }}
        #choice:hover {{ background: {c['panel2']}; }}
        QComboBox {{ background: {c['field']}; border: 1px solid {c['line']}; border-radius: 8px; padding: 6px 10px;
                     min-height: 20px; }}
        QComboBox:hover {{ border-color: {c['faint']}; }}
        QComboBox::drop-down {{ border: none; width: 26px; }}
        QComboBox::down-arrow {{ image: url("{(ASSETS / ('chevron-dark.png' if self.dark else 'chevron-light.png')).as_posix()}");
                                 width: 12px; height: 12px; margin-right: 10px; }}
        QComboBox QAbstractItemView {{ background: {c['panel']}; border: 1px solid {c['line']}; padding: 4px;
                                       selection-background-color: {c['accent']}; selection-color: {c['on_accent']}; outline: 0; }}
        QLineEdit, QPlainTextEdit {{ background: {c['field']}; border: 1px solid {c['line']}; border-radius: 8px; padding: 6px 10px;
                                     selection-background-color: {c['accent']}; }}
        QLineEdit:focus, QComboBox:focus {{ border-color: {c['accent']}; }}
        QPlainTextEdit {{ font-family: Consolas; font-size: 9pt; }}
        QProgressBar {{ background: {c['line']}; border: none; border-radius: 3px; max-height: 6px; }}
        QProgressBar::chunk {{ background: {c['accent']}; border-radius: 3px; }}
        QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
        QScrollBar::handle:vertical {{ background: {c['line']}; border-radius: 4px; min-height: 30px; }}
        QScrollBar::handle:vertical:hover {{ background: {c['other']}; }}
        QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{ height: 0; background: none; }}
        QToolTip {{ background: {c['panel']}; color: {c['text']}; border: 1px solid {c['line']}; }}
        """

    # building blocks ---------------------------------------------------------------------------
    def label(self, text="", kind="body", wrap=False, tone=None):
        lbl = QLabel(text)
        if kind != "body":
            lbl.setProperty("kind", kind)
        if tone:
            lbl.setProperty("tone", tone)
        if wrap:
            lbl.setWordWrap(True)

        return lbl

    @staticmethod
    def restyle(w, **props):
        changed = False
        for k, v in props.items():
            if w.property(k) != v:
                w.setProperty(k, v)
                changed = True
        if changed:
            w.style().unpolish(w)
            w.style().polish(w)

    @staticmethod
    def set_text(lbl, text):
        if lbl.text() != text:
            lbl.setText(text)

    def button(self, text, fn, kind="secondary", small=False):
        b = QPushButton(text)
        b.setCursor(Qt.PointingHandCursor)
        if kind != "secondary":
            b.setProperty("kind", kind)
        if small:
            b.setProperty("size", "small")
        b.clicked.connect(lambda: fn())
        return b

    def badge(self, text, tone="neutral"):
        b = QLabel(text)
        b.setProperty("badge", tone)
        b.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        return b

    def card(self, margins=(20, 16, 20, 16), name="card"):
        f = QFrame()
        f.setObjectName(name)
        lay = QVBoxLayout(f)
        lay.setContentsMargins(*margins)
        lay.setSpacing(8)
        return f, lay

    def sep(self):
        s = QFrame()
        s.setObjectName("sep")
        return s

    def combo(self, choices, on_pick, width=None):
        cb = QComboBox()
        cb.setCursor(Qt.PointingHandCursor)
        self.fill_combo(cb, choices)
        if width:
            cb.setMinimumWidth(width)
        cb.activated.connect(lambda i: on_pick(cb.itemData(i)))       # user picks only
        return cb

    @staticmethod
    def fill_combo(cb, choices, value=None):
        cur = [(cb.itemData(i), cb.itemText(i)) for i in range(cb.count())]
        if cur != [(v, t) for v, t in choices]:
            cb.blockSignals(True)
            cb.clear()
            for v, t in choices:
                cb.addItem(t, v)
            cb.blockSignals(False)
        if value is not None:
            ControlPanel.set_combo(cb, value)

    @staticmethod
    def set_combo(cb, value):
        i = cb.findData(value)
        if i >= 0 and i != cb.currentIndex():
            cb.blockSignals(True)
            cb.setCurrentIndex(i)
            cb.blockSignals(False)

    def setting_row(self, lay, title, desc, control, first=False):
        if not first:
            lay.addWidget(self.sep())
        row = QHBoxLayout()
        row.setContentsMargins(0, 6, 0, 6)
        row.setSpacing(16)
        left = QVBoxLayout()
        left.setSpacing(2)
        left.addWidget(self.label(title))
        if desc:
            left.addWidget(self.label(desc, "small", wrap=True))
        row.addLayout(left, 1)
        row.addWidget(control, 0, Qt.AlignVCenter)
        lay.addLayout(row)
        return row

    def section(self, lay, text, top=22):
        lay.addSpacing(top)
        lay.addWidget(self.label(text, "h2"))
        lay.addSpacing(4)

    def toast(self, text, ms=3500, bad=False):
        self.status.setText(text)
        self.restyle(self.status, tone="bad" if bad else "")
        QTimer.singleShot(ms, lambda: self.status.text() == text and self.status.setText(""))

    def copy(self, text):
        QApplication.clipboard().setText(text)
        self.toast("Copied to the clipboard")

    # ---------------------------------------------------------------- layout
    def _build(self):
        c = self.c
        root = QWidget()
        h = QHBoxLayout(root)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        self.win.setCentralWidget(root)

        # sidebar
        side = QFrame()
        side.setObjectName("side")
        side.setFixedWidth(232)
        sv = QVBoxLayout(side)
        sv.setContentsMargins(14, 20, 14, 16)
        sv.setSpacing(2)
        brand = QHBoxLayout()
        brand.setContentsMargins(6, 0, 0, 18)
        logo = QLabel()
        if (ASSETS / "subtle.png").is_file():
            logo.setPixmap(QIcon(str(ASSETS / "subtle.png")).pixmap(38, 38))
        brand.addWidget(logo)
        brand.addSpacing(10)
        names = QVBoxLayout()
        names.setSpacing(0)
        names.addWidget(self.label("Subtle", "h2"))
        self.ver = self.label("", "faint")
        names.addWidget(self.ver)
        brand.addLayout(names)
        brand.addStretch(1)
        sv.addLayout(brand)

        self.nav = {}
        for key, title in PAGES:
            item = Clickable()
            item.setObjectName("nav")
            item.setCursor(Qt.PointingHandCursor)
            hl = QHBoxLayout(item)
            hl.setContentsMargins(12, 9, 12, 9)
            hl.setSpacing(12)
            icon = QLabel(ICONS[key] if self.icon_family else "")
            if self.icon_family:      # the app-wide stylesheet font would override setFont(), so set it here
                icon.setStyleSheet(f"font-family: '{self.icon_family}'; font-size: 12pt;")
            icon.setFixedWidth(20)
            hl.addWidget(icon)
            hl.addWidget(QLabel(title), 1)
            item.clicked.connect(lambda k=key: self.go(k))
            sv.addWidget(item)
            self.nav[key] = item
        sv.addStretch(1)
        sv.addWidget(self.sep())
        sv.addSpacing(10)
        self.side_status = {}
        for key, title in (("app", "Subtle"), ("engine", "Speech engine"), ("llm", "Translator"), ("delay", "Delay")):
            r = QHBoxLayout()
            r.setContentsMargins(8, 2, 0, 2)
            r.setSpacing(8)
            d = Dot(c, 8)
            r.addWidget(d)
            t = self.label(f"{title}: …", "small")
            r.addWidget(t, 1)
            sv.addLayout(r)
            self.side_status[key] = (d, t, title)
        h.addWidget(side)

        # content: header + pages + status line
        content = QWidget()
        content.setObjectName("content")
        cv = QVBoxLayout(content)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(0)
        head = QWidget()
        hv = QVBoxLayout(head)
        hv.setContentsMargins(40, 24, 40, 14)
        hv.setSpacing(4)
        hrow = QHBoxLayout()
        self.h_title = self.label("", "title")
        hrow.addWidget(self.h_title)
        hrow.addStretch(1)
        self.h_actions = QStackedWidget()
        self.h_actions.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        hrow.addWidget(self.h_actions)
        hv.addLayout(hrow)
        self.h_sub = self.label("", "muted", wrap=True)
        hv.addWidget(self.h_sub)
        cv.addWidget(head)
        line = self.sep()
        lw = QWidget()
        ll = QHBoxLayout(lw)
        ll.setContentsMargins(40, 0, 40, 0)
        ll.addWidget(line)
        cv.addWidget(lw)

        self.stack = QStackedWidget()
        cv.addWidget(self.stack, 1)
        self.status = self.label("", "small")
        sw = QWidget()
        sl = QHBoxLayout(sw)
        sl.setContentsMargins(40, 4, 40, 10)
        sl.addWidget(self.status)
        cv.addWidget(sw)
        h.addWidget(content, 1)

        self.pages, self.page_actions = {}, {}
        builders = {"home": self._home, "models": self._models, "translator": self._translator,
                    "settings": self._settings, "setup": self._setup}
        for key, _ in PAGES:
            sa = QScrollArea()
            sa.setWidgetResizable(True)
            sa.setFrameShape(QFrame.NoFrame)
            sa.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            inner = QWidget()
            inner.setObjectName("page")
            outer = QHBoxLayout(inner)
            outer.setContentsMargins(40, 12, 40, 24)
            col = QWidget()
            col.setMaximumWidth(self.COLUMN)
            lay = QVBoxLayout(col)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(12)
            outer.addWidget(col, 1)
            outer.addStretch(0)
            sa.setWidget(inner)
            self.stack.addWidget(sa)
            self.pages[key] = sa
            acts = QWidget()
            al = QHBoxLayout(acts)
            al.setContentsMargins(0, 0, 0, 0)
            al.setSpacing(8)
            self.h_actions.addWidget(acts)
            self.page_actions[key] = (acts, al)
            builders[key](lay)
            lay.addStretch(1)

    def go(self, key):
        for k, item in self.nav.items():
            self.restyle(item, active="true" if k == key else "false")
        if key != self.page:
            sa = self.pages[key]
            self.park_focus()
            sa.verticalScrollBar().setValue(0)
            self.stack.setCurrentWidget(sa)
            self.h_actions.setCurrentWidget(self.page_actions[key][0])
            self.h_title.setText(dict(PAGES)[key])
            self.h_sub.setText(SUBTITLES[key])
        self.page = key
        if self.visible and key in ("models", "setup"):
            self.refresh(True)

    # ================================================================ HOME
    def _home(self, lay):
        c = self.c
        # "A new version is available" (from the daily GitHub check; Settings can turn it off)
        self.upd = QFrame()
        self.upd.setObjectName("soft")
        ul = QHBoxLayout(self.upd)
        ul.setContentsMargins(20, 12, 20, 12)
        ul.setSpacing(16)
        self.upd_text = self.label("", "bold", wrap=True)
        ul.addWidget(self.upd_text, 1)
        self.upd_url = ""
        ul.addWidget(self.button("Download page", lambda: self.upd_url and QDesktopServices.openUrl(QUrl(self.upd_url)), "primary"),
                     0, Qt.AlignVCenter)
        self.upd.hide()
        lay.addWidget(self.upd)

        hero, hl = self.card((24, 20, 24, 20))
        top = QHBoxLayout()
        top.setSpacing(12)
        self.hero_dot = Dot(c, 14)
        top.addWidget(self.hero_dot, 0, Qt.AlignVCenter)
        self.hero_title = self.label("Starting…", "big")
        top.addWidget(self.hero_title, 1)
        hl.addLayout(top)
        self.hero_sub = self.label("", "muted", wrap=True)
        hl.addWidget(self.hero_sub)
        lay.addWidget(hero)

        # subtitles for the PC's own sound (any app)
        pc, pl = self.card((24, 18, 24, 18))
        ph = QHBoxLayout()
        ph.addWidget(self.label("Subtitles for any app", "h2"))
        ph.addStretch(1)
        self.pc_btn = self.button("Start", self.pc_toggle, "primary")
        ph.addWidget(self.pc_btn)
        pl.addLayout(ph)
        pl.addWidget(self.label("Discord, Zoom, games, VLC, a film on your PC: Subtle listens to what your speakers "
                                "play and shows subtitles at the bottom of your screen.", "small", wrap=True))
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(4)
        self.pc_lang = self.combo(PC_SPOKEN, lambda v: self.pc_save(pc_language=v))
        self.pc_target = self.combo(PC_TARGETS, lambda v: self.pc_save(pc_target=v))
        self.pc_mode = self.combo([("whisper", "Fast (to English)"), ("llm", "Best (any language)")],
                                  lambda v: self.pc_save(pc_mode=v))
        self.pc_dev = self.combo([("", "Default speakers")], lambda v: self.pc_save(pc_device=v))
        for col, (title, w) in enumerate((("Spoken", self.pc_lang), ("Subtitles in", self.pc_target),
                                          ("Engine", self.pc_mode), ("Listen to", self.pc_dev))):
            grid.addWidget(self.label(title, "small"), 0, col)
            grid.addWidget(w, 1, col)
            grid.setColumnStretch(col, 1)
        self.pc_size = self.combo([(v, f"{v}%") for v in (70, 85, 100, 125, 150, 200)],
                                  lambda v: self.pc_save(False, pc_size=v))
        self.pc_height = self.combo([(v, f"{v}% up") for v in (2, 5, 8, 12, 20, 30, 45)],
                                    lambda v: self.pc_save(False, pc_height=v))
        self.pc_opacity = self.combo([(v, f"{v}%") for v in (0, 30, 45, 62, 80, 100)],
                                     lambda v: self.pc_save(False, pc_opacity=v))
        self.pc_screen = self.combo([(0, "Main screen")], lambda v: self.pc_save(False, pc_screen=v))
        for col, (title, w) in enumerate((("Text size", self.pc_size), ("Position", self.pc_height),
                                          ("Box", self.pc_opacity), ("Screen", self.pc_screen))):
            grid.addWidget(self.label(title, "small"), 2, col)
            grid.addWidget(w, 3, col)
        pl.addLayout(grid)
        self.pc_state = self.label("", "small", wrap=True)
        pl.addWidget(self.pc_state)
        lay.addWidget(pc)
        self.overlay = None
        self._pc_devices_loaded = False

        # subtitle delay
        dc, dl = self.card((24, 18, 24, 18))
        dh = QHBoxLayout()
        dh.addWidget(self.label("Subtitle delay", "h2"))
        dh.addStretch(1)
        self.delay_count = self.label("", "small")
        dh.addWidget(self.delay_count)
        dh.addSpacing(10)
        dh.addWidget(self.button("Reset", lambda: self.cmd("reset_delay", lambda r: self.refresh()), small=True))
        dl.addLayout(dh)
        dl.addWidget(self.label("From the moment the speaker stops to the moment the words appear. Lower is better.",
                                "small", wrap=True))
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(2)
        self.delay_vals = {}
        for i, (key, title) in enumerate((("last", "Last line"), ("avg", "Average"), ("median", "Median"),
                                          ("compute", "Speech + translation"))):
            box = QFrame()
            box.setObjectName("soft" if i < 3 else "card")
            bl = QVBoxLayout(box)
            bl.setContentsMargins(16, 12, 16, 12)
            bl.setSpacing(0)
            bl.addWidget(self.label(title, "small"))
            v = self.label("–", "huge")
            bl.addWidget(v)
            u = self.label("", "faint")
            bl.addWidget(u)
            grid.addWidget(box, 0, i)
            grid.setColumnStretch(i, 1)
            self.delay_vals[key] = (v, u)
        dl.addSpacing(6)
        dl.addLayout(grid)
        lay.addWidget(dc)

        # three tiles
        tiles = QHBoxLayout()
        tiles.setSpacing(12)
        self.tiles = {}
        for key, title in (("engine", "Speech engine"), ("llm", "Translator"), ("vram", "Subtle's video memory")):
            t, tl = self.card((18, 14, 18, 14))
            tl.setSpacing(2)
            tl.addWidget(self.label(title, "small"))
            v = self.label("-", "big")
            tl.addWidget(v)
            d = self.label("", "small", wrap=True)
            tl.addWidget(d)
            tiles.addWidget(t, 1)
            self.tiles[key] = (v, d)
        lay.addLayout(tiles)

        # memory
        mc, ml = self.card((24, 18, 24, 18))
        mh = QHBoxLayout()
        mh.addWidget(self.label("Memory", "h2"))
        mh.addStretch(1)
        mh.addWidget(self.button("Free memory now", lambda: self.cmd(
            "unload", lambda r: self.toast("Models unloaded - their memory is free again"))))
        ml.addLayout(mh)
        self.gpu_line = self.label("", "small")
        ml.addWidget(self.gpu_line)
        self.vbar = Bar(c, 12)
        ml.addWidget(self.vbar)
        leg = QHBoxLayout()
        for col, text in ((c["accent"], "Subtle"), (c["other"], "Other programs"), (c["line"], "Free")):
            sq = QLabel("■")
            sq.setStyleSheet(f"color: {col}; font-size: 9pt;")
            leg.addWidget(sq)
            leg.addWidget(self.label(text, "small"))
            leg.addSpacing(10)
        leg.addStretch(1)
        self.vtext = self.label("", "small")
        leg.addWidget(self.vtext)
        ml.addLayout(leg)
        ml.addSpacing(6)
        g = QGridLayout()
        g.setVerticalSpacing(8)
        for col, (t, al) in enumerate((("Part", Qt.AlignLeft), ("Video memory", Qt.AlignRight), ("Memory (RAM)", Qt.AlignRight))):
            g.addWidget(self.label(t, "faint"), 0, col, al)
        g.setColumnStretch(0, 1)
        g.setColumnMinimumWidth(1, 150)
        g.setColumnMinimumWidth(2, 150)
        self.usage_cells = {}
        for r, (key, name) in enumerate((("engine", "Speech engine"), ("translator", "Translator"),
                                          ("supervisor", "Subtle app (window + tray)")), start=1):
            g.addWidget(self.sep(), 2 * r - 1, 0, 1, 3)
            g.addWidget(self.label(name), 2 * r, 0)
            v, m = self.label("-"), self.label("-")
            g.addWidget(v, 2 * r, 1, Qt.AlignRight)
            g.addWidget(m, 2 * r, 2, Qt.AlignRight)
            self.usage_cells[key] = (v, m)
        ml.addLayout(g)
        self.idle_rule = self.label("", "small", wrap=True)
        ml.addSpacing(4)
        ml.addWidget(self.idle_rule)
        lay.addWidget(mc)

        self.notice, nl = self.card((20, 14, 20, 14), name="softwarn")
        nr = QHBoxLayout()
        self.notice_text = self.label("", wrap=True)
        self.notice_text.setStyleSheet(f"color: {c['warn']};")
        nr.addWidget(self.notice_text, 1)
        nr.addWidget(self.button("Open Setup guide", lambda: self.go("setup"), "primary"))
        nl.addLayout(nr)
        self.notice.hide()
        lay.addWidget(self.notice)

    # ---------------------------------------------------------------- PC sound
    def pc_toggle(self):
        running = (self.st.get("pc") or {}).get("running")
        self.pc_btn.setEnabled(False)
        def done(r):
            self.pc_btn.setEnabled(True)
            self.refresh()
        self.cmd("pc_stop" if running else "pc_start", done)

    def pc_save(self, restart=True, **changes):
        running = (self.st.get("pc") or {}).get("running")
        if not restart:                           # only the look: apply it right away
            self.st.setdefault("settings", {}).update(changes)
            self.pc_apply_style()
        def after(r):
            if restart and running:
                if "pc_device" in changes:
                    self.cmd("pc_start", lambda r: self.refresh())
                else:
                    self.cmd("pc_update", lambda r: self.refresh())
            self.refresh(True)
        self.cmd("settings", after, changes=changes)

    def pc_apply_style(self):
        s = self.st.get("settings", {})
        if self.overlay is None:
            from overlay_qt import SubtitleOverlay
            self.overlay = SubtitleOverlay()
        self.overlay.apply_style(s.get("pc_size", 100), s.get("pc_height", 8), s.get("pc_opacity", 62), s.get("pc_screen", 0))

    def pc_message(self, msg):
        if self.overlay is None:
            self.pc_apply_style()
        self.overlay.on_message(msg)

    def render_pc(self):
        st = self.st
        s, pc = st.get("settings", {}), st.get("pc") or {}
        card = self.pc_btn.parentWidget()
        hide = bool(st.get("simple")) and not pc.get("running")
        if card is not None and card.isHidden() != hide:
            card.setVisible(not hide)
        if hide:
            return
        self.set_combo(self.pc_lang, s.get("pc_language", "ja"))
        self.set_combo(self.pc_target, s.get("pc_target", "en"))
        self.set_combo(self.pc_mode, s.get("pc_mode", "whisper"))
        self.set_combo(self.pc_size, s.get("pc_size", 100))
        self.set_combo(self.pc_height, s.get("pc_height", 8))
        self.set_combo(self.pc_opacity, s.get("pc_opacity", 62))
        screens = QApplication.screens()
        self.fill_combo(self.pc_screen, [(i, "Main screen" if i == 0 else f"Screen {i + 1}") for i in range(len(screens))],
                        s.get("pc_screen", 0))
        if not self._pc_devices_loaded:
            self._pc_devices_loaded = True
            def got(r):
                devs = r.get("devices") or []
                choices = [("", "Default speakers")] + [(d["id"], d["name"]) for d in devs if not d.get("default")]
                self.fill_combo(self.pc_dev, choices, s.get("pc_device", ""))
            self.cmd("pc_devices", got)
        else:
            self.set_combo(self.pc_dev, s.get("pc_device", ""))
        running = pc.get("running")
        self.set_text(self.pc_btn, "Stop" if running else "Start")
        kind = "secondary" if running else "primary"
        if self.pc_btn.property("kind") != kind:
            self.pc_btn.setProperty("kind", kind)
            self.pc_btn.style().unpolish(self.pc_btn)
            self.pc_btn.style().polish(self.pc_btn)
        if pc.get("error"):
            text = pc["error"]
        elif running:
            text = f"Listening to {pc.get('device') or 'your speakers'}." +                    (f"  {pc['status']}" if pc.get("status") and pc.get("status") != "Ready" else "") +                    "  Games in exclusive fullscreen hide the subtitles: use borderless or windowed mode."
        else:
            text = "Off. Nothing is recorded; when on, the sound only goes to the speech engine on this PC."
        self.set_text(self.pc_state, text)

    def render_home(self):
        st, c = self.st, self.c
        self.render_pc()
        s, e, t = st.get("settings", {}), st.get("engine", {}), st.get("translator", {})
        ext = s.get("llm_backend") == "external"
        tabs = st.get("tabs", 0)
        loaded = [m.split("/")[-1] for m in e.get("loaded", [])]
        u = st.get("usage") or {}
        subtle_v = (u.get("engine") or {}).get("vram", 0) + (u.get("translator") or {}).get("vram", 0)
        es, ts = e.get("state"), t.get("state")

        if es == "error" or ts == "error":
            tone, title, sub = "bad", "Something needs attention", e.get("error") or t.get("error") or ""
        elif es == "starting" or ts == "starting":
            tone, title, sub = "busy", "Loading models…", "This takes a few seconds; subtitles start right after."
        elif tabs:
            pc_on = bool((st.get("pc") or {}).get("running"))
            browser_tabs = tabs - (1 if pc_on else 0)
            what = [f"{browser_tabs} tab{'s' if browser_tabs != 1 else ''}"] if browser_tabs else []
            what += ["your PC's sound"] if pc_on else []
            tone, title = "ok", "Subtitling " + " and ".join(what)
            sub = f"Using {', '.join(loaded) or 'the speech engine'}" + \
                  (f" and the translator {t.get('model')}" if ts == "ready" else "") + "."
        else:
            tone, title = "off", "Ready"
            sub = ("Nothing is loaded, so Subtle uses no video memory. Start subtitles from the Subtle icon in "
                   "your browser.") if not subtle_v else \
                "No tab is using Subtle right now. Models are still loaded and will be freed automatically."
        self.hero_dot.set_tone(tone)
        self.set_text(self.hero_title, title)
        self.set_text(self.hero_sub, sub)

        up = st.get("update") or {}
        if up.get("version") and up.get("url", "").startswith("https://github.com/"):
            self.upd_url = up["url"]
            self.set_text(self.upd_text, f"Subtle {up['version']} is available. You have {st.get('version', '')}. "
                                         "Download it, then run Install Subtle.bat again (your models stay).")
            if self.upd.isHidden():
                self.upd.show()
        elif not self.upd.isHidden():
            self.upd.hide()

        d = st.get("delay") or {}
        n = d.get("count", 0)
        self.set_text(self.delay_count, f"{n} line{'s' if n != 1 else ''} measured" if n else "No lines yet")
        for key in ("last", "avg", "median"):
            v, unit = self.delay_vals[key]
            self.set_text(v, f"{d[key]}" if n else "–")
            self.set_text(unit, "ms" if n else "")
        v, unit = self.delay_vals["compute"]
        self.set_text(v, f"{d['compute_avg']}" if n else "–")
        self.set_text(unit, f"ms on average (last {d['compute_last']} ms)" if n else "")

        v, dd = self.tiles["engine"]
        self.set_text(v, {"ready": "Running", "starting": "Loading", "error": "Problem"}.get(es, "Idle"))
        self.restyle(v, tone={"ready": "ok", "starting": "busy", "error": "bad"}.get(es, ""))
        self.set_text(dd, ", ".join(loaded) if es == "ready" and loaded else "Loads when a tab starts")
        v, dd = self.tiles["llm"]
        self.set_text(v, "External" if ext else {"ready": "Running", "starting": "Loading", "error": "Problem"}.get(ts, "Off"))
        self.restyle(v, tone={"ready": "ok", "starting": "busy", "error": "bad"}.get(ts, ""))
        self.set_text(dd, s.get("external_url", "") if ext else t.get("model") if ts == "ready" else "Only for the Best engine")
        v, dd = self.tiles["vram"]
        self.set_text(v, mb(subtle_v) if subtle_v else "0 GB")
        g = st.get("gpu") or {}
        self.set_text(dd, f"of {gib(g['total_mb'] * 1048576, 0)} in total" if g.get("total_mb") else "No NVIDIA graphics card")

        if g.get("total_mb"):
            total, used = g["total_mb"] * 1048576, g["used_mb"] * 1048576
            self.set_text(self.gpu_line, f"{g['name']}  ·  driver {g['driver']}")
            self.vbar.set_parts([(subtle_v / total, c["accent"]), (max(0, used - subtle_v) / total, c["other"])])
            self.set_text(self.vtext, f"{gib(total - used)} free of {gib(total, 0)}")
        else:
            self.set_text(self.gpu_line, "No NVIDIA graphics card found.")
        for key, (vc, mc) in self.usage_cells.items():
            x = u.get(key) or {}
            running = bool(x.get("pid"))
            self.set_text(vc, mb(x.get("vram", 0)) if running else "not running")
            self.restyle(vc, kind="body" if running else "faint")
            self.set_text(mc, mb(x.get("ram", 0)) if running else "")
        idle = s.get("idle_unload_min", 5)
        self.set_text(self.idle_rule, "Models stay loaded (Settings → Keep models loaded)." if s.get("keep_loaded") or not idle
                      else f"Everything is unloaded {idle} minute{'s' if idle != 1 else ''} after the last subtitle, "
                           "so Subtle then uses no video memory.")
        models = st.get("models")
        if models is not None:
            missing = not models.get("whisper")
            self.set_text(self.notice_text, "No speech model is installed yet, so subtitles can't start.")
            self.notice.setVisible(missing)

    def render_side(self):
        st = self.st
        s, e, t = st.get("settings", {}), st.get("engine", {}), st.get("translator", {})
        ext = s.get("llm_backend") == "external"
        es, ts = e.get("state"), t.get("state")
        self.set_text(self.ver, "version " + st.get("version", "") + (" Lite" if st.get("lite") else ""))
        self._side("app", "ok", "running")
        self._side("engine", {"ready": "ok", "starting": "busy", "error": "bad"}.get(es, "off"),
                   {"ready": "running", "starting": "loading", "error": "problem"}.get(es, "idle"))
        self._side("llm", "ok" if ext else {"ready": "ok", "starting": "busy", "error": "bad"}.get(ts, "off"),
                   "external" if ext else {"ready": "running", "starting": "loading", "error": "problem"}.get(ts, "off"))
        d = st.get("delay") or {}
        self._side("delay", "ok" if d.get("count") else "off",
                   f"{d['last']} ms (median {d['median']})" if d.get("count") else "no lines yet")

    def _side(self, key, tone, text):
        d, t, title = self.side_status[key]
        d.set_tone(tone)
        self.set_text(t, f"{title}: {text}")

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
    def _models(self, lay):
        acts, al = self.page_actions["models"]
        al.addWidget(self.button("Rescan folders", lambda: self.cmd(
            "rescan", lambda r: (self.toast("Folders scanned"), self.refresh(True)))))
        al.addWidget(self.button("Open models folder", lambda: self.cmd("open", what="models")))

        self.rec = QFrame()
        self.rec.setObjectName("soft")
        rl = QHBoxLayout(self.rec)
        rl.setContentsMargins(20, 14, 20, 14)
        rl.setSpacing(16)
        rt = QVBoxLayout()
        rt.setSpacing(2)
        self.rec_title = self.label("", "bold", wrap=True)
        self.rec_text = self.label("", wrap=True)
        rt.addWidget(self.rec_title)
        rt.addWidget(self.rec_text)
        rl.addLayout(rt, 1)
        self.rec_btn = self.button("Download recommended", self.download_set, "primary")
        rl.addWidget(self.rec_btn, 0, Qt.AlignVCenter)
        lay.addWidget(self.rec)

        lay.addSpacing(10)
        tabs = QHBoxLayout()
        tabs.setSpacing(0)
        self.kind_tabs = {}
        for key, title in KIND_TABS:
            b = QPushButton(title)
            b.setObjectName("tab")
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, k=key: self.set_model_tab(k))
            tabs.addWidget(b)
            self.kind_tabs[key] = b
        tabs.addStretch(1)
        lay.addLayout(tabs)
        lay.addWidget(self.sep())
        self.kind_hint = self.label("", "small", wrap=True)
        lay.addWidget(self.kind_hint)
        self.model_box, self.model_list = self.card((0, 0, 0, 0))
        self.model_list.setSpacing(0)
        lay.addWidget(self.model_box)
        hc, hl = self.card()
        hl.addWidget(self.label("Add a model yourself", "bold"))
        self.help_t = self.label("", "small", wrap=True)
        hl.addWidget(self.help_t)
        lay.addSpacing(6)
        lay.addWidget(hc)

    def set_model_tab(self, key):
        self.model_tab = key
        self.models_sig = None
        self.render_models()

    def render_models(self):
        st = self.st
        name, warn, ids, text = self.profile()
        self.rec.setObjectName("softwarn" if warn else "soft")
        self.rec.style().unpolish(self.rec)
        self.rec.style().polish(self.rec)
        cat = {m["id"]: m for m in st.get("catalog", [])}
        missing = [i for i in ids if i in cat and not cat[i]["installed"]]
        self.set_text(self.rec_title, f"Recommended for your PC ({name})")
        self.set_text(self.rec_text, text + "\nWatching Japanese or Chinese? Also download Parakeet 0.6B (Japanese) or "
                      "SenseVoice Small (Chinese) below: Subtle then uses them by itself with the Best engine.")
        self.rec_btn.setVisible(bool(missing))
        if missing:
            self.rec_btn.setText(f"Download recommended ({gb(sum(cat[i]['size'] for i in missing))})")
        for key, b in self.kind_tabs.items():
            self.restyle(b, active="true" if key == self.model_tab else "false")
        self.set_text(self.kind_hint, {
            "whisper": "Speech models turn what people say into text. Fast needs one that can translate (large-v3); Best "
                       "works with any. Language-specific ones are often more accurate for that language: pick them in "
                       "the extension popup under Speech model, or set them per language in Settings.",
            "llm": "Translator models turn the text into natural subtitles in any language (Best engine). Bigger ones "
                   "translate better but need more video memory.",
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
        self.set_text(self.help_t,
                      f"Speech: put a model folder into {paths.get('models', '')}\\whisper.   Translator: put any "
                      f"instruct .gguf file into {paths.get('models', '')}\\llm (Q4_K_M is a good balance). "
                      + ("Models downloaded with LM Studio are used too, without copying. " if lms else "")
                      + "Then press Rescan folders.")
        self.render_downloads()

    def _build_model_rows(self, rec_ids):
        st = self.st
        self.park_focus()
        clear(self.model_list)
        self.dl_rows = {}
        kind = self.model_tab
        installed = st.get("models", {}).get(kind, [])
        default = st.get("settings", {}).get("llm_model")
        catalog = [m for m in st.get("catalog", []) if m["kind"] == kind]
        known = {m["id"].lower() for m in catalog}

        def inst_for(m):
            for it in installed:
                if it["name"].lower() == m["id"].lower() or it["id"].lower() == m["id"].lower() or kind == "speaker":
                    return it
            return None
        order = sorted(catalog, key=lambda m: (m["id"] not in rec_ids, m.get("experimental", False), m["size"]))
        rows = [(m, inst_for(m), m["id"] in rec_ids) for m in order]
        rows += [(None, it, False) for it in installed if it["name"].lower() not in known and it["id"].lower() not in known]
        if not rows:
            empty = self.label("Nothing here yet.", "muted")
            empty.setContentsMargins(20, 16, 20, 16)
            self.model_list.addWidget(empty)
        for i, (m, inst, rec) in enumerate(rows):
            if i:
                self.model_list.addWidget(self.sep())
            self.model_list.addWidget(self._model_row(m, inst, rec, default))

    def _model_row(self, m, inst, recommended, default):
        w = QWidget()
        row = QHBoxLayout(w)
        row.setContentsMargins(20, 14, 20, 14)
        row.setSpacing(16)
        left = QVBoxLayout()
        left.setSpacing(3)
        title = QHBoxLayout()
        title.setSpacing(6)
        title.addWidget(self.label(m["name"] if m else inst["name"], "bold"))
        if m and recommended:
            title.addWidget(self.badge("Recommended", "accent"))
        if m and m.get("language"):
            title.addWidget(self.badge(LANG_TAGS.get(m["language"], m["language"]) + " only"))
        if m and m.get("experimental"):
            title.addWidget(self.badge("Experimental", "warn"))
        if m and not inst and not self.fits(m):
            title.addWidget(self.badge("Too big for your graphics card", "bad"))
        if inst and inst.get("source") and inst["source"] != "folder":
            title.addWidget(self.badge("from " + SOURCES.get(inst["source"], inst["source"])))
        if self.model_tab == "llm" and inst and inst["id"] == default:
            title.addWidget(self.badge("Default", "good"))
        title.addStretch(1)
        left.addLayout(title)
        if m:
            left.addWidget(self.label(f"{gb(m['size'])} download  ·  about {m['vram_mb'] / 1024:.1f} GB video memory  ·  "
                                      f"{m['license']}", "small"))
            left.addWidget(self.label(m["note"], "muted", wrap=True))
        else:
            left.addWidget(self.label(f"{mb(inst.get('size', 0))}  ·  {inst['path']}", "small", wrap=True))
        prog = QProgressBar()
        prog.setRange(0, 1000)
        prog.setTextVisible(False)
        prog.hide()
        status = self.label("", "small", wrap=True)
        status.hide()
        left.addWidget(prog)
        left.addWidget(status)
        row.addLayout(left, 1)

        right = QHBoxLayout()
        right.setSpacing(8)
        r = {"prog": prog, "status": status}
        if inst:
            ok = self.label("✓ Installed", tone="ok")
            right.addWidget(ok)
            if self.model_tab == "llm" and inst["id"] != default:
                right.addWidget(self.button("Make default", lambda i=inst: self.save(llm_model=i["id"], llm_backend="builtin"),
                                            small=True))
            if inst.get("source") == "folder":
                right.addWidget(self.button("Delete", lambda i=inst, k=self.model_tab: self.delete(k, i), small=True))
        elif m:
            r["dl"] = self.button("Download", lambda mid=m["id"]: self.cmd("download", lambda x: self.refresh(), id=mid), "primary")
            r["cancel"] = self.button("Cancel", lambda mid=m["id"]: self.cmd("cancel_download", id=mid))
            r["cancel"].hide()
            right.addWidget(r["dl"])
            right.addWidget(r["cancel"])
        row.addLayout(right, 0)
        if m:
            self.dl_rows[m["id"]] = r
        return w

    def delete(self, kind, it):
        if QMessageBox.question(self.win, "Delete model", f"Delete {it['name']} ({mb(it.get('size', 0))})?\n\n"
                                "You can download it again later.") == QMessageBox.Yes:
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
            r["prog"].setVisible(active)
            if active:
                r["prog"].setValue(int(1000 * j["done"] / j["total"]) if j["total"] else 0)
            speed = f"  ·  {mb(j['speed'])}/s" if j.get("speed") else ""
            text = {"downloading": f"Downloading {gb(j['done'])} of {gb(j['total'])}{speed}",
                    "verifying": "Checking the download…", "queued": "Waiting for the other downloads…",
                    "error": "Download failed: " + j.get("error", ""),
                    "cancelled": "Paused - press Resume to continue where it stopped", "done": ""}.get(j["state"], "")
            self.set_text(r["status"], text)
            r["status"].setVisible(bool(text))
            self.restyle(r["status"], tone="bad" if j["state"] == "error" else "")
            if "dl" in r:
                r["cancel"].setVisible(active)
                r["dl"].setVisible(not active and j["state"] != "done")
                if j["state"] in ("cancelled", "error"):
                    r["dl"].setText("Resume" if j["state"] == "cancelled" else "Try again")
        active = [j for j in jobs if j["state"] in ("queued", "downloading", "verifying")]
        self.set_text(self.setup_dl, f"Downloading {len(active)} model{'s' if len(active) != 1 else ''}: "
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
    def _translator(self, lay):
        self.section(lay, "Where translations come from", top=4)
        src, sl = self.card((6, 6, 6, 6))
        sl.setSpacing(2)
        self.src_rows = {}
        for i, (key, title, desc) in enumerate((
                ("builtin", "Built-in (recommended)",
                 "Subtle runs the translator itself, only while it's needed, and stops it when you're done."),
                ("external", "Another program",
                 "LM Studio, Ollama or any OpenAI-compatible server that you start yourself."))):
            row = Clickable()
            row.setObjectName("choice")
            row.setCursor(Qt.PointingHandCursor)
            rl = QHBoxLayout(row)
            rl.setContentsMargins(14, 10, 14, 10)
            rl.setSpacing(14)
            mark = self.label("○", "h2")
            mark.setFixedWidth(22)
            rl.addWidget(mark)
            txt = QVBoxLayout()
            txt.setSpacing(1)
            txt.addWidget(self.label(title, "bold"))
            txt.addWidget(self.label(desc, "small", wrap=True))
            rl.addLayout(txt, 1)
            row.clicked.connect(lambda k=key: self.save(llm_backend=k))
            sl.addWidget(row)
            self.src_rows[key] = mark
        lay.addWidget(src)

        self.ext_card, el = self.card()
        self.ext_url, self.ext_model = QLineEdit(), QLineEdit()
        for edit, title, key in ((self.ext_url, "Server address", "external_url"),
                                 (self.ext_model, "Model name (as that server calls it; empty = choose in the popup)", "llm_model")):
            el.addWidget(self.label(title, "small"))
            el.addWidget(edit)
            edit.editingFinished.connect(lambda e=edit, k=key: self.save(**{k: e.text().strip()}))
        lay.addWidget(self.ext_card)

        self.builtin_wrap = QWidget()
        bw = QVBoxLayout(self.builtin_wrap)
        bw.setContentsMargins(0, 0, 0, 0)
        bw.setSpacing(12)
        self.section(bw, "Translator model", top=10)
        bc, bl = self.card()
        self.llm_pick = self.combo([], lambda v: self.save(llm_model=v, llm_backend="builtin"), 320)
        self.setting_row(bl, "Default model", "Used unless you pick another one in the extension popup.", self.llm_pick, first=True)
        status = QWidget()
        stl = QHBoxLayout(status)
        stl.setContentsMargins(0, 0, 0, 0)
        stl.setSpacing(8)
        self.llm_dot = Dot(self.c, 9)
        stl.addWidget(self.llm_dot)
        self.llm_status = self.label("")
        stl.addWidget(self.llm_status)
        stl.addSpacing(8)
        stl.addWidget(self.button("Start now", self.llm_start, "primary", small=True))
        stl.addWidget(self.button("Stop", lambda: self.cmd("llm_stop", lambda x: self.refresh()), small=True))
        self.setting_row(bl, "Status", "", status)
        bw.addWidget(bc)

        self.adv_btn = QPushButton("▸  Advanced")
        self.adv_btn.setObjectName("tab")
        self.adv_btn.setCursor(Qt.PointingHandCursor)
        self.adv_btn.clicked.connect(self.toggle_adv)
        bw.addWidget(self.adv_btn, 0, Qt.AlignLeft)
        self.adv, al = self.card()
        self.layers = self.combo(LAYER_CHOICES, lambda v: self.save(llm_gpu_layers=v), 260)
        self.setting_row(al, "On the graphics card", "Less = less video memory, but slower translations.", self.layers, first=True)
        self.ctx = self.combo(CTX_CHOICES, lambda v: self.save(llm_context=v), 260)
        self.setting_row(al, "Context size", "How much text the translator can look at. Subtitles need little.", self.ctx)
        self.kv = Toggle(self.c, lambda v: self.save(llm_kv_quant=v))
        self.setting_row(al, "Compact memory", "8-bit context memory: less video memory, same translations.", self.kv)
        self.adv.hide()
        bw.addWidget(self.adv)
        lay.addWidget(self.builtin_wrap)

        self.section(lay, "Try it")
        tc, tl = self.card()
        row = QHBoxLayout()
        row.setSpacing(10)
        self.test_text = QLineEdit("今日は本当にありがとうございました。")
        row.addWidget(self.test_text, 1)
        row.addWidget(self.label("into", "muted"))
        self.test_target = self.combo(LANGS, lambda v: None, 130)
        row.addWidget(self.test_target)
        self.test_btn = self.button("Translate", self.llm_test, "primary")
        row.addWidget(self.test_btn)
        tl.addLayout(row)
        self.test_out = self.label("Type something in any language and press Translate.", "muted", wrap=True)
        tl.addWidget(self.test_out)
        lay.addWidget(tc)

    def toggle_adv(self):
        show = not self.adv.isVisible()
        self.adv.setVisible(show)
        self.adv_btn.setText(("▾" if show else "▸") + "  Advanced")

    def llm_start(self):
        self.llm_status.setText("Starting…")
        self.cmd("llm_start", lambda r: self.refresh(), model=self.llm_pick.currentData() or "")

    def llm_test(self):
        s = self.st.get("settings", {})
        self.test_out.setText("Translating… (the first time also loads the model)")
        self.restyle(self.test_out, kind="muted", tone="")
        self.test_btn.setEnabled(False)

        def done(r):
            self.test_btn.setEnabled(True)
            if r.get("error"):
                self.test_out.setText(r["error"])
                self.restyle(self.test_out, kind="body", tone="bad")
            else:
                self.test_out.setText(f"{r['text']}      ({r['ms']} ms)")
                self.restyle(self.test_out, kind="h2", tone="")
            self.refresh()
        model = s.get("llm_model") if s.get("llm_backend") == "external" else (self.llm_pick.currentData() or "")
        self._run(self.sup.panel_command({"cmd": "llm_test", "text": self.test_text.text(),
                                          "target": self.test_target.currentData(), "model": model}), done)

    def render_translator(self):
        st, s = self.st, self.st.get("settings", {})
        ext = s.get("llm_backend") == "external"
        for key, mark in self.src_rows.items():
            on = (key == "external") == ext
            self.set_text(mark, "◉" if on else "○")
            self.restyle(mark, tone="ok" if on else "")
        self.ext_card.setVisible(ext)
        self.builtin_wrap.setVisible(not ext)
        if not self.ext_url.hasFocus():
            self.ext_url.setText(s.get("external_url", ""))
        if not self.ext_model.hasFocus():
            self.ext_model.setText(s.get("llm_model", "") if ext else "")
        llms = st.get("models", {}).get("llm", [])
        choices = [(m["id"], f"{m['name']}   ({mb(m.get('size', 0))})") for m in llms] or \
                  [("", "No translator installed - see Models")]
        ids = [m["id"] for m in llms]
        self.fill_combo(self.llm_pick, choices, s.get("llm_model") if s.get("llm_model") in ids else (ids[0] if ids else ""))
        self.set_combo(self.layers, s.get("llm_gpu_layers", 999) if s.get("llm_gpu_layers") in (999, 24, 12, 0) else 999)
        self.set_combo(self.ctx, s.get("llm_context", 2048))
        self.kv.set_value(s.get("llm_kv_quant", True))
        self.render_llm_status()

    def render_llm_status(self):
        t = self.st.get("translator", {})
        state = t.get("state")
        if not t.get("installed", True):
            text, tone = "The built-in translator is missing - run Install Subtle.bat again", "bad"
        else:
            text = {"ready": f"Running {t.get('model', '')}", "error": t.get("error", "Problem"),
                    "starting": "Starting…"}.get(state, "Not running (starts by itself when needed)")
            tone = {"ready": "ok", "error": "bad", "starting": "busy"}.get(state, "off")
        self.set_text(self.llm_status, text)
        self.restyle(self.llm_status, tone="bad" if tone == "bad" else "")
        self.llm_dot.set_tone(tone)

    # ================================================================ SETTINGS
    def _settings(self, lay):
        c = self.c
        self.section(lay, "Memory", top=4)
        mc, ml = self.card()
        self.idle = self.combo(IDLE_CHOICES, lambda v: self.save(idle_unload_min=v), 170)
        self.setting_row(ml, "Free memory after", "Unload everything when nothing was subtitled for this long.", self.idle, first=True)
        self.keep = Toggle(c, lambda v: self.save(keep_loaded=v))
        self.setting_row(ml, "Keep models loaded", "Subtitles start instantly, but the memory stays in use.", self.keep)
        self.prev = self.combo(PREVIEW_CHOICES, lambda v: self.save(previews=v), 260)
        self.setting_row(ml, "Live previews", "Show the line while someone is still speaking. Less often = less GPU work.", self.prev)
        lay.addWidget(mc)

        self.section(lay, "Speech model for each language")
        sc, sl = self.card()
        self.asr_defaults = {}
        for i, (key, title, desc) in enumerate(ASR_DEFAULT_ROWS):
            cb = self.combo([("auto", "Automatic (recommended)")], lambda v, k=key: self.save(**{f"asr_default_{k}": v}), 300)
            self.setting_row(sl, title, desc + " The popup's own choice still wins.", cb, first=i == 0)
            self.asr_defaults[key] = cb
        lay.addWidget(sc)

        self.section(lay, "Starting Subtle")
        ac, al = self.card()
        self.auto = Toggle(c, self.toggle_autostart)
        self.setting_row(al, "Start with Windows", "Waits in the tray and uses no video memory until you need it.", self.auto, first=True)
        self.open_start = Toggle(c, lambda v: self.save(open_panel_on_start=v))
        self.setting_row(al, "Show this window when Subtle starts", "Otherwise it starts quietly in the tray.", self.open_start)
        self.scan_lms = Toggle(c, lambda v: self.save(scan_lmstudio=v))
        self.setting_row(al, "Use LM Studio's models", "Translator models you downloaded with LM Studio are used without copying.", self.scan_lms)
        self.simple_t = Toggle(c, lambda v: self.save(simple_mode=v))
        self.simple_row = self.setting_row(al, "Simple mode", "Only what the browser extension needs: hides subtitles for "
                                           "any app, learning mode, Catch me up and Save subtitles.", self.simple_t)
        self.check_upd = Toggle(c, lambda v: self.save(check_updates=v))
        self.setting_row(al, "Tell me about new versions", "Once a day Subtle asks GitHub for the newest version number. Nothing else is sent.", self.check_upd)
        lay.addWidget(ac)

        self.section(lay, "Files and logs")
        fc, fl = self.card()
        self.setting_row(fl, "Saved transcripts", "Only when 'Save transcripts' is on in the extension popup.",
                         self.button("Open folder", lambda: self.cmd("open", what="transcripts"), small=True), first=True)
        self.setting_row(fl, "Error log", "Errors only - never what was said.",
                         self.button("Open folder", lambda: self.cmd("open", what="logs"), small=True))
        self.log_btn = self.button("Show", self.toggle_log, small=True)
        self.setting_row(fl, "Activity log", "What Subtle did recently (kept in memory only).", self.log_btn)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(260)
        self.log.hide()
        fl.addWidget(self.log)
        lay.addWidget(fc)

        self.section(lay, "Quit")
        qc, ql = self.card()
        self.setting_row(ql, "Quit Subtle", "Closing this window keeps Subtle in the tray. Quit stops it completely.",
                         self.button("Quit Subtle", self.ask_quit, "danger", small=True), first=True)
        lay.addWidget(qc)

    def render_settings(self):
        s = self.st.get("settings", {})
        self.set_combo(self.idle, s.get("idle_unload_min", 5))
        self.keep.set_value(s.get("keep_loaded"))
        self.set_combo(self.prev, s.get("previews", "normal"))
        installed = (self.st.get("models") or {}).get("whisper", [])
        for key, cb in self.asr_defaults.items():        # installed speech models that suit the language
            fits = []
            for m in installed:
                langs = modelstore.model_languages(m["id"]) if hasattr(modelstore, "model_languages") else None
                if langs is None or key in langs:
                    fits.append((m["id"], m["name"]))
            want = s.get(f"asr_default_{key}", "auto")
            choices = [("auto", "Automatic (recommended)")] + fits
            self.fill_combo(cb, choices, want if any(v == want for v, _ in choices) else "auto")
        self.open_start.set_value(s.get("open_panel_on_start", True))
        self.scan_lms.set_value(s.get("scan_lmstudio", True))
        self.check_upd.set_value(s.get("check_updates", True))
        self.simple_t.set_value(bool(self.st.get("simple")))
        self.simple_t.setEnabled(not self.st.get("lite"))
        self.auto.set_value(self.st.get("autostart"))

    def toggle_autostart(self, on):
        def done(r):
            if r.get("error"):
                self.auto.set_value(not on)
            else:
                self.toast("Subtle will start with Windows" if on else "Subtle won't start with Windows")
        self.cmd("autostart", done, on=on)

    def toggle_log(self):
        if self.log.isVisible():
            self.log.hide()
            self.log_btn.setText("Show")
            return
        self.log.show()
        self.log_btn.setText("Hide")

        def fill(r):
            text = "\n".join(r.get("lines", []))
            if r.get("llm"):
                text += "\n\n--- translator (llama-server) ---\n" + "\n".join(r["llm"])
            self.log.setPlainText(text)
            self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())
        self.cmd("logs", fill)

    def ask_quit(self):
        if QMessageBox.question(self.win, "Quit Subtle", "Quit Subtle? Subtitles stop until you start it again.") \
                == QMessageBox.Yes:
            self.sup.request_stop()

    # ================================================================ SETUP
    def _step(self, lay, n, title):
        cd, cl = self.card((22, 18, 22, 18))
        head = QHBoxLayout()
        head.setSpacing(12)
        num = QLabel(str(n))
        num.setAlignment(Qt.AlignCenter)
        num.setFixedSize(30, 30)
        num.setStyleSheet(f"background: {self.c['accent_soft']}; color: {self.c['accent']}; border-radius: 15px; font-weight: 600;")
        head.addWidget(num)
        head.addWidget(self.label(title, "h2"), 1)
        state = self.label("", tone="ok")
        head.addWidget(state)
        cl.addLayout(head)
        body = QVBoxLayout()
        body.setContentsMargins(42, 4, 0, 0)
        body.setSpacing(6)
        cl.addLayout(body)
        lay.addWidget(cd)
        return body, num, state

    def _numbered(self, body, i, text, extra=None):
        row = QHBoxLayout()
        row.setSpacing(8)
        n = self.label(f"{i}.", "muted")
        n.setFixedWidth(18)
        row.addWidget(n, 0, Qt.AlignTop)
        row.addWidget(self.label(text, wrap=True), 1)
        if extra is not None:
            row.addWidget(extra, 0, Qt.AlignTop)
        body.addLayout(row)

    def _setup(self, lay):
        b, self.step1_num, self.step1_state = self._step(lay, 1, "Get the models for your PC")
        self.setup_gpu = self.label("", "muted", wrap=True)
        b.addWidget(self.setup_gpu)
        self.setup_list = QVBoxLayout()
        self.setup_list.setSpacing(4)
        b.addLayout(self.setup_list)
        r = QHBoxLayout()
        self.get_set = self.button("Download the recommended models", self.download_set, "primary")
        r.addWidget(self.get_set)
        self.setup_dl = self.label("", "small")
        r.addWidget(self.setup_dl, 1)
        b.addSpacing(4)
        b.addLayout(r)

        b, _, _ = self._step(lay, 2, "Add Subtle to your browser (Chrome, Edge, Brave, Opera, Vivaldi or another Chromium browser)")
        self._numbered(b, 1, "Open a new browser tab and go to  chrome://extensions   (works in every Chromium browser)",
                       self.button("Copy address", lambda: self.copy("chrome://extensions"), small=True))
        self._numbered(b, 2, "Turn on Developer mode (a switch at the top right; in Edge it is on the left).")
        self._numbered(b, 3, "Click Load unpacked and choose Subtle's extension folder:")
        pr = QHBoxLayout()
        pr.setContentsMargins(26, 0, 0, 0)
        self.ext_path = QLabel("")
        self.ext_path.setStyleSheet(f"background: {self.c['field']}; border-radius: 6px; padding: 4px 8px; font-family: Consolas;")
        self.ext_path.setTextInteractionFlags(Qt.TextSelectableByMouse)
        pr.addWidget(self.ext_path)
        pr.addWidget(self.button("Copy path", lambda: self.copy(self.st.get("paths", {}).get("extension", "")), small=True))
        pr.addWidget(self.button("Open folder", lambda: self.cmd("open", what="extension"), small=True))
        pr.addStretch(1)
        b.addLayout(pr)
        self._numbered(b, 4, "Optional: on the extension's Details page, turn on Allow in Incognito (Private / InPrivate).")

        b, _, _ = self._step(lay, 3, "Watch something")
        for i, text in enumerate((
                "Open a video, click the Subtle icon in the browser toolbar, then Start subtitles on this tab "
                "(or press Alt+Shift+S).",
                "The first line takes a few seconds while the models load. After that, lines take about 0.1 s.",
                "Fast: English subtitles with the least memory.   Best: natural translations into any language."), start=1):
            self._numbered(b, i, text)
        b.addSpacing(6)
        b.addWidget(self.button("Done - go to Home", self.finish_setup, "primary"), 0, Qt.AlignLeft)

    def render_setup(self):
        st = self.st
        name, warn, ids, text = self.profile()
        g = st.get("gpu") or {}
        self.set_text(self.setup_gpu, (f"Your graphics card: {g['name']} ({self.gpu_gb():.0f} GB). " if g.get("name") else "") + text)
        clear(self.setup_list)
        cat = {m["id"]: m for m in st.get("catalog", [])}
        missing, size = 0, 0
        for i in ids:
            m = cat.get(i)
            if not m:
                continue
            row = QHBoxLayout()
            mark = self.label("✓" if m["installed"] else "•", "bold", tone="ok" if m["installed"] else None)
            mark.setFixedWidth(16)
            row.addWidget(mark)
            row.addWidget(self.label(m["name"]))
            row.addWidget(self.label(gb(m["size"]) + ("  ·  installed" if m["installed"] else ""), "small"))
            row.addStretch(1)
            self.setup_list.addLayout(row)
            if not m["installed"]:
                missing += 1
                size += m["size"]
        done = missing == 0
        self.get_set.setEnabled(not done)
        self.get_set.setText("All recommended models are installed" if done else f"Download the recommended models ({gb(size)})")
        self.set_text(self.step1_state, "✓ Done" if done else "")
        self.set_text(self.step1_num, "✓" if done else "1")
        self.set_text(self.ext_path, st.get("paths", {}).get("extension", ""))

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
        if full or self.page == "home":
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


__all__ = ["ControlPanel"]

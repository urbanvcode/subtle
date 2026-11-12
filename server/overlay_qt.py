"""
Subtitles on top of the desktop, for "PC sound" (Discord, games, VLC...): a frameless, see-through
window that stays above other windows and lets every click through to what is underneath.
It shows the current line (and the previous one, dimmed) like the browser overlay does.

The look has the same options as the browser extension (extension/substyle.js): colours, box, outline
or shadow, font, weight, line width, how long lines stay, the previous line and the original line.
Text is drawn with QPainter paths, so outlines and shadows work on any font; the Subtle window's
preview (SubtitlePreview) uses the same drawing, so it shows exactly what appears on screen.
"""

from PySide6.QtCore import QPointF, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import (QColor, QFont, QFontDatabase, QFontMetricsF, QGuiApplication, QLinearGradient, QPainter,
                           QPainterPath, QPen, QTextLayout, QTextOption)
from PySide6.QtWidgets import QWidget

SPEAKER_COLOURS = ["#ffe873", "#8fd8ff", "#a8f0a0", "#ffb3de", "#ffc285", "#cdb8ff", "#9ff0e6", "#f5f5f5"]

# settings keys (settings.py) and their defaults
DEFAULTS = {
    "pc_size": 100, "pc_height": 8, "pc_screen": 0, "pc_width": 80,
    "pc_x": 500, "pc_y": -1,          # moved by hand: centre (‰ of the screen width) and bottom (‰ up); -1 = pc_height
    "pc_text_color": "#ffffff", "pc_bg_color": "#08080c", "pc_opacity": 62, "pc_edge": "soft",
    "pc_font": "default", "pc_weight": "semibold", "pc_line_time": "normal",
    "pc_keep_prev": True, "pc_show_src": False, "pc_src_size": 70,
}

# what "Reset position" and "Reset style" put back ("Reset everything": all of DEFAULTS, the main screen too)
POSITION_KEYS = ("pc_x", "pc_y", "pc_height")
STYLE_KEYS = tuple(k for k in DEFAULTS if k not in POSITION_KEYS and k != "pc_screen")

PRESETS = {
    "classic": ("Classic box", {"pc_text_color": "#ffffff", "pc_bg_color": "#08080c", "pc_opacity": 62,
                                "pc_edge": "soft", "pc_weight": "semibold"}),
    "outline": ("Outline", {"pc_text_color": "#ffffff", "pc_opacity": 0, "pc_edge": "outline", "pc_weight": "bold"}),
    "shadow": ("Soft shadow", {"pc_text_color": "#ffffff", "pc_opacity": 0, "pc_edge": "drop", "pc_weight": "semibold"}),
    "contrast": ("High contrast", {"pc_text_color": "#ffe873", "pc_bg_color": "#000000", "pc_opacity": 92,
                                   "pc_edge": "none", "pc_weight": "bold"}),
}
TEXT_COLORS = [("#ffffff", "White"), ("#ffe873", "Yellow"), ("#8fd8ff", "Light blue"), ("#a8f0a0", "Light green"),
               ("#ffb3de", "Pink")]
BG_COLORS = [("#08080c", "Black"), ("#2a2d34", "Dark grey"), ("#0b1a33", "Navy"), ("#ffffff", "White")]
FONTS = {
    "default": ("Default", ["Segoe UI"]),
    "arial": ("Arial", ["Arial", "Helvetica", "Liberation Sans"]),
    "verdana": ("Verdana (wide, very readable)", ["Verdana", "DejaVu Sans"]),
    "trebuchet": ("Trebuchet", ["Trebuchet MS", "Segoe UI"]),
    "georgia": ("Georgia (serif)", ["Georgia", "Times New Roman", "DejaVu Serif"]),
    "mono": ("Monospace", ["Consolas", "Cascadia Mono", "Menlo", "DejaVu Sans Mono"]),
}
WEIGHTS = {"regular": ("Regular", QFont.Medium), "semibold": ("Semi-bold", QFont.DemiBold), "bold": ("Bold", QFont.Bold)}
EDGES = {"none": "None", "soft": "Soft shadow", "outline": "Outline", "drop": "Drop shadow"}
TIMES = {"short": ("Shorter", 0.7), "normal": ("Normal", 1.0), "long": ("Longer", 1.5)}


SIZES = (70, 85, 100, 125, 150, 200)


def next_size(cur: int, up: bool) -> int | None:
    """The next text size up or down (mouse wheel), None at the end."""
    nxt = [v for v in SIZES if v > cur] if up else [v for v in SIZES if v < cur][::-1]
    return nxt[0] if nxt else None


def style_from(settings: dict) -> dict:
    s = dict(DEFAULTS)
    s.update({k: settings[k] for k in DEFAULTS if k in settings and settings[k] is not None})
    return s


def placement(s: dict, width: float, height: float) -> tuple[float, float, float]:
    """(centre x, bottom y, line width) of the subtitles in a width x height area (a screen or the preview)."""
    line_w = width * max(30, min(100, int(s["pc_width"] or 80))) / 100
    cx = width * max(0, min(1000, int(s["pc_x"]))) / 1000
    cx = max(line_w / 2, min(width - line_w / 2, cx))           # the lines stay on the screen
    up = int(s["pc_y"]) if int(s["pc_y"]) >= 0 else int(s["pc_height"] or 0) * 10
    bottom = height - height * max(0, min(950, up)) / 1000
    return cx, bottom, line_w


class Painter:
    """Lays out and draws subtitle lines in one style (shared by the overlay and the preview)."""

    def __init__(self, style: dict):
        self.s = style_from(style)
        self.fg = QColor(self.s["pc_text_color"]) if QColor.isValidColorName(str(self.s["pc_text_color"])) \
            else QColor("#ffffff")
        bg = QColor(self.s["pc_bg_color"]) if QColor.isValidColorName(str(self.s["pc_bg_color"])) else QColor("#08080c")
        bg.setAlphaF(max(0, min(100, int(self.s["pc_opacity"]))) / 100)
        self.bg = bg
        self.boxed = bg.alpha() > 0
        fams = list(FONTS.get(self.s["pc_font"], FONTS["default"])[1])
        if self.s["pc_font"] == "default":
            fams.append(QFontDatabase.systemFont(QFontDatabase.GeneralFont).family())
        self.families = fams
        self.weight = WEIGHTS.get(self.s["pc_weight"], WEIGHTS["semibold"])[1]
        self.edge = self.s["pc_edge"] if self.s["pc_edge"] in EDGES else "soft"
        self.time = TIMES.get(self.s["pc_line_time"], TIMES["normal"])[1]

    def font(self, px: float, weight=None, italic=False) -> QFont:
        f = QFont()
        f.setFamilies(self.families)
        f.setPixelSize(max(6, int(round(px))))
        f.setWeight(weight or self.weight)
        f.setItalic(italic)
        f.setHintingPreference(QFont.PreferNoHinting)
        return f

    def pads(self, px):
        return (px * .55, px * .14, px * .2) if self.boxed else (px * .25, px * .04, px * .04)

    @staticmethod
    def _wrap(text: str, font: QFont, width: float) -> list[str]:
        tl = QTextLayout(text, font)
        opt = QTextOption()
        opt.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        tl.setTextOption(opt)
        tl.beginLayout()
        lines = []
        while True:
            ln = tl.createLine()
            if not ln.isValid():
                break
            ln.setLineWidth(max(1.0, width))
            lines.append(text[ln.textStart():ln.textStart() + ln.textLength()].strip())
        tl.endLayout()
        return [l for l in lines if l] or [text]

    def block(self, text: str, px: float, max_w: float, colour: str = "", opacity=1.0, weight=None,
              italic=False) -> dict:
        """One subtitle line, wrapped into max_w (balanced, as the browser's text-wrap: balance)."""
        font = self.font(px, weight, italic)
        fm = QFontMetricsF(font)
        padx, pt, pb = self.pads(px)
        inner = max(px * 2, max_w - 2 * padx)
        lines = self._wrap(text, font, inner)
        if len(lines) > 1:                          # narrowest width that keeps the same number of lines
            lo, hi = inner / 2, inner
            for _ in range(8):
                mid = (lo + hi) / 2
                if len(self._wrap(text, font, mid)) > len(lines):
                    lo = mid
                else:
                    hi = mid
            lines = self._wrap(text, font, hi)
        lh = px * 1.28
        w = max(fm.horizontalAdvance(l) for l in lines)
        return {"lines": lines, "font": font, "fm": fm, "px": px, "lh": lh, "w": w + 2 * padx,
                "h": lh * len(lines) + pt + pb, "colour": colour, "opacity": opacity}

    def draw(self, p: QPainter, b: dict, cx: float, top: float):
        px, lh, fm = b["px"], b["lh"], b["fm"]
        padx, pt, _ = self.pads(px)
        p.save()
        p.setOpacity(b["opacity"])
        if self.boxed:
            p.setPen(Qt.NoPen)
            p.setBrush(self.bg)
            p.drawRoundedRect(QRectF(cx - b["w"] / 2, top, b["w"], b["h"]), px * .35, px * .35)
        path = QPainterPath()
        # centre of each line box, then the font's own ascent/descent around it (as CSS line-height does)
        for i, line in enumerate(b["lines"]):
            base = top + pt + i * lh + (lh - (fm.ascent() + fm.descent())) / 2 + fm.ascent()
            path.addText(QPointF(cx - fm.horizontalAdvance(line) / 2, base), b["font"], line)
        black = QColor(0, 0, 0)
        if self.edge == "outline":
            black.setAlpha(255)
            p.strokePath(path, QPen(black, px * .11, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        elif self.edge == "soft":
            black.setAlpha(150)
            p.strokePath(path, QPen(black, px * .14, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            black.setAlpha(110)
            p.strokePath(path.translated(0, px * .05), QPen(black, px * .12, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        elif self.edge == "drop":
            black.setAlpha(70)
            p.strokePath(path.translated(0, px * .06), QPen(black, px * .32, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            black.setAlpha(240)
            p.fillPath(path.translated(0, px * .08), black)
        p.fillPath(path, QColor(b["colour"]) if b["colour"] else self.fg)
        p.restore()

    def stack(self, p: QPainter, blocks: list[dict], cx: float, bottom: float):
        """Draw blocks top to bottom, the last one ending at `bottom`."""
        if not blocks:
            return
        gap = blocks[-1]["px"] * .18
        y = bottom - sum(b["h"] for b in blocks) - gap * (len(blocks) - 1)
        for b in blocks:
            self.draw(p, b, cx, y)
            y += b["h"] + gap


class SubtitleOverlay(QWidget):
    ACCENT = QColor("#2fbf8f")

    def __init__(self):
        # always click-through: "Move on screen" uses its own window (MoveBox), as Windows doesn't let a
        # window stop being click-through once it is shown
        flags = (Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.WindowTransparentForInput
                 | Qt.WindowDoesNotAcceptFocus | Qt.NoDropShadowWindowHint)
        super().__init__(None, flags)
        self.moving = False
        self.mover: MoveBox | None = None
        self.on_moved = self.on_done = None
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setWindowTitle("Subtle subtitles")
        self.painter = Painter({})
        self.font_px = 32
        self.lines = {"prev": None, "cur": None, "src": None}     # (text, colour) or None
        self.partial = False
        self.blocks: list[dict] = []
        self.last_final_id = -1
        self.hide_timer = QTimer(self, singleShot=True, timeout=self._hide_all)
        self.prev_timer = QTimer(self, singleShot=True, timeout=lambda: self._set("prev", None))
        self.apply_style({})

    # -- look and place ------------------------------------------------------------------------
    def _screen(self, no):
        screens = QGuiApplication.screens()
        if not screens:
            return None
        return screens[no] if 0 <= no < len(screens) else QGuiApplication.primaryScreen()

    def apply_style(self, settings: dict):
        self.painter = Painter(settings)
        s = self.painter.s
        scr = self._screen(int(s["pc_screen"] or 0))
        if scr is None:
            return
        g: QRect = scr.geometry()
        self.font_px = max(16, int(g.height() * 0.034 * int(s["pc_size"] or 100) / 100))
        cx, bottom, w = placement(s, g.width(), g.height())
        # tall enough for a few wrapped lines above the bottom, never above the top of the screen
        h = int(max(self.font_px * 4, min(g.height() * 0.45, bottom)))
        if not (self.mover and self.mover.drag):
            self.setGeometry(int(g.left() + cx - w / 2), int(g.top() + bottom - h), int(w), h)
            if self.mover:
                self.mover.setGeometry(self.geometry())
        if not s["pc_keep_prev"]:
            self.lines["prev"] = None
        if not s["pc_show_src"]:
            self.lines["src"] = None
        self._relayout()

    def _relayout(self):
        """Wrap the visible lines (only when text or look changes: painting then just draws them)."""
        px, w, pt = self.font_px, self.width(), self.painter
        self.blocks = []
        sample = self.moving and not self.lines["cur"]     # something to see while placing the box
        if sample:
            text = SubtitlePreview.SAMPLE
            if pt.s["pc_keep_prev"]:
                self.blocks.append(pt.block(text[0], px * .82, w, "", .7))
            self.blocks.append(pt.block(text[1], px, w))
            if pt.s["pc_show_src"]:
                self.blocks.append(pt.block(text[2], px * max(40, min(100, int(pt.s["pc_src_size"]))) / 100, w,
                                            "", .9, weight=QFont.Medium))
        if self.lines["prev"] and not sample:
            text, colour = self.lines["prev"]
            self.blocks.append(pt.block(text, px * .82, w, colour, .7))
        if self.lines["cur"]:
            text, colour = self.lines["cur"]
            self.blocks.append(pt.block(text, px, w, colour, .85 if self.partial else 1.0, italic=self.partial))
        if self.lines["src"]:
            text, _ = self.lines["src"]
            self.blocks.append(pt.block(text, px * max(40, min(100, int(pt.s["pc_src_size"]))) / 100, w, "", .9,
                                        weight=QFont.Medium))
        if self.moving:                    # the drag box shows the lines (live ones too) meanwhile
            if self.isVisible():
                self.hide()
            self.mover.update()
        elif self.blocks:
            if not self.isVisible():
                self.show()
            self.raise_()
            self.update()
        elif self.isVisible():
            self.hide()

    def paintEvent(self, e):
        if not self.blocks:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        self.painter.stack(p, self.blocks, self.width() / 2, self.height() - 2)
        p.end()

    # -- "Move on screen" ---------------------------------------------------------------------------
    def set_move_mode(self, on: bool, on_moved=None, on_done=None):
        """on: a box to drag (and resize) appears where the subtitles are; on_moved gets the new place as
        settings. off: back to click-through subtitles."""
        if on == self.moving:
            return
        self.moving = on
        if on:
            self.on_moved, self.on_done = on_moved, on_done
            self.mover = MoveBox(self)
            self.mover.setGeometry(self.geometry())
            self._relayout()
            self.mover.show()
            self.mover.raise_()
            self.mover.activateWindow()
            self.mover.setFocus()
        else:
            mover, self.mover = self.mover, None
            if mover is not None:
                mover.close()
                mover.deleteLater()
            self._relayout()
            done, self.on_done = self.on_done, None
            if done:
                done()

    def commit_place(self, geo: QRect, width_changed=False):
        """Where the box was dropped -> pc_screen, pc_x (centre, ‰ of the width), pc_y (bottom, ‰ up), pc_width."""
        centre = geo.center()
        screens = QGuiApplication.screens()
        scr = QGuiApplication.screenAt(centre) or self._screen(int(self.painter.s["pc_screen"] or 0))
        if scr is None:
            return
        g = scr.geometry()
        x = round((centre.x() - g.left()) / g.width() * 1000)
        y = round((g.top() + g.height() - (geo.top() + geo.height())) / g.height() * 1000)
        changes = {"pc_screen": screens.index(scr) if scr in screens else 0,
                   "pc_x": max(0, min(1000, x)), "pc_y": max(0, min(950, y))}
        if width_changed:
            changes["pc_width"] = max(30, min(100, round(geo.width() / g.width() * 100)))
        if self.on_moved:
            self.on_moved(changes)

    def step_size(self, up: bool):
        size = next_size(int(self.painter.s["pc_size"] or 100), up)
        if size and self.on_moved:
            self.on_moved({"pc_size": size})

    def _set(self, key, value):
        self.lines[key] = value
        self._relayout()

    def _hide_all(self):
        self.prev_timer.stop()
        self.lines = {"prev": None, "cur": None, "src": None}
        self.partial = False
        self._relayout()

    # -- messages from the speech engine -------------------------------------------------------
    def on_message(self, msg: dict):
        kind = msg.get("type")
        if kind == "clear":
            self.last_final_id = -1
            self._hide_all()
            return
        if kind != "sub":
            return
        s, t = self.painter.s, self.painter.time
        text, src = msg.get("text") or "", msg.get("src") or ""
        show_src = s["pc_show_src"] and src and src != text
        if msg.get("final"):
            self.last_final_id = msg.get("id", -1)
            if not text:
                if self.partial:
                    self.partial = False
                    self.lines["cur"] = self.lines["src"] = None
                    self._relayout()
                return
            self._move_up()
            colour = SPEAKER_COLOURS[(msg["speaker"] - 1) % len(SPEAKER_COLOURS)] if msg.get("speaker") else ""
            self.partial = False
            self.lines["cur"] = (text, colour)
            self.lines["src"] = (src, "") if show_src else None
            self._relayout()
            self.hide_timer.start(int(min(7000, max(2200, len(text) * 70)) * t))
        elif text and msg.get("id", 0) > self.last_final_id:
            self._move_up()
            self.partial = True
            self.lines["cur"] = (text, "")
            self.lines["src"] = (src, "") if show_src else None
            self._relayout()
            self.hide_timer.start(int(8000 * t))

    def _move_up(self):
        if self.lines["cur"] and not self.partial and self.painter.s["pc_keep_prev"]:
            self.lines["prev"] = self.lines["cur"]
            self.prev_timer.start(int(4000 * self.painter.time))


class MoveBox(QWidget):
    """'Move on screen': a normal (clickable) see-through window over the subtitles. Drag the box to move
    them, its left or right edge to change the line width, the mouse wheel for the text size; double-click,
    Enter or Esc when done. It shows the overlay's lines (live ones while subtitles run)."""

    EDGE = 14

    def __init__(self, overlay: "SubtitleOverlay"):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowTitle("Move Subtle subtitles")
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.ov = overlay
        self.drag = None                   # (kind, start in screen coordinates, geometry at the start)
        self.box = QRectF()

    def paintEvent(self, e):
        ov = self.ov
        px = ov.font_px
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        gap = px * .18
        h = sum(b["h"] for b in ov.blocks) + gap * max(0, len(ov.blocks) - 1) + px * .5 if ov.blocks else px * 1.8
        # tinted, not see-through: a fully see-through pixel would let the click go to the window underneath
        self.box = QRectF(2, self.height() - 2 - h, self.width() - 4, h)
        fill = QColor(ov.ACCENT)
        fill.setAlpha(56)
        p.setBrush(fill)
        p.setPen(QPen(ov.ACCENT, 2, Qt.DashLine))
        p.drawRoundedRect(self.box, 10, 10)
        p.setPen(Qt.NoPen)
        p.setBrush(ov.ACCENT)
        for x in (self.box.left() + 3, self.box.right() - 9):          # width handles
            p.drawRoundedRect(QRectF(x, self.box.center().y() - px * .5, 6, px), 3, 3)
        hint = "Drag to move (Shift: centred)  ·  edges: width  ·  wheel: size  ·  double-click or Enter when done"
        f = QFont()
        f.setPixelSize(max(13, int(px * .42)))
        f.setWeight(QFont.DemiBold)
        fm = QFontMetricsF(f)
        hw, hh = min(self.width() - 4, fm.horizontalAdvance(hint) + 28), fm.height() + 10
        pill = QRectF((self.width() - hw) / 2, max(0, self.box.top() - hh - 8), hw, hh)
        p.drawRoundedRect(pill, hh / 2, hh / 2)
        p.setPen(QColor("#06261b"))
        p.setFont(f)
        p.drawText(pill, Qt.AlignCenter, fm.elidedText(hint, Qt.ElideRight, hw - 20))
        ov.painter.stack(p, ov.blocks, self.width() / 2, self.height() - 2 - px * .25)
        p.end()

    def _kind(self, pos) -> str | None:
        if not self.box.adjusted(-4, 0, 4, 0).contains(pos):
            return None
        if pos.x() < self.box.left() + self.EDGE:
            return "left"
        if pos.x() > self.box.right() - self.EDGE:
            return "right"
        return "move"

    def mousePressEvent(self, e):
        kind = self._kind(e.position()) if e.button() == Qt.LeftButton else None
        if kind:
            self.drag = (kind, e.globalPosition().toPoint(), QRect(self.geometry()))

    def mouseMoveEvent(self, e):
        if not self.drag:
            kind = self._kind(e.position())
            self.setCursor(Qt.SizeHorCursor if kind in ("left", "right") else Qt.SizeAllCursor if kind else Qt.ArrowCursor)
            return
        kind, start, geo = self.drag
        d = e.globalPosition().toPoint() - start
        if kind == "move":
            top_left = geo.topLeft() + d
            if e.modifiers() & Qt.ShiftModifier:                       # Shift: keep them centred
                scr = QGuiApplication.screenAt(geo.center()) or QGuiApplication.primaryScreen()
                top_left.setX(scr.geometry().center().x() - self.width() // 2)
            self.move(top_left)
            return
        scr = QGuiApplication.screenAt(geo.center()) or QGuiApplication.primaryScreen()
        grow = d.x() if kind == "right" else -d.x()          # both edges move: the lines stay centred
        w = int(max(scr.geometry().width() * .3, min(scr.geometry().width(), geo.width() + 2 * grow)))
        g = QRect(geo.center().x() - w // 2, geo.top(), w, geo.height())
        self.setGeometry(g)
        self.ov.setGeometry(g)             # the lines re-wrap to the new width as you drag
        self.ov._relayout()

    def mouseReleaseEvent(self, e):
        if self.drag:
            kind = self.drag[0]
            self.ov.commit_place(self.frameGeometry(), width_changed=kind != "move")
            self.drag = None

    def wheelEvent(self, e):
        self.ov.step_size(e.angleDelta().y() > 0)

    def mouseDoubleClickEvent(self, e):
        self.ov.set_move_mode(False)

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Escape):
            self.ov.set_move_mode(False)


class SubtitlePreview(QWidget):
    """The Subtle window's preview: the chosen screen in miniature (its own shape, the lines at true scale),
    drawn exactly as the overlay draws them. Drag the lines to place them (Shift keeps them centred), drag the
    box's edges for the line width, use the mouse wheel for the text size."""

    SAMPLE = ("I thought you'd left already.", "Where were you last night?", "昨日の夜はどこにいたの？")
    HEIGHT = 330
    EDGE = 10

    def __init__(self, parent=None, on_moved=None):
        super().__init__(parent)
        self.setFixedHeight(self.HEIGHT)
        self.painter = Painter({})
        self.on_moved = on_moved          # changes made here go to the real screen
        self.area = QRectF()              # the lines' box: the line width wide, as tall as the lines
        self.bottom = 0.0                 # where the lines end (widget coordinates)
        self.drag = None
        self.setMouseTracking(True)

    def set_style(self, settings: dict):
        if self.drag:                      # mid-drag: the page's refresh would put the old place back
            return
        if style_from(settings) != self.painter.s:        # the page redraws every 2 s: repaint only on a change
            self.painter = Painter(settings)
            self.update()

    def _screen_geo(self) -> QRect:
        screens = QGuiApplication.screens()
        no = int(self.painter.s["pc_screen"] or 0)
        scr = screens[no] if 0 <= no < len(screens) else QGuiApplication.primaryScreen()
        return scr.geometry() if scr else QRect(0, 0, 1920, 1080)

    def _screen_rect(self) -> QRectF:
        """The screen's own shape, as big as fits."""
        g = self._screen_geo()
        ratio = g.width() / max(1, g.height())
        h = self.height() - 2.0
        w = h * ratio
        if w > self.width() - 2:
            w = self.width() - 2.0
            h = w / ratio
        return QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self._screen_rect()
        g = self._screen_geo()
        clip = QPainterPath()
        clip.addRoundedRect(r, 8, 8)
        p.setClipPath(clip)
        grad = QLinearGradient(r.topLeft(), r.bottomRight())
        grad.setColorAt(0, QColor("#3b4a5c"))
        grad.setColorAt(.6, QColor("#1b2129"))
        grad.setColorAt(1, QColor("#101419"))
        p.fillRect(r, grad)
        pt, s = self.painter, self.painter.s
        scale = r.height() / g.height()
        px = max(16, g.height() * 0.034 * int(s["pc_size"] or 100) / 100) * scale      # as on the screen
        cx, bottom, w = placement(s, r.width(), r.height())
        blocks = []
        if s["pc_keep_prev"]:
            blocks.append(pt.block(self.SAMPLE[0], px * .82, w, "", .7))
        blocks.append(pt.block(self.SAMPLE[1], px, w))
        if s["pc_show_src"]:
            blocks.append(pt.block(self.SAMPLE[2], px * max(40, min(100, int(s["pc_src_size"]))) / 100, w, "", .9,
                                   weight=QFont.Medium))
        total = sum(b["h"] for b in blocks) + px * .18 * (len(blocks) - 1)
        bottom = max(bottom, total + 4)                    # moved high up: still fully in the picture
        pt.stack(p, blocks, r.left() + cx, r.top() + bottom)
        self.bottom = r.top() + bottom
        pad = max(3.0, px * .25)
        self.area = QRectF(r.left() + cx - w / 2, self.bottom - total - pad, w, total + 2 * pad)
        small = QFont()
        small.setPixelSize(11)
        p.setFont(small)
        p.setPen(QColor(255, 255, 255, 120))
        p.drawText(r.adjusted(10, 6, -10, -6), Qt.AlignTop | Qt.AlignLeft, f"Your screen  ·  {g.width()} × {g.height()}")
        hover = self.underMouse() and self.area.adjusted(-4, 0, 4, 0).contains(
            self.mapFromGlobal(self.cursor().pos()).toPointF())
        if self.drag or hover:
            acc = QColor(SubtitleOverlay.ACCENT)
            p.setPen(QPen(acc, 1.5, Qt.DashLine))
            fill = QColor(acc)
            fill.setAlpha(28)
            p.setBrush(fill)
            p.drawRoundedRect(self.area, 6, 6)
            p.setPen(Qt.NoPen)
            p.setBrush(acc)
            hh = min(self.area.height() - 4, 18)
            for x in (self.area.left() + 2, self.area.right() - 6):    # width handles
                p.drawRoundedRect(QRectF(x, self.area.center().y() - hh / 2, 4, hh), 2, 2)
            p.setPen(QColor(255, 255, 255, 170))
            p.drawText(r.adjusted(10, 6, -10, -6), Qt.AlignTop | Qt.AlignRight,
                       "Drag: move  ·  Shift: keep centred  ·  edges: width  ·  wheel: size")
        p.setClipping(False)
        p.setPen(QPen(QColor(255, 255, 255, 40), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(r.adjusted(.5, .5, -.5, -.5), 8, 8)
        p.end()

    def _kind(self, pos) -> str | None:
        if not self.area.adjusted(-4, 0, 4, 0).contains(pos):
            return None
        if pos.x() < self.area.left() + self.EDGE:
            return "left"
        if pos.x() > self.area.right() - self.EDGE:
            return "right"
        return "move"

    def mousePressEvent(self, e):
        kind = self._kind(e.position()) if e.button() == Qt.LeftButton else None
        if kind:
            self.drag = (kind, e.position(), QRectF(self.area), self.bottom)

    def mouseMoveEvent(self, e):
        if not self.drag:
            kind = self._kind(e.position())
            self.setCursor(Qt.SizeHorCursor if kind in ("left", "right") else Qt.SizeAllCursor if kind else Qt.ArrowCursor)
            self.update()                  # the dashed box follows the mouse
            return
        kind, start, area, bottom = self.drag
        r = self._screen_rect()
        d = e.position() - start
        s = dict(self.painter.s)
        if kind == "move":
            cx = r.center().x() if e.modifiers() & Qt.ShiftModifier else area.center().x() + d.x()
            s["pc_x"] = round(max(0, min(1000, (cx - r.left()) / r.width() * 1000)))
            s["pc_y"] = round(max(0, min(950, (r.bottom() - (bottom + d.y())) / r.height() * 1000)))
        else:
            grow = d.x() if kind == "right" else -d.x()          # both edges move: the lines stay centred
            s["pc_width"] = round(max(30, min(100, (area.width() + 2 * grow) / r.width() * 100)))
        self.painter = Painter(s)
        self.update()

    def mouseReleaseEvent(self, e):
        if self.drag:
            kind = self.drag[0]
            self.drag = None
            s = self.painter.s
            if self.on_moved:
                self.on_moved({"pc_x": s["pc_x"], "pc_y": s["pc_y"]} if kind == "move" else {"pc_width": s["pc_width"]})
            self.update()

    def wheelEvent(self, e):
        if self.area.contains(e.position()) and self.on_moved:
            size = next_size(int(self.painter.s["pc_size"] or 100), e.angleDelta().y() > 0)
            if size:
                self.on_moved({"pc_size": size})
            e.accept()
        else:
            e.ignore()                     # elsewhere the wheel scrolls the page

    def leaveEvent(self, e):
        self.update()

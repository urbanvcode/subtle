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
    BASE = Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.NoDropShadowWindowHint
    CLICK_THROUGH = Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus
    ACCENT = QColor("#2fbf8f")

    def __init__(self):
        super().__init__(None, self.BASE | self.CLICK_THROUGH)
        self.moving = False                # "Move on screen": a box to drag, instead of click-through text
        self.on_moved = self.on_done = None
        self.drag = None
        self.box = QRectF()
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
        if not self.drag:
            self.setGeometry(int(g.left() + cx - w / 2), int(g.top() + bottom - h), int(w), h)
        if not s["pc_keep_prev"]:
            self.lines["prev"] = None
        if not s["pc_show_src"]:
            self.lines["src"] = None
        self._relayout()

    def _relayout(self):
        """Wrap the visible lines (only when text or look changes: painting then just draws them)."""
        px, w, pt = self.font_px, self.width(), self.painter
        self.blocks = []
        if self.moving and not self.lines["cur"]:          # something to see while placing the box
            sample = SubtitlePreview.SAMPLE
            if pt.s["pc_keep_prev"]:
                self.blocks.append(pt.block(sample[0], px * .82, w, "", .7))
            self.blocks.append(pt.block(sample[1], px, w))
            if pt.s["pc_show_src"]:
                self.blocks.append(pt.block(sample[2], px * max(40, min(100, int(pt.s["pc_src_size"]))) / 100, w,
                                            "", .9, weight=QFont.Medium))
        if self.lines["prev"] and not (self.moving and not self.lines["cur"]):
            text, colour = self.lines["prev"]
            self.blocks.append(pt.block(text, px * .82, w, colour, .7))
        if self.lines["cur"]:
            text, colour = self.lines["cur"]
            self.blocks.append(pt.block(text, px, w, colour, .85 if self.partial else 1.0, italic=self.partial))
        if self.lines["src"]:
            text, _ = self.lines["src"]
            self.blocks.append(pt.block(text, px * max(40, min(100, int(pt.s["pc_src_size"]))) / 100, w, "", .9,
                                        weight=QFont.Medium))
        if self.blocks:
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
        if self.moving:
            # the box to drag: the lines' area, tinted (a see-through pixel would let the click through)
            gap = self.font_px * .18
            h = sum(b["h"] for b in self.blocks) + gap * (len(self.blocks) - 1) + self.font_px * .5
            self.box = QRectF(2, self.height() - 2 - h, self.width() - 4, h)
            fill = QColor(self.ACCENT)
            fill.setAlpha(46)
            p.setBrush(fill)
            p.setPen(QPen(self.ACCENT, 2, Qt.DashLine))
            p.drawRoundedRect(self.box, 10, 10)
            hint = "Drag to move  ·  double-click or press Enter when done"
            f = QFont()
            f.setPixelSize(max(13, int(self.font_px * .45)))
            f.setWeight(QFont.DemiBold)
            fm = QFontMetricsF(f)
            hw, hh = fm.horizontalAdvance(hint) + 24, fm.height() + 10
            pill = QRectF((self.width() - hw) / 2, max(0, self.box.top() - hh - 8), hw, hh)
            p.setPen(Qt.NoPen)
            p.setBrush(self.ACCENT)
            p.drawRoundedRect(pill, hh / 2, hh / 2)
            p.setPen(QColor("#06261b"))
            p.setFont(f)
            p.drawText(pill, Qt.AlignCenter, hint)
        self.painter.stack(p, self.blocks, self.width() / 2, self.height() - 2 - (self.font_px * .25 if self.moving else 0))
        p.end()

    # -- "Move on screen" ---------------------------------------------------------------------------
    def set_move_mode(self, on: bool, on_moved=None, on_done=None):
        """on: the subtitles become a box to drag (on_moved gets the new place as settings); off: click-through."""
        if on == self.moving:
            return
        self.moving, self.drag = on, None
        if on:
            self.on_moved, self.on_done = on_moved, on_done
        self.setWindowFlags(self.BASE if on else self.BASE | self.CLICK_THROUGH)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, not on)
        self.setAttribute(Qt.WA_ShowWithoutActivating, not on)
        self.setCursor(Qt.SizeAllCursor if on else Qt.ArrowCursor)
        self._relayout()
        if on:
            self.show()
            self.raise_()
            self.activateWindow()
            self.setFocus()
        else:
            done, self.on_done = self.on_done, None
            if done:
                done()

    def mousePressEvent(self, e):
        if self.moving and e.button() == Qt.LeftButton and self.box.contains(e.position()):
            self.drag = (e.globalPosition().toPoint(), self.pos())

    def mouseMoveEvent(self, e):
        if self.drag:
            start, pos = self.drag
            self.move(pos + e.globalPosition().toPoint() - start)

    def mouseReleaseEvent(self, e):
        if self.drag:
            self.drag = None
            self._commit_place()

    def mouseDoubleClickEvent(self, e):
        if self.moving:
            self.set_move_mode(False)

    def keyPressEvent(self, e):
        if self.moving and e.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Escape):
            self.set_move_mode(False)

    def _commit_place(self):
        """Where the box was dropped -> pc_screen, pc_x (centre, ‰ of the width), pc_y (bottom, ‰ up)."""
        geo = self.frameGeometry()
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
        if self.on_moved:
            self.on_moved(changes)

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


class SubtitlePreview(QWidget):
    """The Subtle window's preview: a small 'screen' with sample lines drawn exactly as the overlay draws them."""

    SAMPLE = ("I thought you'd left already.", "Where were you last night?", "昨日の夜はどこにいたの？")

    def __init__(self, parent=None, on_moved=None):
        super().__init__(parent)
        self.setMinimumHeight(170)
        self.painter = Painter({})
        self.on_moved = on_moved          # dragging the lines here places them on the real screen
        self.lines_rect = QRectF()
        self.drag = None
        self.setMouseTracking(True)
        self.setToolTip("Drag the subtitles to place them on your screen")

    def set_style(self, settings: dict):
        if style_from(settings) != self.painter.s:        # the page redraws every 2 s: repaint only on a change
            self.painter = Painter(settings)
            self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect())
        clip = QPainterPath()
        clip.addRoundedRect(r, 10, 10)
        p.setClipPath(clip)
        grad = QLinearGradient(r.topLeft(), r.bottomRight())
        grad.setColorAt(0, QColor("#3b4a5c"))
        grad.setColorAt(.6, QColor("#1b2129"))
        grad.setColorAt(1, QColor("#101419"))
        p.fillRect(r, grad)
        pt, s = self.painter, self.painter.s
        px = 17 * int(s["pc_size"] or 100) / 100
        cx, bottom, w = placement(s, r.width(), r.height())
        bottom = min(bottom, r.height() - 4)
        blocks = []
        if s["pc_keep_prev"]:
            blocks.append(pt.block(self.SAMPLE[0], px * .82, w, "", .7))
        blocks.append(pt.block(self.SAMPLE[1], px, w))
        if s["pc_show_src"]:
            blocks.append(pt.block(self.SAMPLE[2], px * max(40, min(100, int(s["pc_src_size"]))) / 100, w, "", .9,
                                   weight=QFont.Medium))
        total = sum(b["h"] for b in blocks) + px * .18 * (len(blocks) - 1)
        bottom = max(bottom, total + 4)                    # moved high up: still fully in the preview
        pt.stack(p, blocks, cx, bottom)
        bw = max(b["w"] for b in blocks)
        self.lines_rect = QRectF(cx - bw / 2, bottom - total, bw, total)
        if self.drag or self.underMouse() and self.lines_rect.contains(self.mapFromGlobal(self.cursor().pos()).toPointF()):
            p.setPen(QPen(SubtitleOverlay.ACCENT, 1.5, Qt.DashLine))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(self.lines_rect.adjusted(-6, -5, 6, 5), 8, 8)
        p.end()

    # dragging the lines: the preview stands for the whole screen
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self.lines_rect.contains(e.position()):
            self.drag = (e.position(), self.lines_rect.center().x(), self.lines_rect.bottom())

    def mouseMoveEvent(self, e):
        over = self.lines_rect.contains(e.position())
        self.setCursor(Qt.SizeAllCursor if self.drag or over else Qt.ArrowCursor)
        if not self.drag:
            self.update()                  # the dashed outline follows the mouse
            return
        start, cx, bottom = self.drag
        d = e.position() - start
        s = dict(self.painter.s)
        s["pc_x"] = round(max(0, min(1000, (cx + d.x()) / self.width() * 1000)))
        s["pc_y"] = round(max(0, min(950, (self.height() - bottom - d.y()) / self.height() * 1000)))
        self.painter = Painter(s)
        self.update()

    def mouseReleaseEvent(self, e):
        if self.drag:
            self.drag = None
            s = self.painter.s
            if self.on_moved:
                self.on_moved({"pc_x": s["pc_x"], "pc_y": s["pc_y"]})
            self.update()

    def leaveEvent(self, e):
        self.update()

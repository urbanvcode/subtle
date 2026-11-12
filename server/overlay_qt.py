"""
Subtitles on top of the desktop, for "PC sound" (Discord, games, VLC...): a frameless, see-through
window that stays above other windows and lets every click through to what is underneath.
It shows the current line (and the previous one, dimmed) like the browser overlay does.
"""

from PySide6.QtCore import QRect, Qt, QTimer
from PySide6.QtGui import QFont, QFontMetrics, QGuiApplication
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

SPEAKER_COLOURS = ["#ffe873", "#8fd8ff", "#a8f0a0", "#ffb3de", "#ffc285", "#cdb8ff", "#9ff0e6", "#f5f5f5"]


class SubtitleOverlay(QWidget):
    def __init__(self):
        flags = (Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.WindowTransparentForInput
                 | Qt.WindowDoesNotAcceptFocus | Qt.NoDropShadowWindowHint)
        super().__init__(None, flags)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setWindowTitle("Subtle subtitles")
        self.setStyleSheet("background: transparent;")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        lay.addStretch(1)
        self.prev, self.cur, self.src = QLabel(), QLabel(), QLabel()
        for lbl in (self.prev, self.cur, self.src):
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setTextFormat(Qt.PlainText)
            lay.addWidget(lbl, 0, Qt.AlignHCenter)
            lbl.hide()
        self.size_pct, self.height_pct, self.opacity, self.screen_no = 100, 8, 62, 0
        self.font_px = 32
        self.last_final_id = -1
        self.hide_timer = QTimer(self, singleShot=True, timeout=self._hide_all)
        self.prev_timer = QTimer(self, singleShot=True, timeout=lambda: self._set(self.prev, ""))
        self.apply_style(100, 8, 62, 0)

    # -- look and place ------------------------------------------------------------------------
    def _screen(self):
        screens = QGuiApplication.screens()
        if not screens:
            return None
        return screens[self.screen_no] if 0 <= self.screen_no < len(screens) else QGuiApplication.primaryScreen()

    def apply_style(self, size_pct, height_pct, opacity, screen_no):
        self.size_pct, self.height_pct = int(size_pct or 100), int(height_pct or 8)
        self.opacity, self.screen_no = int(opacity if opacity is not None else 62), int(screen_no or 0)
        scr = self._screen()
        if scr is None:
            return
        g: QRect = scr.geometry()
        self.font_px = max(16, int(g.height() * 0.034 * self.size_pct / 100))
        w = int(g.width() * 0.8)
        h = int(g.height() * 0.45)
        bottom = g.bottom() - int(g.height() * self.height_pct / 100)
        self.setGeometry(g.left() + (g.width() - w) // 2, bottom - h, w, h)
        alpha = max(0, min(100, self.opacity)) / 100
        f = QFont("Segoe UI", 10)
        f.setWeight(QFont.DemiBold)
        for lbl, scale, extra in ((self.prev, 0.82, "color: rgba(255,255,255,.72);"), (self.cur, 1.0, ""),
                                  (self.src, 0.7, "color: rgba(255,255,255,.9);")):
            px = int(self.font_px * scale)
            ff = QFont(f)
            ff.setPixelSize(px)
            lbl.setFont(ff)               # for measuring; the style sheet below is what draws it
            # (the app's style sheet sets a 10 pt font on every widget, so the size must be set here too)
            lbl.setStyleSheet(f"color: #ffffff; background: rgba(8, 8, 12, {alpha:.2f}); border-radius: {px // 4}px; "
                              f"padding: {px // 7}px {px // 2}px; font-family: 'Segoe UI'; font-size: {px}px; "
                              f"font-weight: 600; {extra}")
        for lbl in (self.prev, self.cur, self.src):     # re-fit the visible lines to the new size
            if lbl.text():
                self._set(lbl, lbl.text(), lbl.property("colour") or "")

    def _set(self, lbl: QLabel, text: str, colour: str = ""):
        if not text:
            lbl.setText("")
            lbl.hide()
            self._maybe_hide_window()
            return
        maxw = self.width()
        pad = self.font_px  # left + right padding
        width = QFontMetrics(lbl.font()).horizontalAdvance(text) + pad + 4
        lbl.setWordWrap(width > maxw)
        lbl.setFixedWidth(min(width, maxw))
        lbl.setText(text)
        lbl.setProperty("colour", colour)
        style = lbl.styleSheet().split(" /*c*/")[0]
        lbl.setStyleSheet(style + (f" /*c*/ color: {colour};" if colour else ""))
        lbl.show()
        if not self.isVisible():
            self.show()
        self.raise_()

    def _maybe_hide_window(self):
        if not any(l.text() for l in (self.prev, self.cur, self.src)) and self.isVisible():
            self.hide()

    def _hide_all(self):
        self.prev_timer.stop()
        for lbl in (self.prev, self.cur, self.src):
            self._set(lbl, "")

    # -- messages from the speech engine -------------------------------------------------------
    def on_message(self, msg: dict, show_original: bool = False):
        kind = msg.get("type")
        if kind == "clear":
            self.last_final_id = -1
            self._hide_all()
            return
        if kind != "sub":
            return
        text, src = msg.get("text") or "", msg.get("src") or ""
        if msg.get("final"):
            self.last_final_id = msg.get("id", -1)
            if not text:
                if self.cur.property("partial"):
                    self._set(self.cur, "")
                return
            self._move_up()
            colour = SPEAKER_COLOURS[(msg["speaker"] - 1) % len(SPEAKER_COLOURS)] if msg.get("speaker") else ""
            self.cur.setProperty("partial", False)
            self._set(self.cur, text, colour)
            self._set(self.src, src if show_original and src and src != text else "")
            self.hide_timer.start(int(min(7000, max(2200, len(text) * 70))))
        elif text and msg.get("id", 0) > self.last_final_id:
            self._move_up()
            self.cur.setProperty("partial", True)
            self._set(self.cur, text)
            self._set(self.src, src if show_original and src and src != text else "")
            self.hide_timer.start(8000)

    def _move_up(self):
        if self.cur.text() and not self.cur.property("partial"):
            self._set(self.prev, self.cur.text(), self.cur.property("colour") or "")
            self.prev_timer.start(4000)

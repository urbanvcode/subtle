"""Tray icon for Subtle (pystray). Click: open the Subtle window."""

import threading

import pystray
from PIL import Image, ImageDraw

import modelstore
import winutil


def icon_image(size: int = 64) -> Image.Image:
    """A dark rounded square with two subtitle bars (drawn, so no image file is needed)."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = size // 5
    d.rounded_rectangle((2, 2, size - 3, size - 3), radius=r, fill=(24, 26, 32, 255), outline=(90, 200, 160, 255),
                        width=max(2, size // 32))
    h = size // 9
    d.rounded_rectangle((size * 0.2, size * 0.52, size * 0.8, size * 0.52 + h), radius=h // 2, fill=(240, 240, 240, 255))
    d.rounded_rectangle((size * 0.3, size * 0.7, size * 0.7, size * 0.7 + h), radius=h // 2, fill=(90, 200, 160, 255))
    return img


def start(sup, port: int):
    def open_window(icon=None, item=None):
        sup.show_window()

    def unload(icon, item):
        sup.request_unload()

    def open_models(icon, item):
        winutil.open_path(modelstore.MODELS_DIR)

    def quit_app(icon, item):
        sup.request_stop()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Open Subtle", open_window, default=True),
        pystray.MenuItem("Free memory now (unload models)", unload),
        pystray.MenuItem("Open models folder", open_models),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit Subtle", quit_app),
    )
    icon = pystray.Icon("Subtle", icon_image(), "Subtle - subtitles", menu)
    threading.Thread(target=icon.run, daemon=True, name="tray").start()
    return icon

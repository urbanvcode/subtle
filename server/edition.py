"""Which Subtle this is: the full version, or Subtle Lite (the browser extension's server only).

The Lite download is the same code with an `edition.txt` saying "lite" next to Install Subtle.bat:
the installer then skips the extra packages and the app hides the extra features for good."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _read() -> str:
    try:
        return (ROOT / "edition.txt").read_text(encoding="utf-8").strip().lower()
    except OSError:
        return ""


LITE = _read() == "lite"
NAME = "Subtle Lite" if LITE else "Subtle"

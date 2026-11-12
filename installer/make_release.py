"""
Build the two downloads of a release from the current commit:

  Subtle-<version>.zip        the full version
  Subtle-Lite-<version>.zip   the same files plus edition.txt = "lite": the browser extension's server only
                              (the installer skips the extra packages; the app hides the extra features)

usage: python installer/make_release.py <output folder>
"""

import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))
from version import VERSION  # noqa: E402

out = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
out.mkdir(parents=True, exist_ok=True)
full = out / f"Subtle-{VERSION}.zip"
lite = out / f"Subtle-Lite-{VERSION}.zip"

subprocess.run(["git", "archive", "--format=zip", "--prefix=Subtle/", "-o", str(full), "HEAD"], cwd=ROOT, check=True)

with zipfile.ZipFile(full) as src, zipfile.ZipFile(lite, "w", zipfile.ZIP_DEFLATED) as dst:
    for item in src.infolist():
        if item.filename.startswith("Subtle/docs/"):
            continue                       # screenshots for the GitHub page only
        dst.writestr(item, src.read(item.filename))
    dst.writestr("Subtle/edition.txt", "lite\n")

for z in (full, lite):
    with zipfile.ZipFile(z) as f:
        print(f"{z.name}: {len(f.namelist())} files, {z.stat().st_size // 1024} KB")

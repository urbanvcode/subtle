"""
Build the downloads of a release from the current commit:

  Subtle-<version>.zip                    the full version
  Subtle-Lite-<version>.zip               the same files plus edition.txt = "lite": the browser extension's
                                          server only (the installer skips the extra packages; the app hides
                                          the extra features)
  Subtle-Firefox-extension-<version>.zip  the extension for Firefox (Experimental), files at the top level
  Subtle-Setup-<version>.exe              Windows only: asks for a folder, unpacks the full zip, runs the installer
                                          (a self-extracting package made with Windows' own IExpress)

Both Subtle zips also carry the Firefox extension as extension-firefox/.

usage: python installer/make_release.py <output folder>
       python installer/make_release.py --firefox <folder>    (only build the Firefox extension, for testing)
"""

import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))
from version import VERSION  # noqa: E402

FIREFOX_ID = "subtle@ddsaamir.github.io"


def firefox_manifest(manifest: dict) -> dict:
    """Chrome's manifest -> Firefox's: a background page instead of a service worker (offscreen.js runs in
    it, as Firefox has no offscreen documents), no tab capture, and Firefox's own add-on settings."""
    m = json.loads(json.dumps(manifest))
    m["background"] = {"scripts": ["offscreen.js", "background.js"]}
    m["permissions"] = [p for p in m.get("permissions", []) if p not in ("tabCapture", "offscreen")]
    m.pop("incognito", None)              # Firefox doesn't know "split"
    # Firefox's default policy for MV3 extension pages adds upgrade-insecure-requests, which would turn
    # ws://127.0.0.1 (the Subtle app on this PC) into wss:// and fail
    m["content_security_policy"] = {"extension_pages": "script-src 'self'; object-src 'self'"}
    m["browser_specific_settings"] = {"gecko": {"id": FIREFOX_ID, "strict_min_version": "128.0"}}
    m["description"] = m.get("description", "") + " (Firefox version: Experimental)"
    return m


def firefox_files(read) -> dict[str, bytes]:
    """{path inside the extension: bytes} for the Firefox extension; read(rel) returns a file of extension/."""
    names = read(None)
    out = {}
    for rel in names:
        if rel in ("offscreen.html", "capture-worklet.js"):
            continue                      # Chrome's offscreen document and tab-capture worklet only
        data = read(rel)
        if rel == "manifest.json":
            data = (json.dumps(firefox_manifest(json.loads(data)), indent=2) + "\n").encode()
        out[rel] = data
    return out


def build_firefox_folder(dst: Path):
    src = ROOT / "extension"

    def read(rel):
        if rel is None:
            return [p.relative_to(src).as_posix() for p in src.rglob("*") if p.is_file()]
        return (src / rel).read_bytes()

    for rel, data in firefox_files(read).items():
        p = dst / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    print(f"Firefox extension in {dst}")


def build_setup_exe(full_zip: Path, out: Path):
    """Subtle-Setup-<v>.exe: IExpress unpacks payload.zip + setup.ps1 to a temp folder and runs setup.cmd."""
    import os
    import shutil
    import tempfile
    iexpress = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "iexpress.exe"
    if sys.platform != "win32" or not iexpress.is_file():
        print("(no IExpress here: Setup.exe skipped)")
        return
    work = Path(tempfile.mkdtemp(prefix="subtle-sfx-"))
    shutil.copy(full_zip, work / "payload.zip")
    shutil.copy(ROOT / "installer" / "setup.ps1", work / "setup.ps1")
    crlf = "\r\n"
    (work / "setup.cmd").write_text(
        "@echo off" + crlf + 'powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"' + crlf,
        encoding="ascii", newline="")
    target = out / f"Subtle-Setup-{VERSION}.exe"
    files = ["payload.zip", "setup.ps1", "setup.cmd"]
    sed = crlf.join([
        "[Version]", "Class=IEXPRESS", "SEDVersion=3", "[Options]", "PackagePurpose=InstallApp", "ShowInstallProgramWindow=1",
        "HideExtractAnimation=0", "UseLongFileName=1", "InsideCompressed=0", "CAB_FixedSize=0", "CAB_ResvCodeSigning=0",
        "RebootMode=N", "InstallPrompt=%InstallPrompt%", "DisplayLicense=%DisplayLicense%", "FinishMessage=%FinishMessage%",
        "TargetName=%TargetName%", "FriendlyName=%FriendlyName%", "AppLaunched=%AppLaunched%", "PostInstallCmd=%PostInstallCmd%",
        "AdminQuietInstCmd=%AdminQuietInstCmd%", "UserQuietInstCmd=%UserQuietInstCmd%", "SourceFiles=SourceFiles",
        "[Strings]", "InstallPrompt=", "DisplayLicense=", "FinishMessage=", f"TargetName={target}",
        f"FriendlyName=Subtle {VERSION} setup", "AppLaunched=cmd /c setup.cmd", "PostInstallCmd=<None>",
        "AdminQuietInstCmd=", "UserQuietInstCmd="]
        + [f"FILE{i}=\"{f}\"" for i, f in enumerate(files)]
        + ["[SourceFiles]", f"SourceFiles0={work}\\", "[SourceFiles0]"] + [f"%FILE{i}%=" for i in range(len(files))]) + crlf
    (work / "subtle.sed").write_text(sed, encoding="ascii", newline="")
    subprocess.run([str(iexpress), "/N", "/Q", str(work / "subtle.sed")], check=True)
    shutil.rmtree(work, ignore_errors=True)
    if not target.is_file():
        print("(IExpress made no Setup.exe)")


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "--firefox":
        return build_firefox_folder(Path(sys.argv[2]).resolve())
    out = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    out.mkdir(parents=True, exist_ok=True)
    base = out / "base.zip"
    subprocess.run(["git", "archive", "--format=zip", "--prefix=Subtle/", "-o", str(base), "HEAD"], cwd=ROOT, check=True)

    with zipfile.ZipFile(base) as src:
        ext = {i.filename[len("Subtle/extension/"):]: i for i in src.infolist()
               if i.filename.startswith("Subtle/extension/") and not i.is_dir()}

        def read(rel):
            return list(ext) if rel is None else src.read(ext[rel].filename)

        ff = firefox_files(read)
        with zipfile.ZipFile(out / f"Subtle-Firefox-extension-{VERSION}.zip", "w", zipfile.ZIP_DEFLATED) as z:
            for rel, data in ff.items():
                z.writestr(rel, data)

        for name, lite in ((f"Subtle-{VERSION}.zip", False), (f"Subtle-Lite-{VERSION}.zip", True)):
            with zipfile.ZipFile(out / name, "w", zipfile.ZIP_DEFLATED) as dst:
                for item in src.infolist():
                    if lite and item.filename.startswith("Subtle/docs/"):
                        continue          # screenshots for the GitHub page only
                    dst.writestr(item, src.read(item.filename))
                for rel, data in ff.items():
                    dst.writestr(f"Subtle/extension-firefox/{rel}", data)
                if lite:
                    dst.writestr("Subtle/edition.txt", "lite\n")
    base.unlink()
    build_setup_exe(out / f"Subtle-{VERSION}.zip", out)
    for z in sorted(list(out.glob(f"*{VERSION}.zip")) + list(out.glob(f"*{VERSION}.exe"))):
        if z.suffix == ".exe":
            print(f"{z.name}: {z.stat().st_size // 1024} KB")
            continue
        with zipfile.ZipFile(z) as f:
            print(f"{z.name}: {len(f.namelist())} files, {z.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()

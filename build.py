"""Package LeanCut as a desktop app with PyInstaller.

    pip install -r requirements.txt -r requirements-build.txt
    python build.py

macOS   -> dist/LeanCut.app and dist/LeanCut-macOS.dmg
Windows -> dist/LeanCut/LeanCut.exe and dist/LeanCut-Windows.zip
Linux   -> dist/LeanCut/LeanCut and dist/LeanCut-Linux-x64.tar.gz (LeanCut/ with install.sh; needs pywebview[qt])
PyInstaller cannot cross-compile: build each OS on that OS (see .github/workflows/release.yml).

macOS signing: set LEANCUT_CODESIGN_IDENTITY (e.g. "Developer ID Application: Name (TEAMID)") to sign the app with
the hardened runtime; notarization happens in CI. Without it the app is ad-hoc signed, as before.
"""
import os
import plistlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

import PyInstaller.__main__
from PIL import Image

ROOT = Path(__file__).parent
DIST = ROOT / "dist"
VERSION = re.search(r'^VERSION = "([^"]+)"', (ROOT / "app.py").read_text(encoding="utf-8"), re.M).group(1)

PyInstaller.__main__.run([
    str(ROOT / "desktop.py"),
    "--name", "LeanCut",
    "--windowed",
    "--noconfirm",
    "--clean",
    "--icon", str(ROOT / "assets" / "icon.png"),  # Pillow converts it to .icns / .ico
    "--add-data", f"{ROOT / 'static'}{os.pathsep}static",
    "--add-data", f"{ROOT / 'graphics_templates'}{os.pathsep}graphics_templates",
    "--hidden-import", "websockets.sync.client",
    "--osx-bundle-identifier", "app.leancut.desktop",
    # packages that load models, native libs or binaries at runtime
    "--collect-all", "faster_whisper",
    "--collect-all", "ctranslate2",
    "--collect-all", "onnxruntime",
    "--collect-all", "imageio_ffmpeg",
    "--collect-submodules", "uvicorn",
    # optional features app.py imports by name at runtime (PyInstaller can't see those imports)
    *[a for m in ("telemetry", "vision", "enhance", "voice", "graphics", "agent_tools", "mcp_server", "leancut_cli")
      for a in ("--hidden-import", m)],
    "--collect-data", "tokenizers",
])

if sys.platform == "darwin":
    app = DIST / "LeanCut.app"
    info = app / "Contents" / "Info.plist"
    plist = plistlib.loads(info.read_bytes())
    plist.update(CFBundleShortVersionString=VERSION, CFBundleVersion=VERSION, NSHighResolutionCapable=True,
                 LSMinimumSystemVersion="12.0")
    info.write_bytes(plistlib.dumps(plist))
    identity = os.environ.get("LEANCUT_CODESIGN_IDENTITY") or "-"  # "-" = ad-hoc, re-done after the plist edit
    sign = ["codesign", "--force", "--deep", "--sign", identity]
    if identity != "-":
        sign += ["--options", "runtime", "--timestamp", "--entitlements", str(ROOT / "assets" / "entitlements.plist")]
    subprocess.run([*sign, str(app)], check=True)
    dmg = DIST / "LeanCut-macOS.dmg"
    dmg.unlink(missing_ok=True)
    subprocess.run(["hdiutil", "create", "-volname", "LeanCut", "-srcfolder", str(app), "-ov", "-format", "UDZO",
                    str(dmg)], check=True)
    print("Built", dmg, "version", VERSION)
elif sys.platform.startswith("linux"):
    app = DIST / "LeanCut"
    shutil.copy2(ROOT / "packaging" / "linux" / "leancut.desktop", app)
    Image.open(ROOT / "assets" / "icon.png").resize((512, 512)).save(app / "leancut.png")  # installed as 512x512
    shutil.copy2(ROOT / "packaging" / "linux" / "install.sh", app)
    (app / "install.sh").chmod(0o755)  # in case the checkout lost the executable bit
    print("Built", shutil.make_archive(str(DIST / "LeanCut-Linux-x64"), "gztar", DIST, "LeanCut"), "version", VERSION)
elif os.name == "nt" and not os.environ.get("LEANCUT_SKIP_ZIP"):  # CI signs the exe first, then zips
    print("Built", shutil.make_archive(str(DIST / "LeanCut-Windows"), "zip", DIST, "LeanCut"), "version", VERSION)

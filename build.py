"""Package LeanCut as a desktop app with PyInstaller.

    pip install -r requirements.txt -r requirements-build.txt
    python build.py

macOS   -> dist/LeanCut.app and dist/LeanCut-macOS.dmg
Windows -> dist/LeanCut/LeanCut.exe and dist/LeanCut-Windows.zip
PyInstaller cannot cross-compile: build each OS on that OS (see .github/workflows/build.yml).
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import PyInstaller.__main__

ROOT = Path(__file__).parent
DIST = ROOT / "dist"

PyInstaller.__main__.run([
    str(ROOT / "desktop.py"),
    "--name", "LeanCut",
    "--windowed",
    "--noconfirm",
    "--clean",
    "--icon", str(ROOT / "assets" / "icon.png"),  # Pillow converts it to .icns / .ico
    "--add-data", f"{ROOT / 'static'}{os.pathsep}static",
    "--osx-bundle-identifier", "app.leancut.desktop",
    # packages that load models, native libs or binaries at runtime
    "--collect-all", "faster_whisper",
    "--collect-all", "ctranslate2",
    "--collect-all", "onnxruntime",
    "--collect-all", "imageio_ffmpeg",
    "--collect-submodules", "uvicorn",
])

if sys.platform == "darwin":
    dmg = DIST / "LeanCut-macOS.dmg"
    dmg.unlink(missing_ok=True)
    subprocess.run(["hdiutil", "create", "-volname", "LeanCut", "-srcfolder", str(DIST / "LeanCut.app"),
                    "-ov", "-format", "UDZO", str(dmg)], check=True)
    print("Built", dmg)
elif os.name == "nt":
    print("Built", shutil.make_archive(str(DIST / "LeanCut-Windows"), "zip", DIST, "LeanCut"))

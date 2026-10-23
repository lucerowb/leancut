"""LeanCut desktop app: runs the FastAPI backend in-process and shows the UI in a native window
(WebKit on macOS, WebView2 on Windows). This is also the PyInstaller entry point.

Run from source:  python desktop.py
"""
import os
import socket
import sys
import threading
import time

import uvicorn
import webview

import app
import video_engine

if getattr(sys, "frozen", False):
    # Windowed builds have no console (sys.stdout/stderr are None on Windows), which crashes tqdm/uvicorn logging.
    # Send everything to a log file in the user data folder instead — also handy for bug reports.
    sys.stdout = sys.stderr = open(app.WORK / "leancut.log", "a", buffering=1, encoding="utf-8")

VIDEO_TYPES = ("Video files (*.mp4;*.mov;*.mkv;*.MP4;*.MOV;*.MKV)",)
AUDIO_TYPES = ("Audio files (*.mp3;*.m4a;*.wav;*.aac;*.flac;*.ogg)",)


class Api:
    """Exposed to the page as window.pywebview.api."""

    def pick_files(self):
        paths = webview.windows[0].create_file_dialog(webview.FileDialog.OPEN, allow_multiple=True, file_types=VIDEO_TYPES)
        return [p for p in paths or [] if os.path.splitext(p)[1].lower() in video_engine.VIDEO_EXTS]

    def pick_folder(self):
        paths = webview.windows[0].create_file_dialog(webview.FileDialog.FOLDER)
        return paths[0] if paths else None

    def pick_audio(self):
        paths = webview.windows[0].create_file_dialog(webview.FileDialog.OPEN, file_types=AUDIO_TYPES)
        return paths[0] if paths else None


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main():
    port = _free_port()  # never collides with a dev server on 8765
    server = uvicorn.Server(uvicorn.Config(app.api, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    webview.create_window(app.APP_NAME, f"http://127.0.0.1:{port}/", js_api=Api(),
                          width=1440, height=920, min_size=(900, 640), background_color="#0f1115")
    webview.start()
    server.should_exit = True


if __name__ == "__main__":
    main()

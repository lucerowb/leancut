# Third-party notices

LeanCut's own code is MIT-licensed (see [LICENSE](LICENSE)). The packaged apps also contain, or download at runtime,
the following components under their own licenses.

## Bundled in the desktop apps

| Component | License | Source |
|---|---|---|
| FFmpeg 7.1 (via `imageio-ffmpeg`) | **GPL v2 or later** for the bundled build (`--enable-gpl`, includes x264, x265, FreeType and vid.stab) | https://ffmpeg.org/download.html · build scripts: https://github.com/imageio/imageio-binaries |
| faster-whisper | MIT | https://github.com/SYSTRAN/faster-whisper |
| CTranslate2 | MIT | https://github.com/OpenNMT/CTranslate2 |
| Silero VAD model (shipped inside faster-whisper) | MIT | https://github.com/snakers4/silero-vad |
| ONNX Runtime (also runs the vision and voice-enhancement models); the Windows build uses `onnxruntime-directml`, which includes Microsoft's `DirectML.dll` | MIT (ONNX Runtime); DirectML is redistributed under Microsoft's DirectML licence terms | https://github.com/microsoft/onnxruntime · https://github.com/microsoft/DirectML |
| PyAV / FFmpeg libraries used by faster-whisper | BSD-3 / LGPL | https://github.com/PyAV-Org/PyAV |
| NumPy | BSD-3 | https://github.com/numpy/numpy |
| FastAPI, Starlette, Pydantic | MIT | https://github.com/fastapi/fastapi |
| python-multipart | Apache-2.0 | https://github.com/Kludex/python-multipart |
| websockets (talks to the headless browser that draws motion graphics) | BSD-3-Clause | https://github.com/python-websockets/websockets |
| Uvicorn | BSD-3 | https://github.com/encode/uvicorn |
| pywebview | BSD-3 | https://github.com/r0x0r/pywebview |
| pythonnet (Windows) | MIT | https://github.com/pythonnet/pythonnet |
| Hugging Face Hub client, tokenizers (also CLIP's tokenizer) | Apache-2.0 | https://github.com/huggingface |
| PyQt6, Qt 6 and Qt WebEngine (Linux build only, the window) | PyQt6: **GPL v3**; Qt and Qt WebEngine: LGPL v3 (WebEngine includes Chromium, BSD-style and others) | https://www.riverbankcomputing.com/software/pyqt/ · https://www.qt.io/download-open-source |
| Python | PSF License | https://www.python.org |

**FFmpeg / GPL:** the FFmpeg executable is a separate program that LeanCut runs as a subprocess. Its source code is
available from the links above; on request we will point you to the exact FFmpeg version shipped in any LeanCut
release (open an issue). If you redistribute LeanCut's packaged apps, you take on the GPL obligations for the bundled
FFmpeg binary. The same applies to PyQt6 (GPL v3) in the Linux build.

## Downloaded at runtime

| Component | License | Notes |
|---|---|---|
| Whisper models (OpenAI), CTranslate2 conversions by Systran / Mobius Labs | MIT | Downloaded from Hugging Face on first use |
| YuNet face detector (`opencv/face_detection_yunet`, `face_detection_yunet_2023mar.onnx`) | MIT | From Hugging Face on first use of face blur or Shorts tracking, pinned commit, 0.2 MB. Source: https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet |
| LPD-YuNet licence-plate detector (`opencv/license_plate_detection_yunet`) | Apache-2.0 | From Hugging Face on first use of plate blur, pinned commit, 4 MB. Source: https://github.com/opencv/opencv_zoo/tree/main/models/license_plate_detection_yunet |
| CLIP ViT-B/32 (OpenAI), int8 ONNX export by Xenova (`Xenova/clip-vit-base-patch32`: vision model, text model, tokenizer) | MIT | From Hugging Face on first footage search, pinned commit, about 150 MB. Source: https://github.com/openai/CLIP |
| DeepFilterNet3 ONNX export (Hendrik Schröter) | MIT or Apache-2.0 | From `raw.githubusercontent.com/Rikorose/DeepFilterNet` on first use of voice enhancement, pinned commit, SHA-256 checked, 8 MB. Source: https://github.com/Rikorose/DeepFilterNet |
| Chrome for Testing `chrome-headless-shell` 156.0.8078.47 (Google / the Chromium project) | BSD-3-Clause (Chromium) and the licences of its third-party components, listed in the archive | Downloaded from storage.googleapis.com/chrome-for-testing-public on the first motion-graphics render, only when no Chrome, Chromium, Edge or Brave is installed. Pinned version, exact size checked, 104–126 MB zipped. https://github.com/GoogleChromeLabs/chrome-for-testing |
| An installed Google Chrome, Microsoft Edge, Brave or Chromium | Its own licence | Used as it is, headless, to draw motion graphics when present |
| Vision models you pull into Ollama for the visual judge | Each model's own license | e.g. Gemma 3 (Gemma Terms of Use). |
| Language models you pull into Ollama or LM Studio | Each model's own license | e.g. Qwen 2.5 (Apache-2.0 for most sizes), Llama 3.x (Llama Community License), Gemma (Gemma Terms). Check the license of the model you use. |

## Optional voice cloning (not bundled)

Installed only if you run `pip install -r requirements-voice.txt` from a source checkout, or use `voice_server.py`.

| Component | License | Notes |
|---|---|---|
| `chatterbox-tts` and the Chatterbox-Turbo model (Resemble AI) | MIT | The model (about 3.8 GB) downloads from Hugging Face on first use. Its output carries Resemble AI's inaudible Perth watermark. https://github.com/resemble-ai/chatterbox |
| PyTorch | BSD-3 | https://github.com/pytorch/pytorch |
| Their other dependencies (Transformers and others) | Each under its own license | Check them before redistributing an environment that contains them. |

## Coding agents (not bundled)

Claude Code, Codex, Cursor, OpenCode, Gemini CLI and other agents you connect to LeanCut are your own installs,
under their own terms; LeanCut only starts them or answers their MCP calls on your machine.

## Design credit

LeanCut's motion-graphics pages follow the page contract of [OpenFilm](https://github.com/openfilm/openfilm)
(MIT, © the OpenFilm authors), so pages work in both. No OpenFilm code is included. "OpenFilm" is a trademark of
its project; LeanCut is not affiliated with or endorsed by it.

## Test data

`tests/fixtures/gopro13_ride_gpmd.mov`: the first 3 s of GoPro metadata from "ride-recap sample ride — 2026-07-10"
by Ian Macomber, CC BY 4.0. Details in `tests/fixtures/LICENSE.md`.

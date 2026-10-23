# Third-party notices

LeanCut's own code is MIT-licensed (see [LICENSE](LICENSE)). The packaged apps also contain, or download at runtime,
the following components under their own licenses.

## Bundled in the desktop apps

| Component | License | Source |
|---|---|---|
| FFmpeg 7.1 (via `imageio-ffmpeg`) | **GPL v2 or later** for the bundled build (`--enable-gpl`, includes x264/x265) | https://ffmpeg.org/download.html · build scripts: https://github.com/imageio/imageio-binaries |
| faster-whisper | MIT | https://github.com/SYSTRAN/faster-whisper |
| CTranslate2 | MIT | https://github.com/OpenNMT/CTranslate2 |
| Silero VAD model (shipped inside faster-whisper) | MIT | https://github.com/snakers4/silero-vad |
| ONNX Runtime | MIT | https://github.com/microsoft/onnxruntime |
| PyAV / FFmpeg libraries used by faster-whisper | BSD-3 / LGPL | https://github.com/PyAV-Org/PyAV |
| NumPy | BSD-3 | https://github.com/numpy/numpy |
| FastAPI, Starlette, Pydantic | MIT | https://github.com/fastapi/fastapi |
| Uvicorn | BSD-3 | https://github.com/encode/uvicorn |
| pywebview | BSD-3 | https://github.com/r0x0r/pywebview |
| pythonnet (Windows) | MIT | https://github.com/pythonnet/pythonnet |
| Hugging Face Hub client, tokenizers | Apache-2.0 | https://github.com/huggingface |
| Python | PSF License | https://www.python.org |

**FFmpeg / GPL:** the FFmpeg executable is a separate program that LeanCut runs as a subprocess. Its source code is
available from the links above; on request we will point you to the exact FFmpeg version shipped in any LeanCut
release (open an issue). If you redistribute LeanCut's packaged apps, you take on the GPL obligations for the bundled
FFmpeg binary.

## Downloaded at runtime

| Component | License | Notes |
|---|---|---|
| Whisper models (OpenAI), CTranslate2 conversions by Systran / Mobius Labs | MIT | Downloaded from Hugging Face on first use |
| Language models you pull into Ollama or LM Studio | Each model's own license | e.g. Qwen 2.5 (Apache-2.0 for most sizes), Llama 3.x (Llama Community License), Gemma (Gemma Terms). Check the license of the model you use. |

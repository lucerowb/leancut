# LeanCut — local AI video editor

Turns hours of raw moto-vlog / daily-vlog footage into a tight YouTube cut, using only AI that runs on your machine.
Named after the lean angle through a corner, and a lean, tight edit.

1. **Listen** — FFmpeg extracts 16 kHz mono audio (high-pass filter for wind rumble); Silero VAD drops silence, wind and engine noise.
2. **Transcribe** — faster-whisper produces timecoded sentences (GPU when available, automatic CPU fallback). Short fragments are merged into whole thoughts.
3. **Judge** — a local LLM (Ollama or LM Studio) scores every segment 1–5 with a reason; LeanCut's code then keeps the best segments up to your target length. Timestamps always come from Whisper, never the LLM.
4. **Review** — timeline + transcript with scores; tick/untick any line, click to preview.
5. **Cut** — `final_youtube_cut.mp4` plus a CMX3600 `final_youtube_cut.edl` for DaVinci Resolve / Premiere Pro.

Full user guide: [`site/docs.html`](site/docs.html). Landing page: [`site/index.html`](site/index.html).

## Desktop app (macOS & Windows)

Download `LeanCut-macOS.dmg` or `LeanCut-Windows.zip` from Releases (built by CI on every `v*` tag), or build it yourself:

```bash
python3.12 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt -r requirements-build.txt
python build.py        # macOS: dist/LeanCut.app + dist/LeanCut-macOS.dmg · Windows: dist/LeanCut/ + dist/LeanCut-Windows.zip
```

PyInstaller can't cross-compile; `.github/workflows/build.yml` builds both OSes on GitHub Actions and attaches them to the release.
Builds are unsigned: first launch needs right-click → Open on macOS, or More info → Run anyway on Windows.

## Run from source

Requires **Python 3.10 – 3.12** (faster-whisper's CTranslate2/onnxruntime wheels lag behind newer Pythons).

```bash
pip install -r requirements.txt -r requirements-build.txt
python desktop.py      # native window
python app.py          # or browser UI at http://127.0.0.1:8765
python -m pytest -q tests
```

You also need a local LLM server: [Ollama](https://ollama.com) (`ollama pull qwen2.5:7b`) or LM Studio (load a model → Developer → Start Server).
Pick a model whose size is under ~75% of your RAM — LeanCut refuses bigger Ollama models because they swap and stall.

## Project layout

```
desktop.py         native window (pywebview) around the backend; PyInstaller entry point
app.py             FastAPI server, job runner, pipeline orchestration, project state
transcriber.py     faster-whisper + VAD, GPU→CPU fallback, sentence split + fragment merge
llm_agent.py       Ollama / OpenAI-compatible streaming client, memory check, scoring prompt, budget selection
video_engine.py    FFmpeg probe, audio extraction, cut/concat render, CMX3600 EDL
static/            app UI (HTML/JS/CSS, no build step)
site/              landing page + docs (static, host anywhere e.g. GitHub Pages)
assets/            app icon (assets/make_icon.py draws it)
build.py           PyInstaller packaging (+ .dmg / .zip)
tests/test_core.py unit tests
```

Data lives in `workspace/` when running from source, and in `~/Library/Application Support/LeanCut` (macOS) or
`%APPDATA%\LeanCut` (Windows) in the packaged app. `LEANCUT_HOME` overrides both. The desktop app logs to `leancut.log` there.

## Known limits

- Only talking is kept; silent scenic B-roll is dropped (top roadmap item).
- Whisper is weak on some languages (e.g. Nepali): set *Spoken language* to English for a rough English transcript the LLM can judge.
- EDL is non-drop-frame (29.97 fps written as 30), so it can drift ~1 frame per minute on 29.97 material.
- Unsigned builds; Windows build is produced by CI and not yet tested on a real Windows machine.

## Roadmap

See [`site/docs.html#roadmap`](site/docs.html#roadmap): subtitles, −14 LUFS loudness, hardware encoding, cancel/resume, scenic B-roll keeper,
wind-noise reduction, Shorts, chapters/metadata, FCPXML/OTIO, multi-cam sync, music ducking, language-tuned Whisper, signed auto-updating builds.

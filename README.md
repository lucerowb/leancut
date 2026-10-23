<div align="center">

<img src="assets/icon.png" width="96" alt="LeanCut icon">

# LeanCut

**Turn hours of moto-vlog and daily-vlog footage into a tight YouTube cut, with AI that runs entirely on your computer.**

[![CI](https://github.com/lucerowb/leancut/actions/workflows/ci.yml/badge.svg)](https://github.com/lucerowb/leancut/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-orange.svg)](LICENSE)
![Platforms](https://img.shields.io/badge/macOS%20·%20Windows%20·%20Linux-informational)

[Website](https://leancut.lucerowb.com/) · [User guide](https://leancut.lucerowb.com/guide.html) · [Download](https://github.com/lucerowb/leancut/releases/latest) · [Changelog](CHANGELOG.md) · [Architecture](ARCHITECTURE.md)

<img src="docs/assets/screens/editor.png" width="820" alt="LeanCut editor: program monitor, AI assistant, programme timeline and inspector">

</div>

---

LeanCut listens to your footage, transcribes it, lets a language model on your own machine score every line, keeps
the best of it (plus the best silent scenery) up to the length you want, and renders the cut. Then you finish it in the
editor, or just tell the assistant what to change. Your footage, audio and transcripts never leave your computer, and
there's no account, subscription or upload.

Named after the lean angle through a corner, and a lean, tight edit.

## Features

- **AI edit, fully local.** faster-whisper transcribes; Ollama or LM Studio scores every line 1–5 with a reason; LeanCut's code keeps the best lines up to your target length. Timestamps always come from Whisper, never the LLM.
- **AI assistant and suggestions.** Type "make it about 8 minutes" or "slow down the biggest reaction" and it becomes checked, undoable edit actions. Per-part suggestions (rules plus the model's own ideas) can be applied or dismissed one by one.
- **An editor with instant preview.** Program monitor, a timeline with Picture, Titles and Voice tracks, an Inspector for the selected part, keyboard shortcuts, and undo/redo for every edit. **Render preview** makes an exact 540p render.
- **Looks.** 8 effects (punch-in, push-in, slow motion, speed-up, black & white, vignette, glow, stabilise), 15 transitions including J-cuts and crossfades, 9 colour presets, auto colour, manual sliders, LUTs, titles, intro/outro cards and burned-in captions.
- **Voice-overs in your own voice**, cloned locally from a stretch of your own speech (optional install; consent required).
- **Decision engine.** Scores come from the model's token probabilities instead of generated JSON, with a confidence per line; unsure lines become "your call" suggestions. It learns from the lines you keep or cut yourself.
- **Built for rides:**
  - **Scenic B-roll** from silent stretches, scored for movement, colour and exposure, optionally rated by a local vision model.
  - **Say "clip that"** to keep the 30 seconds before it. Loud moments (shouts, laughs, revs) lift a line's score.
  - **GoPro telemetry**: hard braking, twisties, top speed and summits lift what was said there; motorways and stops drop out.
  - **Camera chapters** (GoPro, timecode-continuous files) join into one clip automatically.
  - **Stabilisation** with vid.stab, or swap in a Gyroflow export. **Ride-through fast-forwards** over long dropped stretches.
- **Clean sound.** −14 LUFS loudness, neural voice enhancement (DeepFilterNet3), wind and road-noise reduction, a music bed that ducks under speech, profanity bleep, and click-free cuts.
- **Privacy blur** for faces and licence plates, and **footage search** by what's on screen (on-device CLIP).
- **Publish-ready output:** the MP4, subtitles (`.srt`), a YouTube title, description, tags and valid chapters, six thumbnail picks, and vertical Shorts that follow the subject.
- **Finish in your editor.** EDL (Resolve, Premiere), FCPXML (Final Cut, Resolve) and OpenTimelineIO, on each camera's own timecode (drop-frame aware).
- **Fast and resumable.** Hardware encoding (VideoToolbox, NVENC, Quick Sync, AMF) with GPU decoding; a re-export reuses finished parts (28 s instead of 9 min in our 4K test).
- **Projects, an overnight batch queue and second-camera sync** by audio.
- **In-app updates.** One anonymous request to GitHub tells you when a new version is out; install it from the banner. Turn the check off in Settings › Updates.
- **No analytics, ads or accounts.** See the [privacy policy](https://leancut.lucerowb.com/privacy.html).

## Download

| Platform | File | Notes |
|---|---|---|
| macOS (Apple Silicon) | `LeanCut-macOS.dmg` | Drag LeanCut to Applications. Builds are unsigned for now: the first time, right-click → **Open**. |
| Windows 10/11 (64-bit) | `LeanCut-Windows.zip` | Extract and run `LeanCut.exe`. SmartScreen: **More info → Run anyway**. Needs the WebView2 runtime (built into Windows 11). |
| Linux (x64) | `LeanCut-Linux-x64.tar.gz` | Extract and run `./LeanCut/install.sh` (installs for your user, no root). Ubuntu 22.04, Debian 12 or newer. |

Get them from [Releases](https://github.com/lucerowb/leancut/releases/latest). You also need a local language-model
server: [Ollama](https://ollama.com) (`ollama pull qwen2.5:7b`) or LM Studio (load a model → Developer → Start Server).
Pick a model that fits your memory; the [guide](https://leancut.lucerowb.com/guide.html#models) has a table.
Voice cloning isn't in the packaged apps (it needs PyTorch); see [Voice-overs](https://leancut.lucerowb.com/guide.html#voice).

## Screenshots

<p>
  <img src="docs/assets/screens/settings.png" width="400" alt="Settings: updates, AI models, export destination and formats, video, audio, voice, rider AI">
  <img src="docs/assets/screens/projects.png" width="400" alt="Projects and the overnight batch">
</p>

## Run from source

Requires **Python 3.10–3.12** (faster-whisper's CTranslate2 and onnxruntime wheels lag behind newer Pythons).

```bash
python3.12 -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt -r requirements-build.txt -r requirements-dev.txt
python desktop.py          # native window
python app.py              # or the browser UI at http://127.0.0.1:8765
python -m pytest -q tests  # tests (no models or network needed)
python build.py            # package: .app + .dmg on macOS, .zip on Windows, .tar.gz on Linux

pip install -r requirements-voice.txt   # optional: local voice cloning (PyTorch + Chatterbox)
```

On Linux the native window needs `pip install "pywebview[qt]"`. Data lives in `workspace/` when running from source,
and in `~/Library/Application Support/LeanCut` (macOS), `%APPDATA%\LeanCut` (Windows) or `~/.local/share/LeanCut`
(Linux) in the packaged app. `LEANCUT_HOME` overrides all of them.

## How it works

1. **Listen:** FFmpeg extracts 16 kHz mono audio; Silero VAD drops silence, wind and engine noise.
2. **Transcribe:** faster-whisper (`large-v3-turbo` by default) writes timecoded sentences; GPU when available, CPU fallback.
3. **Judge:** retakes, markers, loud moments and telemetry are found in code; the local LLM scores the rest from its token probabilities; `select()` fits the target length.
4. **Edit:** every change, from the UI, a suggestion or the assistant, is a validated action on the edit, with undo.
5. **Cut:** each part is rendered with its look baked in (cached, cancellable), joined with its transitions, finished to −14 LUFS, and exported with edit lists, subtitles and YouTube text.

Details, including the security model and timecode handling, are in [ARCHITECTURE.md](ARCHITECTURE.md).

## Project layout

```
app.py              API, projects, settings, jobs, analyze/export pipelines, undo, chat
editing.py          the edit model: parts, looks, the action vocabulary, rule-based suggestions
production.py       turns an edit into render specs: filters, titles, intro/outro cards, transitions, bleeps
video_engine.py     FFmpeg: probe, extract, cached two-stage render, encoders, audio finishing, vid.stab
llm_agent.py        LLM client, decision engine, batch scoring, assistant, suggestions, visual judge, YouTube text
transcriber.py      Whisper, sentences, fragments, retakes, markers
media_analysis.py   B-roll, loud moments, audio sync, thumbnails, Shorts windows, ride-throughs
exporters.py        timecode, EDL, FCPXML, OTIO, SRT, chapters, YouTube text
vision.py           optional: face/plate blur, CLIP search, subject tracking (ONNX, CPU)
enhance.py          optional: DeepFilterNet3 voice enhancement (ONNX, CPU)
voice.py            optional: voice-overs (Chatterbox, a voice server, or the system voice)
voice_server.py     OpenAI-compatible voice-cloning server for the packaged apps
telemetry.py        optional: GoPro GPMF telemetry highlights
updates.py          GitHub release check and in-app install
desktop.py          native window, file pickers, PyInstaller entry point
build.py            PyInstaller packaging; packaging/linux/ holds the Linux installer
static/             UI (no build step) · docs/ website · tests/ unit tests
```

## FAQ

**Can LeanCut use ScaleDown's CDMs or TypeSafe's Jev?** Not today. ScaleDown's CDMs and Jev are hosted, closed-weight
APIs with no free local version, and VideoJev (Apache-2.0, open weights) needs an NVIDIA GPU with CUDA 12.8 and
FlashAttention and has no serving support, so none of them run on a typical Mac or PC. LeanCut implements the ideas
locally instead: schema-constrained output (like CDMs), decisions read from token probabilities (like Jev/OpenJev),
and a local vision model for visual judgement (a lighter stand-in for VideoJev).

## Contributing

Issues and pull requests are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md). Please report security problems
privately, as described in [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE). The packaged apps bundle FFmpeg (GPL v2 or later) and other open-source components; see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

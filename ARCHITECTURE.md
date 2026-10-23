# Architecture

## Overview

LeanCut is a local web app wrapped in a native window. A FastAPI server runs inside the desktop process and serves a
plain HTML/JS interface; [pywebview](https://pywebview.flowrl.com) shows it with the system web view (WebKit on macOS,
WebView2 on Windows). Every heavy step is a subprocess or a library call on the same machine: FFmpeg for media,
faster-whisper for speech, and a local LLM server (Ollama or LM Studio) for judgement.

```
┌──────────── desktop.py (pywebview window) ────────────┐
│  static/ UI  ──HTTP 127.0.0.1──►  app.py (FastAPI)    │
│                                    │                  │
│     transcriber.py ◄───────────────┤  background job  │
│     llm_agent.py   ──► Ollama / LM Studio (localhost) │
│     media_analysis.py              │                  │
│     video_engine.py ──► ffmpeg (subprocess)           │
│     exporters.py                   │                  │
│     updates.py ──► api.github.com (optional, anon.)   │
└───────────────────────────────────────────────────────┘
```

## Tech stack

| Concern | Choice | Why |
|---|---|---|
| Window | pywebview | Native window around the existing web UI; no Electron runtime to ship |
| Server | FastAPI + uvicorn | Small, typed, runs in-process |
| Media | FFmpeg via `imageio-ffmpeg` | One bundled binary on every OS; probing parses `ffmpeg -i`, so no ffprobe |
| Speech | faster-whisper (CTranslate2) + Silero VAD | Fast on CPU, GPU when available, VAD built in |
| Judgement | Any Ollama / OpenAI-compatible model | User's choice; nothing leaves the machine by default |
| Packaging | PyInstaller | `.app`/`.dmg` on macOS, folder/`.zip` on Windows |

## Pipeline

**Analyze** (`app.analyze`):

1. Fail fast: check the LLM server is up, the model exists and fits in memory (`llm_agent.check_fits`).
2. Extract 16 kHz mono audio per clip (`video_engine.extract_audio`, high-pass at 120 Hz).
3. Transcribe with VAD and word timestamps; split into sentences (`transcriber.split_sentences`). Cached per clip,
   keyed by model + language.
4. Clean up: merge fragments into thoughts ≤ 20 s (`merge_fragments`), collapse Whisper repeat loops and flag
   retakes (`flag_retakes`), find spoken markers (`find_markers`).
5. Loud moments: loudness envelope vs. a ±10 s local median (`media_analysis.loud_moments`).
6. Score: lines already decided (cache, retakes, markers) are passed as `known`; the rest go to the LLM in ~6 000
   character chunks, streamed with a 300 s inactivity timeout (`llm_agent.decide`). Loud moments add +1.
7. Select: `llm_agent.select` turns scores into a keep set that fits the target length. The LLM never chooses
   timestamps and never enforces the budget; code does both.
8. B-roll: keyframe-only scan (`-skip_frame nokey`, `signalstats`, scene score) → 5 s windows in silent gaps, ranked
   per clip and picked globally until the B-roll share is used (`broll_candidates`, `pick_broll`).

**Export** (`app.export`):

1. `cut_entries` merges kept lines and B-roll per clip into padded ranges, plus optional fast-forward connectors.
2. `video_engine.render` cuts each range (GPU decode, hardware or x264 encode, 30 ms audio fades), caching every part
   under `cache/parts/<hash>.mp4`, then concatenates losslessly. Cancelling kills the running FFmpeg process; the next
   export reuses finished parts.
3. `video_engine.finish_audio`: optional denoise → optional music bed with sidechain ducking → two-pass `loudnorm`
   to −14 LUFS. Video is stream-copied.
4. `exporters.timeline` places every range on the output timeline; EDL, FCPXML, OTIO, SRT, chapters and the YouTube
   text are all derived from it. Thumbnails and Shorts come from `media_analysis`.

## Timecode

Cameras embed a start timecode (DJI writes time of day, drop-frame at 29.97). NLEs place media by it, so every
source time in EDL/FCPXML/OTIO is `clip start timecode + offset`, computed in frames with exact rational rates
(`30000/1001`). Drop-frame conversion follows SMPTE 12M (`exporters.frames_to_tc` / `tc_to_frames`).

## Security model

- The server binds to `127.0.0.1` only.
- A middleware rejects any request whose `Host` isn't `127.0.0.1`/`localhost`/`[::1]` or whose `Origin` is another
  site, which blocks DNS rebinding and cross-site requests from web pages. There is no login: local programs running
  as the same user can reach it, as they can reach the user's files.
- `/api/open-url` only opens `https://github.com/lucerowb/leancut/…` links.
- Clips added by path are referenced, never moved; removing a clip only deletes copies LeanCut made.
- Outgoing traffic: Whisper downloads (Hugging Face; cached models load offline), the configured LLM server, and the
  optional anonymous update check. See the [privacy policy](https://lucerowb.github.io/leancut/privacy.html).

## Storage

```
<data folder>/                     ~/Library/Application Support/LeanCut, %APPDATA%\LeanCut, or ./workspace from source
  settings.json                    app-wide settings, current project
  projects/<id>/project.json       clips, transcripts, scores, B-roll, target length
  projects/<id>/output/            default export folder
  projects/.trash-<id>/            removed projects (moved, not deleted)
  cache/<clip>.wav                 16 kHz audio for Whisper, moments and sync
  cache/<clip>.keyframes8.json     keyframe measurements for B-roll
  cache/parts/<hash>.mp4           rendered parts, reused across exports
  media/<id>/<name>                clips that were uploaded/dragged in (copies)
  leancut.log                      packaged app log
```

All JSON is written atomically (temp file + rename). v0.1's single `project.json` is migrated on first start.

## Project layout

```
desktop.py          native window, file/folder pickers, PyInstaller entry point
app.py              API, projects, settings, jobs, analyze/export pipelines, migration
transcriber.py      Whisper, sentences, fragments, retakes, markers
llm_agent.py        LLM client, memory guard, scoring prompt, selection, YouTube metadata
media_analysis.py   B-roll, loud moments, audio sync, thumbnails, Shorts, connectors
video_engine.py     FFmpeg wrapper: probe, extract, render, encoders, audio finishing, frame grabs
exporters.py        timecode, EDL, FCPXML, OTIO, SRT, chapters, YouTube text
updates.py          GitHub release check
static/             UI (no build step)
docs/               website (GitHub Pages)
tests/test_core.py  unit tests (no network, no models needed)
build.py            PyInstaller packaging
```

## Known limitations

- B-roll and loud moments are heuristics (motion/colour/exposure, loudness), not trained classifiers.
- Shorts use a centre crop; there is no subject tracking yet.
- Ride-through fast-forwards exist only in the rendered MP4, not in the edit lists.
- Mixed frame rates are normalised to the first clip's rate in the MP4; edit lists use the first clip's rate as the
  sequence rate.
- One job runs at a time.

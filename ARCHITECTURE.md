# Architecture

## Overview

LeanCut is a local web app wrapped in a native window. A FastAPI server runs inside the desktop process and serves a
plain HTML/JS interface; [pywebview](https://pywebview.flowrl.com) shows it with the system web view (WebKit on macOS,
WebView2 on Windows, Qt WebEngine on Linux). Every heavy step is a subprocess or a library call on the same machine:
FFmpeg for media, faster-whisper for speech, onnxruntime for the small vision and audio models, and a local LLM server
(Ollama or LM Studio) for judgement and editing.

```
┌──────────────── desktop.py (pywebview window) ────────────────┐
│  static/ UI  ──HTTP 127.0.0.1──►  app.py (FastAPI)            │
│                                    │  one background job      │
│     transcriber.py ◄───────────────┤                          │
│     llm_agent.py   ──► Ollama / LM Studio (localhost)         │
│     editing.py  (edit model, actions, undo snapshots)         │
│     production.py ─► video_engine.py ──► ffmpeg (subprocess)  │
│     media_analysis.py · exporters.py                          │
│     vision.py · enhance.py · telemetry.py · voice.py  (lazy)  │
│        └─► models/ (downloaded once: Hugging Face, GitHub)    │
│     updates.py ──► api.github.com, release assets (optional)  │
└───────────────────────────────────────────────────────────────┘
```

## Tech stack

| Concern | Choice | Why |
|---|---|---|
| Window | pywebview | Native window around the web UI; no Electron runtime to ship. Qt backend on Linux |
| Server | FastAPI + uvicorn | Small, typed, runs in-process |
| Media | FFmpeg via `imageio-ffmpeg` | One bundled GPL build per OS (x264, x265, freetype for titles, vid.stab), preferred over a system ffmpeg so features match everywhere (`LEANCUT_FFMPEG` overrides); probing parses `ffmpeg -i`, so no ffprobe |
| Speech | faster-whisper (CTranslate2) + Silero VAD | Fast on CPU, GPU when available, VAD built in |
| Judgement and editing | Any Ollama / OpenAI-compatible model | User's choice; nothing leaves the machine by default |
| Vision, enhancement | ONNX models on onnxruntime (`onnx_runtime.session`: CUDA, DirectML in the Windows build, CoreML where measured faster, else CPU, with a CPU fallback; `LEANCUT_ORT_PROVIDER` overrides) | Small downloads, no PyTorch in the packaged app |
| Voice cloning | Chatterbox-Turbo (PyTorch), optional | Too big to bundle; installed from source or run as a separate server |
| Packaging | PyInstaller | `.app`/`.dmg` on macOS, folder/`.zip` on Windows, folder/`.tar.gz` on Linux |

## Pipeline

**Analyze** (`app.analyze`):

1. Fail fast: check the LLM server is up, the model exists and fits in memory (`llm_agent.check_fits`).
2. Extract 16 kHz mono audio per clip (`video_engine.extract_audio`, high-pass at 120 Hz).
3. Transcribe with VAD and word timestamps; split into sentences (`transcriber.split_sentences`). Cached per clip,
   keyed by model + language.
4. Measure shots: keyframe stats give B-roll interest, per-clip auto-colour corrections (`editing.auto_grade_params`)
   and a shake estimate. GoPro clips with a GPMF track are read for telemetry highlights and boring stretches.
5. Clean up: merge fragments into thoughts ≤ 20 s (`merge_fragments`), collapse Whisper repeat loops and flag
   retakes (`flag_retakes`), find spoken markers (`find_markers`).
6. Loud moments (`media_analysis.loud_moments`) and telemetry highlights add +1 to the lines they overlap.
7. Score: lines already decided (cache, retakes, markers) are passed as `known`; the rest go to the decision engine,
   or to the batch scorer when the server can't return token probabilities (see AI engines).
8. Select: `llm_agent.select` turns scores into a keep set that fits the target length. The LLM never chooses
   timestamps and never enforces the budget; code does both.
9. B-roll: 5 s windows in silent gaps, ranked by interest (telemetry highlights +0.3, stopped and motorway stretches
   0), optionally re-rated by the visual judge, and picked globally until the B-roll share is used.
10. Placement (`editing.place_broll`): a kept window more than 75 s from any kept line of its clip shows another
    place and time, so it's unticked; the rest are laid over the nearest long-enough kept line of their scene as a
    cutaway (`over` + `offset`, one per line) or, with style "insert", kept as their own part in source order.
    Clips are added in recording order (`app.recorded_at`: a time stamp in the file name, else the creation time).
11. Suggestions: rule-based ones (`editing.heuristic_suggestions`) plus the LLM's, minus duplicates and no-ops.

**Edit.** The edit is the project itself: transcript lines and B-roll windows, addressed as `c<clip>.s<n>` and
`c<clip>.b<n>`, each with `keep`, `start`/`end`, and optionally `effect`, `transition` (into it), `grade`, `texts`
(with `x`/`y`/`size` when placed on the monitor), `voiceovers`, `graphics` (pages in the project's `graphics/`
folder) and, for B-roll, `over` (the line it's laid over). `split` and `cut_words` take a range out of an item: the
piece after the cut is appended as a new item, so ids never shift, and the cut edges are hard (`cut_in`/`cut_out`:
no padding, no merging back). Project-wide: `grade` (auto, preset, manual, LUT), `cut_style`, `intro`, `outro`, `captions`,
`privacy_blur`, `broll_share`, `music_volume`.

Every change goes through `editing.apply_action`, whatever its source: the Inspector and Look panel, a suggestion, or
the assistant. `ACTION_TYPES` is the whole vocabulary; each action is validated (known ids, value ranges, existing
LUT files) and applied, and invalid actions are skipped with a reason instead of failing the batch. Before applying,
`app.apply_edit` takes an `editing.snapshot` of the undoable state; undo and redo swap snapshots (40 steps, per
project, in memory only). Keep/cut changes made by hand are appended to `taste.json`.

**Export** (`app.export`, `app.render_program`):

1. `editing.build_parts` turns kept items into ordered render parts. Adjacent kept items of a clip merge (0.15 s
   padding, gaps under 0.6 s) unless their look differs, the later one has its own transition, or a split / word
   cut separates them; padding never replays frames the previous part showed. Cutaway B-roll rides on the part of
   its line as an overlay. Optional ride-through fast-forwards are inserted between parts, and
   `app.spread_graphics` gives every part the slice of each graphic on screen during it, so graphics run on across
   cuts.
2. `production.part_spec` builds each part's filter chain, in order: privacy blur → stabilisation (vid.stab pass 2)
   → speed → scale/pad → grade (curves, colour balance, temperature, eq, LUT, vignette) → effect → overlays
   (cutaway B-roll with a short fade; motion graphics as `.mov` with alpha) → titles → burned-in captions → fps. Intro and outro are `card_spec`s: a title over darkened, softened footage.
3. `video_engine.render_parts` renders in two stages. Stage 1 renders every part to its own file under
   `cache/parts/` (`cache/preview-parts/` for the 540p preview), keyed by source, range, filters and codec, so an
   unchanged part is never rendered twice. Stage 2 joins them: a lossless concat when every cut is a hard cut,
   otherwise one pass chaining `xfade`/`acrossfade`, with J-cuts made from a short pre-roll of the next part's audio
   crossfaded under the previous picture.
4. `video_engine.finish_audio`: optional neural enhancement (replaces the programme audio), denoise, bleeps,
   voice-overs mixed in with the programme ducked under them, a music bed with sidechain ducking, then two-pass
   `loudnorm` to −14 LUFS. Video is stream-copied.
5. `app.nle_entries` turns the parts into edit-list entries that carry the transition into each, effect, speed,
   grade, titles and cutaways; joined camera chapters are split back into their files and intro/outro cards are
   added (`app.expand_chapters`). EDL (dissolves, wipes, `M2` speed, the rest as comments), FCPXML (transitions,
   Basic Title clips, `timeMap`, connected B-roll) and OTIO (transitions, time warps, an overlay track, titles as
   markers) are written from them; SRT, chapters and the YouTube text from the programme timeline.
   Thumbnails and Shorts come from `media_analysis`; Shorts are cropped along `vision.subject_track`.

Fast mode (stream copy through `video_engine.render`) is only used when nothing needs baking in.

**Working folder.** Titles, fonts, LUTs and vid.stab data are written to `cache/render/` (`texts/`, `fonts/`,
`luts/`, `trf/`) and FFmpeg runs with that folder as its working directory, so filter strings only ever contain
short relative paths: no escaping of Windows drive letters, quotes or colons in user file names.

## Agents and motion graphics

**Tools.** `agent_tools.py` defines the tools an agent edits with: `get_edit`, `apply_actions` (the same validated
vocabulary as the assistant), `undo`/`redo`, `look` (one frame of the finished video, rendered through the export's
own filters by `video_engine.still_of`, with graphics composited), `look_sheet`, `list_templates`, `add_graphic`,
`update_graphic`, `write_page`, `read_page`, `check_page`, `get_telemetry`, `search_footage`, `job_status`,
`render_preview` and `export`. Results are MCP-shaped (`{content: [text | image], isError}`); a failing tool returns
its reason instead of raising. The app serves them at `GET /api/agent/tools`, `POST /api/agent/call` and the manual at
`GET /api/agent/manual`.

**Built-in agent.** `llm_agent.agent_loop` runs a tool-calling conversation on the user's Ollama (`/api/chat` with
`tools`, streamed) or OpenAI-compatible server, up to 16 steps, as a cancellable job whose steps stream into the chat.
`llm_agent.capabilities` reads Ollama's `/api/show`: a model without `tools` is refused with a clear message; a model
without `vision` gets the visual judge's description of each `look` frame instead of the picture.

**Coding agents.** `mcp_server.py` is a stdio MCP server (stdlib only) that proxies the tools to the running app,
found through `server.json` in the data folder (written by `app.announce`, removed on exit) or `LEANCUT_URL`. The
packaged app runs it as `LeanCut --mcp` and the CLI as `LeanCut --cli …` (dispatched in `desktop.py` before anything
else loads). `leancut_cli.connect_snippets` writes each agent's config. `app.run_coding_agent` starts Codex
(`codex exec`, MCP passed with `-c`; `--oss` with a local gpt-oss model), Claude Code (`claude -p --mcp-config`),
Gemini CLI or OpenCode in the project folder and streams their output into the chat.

**Motion graphics.** A graphic is a page in the project's `graphics/` folder following OpenFilm's contract:
`window.film = {frame(t), duration?, ready?}`, the same `t` always drawing the same picture. `graphics.py` drives an
installed Chrome / Edge / Brave / Chromium (or a downloaded Chrome for Testing headless shell) over the DevTools
protocol: `render_clip` captures frames with a transparent background and encodes a qtrle `.mov` with alpha, cached
by the page's content; `still` draws one frame; `check` reports page errors, failed fonts, text off the frame and
non-deterministic frames (two shuffled passes compared). Templates in `graphics_templates/` are filled with
HTML-escaped fields (and JSON data for the telemetry ones). In the editor, pages run in sandboxed iframes
(`sandbox="allow-scripts"`, no same origin, so a page can't call LeanCut's API) driven by `static/graphics-host.js`
over `postMessage`.

## AI engines

- **Decision engine** (`llm_agent.decide_probs`, the default). For each line it asks two one-token questions, "score
  it 1–5" and "which reason fits" (11 fixed labels), with `num_predict: 1` and `logprobs`, and reads the probability
  of each answer token. The score is the probability-weighted mean of ★1–★5 and the confidence is the share of the
  top digit. The transcript chunk sits in the system prompt, so the server's prompt cache reuses it and each further
  question costs about 0.2 s. Lines under 55% confidence become "your call" suggestions. The creator's recent
  keep/cut overrides (`taste.json`, up to 8 of each) are included as examples. If the server returns no logprobs,
  `engine: "auto"` falls back to the batch scorer.
- **Batch scorer** (`llm_agent.decide`, as in v0.2): chunks of about 6 000 characters, a JSON list of scores per
  chunk, streamed with a 300 s inactivity timeout, two attempts, then neutral scores so nothing is lost.
- **Constrained output.** `llm_agent.chat(schema=…)` passes a JSON schema as Ollama's `format` or as LM Studio's
  `response_format: json_schema`, so the server can only produce matching JSON. The assistant and suggestions are
  constrained to `editing.ACTION_SCHEMA` (action types as an enum); the visual judge's description to a one-field
  schema. Batch scoring and the YouTube text use plain JSON mode with a tolerant parser.
- **Assistant** (`llm_agent.assistant`): system prompt = `ASSISTANT_PROMPT` + `editing.context` (one line per part:
  id, source time, keep/cut, score, effects, text) + the last 6 chat turns. It returns `{reply, actions}`; the actions
  are applied as one undo step.
- **Suggestions** (`llm_agent.suggest`): the same context and vocabulary, up to 10 suggestions with one action each.
  `app.make_suggestions` merges them with the rule-based ones and drops duplicates and no-ops (keeping a kept line,
  an effect already applied, …).
- **Visual judge** (`llm_agent.look_at`), only when a vision model is set: the middle frame of each of the top 16
  B-roll candidates gets a 12-word description (schema-constrained) and a 1–5 scenic rating from token
  probabilities, blended 50/50 into the window's interest.

## Optional modules

`telemetry`, `vision`, `enhance` and `voice` are imported lazily through `app._optional`. If a module or its
dependencies are missing, the feature logs a clear message and the rest of the pipeline carries on. Their models are
downloaded on first use into `<data>/models` and are never bundled:

| Module | Model | Source | Size |
|---|---|---|---|
| `vision` | YuNet face detector, LPD-YuNet plate detector | Hugging Face (`opencv/…`), pinned commits | 0.2 MB, 4 MB |
| `vision` | CLIP ViT-B/32 image and text encoders (int8 ONNX) + tokenizer | Hugging Face (`Xenova/clip-vit-base-patch32`), pinned commit | 85 + 62 + 2.2 MB |
| `enhance` | DeepFilterNet3 ONNX export | `raw.githubusercontent.com/Rikorose/DeepFilterNet`, pinned commit, SHA-256 checked | 8 MB |
| `voice` | Chatterbox-Turbo (only with `requirements-voice.txt`) | Hugging Face, through the `chatterbox-tts` package's own cache | ~3.8 GB |

Existing model files are used without touching the network. All ONNX models run on the CPU.

- **Privacy blur** samples two frames a second at 640 px on the long side, runs the detectors, merges detections
  into tracks with padded boxes, and blurs each box with `crop`+`boxblur`+`overlay` for its time range.
- **Footage search** embeds one keyframe every 2 s with CLIP, caches the index per file
  (`cache/<clip>.<hash>.clip.npz`) and ranks frames by cosine similarity to the text query.
- **Subject tracking** for Shorts uses the largest confident face, else a spectral-residual saliency centre, smoothed,
  as a piecewise-linear crop expression.
- **Voice** has three engines: Chatterbox in-process, any OpenAI-compatible `/audio/speech` server (`voice_server.py`
  is one), and the system voice. Outputs are cached in `cache/voice/` by text, engine and sample.
- **Telemetry** parses the GPMF stream (KLV records) for GPS5, and derives speed, lateral acceleration and altitude on
  a 0.1 s grid.

## Camera chapters

When a clip is added, `app.chapter_follows` checks it against the last main clip: same resolution and frame rate, and
either GoPro chapter naming (`GH01xxxx` → `GH02xxxx`) or a start timecode within 3 frames of where the previous file
ended. Joined files are read through an FFmpeg concat list (`cache/<clip>.ffconcat`, `video_engine.inp`), so the rest of
the pipeline sees one clip. `app.locate` maps a clip time back to the chapter file for frame grabs, previews and
detectors, and edit lists are split back at chapter boundaries so NLEs relink to the real files.

## Timecode

Cameras embed a start timecode (DJI writes time of day, drop-frame at 29.97). NLEs place media by it, so every
source time in EDL/FCPXML/OTIO is `clip start timecode + offset`, computed in frames with exact rational rates
(`30000/1001`). Drop-frame conversion follows SMPTE 12M (`exporters.frames_to_tc` / `tc_to_frames`).

## Security model

- The server binds to `127.0.0.1` only.
- A middleware rejects any request whose `Host` isn't `127.0.0.1`/`localhost`/`[::1]` or whose `Origin` is another
  site, which blocks DNS rebinding and cross-site requests from web pages. There is no login: local programs running
  as the same user can reach it, as they can reach the user's files. `voice_server.py` applies the same rule and also
  binds to `127.0.0.1` only.
- `/api/open-url` only opens `https://github.com/lucerowb/leancut/…` links.
- In-app updates only download from `https://github.com/lucerowb/leancut/…`; the only redirects followed are to
  GitHub's asset hosts (`objects.githubusercontent.com`, `release-assets.githubusercontent.com`). The download must
  match the size GitHub announces. The new version is staged next to the app and swapped in by a helper after the app
  exits; if the swap or launch fails, the old version is restored. An app folder that contains `settings.json` is
  never replaced.
- Model downloads are pinned to a commit; DeepFilterNet's archive is checked against a SHA-256 and only its known
  file names are extracted.
- LLM output is untrusted: it can only produce actions from the fixed vocabulary, which are validated before they
  touch the project. It never chooses timestamps for scoring, and never names files to read or write (a LUT path must
  be an existing `.cube`/`.3dl` file).
- Agents (built-in or connected over MCP) act only through the tools: edits are the validated actions above, and
  the files they can write are pages in the project's `graphics/` folder (names restricted, no path separators).
  Coding agents LeanCut starts run in the project folder with their own sandbox settings (Codex `workspace-write`;
  Claude Code limited to LeanCut's tools and file edits). The MCP server talks only to `127.0.0.1`.
- Graphics pages are untrusted code: in the editor they run in sandboxed iframes with an opaque origin, and the API
  rejects their `Origin: null`; for rendering they run in a separate headless browser profile that is deleted after.
- Clips added by path are referenced, never moved; removing a clip only deletes copies LeanCut made.
- Outgoing traffic: Whisper and optional model downloads, the configured LLM and voice servers, the optional
  update check and update download, and the headless Chrome download when no Chrome-type browser is installed. A
  coding agent you choose (Codex, Claude Code…) talks to its own service under its own terms. See the [privacy policy](https://leancut.lucerowb.com/privacy.html).

## Storage

```
<data folder>/                     ~/Library/Application Support/LeanCut, %APPDATA%\LeanCut, ~/.local/share/LeanCut,
                                   or ./workspace from source (LEANCUT_HOME overrides)
  settings.json                    app-wide settings, current project
  taste.json                       your last 200 keep/cut overrides (few-shot examples for scoring)
  projects/<id>/project.json       clips, transcripts, scores, B-roll, the edit, chat, suggestions
  projects/<id>/output/            default export folder
  projects/.trash-<id>/            removed projects (moved, not deleted)
  voices/<project>.wav             voice samples taken from your own speech
  models/                          optional models (vision, CLIP, DeepFilterNet3)
  cache/<clip>.wav                 16 kHz audio for Whisper, moments, sync and voice samples
  cache/<clip>.keyframes9.json     keyframe measurements (B-roll, auto colour, shake)
  cache/<clip>.ffconcat            concat list of a clip joined from camera chapters
  cache/<clip>.<hash>.clip.npz     footage-search index
  cache/parts/<hash>.mp4           rendered parts, reused across exports
  cache/preview-parts/             rendered 540p preview parts
  cache/preview/<project>.mp4      the last preview render
  cache/render/                    working folder: title texts, font, LUT copies, vid.stab data
  cache/voice/<hash>.wav           synthesised voice-over lines
  media/<id>/<name>                clips that were uploaded/dragged in (copies)
  leancut.log                      packaged app log
```

All JSON is written atomically (temp file + rename). Everything in `cache/` can be deleted; it is rebuilt when needed.
On Linux the installed app lives in `~/.local/share/LeanCut/app/`, inside the data folder; the in-app updater
replaces only that folder.

## Project layout

```
desktop.py          native window, file/folder/LUT/music pickers, PyInstaller entry point
app.py              API, projects, settings, jobs, analyze/export pipelines, undo, chat, migration
editing.py          the edit model: items, parts, looks (grade, effects, text), actions, context, rule suggestions
production.py       render specs: filter chains, intro/outro cards, transitions, bleep windows, Shorts crop
video_engine.py     FFmpeg wrapper: probe, extract, two-stage render, encoders, audio finishing, vid.stab, frames
llm_agent.py        LLM client, memory guard, decision engine, batch scoring, assistant, suggestions, visual judge
transcriber.py      Whisper, sentences, fragments, retakes, markers
media_analysis.py   B-roll, loud moments, audio sync, thumbnails, Shorts windows, connectors
exporters.py        timecode, EDL, FCPXML, OTIO, SRT, chapters, YouTube text
vision.py           face/plate detection and blur, CLIP search, subject tracking (optional)
enhance.py          DeepFilterNet3 speech enhancement in numpy + onnxruntime (optional)
voice.py            voice-over engines and voice-sample picking (optional)
voice_server.py     OpenAI-compatible TTS server around Chatterbox, run from source
telemetry.py        GoPro GPMF parser, ride highlights and boring stretches (optional)
updates.py          GitHub release check, download, staged install and restart
packaging/linux/    install.sh and the .desktop entry for the Linux tarball
static/             UI (no build step)
docs/               website (GitHub Pages)
tests/              unit tests (no network, no models needed)
build.py            PyInstaller packaging
```

## Known limitations

- B-roll and loud moments are heuristics (motion/colour/exposure, loudness); the visual judge only re-rates the top
  16 candidates.
- The instant preview approximates looks with CSS; only **Render preview** is exact. The preview render skips
  stabilisation and voice enhancement.
- Privacy blur misses faces smaller than about 10 px in its 640 px pass (about 60 px in 4K). The plate model is
  trained on Chinese plates and is weak on others. Thumbnails and the intro/outro backgrounds aren't blurred (the main video, the preview and Shorts are).
- Edit lists carry transitions, speed changes, titles, cards and cutaways, but not grades, LUTs, most effects,
  voice-overs, motion graphics or ride-throughs (FCPXML gets to-do markers for grades; EDL comments). The FCPXML
  effect identifiers haven't been checked in Final Cut itself.
- Profanity bleeps use Whisper word timings stored since v0.3; lines transcribed earlier spread their words evenly, so a bleep there can be slightly off the word.
- Telemetry matches GoPro's reference parser on HERO5–HERO13 files, but its riding thresholds (braking, twisties)
  haven't been checked on a real motorcycle ride: the only openly licensed GPS ride found is a bicycle.
- Local models of 7–14B are weaker agents than hosted ones: they pick the wrong line now and then, and write simpler
  animated pages. Codex's local mode needs OpenAI's open gpt-oss model.
- The instant preview shows motion graphics in a sandboxed frame of the app's window (WebKit on macOS); exports
  draw them in Chrome, the reference.
- Mixed frame rates are normalised to the first clip's rate in the MP4; edit lists use the first clip's rate as the
  sequence rate.
- Undo history is kept in memory and is lost when LeanCut closes.
- One job runs at a time.

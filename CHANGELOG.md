# Changelog

## 0.4.0

Agents that edit with you, animated graphics, a proper timeline, B-roll that follows the story, and text you place right on the video.

### Highlights

- **Agents edit with you.** Pick who edits in the assistant panel: the quick assistant, an **agent with tools** that runs on your own local model (Ollama or LM Studio, e.g. `qwen2.5:14b`), or a coding agent such as **Codex** or **Claude Code**. Agents read the edit, change it with LeanCut's validated actions (so every change can be undone), write animated graphics, and **look** at frames of the finished video, rendered exactly as the export will be, to check their work. A text-only local model gets its eyes from your vision model (e.g. `gemma3:4b`).
- **Connect any coding agent over MCP.** LeanCut runs an MCP server (`LeanCut --mcp`) with the same tools; Settings › Agents shows the line to paste for Claude Code, Codex, Cursor, OpenCode, Gemini CLI and Qwen Code, and `leancut connect … --write` sets it up for you. There's also a CLI (`leancut look 12.5`, `leancut call get_edit`). See CONNECT_AGENTS.md.
- **Animated graphics, inspired by OpenFilm.** A graphic is a web page that draws any moment `t` (the same page format as [OpenFilm](https://github.com/openfilm/openfilm), so pages work in both), laid over your footage on a new Graphics track and rendered frame-accurately with headless Chrome. Ten templates come with it: title, lower third, chapter, location pin, end screen, quote, time jump, stat card, and a **speedometer** and **route map** drawn from a GoPro's GPS. Agents can write new ones and check them: page errors, missing fonts, text off the frame, and whether the same `t` always draws the same picture. Graphics play live on the monitor and run on across cuts.

- **A timeline that works like an editor's.** Filmstrip thumbnails on the video track and a waveform on the audio track. Drag a part's edge to trim it (it snaps between words), drag the ruler to scrub, press S to split at the playhead, ⇧-click or ⌘-click to select several parts and change them all at once. ⌘-scroll or pinch zooms around the pointer; wheel scrolls. Tracks: B-roll, Video, Audio, Titles, Voice-over.
- **B-roll knows the order.** Clips are added in the order they were recorded (from the time in the file name, or the camera's creation time), and **Sort by time recorded** fixes an existing project. Scenic B-roll is only used near the talking it belongs to: a shot more than about a minute from anything you say shows another place and time, so it's left out instead of being dropped in at random.
- **B-roll over the talking.** By default B-roll is now laid *over* the nearest line from the same scene (a cutaway): the picture changes and your voice keeps going, the way an editor would cut it. Drag it on the B-roll track to move it, or make it a shot of its own. Settings › Rider AI › B-roll placement chooses the default.
- **Text right on the video.** Press **T+ Text** (or T) to add text at the playhead, drag it anywhere on the monitor, scroll over it to resize, double-click to type. Double-click a burned-in caption to correct it. Intro and outro titles can sit over the first and last shots instead of a separate card.
- **Cut by editing words.** In the Transcript tab, select words and press Delete: just those words are cut, with a clean jump cut and no padding that would bring them back.
- **Effects, transitions and titles in the edit lists.** EDL, FCPXML and OpenTimelineIO now carry dissolves, speed changes, titles, intro and outro cards and B-roll cutaways, not only the cuts.

### Also new

- **GoPro telemetry checked against real files.** The parser now matches GoPro's own reference parser exactly on HERO5 to HERO13 files. HERO11 and newer (GPS9) now have GPS at all, timing comes from the real packet times (no drift), Karma and HERO6+BLE timing is fixed, and poor GPS fixes are dropped.
- **In-app updates tested end to end** by a new CI workflow on Windows, Linux and macOS (download, install, swap, relaunch). Two Windows fixes: a non-ASCII install folder, or `[ ]` in its name, no longer breaks an update.
- **Nepali and Hindi transcription notes.** Tested on real Nepali footage: stock Whisper set to English invents much of what it writes, while a Nepali fine-tune (`Dragneel/whisper-medium-nepali-openslr-ct2`) writes real Nepali. The guide lists tested Nepali and Hindi models and `tools/convert_whisper.py` converts others. Devanagari now splits into words and sentences (।) correctly, so retakes and spoken markers work in Nepali and Hindi.
- **GPU for the vision models.** Face and plate blur, footage search and voice enhancement use the GPU where it helps: DirectML on Windows (any DirectX 12 GPU), CUDA on Linux from source (`onnxruntime-gpu`), and CoreML for the plate detector on Macs, where it was measured faster with identical detections. Any GPU failure falls back to the CPU.
- **Ready for signed builds.** SIGNING.md walks through Apple Developer ID notarization and Azure Artifact Signing for Windows; the release workflow uses them when the secrets exist, and every release now has a `SHA256SUMS.txt`.

### Fixed

- **In-app updates on Windows never finished.** The helper that swaps in the new version was started in a way Windows PowerShell quietly refuses, and `[ ]` in the install folder's name stopped it too. Both are fixed, and the helper now keeps a log (`.leancut-update.log` beside the app folder). **If you're on 0.3.0 for Windows, download 0.4.0 from the releases page once**: 0.3.0's own updater can't install it. From 0.4.0 on, updates install from inside the app.
- **Pause didn't always pause.** Pressing pause while the preview was switching to another clip let playback start again, and Space could toggle twice when the play button had focus. Both are fixed.
- Two neighbouring parts with different looks could both play the same 0.3 s of footage at the join.

## 0.3.0

From an auto-cut to an editor. Tell LeanCut what to change in plain words, watch it straight away, and finish the look, titles and voice-over without leaving the app.

### Highlights

- **A real editor.** A program monitor, a timeline of the finished video with Picture, Titles and Voice tracks, and an Inspector for the part you select: trim, effect, the transition into it, its own colour, titles, voice-over and suggestions. Every edit can be undone (⌘Z) and redone (⇧⌘Z).
- **Instant and rendered preview.** The monitor plays the edit straight from your source files, with colour, effects, titles, captions, intro cards and transitions approximated in the window. **Render preview** makes an exact 540p render with everything baked in; switch the monitor to **Rendered** to watch it.
- **AI assistant.** Type "make it about 8 minutes" or "slow down the biggest reaction" and your local model turns it into edit actions. LeanCut checks every action before applying it, and one request is one undo step.
- **Suggestions for every part.** Rules flag lines the AI is unsure about, a missing intro title, dark clips, hard cuts, long talking stretches, loud reactions and shaky shots; the language model adds its own ideas. Apply, dismiss, apply all, or ask again.
- **Decision engine.** Instead of asking the model to write JSON, LeanCut reads its token probabilities for ★1–★5 and for a fixed list of reasons (Ollama's logprobs). Each line gets a probability-weighted score and a confidence, and lines under 55% confidence become "your call" suggestions. The transcript is read once per chunk, so each further question costs about 0.2 s. Servers without logprobs, such as LM Studio, fall back to batch scoring.
- **Looks.** 8 effects, 15 transitions, 9 colour presets, auto colour per clip, exposure, contrast, saturation, temperature and vignette sliders, `.cube` and `.3dl` LUTs, four title styles, intro and outro cards, and burned-in captions.
- **Voice-overs in your own voice.** Type a line and it's spoken in your cloned voice and mixed in, with the video's sound ducked under it. **Use my voice from this project** picks the cleanest stretch of your own speech. Cloning needs your consent and an optional install (see the upgrade notes).
- **Linux.** A `.tar.gz` with an installer, for Ubuntu 22.04, Debian 12 and newer.

### Also new

- **Camera chapters join automatically.** GoPro files named `GH01xxxx`, `GH02xxxx`… and files whose timecode continues where the previous one ended are read as one clip. Edit lists still point at the real files.
- **J-cuts and crossfades.** Pick a cut style for the whole video, or a transition into one part.
- **Learns your taste.** Lines you keep or cut yourself are saved in `taste.json` in the data folder and shown to the decision engine as examples of what you like.
- **Profanity bleep.** Swear words are muted under a 1 kHz tone, timed by Whisper's word timings, and masked in burned-in captions and the exported `.srt`. Add your own words in Settings › Audio.
- **GoPro telemetry highlights.** The GPS track marks hard braking, twisties, top speed and summits, which lift nearby lines and B-roll. Stopped and motorway stretches are left out of the B-roll. It hasn't been tested on a real GoPro file yet.
- **Neural voice enhancement.** DeepFilterNet3 lifts your voice out of engine and wind noise on the CPU: +10.7 dB SNR on a synthetic 0 dB engine-noise test, and 30 minutes of stereo audio in 37 seconds.
- **Face and licence-plate blur** with two small on-device detectors.
- **Stabilisation.** A built-in two-pass vid.stab per part, or stabilise in Gyroflow and click **Use Gyroflow export**.
- **Search your footage by what's on screen** ("lake", "hairpin") with an on-device CLIP model, and add a hit to the cut as B-roll.
- **Visual judge.** An optional local vision model in Ollama (for example `gemma3:4b`) describes and rates the top B-roll candidates; its rating is blended with the motion and colour score.
- **Shorts follow the subject**: the 9:16 crop tracks the largest face, or the most eye-catching part of the frame.
- **In-app updates.** Download, install and restart from the update banner. Tested on macOS; the Windows and Linux installers haven't been tested on a real update yet.
- **Signing-ready releases.** The release workflow signs and notarizes builds when signing certificates are configured. None are yet, so this release is still unsigned.
- **Constrained output.** The assistant's and suggestions' answers are constrained to a JSON schema (Ollama `format`, LM Studio `json_schema`), so the model can't return something LeanCut can't use.

### Fixed

- Lower-third titles sat on top of burned-in captions; they now sit above them.
- When FFmpeg failed, the error showed only the end of its log. It now shows the first error lines.
- Subtitles and chapters skipped parts that play slower or faster; they're now timed to them.

### Upgrade notes

- Lines are re-scored once, because scoring changed.
- Clips are measured once more, because auto colour needs two more values from each keyframe.
- The language model, Whisper model and server settings moved from the editor to **Settings › AI models**.
- Optional models download the first time you use the feature, into `models/` in the data folder: face detector (0.2 MB) and plate detector (4 MB) for blur and Shorts tracking, CLIP (about 150 MB) for footage search, DeepFilterNet3 (8 MB) for voice enhancement. The visual judge uses a vision model you pull into Ollama yourself.
- Voice cloning isn't in the packaged apps, because it needs PyTorch. From a source checkout run `pip install -r requirements-voice.txt` (about 1 GB, plus a 3.8 GB model on first use), or run `voice_server.py` and point **Settings › Voice** at it. Without either, voice-overs use your system's voice.
- Linux is new: extract `LeanCut-Linux-x64.tar.gz` and run `./LeanCut/install.sh`.

## 0.2.0

Built for the whole ride, not just the talking. LeanCut is now open source.

### Highlights

- **Scenic B-roll.** Silent stretches are scored for movement, colour and exposure, and the best views fill a share of the video you choose (15% by default).
- **Say "clip that".** A spoken marker keeps the 30 seconds before it; the command itself is cut.
- **Publish-ready extras.** Subtitles (`.srt`), a YouTube title, description, tags and valid chapters, six thumbnail picks, and optional vertical Shorts.
- **Better sound.** −14 LUFS loudness, optional wind and road-noise reduction, a music bed that ducks under speech, and click-free cuts.
- **Faster and resumable exports.** Hardware encoding (VideoToolbox, NVENC, Quick Sync, AMF) with GPU decoding, a Cancel button, and cached parts: in our 4K test a re-export took 28 seconds instead of 9 minutes.
- **More ways into your editor.** FCPXML (Final Cut Pro, Resolve) with chapter markers, and OpenTimelineIO, next to the EDL.
- **Settings page.** Updates, export folder and file names, formats, encoder, audio, rider AI and models in one place.
- **Update alerts.** A banner tells you when a new version is out, with the right download. It's one anonymous request to GitHub; turn it off in **Settings › Updates**.

### Also new

- Projects: keep every trip separate, switch from the top bar, and run an overnight batch of auto-edits and exports.
- Second-camera sync: mark a clip as an angle of another and LeanCut lines them up by audio for your editor.
- Retakes are detected and the earlier take is cut; Whisper repeat loops are cleaned up.
- Loud moments (shouts, laughs, revs) lift the score of what was said there.
- Optional ride-through fast-forwards over long dropped stretches.
- Drag the edges of any kept block on the timeline to trim it.
- Custom Whisper models: use any CTranslate2 model, including ones fine-tuned for one language.
- AI scores are cached per line, so changing the target length is instant.

### Fixed

- **EDL source times were wrong for cameras that embed timecode** (DJI, GoPro and others), so an imported edit pointed at the wrong frames. Edit lists now use each clip's own timecode, in drop-frame at 29.97 fps, which also removes the one-frame-per-minute drift.
- **Security:** the local server answered requests from any web page. It now accepts requests only from its own window.
- Whisper checked Hugging Face for model updates every time it loaded; cached models now load fully offline.
- 10-bit footage reported brightness on the wrong scale, which hid most B-roll.

### Upgrade notes

- Your v0.1 project is moved into the new projects layout the first time v0.2 starts, including its exports. Nothing is deleted; the old file is kept as `project.v0.1.json.bak`.
- Lines are re-scored once, because the scoring prompt changed.
- Exports are now named after the project instead of `final_youtube_cut`. Change it in **Settings › Export destination**.

## 0.1.0

First release: a desktop app for macOS and Windows around a fully local pipeline (FFmpeg, Silero VAD, faster-whisper, a local LLM via Ollama or LM Studio) that scores every transcript line, keeps the best up to a target length, and exports an MP4 plus a CMX3600 EDL. Includes a memory guard for oversized models, streaming with stall detection, a spoken-language setting and `large-v3-turbo` as the default Whisper model.

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

- **Pause didn't always pause.** Pressing pause while the preview was switching to another clip let playback start again, and Space could toggle twice when the play button had focus. Both are fixed.
- Two neighbouring parts with different looks could both play the same 0.3 s of footage at the join.

**Downloads:** `LeanCut-macOS.dmg`, `LeanCut-Windows.zip`, `LeanCut-Linux-x64.tar.gz` (check them against `SHA256SUMS.txt`). Builds are still unsigned: see the README for the first-launch steps.

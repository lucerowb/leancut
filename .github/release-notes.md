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

### Download

| Platform | File | Notes |
|---|---|---|
| macOS (Apple Silicon) | `LeanCut-macOS.dmg` | Open it and drag LeanCut to Applications. Unsigned: the first time, right-click → **Open**. |
| Windows 10/11 (64-bit) | `LeanCut-Windows.zip` | Extract and run `LeanCut.exe`. SmartScreen: **More info → Run anyway**. |
| Linux (x64) | `LeanCut-Linux-x64.tar.gz` | Extract, then run `./LeanCut/install.sh`. Ubuntu 22.04, Debian 12 or newer. |

Already on v0.2? Download the new version and replace the app; your projects and settings are kept. From v0.3 on, LeanCut can install updates itself.

You also need [Ollama](https://ollama.com) or LM Studio with a model that fits your memory. See the [guide](https://leancut.lucerowb.com/guide.html).

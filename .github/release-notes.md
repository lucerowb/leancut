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

### Download

| Platform | File | Notes |
|---|---|---|
| macOS (Apple Silicon) | `LeanCut-macOS.dmg` | Open it and drag LeanCut to Applications. Unsigned: the first time, right-click → **Open**. |
| Windows 10/11 (64-bit) | `LeanCut-Windows.zip` | Extract and run `LeanCut.exe`. SmartScreen: **More info → Run anyway**. |

You also need [Ollama](https://ollama.com) or LM Studio with a model that fits your memory. See the [guide](https://leancut.lucerowb.com/guide.html).

# Contributing to LeanCut

Thanks for helping out. This guide covers how to set up the project, what's expected in a pull request, and how releases are made.

## Ground rules

- **Local is the product.** Footage, audio and transcripts never leave the user's computer. Pull requests that add analytics, telemetry, ads, accounts, or network traffic beyond the Whisper model download, the user's own LLM server and the optional update check won't be merged.
- **Never touch the user's originals.** LeanCut reads source clips where they are and only ever deletes copies it made itself. Changes to file handling need a test or a clear explanation of how you verified it.
- Keep changes focused: one fix or feature per pull request.

## Development setup

1. Install **Python 3.10–3.12**. faster-whisper's CTranslate2 and onnxruntime wheels don't support newer Pythons yet.
2. Create a virtual environment and install the dependencies:
   ```bash
   python3.12 -m venv .venv
   source .venv/bin/activate        # Windows: .venv\Scripts\activate
   pip install -r requirements.txt -r requirements-build.txt
   pip install -r requirements-dev.txt   # pytest, httpx, opentimelineio
   ```
3. Install a local language-model server and pull a model that fits your memory:
   - [Ollama](https://ollama.com): `ollama pull qwen2.5:7b` (listens on `http://localhost:11434`)
   - LM Studio: load a model, open the **Developer** tab and click **Start Server** (listens on `http://localhost:1234/v1`)
4. Run the app:
   ```bash
   python desktop.py      # native window (pywebview)
   python app.py          # or the browser UI at http://127.0.0.1:8765
   ```

When run from source, LeanCut keeps its project, cache and exports in `workspace/` (git-ignored). Set `LEANCUT_HOME` to use another folder. The first edit downloads the selected Whisper model from Hugging Face into `~/.cache/huggingface`.

## Code style

- Small modules with one job each: `app.py` (server and pipeline), `transcriber.py`, `llm_agent.py`, `video_engine.py`, `desktop.py`. See the project layout in [README.md](README.md#project-layout).
- Match the surrounding code: its naming, formatting and comment style. Comment why, not what.
- The UI in `static/` is plain HTML, CSS and JavaScript with no build step. Keep it that way.
- Don't add a dependency for something a few lines of code can do.
- New logic needs a test in `tests/`. Tests must run without a GPU, an LLM server or a Whisper model download.

## Before you open a pull request

```bash
python -m pytest -q tests
```

Tests must pass. If you changed the pipeline, also run one real edit end to end with a short clip and say which Whisper model, LLM server and model you used.

To check the packaged app:

```bash
python build.py        # macOS: dist/LeanCut.app + dist/LeanCut-macOS.dmg · Windows: dist/LeanCut/ + dist/LeanCut-Windows.zip
```

PyInstaller can't cross-compile, so each OS builds its own app.

## Pull request process

1. Fork the repository and create a branch from `main`.
2. Make your change, add tests, and update the user guide (`docs/guide.html`) if behaviour or options changed.
3. Open a pull request and fill in the template. Describe what changed and how you tested it.
4. A maintainer reviews it. Please keep the conversation in the pull request so others can follow along.

## Releasing

1. Add a section for the new version to `docs/changelog.html`.
2. Tag and push:
   ```bash
   git tag v0.2.0 && git push origin v0.2.0
   ```
3. The **build** workflow (`.github/workflows/build.yml`) runs the tests, builds the macOS `.dmg` and the Windows `.zip`, and attaches both to the GitHub release.

## Reporting bugs and security issues

Open an [issue](https://github.com/lucerowb/leancut/issues/new/choose) for bugs and feature ideas. Report security problems privately; see [SECURITY.md](SECURITY.md).

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE) and that you'll follow the [code of conduct](CODE_OF_CONDUCT.md).

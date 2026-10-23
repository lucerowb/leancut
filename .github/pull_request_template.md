## What does this change?

## How was it tested?

<!-- Unit tests? For pipeline changes: which OS, footage, Whisper model, LLM server and model did you run a real edit with? -->

## Checklist

- [ ] `python -m pytest -q tests` passes
- [ ] New logic has tests that run without a GPU, an LLM server or a model download
- [ ] No new network traffic, analytics or tracking; footage, audio and transcripts stay on the computer
- [ ] User-facing changes are reflected in `docs/guide.html` (and `docs/changelog.html` for a release)

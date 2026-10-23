# Security policy

## Reporting a vulnerability

Please **don't** open a public issue for security problems. Report them privately through GitHub's [private vulnerability reporting](https://github.com/lucerowb/leancut/security/advisories/new) (the repository's **Security** tab › **Report a vulnerability**). You'll get a reply within a week.

Include what you found, how to reproduce it, and the affected versions and platforms. Please don't attach footage or transcripts you wouldn't want shared.

## Security design

LeanCut is a desktop app with no accounts and no servers of its own. In short:

- **The backend listens on `127.0.0.1` only.** The desktop app picks a free port at launch; `python app.py` uses port 8765. Neither is reachable from the network.
- **Footage, audio, transcripts and exports stay on your computer.** LeanCut reads clips where they are and only deletes copies it made itself, never your originals.
- **Outgoing network traffic is limited to three things:** the one-time Whisper model download from Hugging Face (cached models load offline), requests to the language-model server you configure (by default Ollama or LM Studio on `localhost`), and an optional update check against GitHub's public releases API that can be turned off in **Settings › Updates**. See the [privacy policy](https://lucerowb.github.io/leancut/privacy.html) for details.
- **Language-model output is treated as untrusted.** The model only returns scores and reasons for transcript segments; timestamps always come from Whisper, and the files to cut are chosen by LeanCut's own code.
- **Media is processed by FFmpeg**, bundled through `imageio-ffmpeg` when it isn't on your `PATH`. Malformed media files are handled by FFmpeg's own parsers.

## Known gaps

- The local server has no login. It only answers requests whose Host is `127.0.0.1`/`localhost` and that come from no other web origin, which blocks web pages (including DNS-rebinding attacks) from reaching it. Other programs running on your computer under your user account can still talk to it, as they could read your files directly.
- If you point the LLM base URL at a server on another machine or a cloud provider, your transcript text is sent there, over plain HTTP unless the URL starts with `https://`.
- Releases aren't code-signed by a paid Apple or Microsoft developer certificate yet, so verify you downloaded them from the [official repository](https://github.com/lucerowb/leancut/releases).

## Supported versions

Only the latest release gets security fixes.

# Security policy

## Reporting a vulnerability

Please **don't** open a public issue for security problems. Report them privately through GitHub's [private vulnerability reporting](https://github.com/lucerowb/leancut/security/advisories/new) (the repository's **Security** tab › **Report a vulnerability**). You'll get a reply within a week.

Include what you found, how to reproduce it, and the affected versions and platforms. Please don't attach footage or transcripts you wouldn't want shared.

## Security design

LeanCut is a desktop app with no accounts and no servers of its own. In short:

- **The backend listens on `127.0.0.1` only.** The desktop app picks a free port at launch; `python app.py` uses port 8765, and the optional `voice_server.py` 8880. None is reachable from the network.
- **Footage, audio, transcripts and exports stay on your computer.** LeanCut reads clips where they are and only deletes copies it made itself, never your originals.
- **Outgoing network traffic is limited to:**
  - one-time model downloads: Whisper, and the optional face, plate and CLIP models, from Hugging Face; the voice-enhancement model (DeepFilterNet3) from `raw.githubusercontent.com`; the Chatterbox voice model from Hugging Face if you installed voice cloning. Downloads are pinned to a commit, the DeepFilterNet archive is checked against a SHA-256, and files already on disk are used without contacting anyone;
  - the language-model server and voice server you configure (by default on `localhost`);
  - the optional update check against GitHub's public releases API, and, when you click **Install**, the update download from `https://github.com/lucerowb/leancut/releases/…`. Only redirects to GitHub's asset hosts (`objects.githubusercontent.com`, `release-assets.githubusercontent.com`) are followed, and the file must match the size GitHub announces. Turn the check off in **Settings › Updates**.

  See the [privacy policy](https://leancut.lucerowb.com/privacy.html) for details.
- **Language-model output is treated as untrusted.** The model can only return scores, reasons and edit actions from a fixed vocabulary, constrained to a JSON schema where the server supports it. Every action is validated before it changes the project; timestamps for scoring always come from Whisper, files are chosen by LeanCut's own code, and a LUT path must be an existing `.cube` or `.3dl` file.
- **Voice samples and your editing taste stay local.** `voices/` and `taste.json` live in the data folder. Voice cloning asks you to confirm the voice is yours or that you have permission.
- **Media is processed by FFmpeg**, bundled through `imageio-ffmpeg` when it isn't on your `PATH`. Malformed media files are handled by FFmpeg's own parsers.

## Known gaps

- The local server has no login. It only answers requests whose Host is `127.0.0.1`/`localhost` and that come from no other web origin, which blocks web pages (including DNS-rebinding attacks) from reaching it. Other programs running on your computer under your user account can still talk to it, as they could read your files directly.
- If you point the LLM base URL at a server on another machine or a cloud provider, your transcript text is sent there, over plain HTTP unless the URL starts with `https://`.
- Releases aren't code-signed by a paid Apple or Microsoft developer certificate yet, so verify you downloaded them from the [official repository](https://github.com/lucerowb/leancut/releases). The release workflow signs and notarizes as soon as certificates are configured. Until then, in-app updates are verified by origin (our GitHub repository over HTTPS) and size, not by a signature.
- The Windows and Linux update helpers haven't been tested on a real update yet.

## Supported versions

Only the latest release gets security fixes.

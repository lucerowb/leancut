"""Voice-overs from typed text, spoken in the creator's own (cloned) voice, fully local.

Consent: only clone your own voice, or a voice whose owner has given you permission. Chatterbox also embeds an
inaudible watermark (Resemble AI's Perth) in everything it generates, so cloned speech stays identifiable.

Three engines, because a cloning model with PyTorch is too big to bundle in the packaged app:
- "chatterbox": Resemble AI's Chatterbox-Turbo (MIT) in this process, when `pip install -r requirements-voice.txt` was
  run. Zero-shot cloning from a 5-20 s reference clip (English); CUDA when available, else CPU.
- "server": any OpenAI-compatible TTS server (POST {base}/audio/speech -> audio). `voice_server.py` is one, built on
  Chatterbox, so packaged-app users can run it from a source checkout and point LeanCut at it.
- "system": the OS voice (macOS `say`, Windows SAPI, Linux espeak) for quick previews. No cloning.

Every engine's output is converted to a 48 kHz stereo WAV, ready to mix into a video.
"""
import functools
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import wave
from pathlib import Path

import video_engine

ENGINES = ("chatterbox", "server", "system")
SAMPLE_RATE = 16000  # pick_voice_sample writes the transcription WAV's own rate; Chatterbox resamples it itself
CHUNK_CHARS = 300  # Chatterbox stops after 1000 speech tokens (~40 s) per call: speak long text in pieces
SERVER_MODEL = "tts-1"  # shortcut: OpenAI's name, which most local servers accept or ignore; add a setting if one won't


class VoiceError(RuntimeError):
    pass


def engines_available(server_url=None) -> dict:
    return {"chatterbox": importlib.util.find_spec("chatterbox") is not None,
            "server": bool(server_url) and _reachable(server_url),
            "system": _system_command() is not None}


def _reachable(url) -> bool:
    """Any HTTP answer (even a 404 at the base URL) means a server is listening."""
    try:
        urllib.request.urlopen(url, timeout=2).close()
        return True
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


# ---------------------------------------------------------------- picking a reference clip

def _read_wav(path):
    """16-bit PCM WAV -> (float32 mono samples, sample rate)."""
    import numpy as np
    with wave.open(str(path), "rb") as w:
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
        return x.reshape(-1, w.getnchannels()).mean(axis=1), w.getframerate()


def _write_wav(path, samples, rate):
    """float mono samples -> 16-bit PCM WAV, written to a temp name first so a crash never leaves half a file."""
    import numpy as np
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part.wav")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())
    tmp.replace(path)
    return path


def _rank(x, rate, span_segments, start, end, max_len):
    """Higher is a better reference: scored, densely spoken, steady in loudness, no shouts, no clipping.

    Cloning copies whatever is in the reference, so a shout, a wind gust or a clipped peak ends up in every line."""
    import numpy as np
    clip = x[int(start * rate):int(end * rate)]
    if len(clip) < rate:
        return float("-inf")
    hop = rate // 50  # 20 ms frames
    frames = clip[:len(clip) // hop * hop].reshape(-1, hop)
    db = 20 * np.log10(np.sqrt((frames ** 2).mean(axis=1)) + 1e-6)
    voiced = db[db > np.median(db) - 15]  # ignore pauses between words
    spoken = sum(s["end"] - s["start"] for s in span_segments)
    score = sum(s.get("score", 3) * (s["end"] - s["start"]) for s in span_segments) / max(spoken, 1e-6)
    return (score / 5                                   # the LLM's judgement of the line, when there is one
            + spoken / (end - start)                    # fewer gaps
            + (end - start) / max_len                   # longer, up to max_len, clones better
            - np.std(voiced) / 6                        # steady loudness (normal speech: std ~4-6 dB)
            - max(0.0, np.max(db) - np.median(voiced) - 12) / 3  # loud spikes: shouts, revs, gusts
            - 200 * float(np.mean(np.abs(clip) > 0.98)))  # clipped samples


def pick_voice_sample(segments, wav16k, out_wav, min_len=8.0, max_len=20.0) -> dict:
    """Write the cleanest stretch of the creator's own speech in a transcribed clip to out_wav (16 kHz mono WAV).

    segments: [{start, end, text, score?}] of that clip; wav16k: the clip's 16 kHz mono WAV (video_engine.extract_audio).
    Candidates are runs of consecutive lines between min_len and max_len seconds; when no run reaches min_len the
    longest one is used. Returns {"path", "start", "end", "seconds"}.
    """
    # shortcut: the transcription WAV is high-passed at 120 Hz, so deep voices lose a little body;
    # extract the reference straight from the video at 24 kHz if clones sound thin.
    segs = sorted((s for s in segments if s["end"] > s["start"]), key=lambda s: s["start"])
    if not segs:
        raise VoiceError("No speech found in this clip to learn the voice from. Pick a clip where you talk.")
    x, rate = _read_wav(wav16k)
    runs = []
    for i in range(len(segs)):
        for j in range(i, len(segs)):
            if segs[j]["end"] - segs[i]["start"] > max_len:
                break
            runs.append(segs[i:j + 1])
    if not runs:  # every single line is longer than max_len: take the start of the best one
        runs = [[{**s, "end": s["start"] + max_len}] for s in segs]
    longest = max(r[-1]["end"] - r[0]["start"] for r in runs)
    runs = [r for r in runs if r[-1]["end"] - r[0]["start"] >= min(min_len, longest)]
    best = max(runs, key=lambda r: _rank(x, rate, r, r[0]["start"], r[-1]["end"], max_len))
    start, end = float(best[0]["start"]), float(min(best[-1]["end"], len(x) / rate))  # Whisper times may be numpy
    _write_wav(out_wav, x[int(start * rate):int(end * rate)], rate)
    return {"path": str(out_wav), "start": start, "end": end, "seconds": round(end - start, 3)}


# ---------------------------------------------------------------- synthesis

def synthesize(text, out_wav, engine, voice_sample=None, server_url=None, server_voice=None, speed=1.0) -> float:
    """Speak text into out_wav as a 48 kHz stereo WAV; returns its duration in seconds.

    voice_sample: reference clip to clone ("chatterbox"; for "server" it is sent as the voice when server_voice is
    not set, which voice_server.py accepts because it runs on this machine). speed: 0.5-2.0, applied by FFmpeg so
    every engine behaves the same.
    """
    text = (text or "").strip()
    if not text:
        raise VoiceError("Type the text to speak first.")
    if engine not in ENGINES:
        raise VoiceError(f"Unknown voice engine {engine!r}; choose one of {', '.join(ENGINES)}.")
    if not 0.5 <= float(speed) <= 2.0:
        raise VoiceError("Speed must be between 0.5 and 2.0.")
    out_wav = Path(out_wav)
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=out_wav.parent) as tmp:
        raw = Path(tmp) / "raw"
        if engine == "chatterbox":
            raw = chatterbox_wav(text, voice_sample, raw.with_suffix(".wav"))
        elif engine == "server":
            raw = _server_wav(text, raw.with_suffix(".audio"), server_url, server_voice or voice_sample)
        else:
            raw = _system_wav(text, Path(tmp))
        part = Path(tmp) / "out.wav"
        tempo = ["-af", f"atempo={float(speed):.3f}"] if float(speed) != 1.0 else []
        video_engine._ffmpeg(["-y", "-i", str(raw), *tempo, "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le",
                              str(part)])
        part.replace(out_wav)
    with wave.open(str(out_wav), "rb") as w:
        return w.getnframes() / w.getframerate()


def _chunks(text, limit=CHUNK_CHARS):
    """Sentences packed into pieces of at most ~limit characters (a single longer sentence stays whole)."""
    out = []
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if out and len(out[-1]) + len(sentence) < limit:
            out[-1] += " " + sentence
        else:
            out.append(sentence)
    return out


def _device():
    """CUDA if present, else CPU. Not MPS: on an M5 Pro, token-by-token decoding ran 2-4x slower on the Apple GPU
    than on the CPU (real-time factor 1.2-2.3 vs 0.6)."""
    import torch
    return "cuda" if torch.cuda.is_available() else "cpu"


@functools.cache
def _chatterbox_model():
    """Turbo, not the original Chatterbox: ~6x faster on a Mac CPU (faster than real time) for slightly lower
    speaker similarity (0.86-0.91 vs 0.90-0.96 cosine in our test)."""
    from chatterbox.tts_turbo import ChatterboxTurboTTS
    return ChatterboxTurboTTS.from_pretrained(device=_device())  # ~3.8 GB from Hugging Face on first use


def chatterbox_wav(text, voice_sample, out_wav) -> Path:
    """Clone voice_sample and speak text with Chatterbox into out_wav (its native 24 kHz mono). Used by voice_server."""
    if not voice_sample or not Path(voice_sample).is_file():
        raise VoiceError("Chatterbox needs a voice sample: 5-20 s of your own clean speech as a WAV "
                         "(pick one from a clip, or record one).")
    if importlib.util.find_spec("chatterbox") is None:
        raise VoiceError("Voice cloning isn't installed in this LeanCut. From a source checkout run "
                         "`pip install -r requirements-voice.txt`, or start `python voice_server.py --voice "
                         "my_voice.wav` in a Python that has it and choose the 'server' engine.")
    import numpy as np
    model = _chatterbox_model()
    key = (str(Path(voice_sample).resolve()), Path(voice_sample).stat().st_mtime)
    if getattr(model, "_leancut_voice", None) != key:  # embedding the reference costs ~1 s; reuse it per sample
        try:
            model.prepare_conditionals(str(voice_sample))
        except AssertionError as e:  # Turbo asserts the reference is longer than 5 s
            raise VoiceError(f"The voice sample is too short: {e} Use 5-20 s of clean speech.") from e
        model._leancut_voice = key
    gap = np.zeros(int(model.sr * 0.2), dtype=np.float32)
    pieces = []
    for chunk in _chunks(text):
        pieces += [model.generate(chunk).squeeze(0).cpu().numpy(), gap]
    return _write_wav(out_wav, np.concatenate(pieces[:-1]), model.sr)


def _server_wav(text, out, server_url, voice) -> Path:
    """POST to an OpenAI-compatible /audio/speech endpoint and save the audio it returns."""
    if not server_url:
        raise VoiceError("Set the voice server URL first, e.g. http://127.0.0.1:8880/v1 for voice_server.py.")
    url = server_url.rstrip("/") + "/audio/speech"
    if voice and Path(str(voice)).is_file():
        voice = str(Path(voice).resolve())  # voice_server.py runs on this machine, so it can read the file itself
    payload = {"model": SERVER_MODEL, "input": text, "voice": str(voice or "default"), "response_format": "wav"}
    req = urllib.request.Request(url, json.dumps(payload).encode(), {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=900) as r:  # CPU-only cloning of a long text takes minutes
            data = r.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        if e.code == 404:
            raise VoiceError(f"{url} returned 404: the base URL must end where /audio/speech starts "
                             f"(usually …/v1). {body}") from e
        raise VoiceError(f"Voice server error {e.code}: {body}") from e
    except (urllib.error.URLError, OSError) as e:
        raise VoiceError(f"Cannot reach the voice server at {server_url} ({getattr(e, 'reason', e)}). Start it with "
                         "`python voice_server.py --voice my_voice.wav` and check the URL.") from e
    if len(data) < 64:
        raise VoiceError(f"The voice server at {server_url} returned no audio.")
    out.write_bytes(data)
    return out


def _system_command():
    """The OS speech tool for this platform, or None."""
    if sys.platform == "darwin":
        return shutil.which("say")
    if sys.platform == "win32":
        return shutil.which("powershell")
    return shutil.which("espeak-ng") or shutil.which("espeak")


# Paths come in through environment variables, so no quoting of user text into a PowerShell command line.
_SAPI = ("Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
         "$s.SetOutputToWaveFile($env:LEANCUT_TTS_OUT); "
         "$s.Speak([IO.File]::ReadAllText($env:LEANCUT_TTS_IN, [Text.Encoding]::UTF8)); $s.Dispose()")


def _system_wav(text, tmp) -> Path:
    exe = _system_command()
    if not exe:
        raise VoiceError("No system voice found. Linux: `sudo apt install espeak-ng`. "
                         "Or use the 'chatterbox' or 'server' engine.")
    src, out = tmp / "text.txt", tmp / ("system.aiff" if sys.platform == "darwin" else "system.wav")
    src.write_text(text, encoding="utf-8")  # a file, not argv: text starting with "-" can't become an option
    if sys.platform == "darwin":
        cmd, env = [exe, "-f", str(src), "-o", str(out)], None
    elif sys.platform == "win32":
        cmd = [exe, "-NoProfile", "-NonInteractive", "-Command", _SAPI]
        env = {**os.environ, "LEANCUT_TTS_IN": str(src), "LEANCUT_TTS_OUT": str(out)}
    else:
        cmd, env = [exe, "-f", str(src), "-w", str(out)], None
    p = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=300)
    if p.returncode != 0 or not out.exists():
        raise VoiceError(f"The system voice failed: {(p.stderr or p.stdout).strip()[-300:]}")
    return out

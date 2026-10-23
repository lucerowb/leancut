"""Local speech-to-text with faster-whisper.

VAD (Silero, built into faster-whisper) drops silence, wind and other non-speech before decoding,
so the returned segments are exactly the "talking" parts of each clip, in source-clip time.
"""
import difflib
import gc
import re
import wave

WHISPER_SIZES = ["tiny", "base", "small", "medium", "large-v3-turbo"]
LANGUAGES = {"auto": "Auto-detect", "en": "English", "ne": "Nepali", "hi": "Hindi", "ur": "Urdu", "bn": "Bengali",
             "es": "Spanish", "pt": "Portuguese", "fr": "French", "de": "German", "it": "Italian",
             "ja": "Japanese", "ko": "Korean", "zh": "Chinese", "id": "Indonesian", "ar": "Arabic", "ru": "Russian"}


def is_downloaded(size: str) -> bool:
    from faster_whisper.utils import download_model
    try:
        download_model(size, local_files_only=True)
        return True
    except Exception:
        return False


def download(size: str) -> str:
    from faster_whisper.utils import download_model
    return download_model(size)


def _gpu_error(e: Exception) -> bool:
    msg = str(e).lower()
    return any(k in msg for k in ("cuda", "cudnn", "cublas", "out of memory", "gpu"))


def _load(size, device, log):
    from faster_whisper import WhisperModel
    from faster_whisper.utils import download_model
    try:  # use the cached copy so loading doesn't contact Hugging Face again
        size = download_model(size, local_files_only=True)
    except Exception:
        pass  # not cached yet (or a custom repo id / folder): WhisperModel downloads or opens it
    if device == "cpu":
        return WhisperModel(size, device="cpu", compute_type="int8")
    try:
        return WhisperModel(size, device="auto", compute_type="auto")
    except Exception as e:
        if not _gpu_error(e):
            raise
        log(f"GPU unavailable or VRAM full ({e}). Falling back to CPU (int8) — slower but works.")
        return WhisperModel(size, device="cpu", compute_type="int8")


def _read_wav(path):
    """Our own 16 kHz mono s16 WAV -> float32 samples. Bypasses faster-whisper's PyAV decoder (breaks on PyAV >= 15)."""
    import numpy as np
    with wave.open(str(path), "rb") as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0


def _transcribe_one(model, wav, language, on_progress):
    segments, info = model.transcribe(
        _read_wav(wav),
        language=None if language == "auto" else language,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 700, "speech_pad_ms": 200},
        beam_size=5,
        word_timestamps=True,
        condition_on_previous_text=False,  # avoids runaway repetition on long noisy rides
    )
    out = []
    for s in segments:  # generator: decoding happens while iterating
        on_progress(min(1.0, s.end / max(info.duration, 1e-6)))
        if s.no_speech_prob > 0.6:  # Whisper "hallucinations" on wind/engine noise
            continue
        out += split_sentences(s.words or [])
    return out, info


SCORE_FIELDS = ("score", "reason", "scored_by", "keep", "auto")


def merge_fragments(segments, min_dur=4.0, max_gap=1.5, max_dur=20.0):
    """Join short or unfinished pieces ("I" / "have to") into editable thoughts. Idempotent, so it is safe to run
    on cached transcripts. A long pause (> max_gap) always stays a cut point. Segments that are not merged keep
    their cached AI scores; merged ones lose them, since their text changed."""
    out = []
    for s in segments:
        cur = out[-1] if out else None
        if (cur and s["start"] - cur["end"] <= max_gap and s["end"] - cur["start"] <= max_dur
                and (cur["end"] - cur["start"] < min_dur or not cur["text"].endswith((".", "!", "?")))):
            cur["end"], cur["text"] = s["end"], f"{cur['text']} {s['text']}"
            for k in SCORE_FIELDS:
                cur.pop(k, None)
        else:
            out.append(dict(s))
    return out


def _words(text):
    return re.sub(r"[^\w\s]", " ", text.lower()).split()


def collapse_loops(text):
    """'She is the type of child. She is the type of child?' -> 'She is the type of child.' (Whisper repeat loops)."""
    out = []
    for sentence in re.split(r"(?<=[.!?])\s+", text.strip()):
        if not out or _words(sentence) != _words(out[-1]):
            out.append(sentence)
    return " ".join(out)


def flag_retakes(segments, similarity=0.75, max_gap=12.0):
    """Collapse repeat loops and mark the earlier of two near-identical neighbouring lines as a retake
    (vloggers redo a line until it lands; the last take is usually the keeper). Sets seg['auto'] = 'retake'."""
    for s in segments:
        s["text"] = collapse_loops(s["text"])
        if s.get("auto") == "retake":
            s.pop("auto")
    for a, b in zip(segments, segments[1:]):
        wa, wb = _words(a["text"]), _words(b["text"])
        if (len(wa) >= 3 and b["start"] - a["end"] <= max_gap
                and difflib.SequenceMatcher(None, wa, wb).ratio() >= similarity):
            a["auto"] = "retake"
    return segments


def is_marker_only(text, phrases):
    """True when a line is nothing but the spoken command ("Clip that!"), which viewers shouldn't hear."""
    rest = f" {' '.join(_words(text))} "
    for p in phrases.split(","):
        if _words(p):
            rest = rest.replace(f" {' '.join(_words(p))} ", " ")
    return bool(_words(text)) and not rest.strip()


def find_markers(segments, phrases):
    """Times (end of the segment) where the rider said a marker phrase like "clip that" or "mark this"."""
    pats = [f" {' '.join(_words(p))} " for p in phrases.split(",") if _words(p)]
    return [s["end"] for s in segments if any(p in f" {' '.join(_words(s['text']))} " for p in pats)]


def split_sentences(words, max_pause=1.0):
    """Whisper segments can run 30 s; cut them at sentence ends / pauses so the LLM can drop single sentences."""
    out, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        if nxt is None or w.word.strip().endswith((".", "!", "?")) or nxt.start - w.end > max_pause:
            text = "".join(x.word for x in cur).strip()
            if text:
                out.append({"start": round(cur[0].start, 2), "end": round(cur[-1].end, 2), "text": text})
            cur = []
    return out


def transcribe_files(size, wav_paths, language="auto", log=print, on_progress=lambda i, f: None):
    """Yield (index, segments) per WAV. The model is loaded once and released afterwards to free VRAM for the LLM.

    size: a size name, a Hugging Face repo id or a local folder of a CTranslate2-converted (e.g. fine-tuned) model."""
    model = _load(size, "auto", log)
    try:
        for i, wav in enumerate(wav_paths):
            progress = lambda f, i=i: on_progress(i, f)
            try:
                segs, info = _transcribe_one(model, wav, language, progress)
            except Exception as e:
                if not _gpu_error(e):
                    raise
                log(f"GPU error during transcription ({e}). Retrying on CPU.")
                model = _load(size, "cpu", log)
                segs, info = _transcribe_one(model, wav, language, progress)
            if language == "auto":
                log(f"Detected language: {LANGUAGES.get(info.language, info.language)} "
                    f"({info.language_probability:.0%} sure). If that's wrong, set the language explicitly.")
            yield i, segs
    finally:
        del model
        gc.collect()

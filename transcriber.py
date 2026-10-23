"""Local speech-to-text with faster-whisper.

VAD (Silero, built into faster-whisper) drops silence, wind and other non-speech before decoding,
so the returned segments are exactly the "talking" parts of each clip, in source-clip time.
"""
import difflib
import gc
import re
import unicodedata
import wave
from bisect import bisect_right
from types import SimpleNamespace

WHISPER_SIZES = ["tiny", "base", "small", "medium", "large-v3-turbo"]
LANGUAGES = {"auto": "Auto-detect", "en": "English", "ne": "Nepali", "hi": "Hindi", "ur": "Urdu", "bn": "Bengali",
             "es": "Spanish", "pt": "Portuguese", "fr": "French", "de": "German", "it": "Italian",
             "ja": "Japanese", "ko": "Korean", "zh": "Chinese", "id": "Indonesian", "ar": "Arabic", "ru": "Russian"}
# "xx+en": creators who switch between their language and English inside one video (code-switching).
# shortcut: not in LANGUAGES (so not in the UI) yet; slower than real time on CPU, add it once it's tuned.
MIXED = {"ne+en": "Nepali + English (mixed)", "hi+en": "Hindi + English (mixed)"}
# Whisper's language guess for code-switching: large-v3-turbo's is unusable (it calls long Nepali stretches English
# with 0.7-0.95), `small` tells Nepali from English well and costs ~0.13 s per second of speech on a laptop CPU.
DETECTOR = "small"
SR = 16000
VAD = {"min_silence_duration_ms": 700, "speech_pad_ms": 200}
ENDS = (".", "!", "?", "।")  # "।" (danda) ends a Nepali / Hindi sentence


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


def _decode(model, audio, language, t0=0.0, on_progress=lambda f: None):
    """Whisper segments [(start, end, text, words)] of the speech in `audio`, shifted to start at t0 seconds."""
    segments, info = model.transcribe(
        audio,
        language=language,
        vad_filter=True,
        vad_parameters=VAD,
        beam_size=5,
        word_timestamps=True,
        condition_on_previous_text=False,  # avoids runaway repetition on long noisy rides
        append_punctuations="\"'.。,，!！?？:：”)]}、।",  # faster-whisper's default + danda
    )
    out = []
    for s in segments:  # generator: decoding happens while iterating
        on_progress(min(1.0, s.end / max(info.duration, 1e-6)))
        if s.no_speech_prob > 0.6 or not s.words:  # Whisper "hallucinations" on wind/engine noise
            continue
        out.append(SimpleNamespace(start=s.words[0].start + t0, end=s.words[-1].end + t0, text=s.text.strip(),
                                   words=[SimpleNamespace(word=w.word, start=w.start + t0, end=w.end + t0)
                                          for w in s.words]))
    return out, info


def _flat(segments):
    return [w for s in segments for w in s.words]


def _transcribe_one(model, wav, language, on_progress):
    segs, info = _decode(model, _read_wav(wav), None if language == "auto" else language, on_progress=on_progress)
    return split_sentences(_flat(segs)), info


def merge_chunks(chunks, min_len=3.0, max_gap=2.0, max_len=25.0):
    """Join VAD speech chunks [(start, end)] that are too short for a reliable language guess to a neighbour.
    Chunks that are long enough on both sides stay apart, so a switch between languages is kept."""
    out = []
    for s, e in chunks:
        if out and s - out[-1][1] <= max_gap and e - out[-1][0] <= max_len and (
                out[-1][1] - out[-1][0] < min_len or e - s < min_len):
            out[-1] = (out[-1][0], e)
        else:
            out.append((s, e))
    return out


def speech_units(audio):
    """Pause-separated stretches of speech (seconds), the unit for language decisions in a mixed language."""
    from faster_whisper.vad import VadOptions, get_speech_timestamps
    chunks = get_speech_timestamps(audio, VadOptions(**VAD, max_speech_duration_s=25))
    return merge_chunks([(c["start"] / SR, c["end"] / SR) for c in chunks])


def pick_language(probs, native, min_en=0.6):
    """English or `native` from Whisper's [(language, probability)]: English only when clearly English. Short or
    noisy units spread their probability over unrelated languages (Hindi, Marathi, Korean for Nepali), and a wrong
    "native" costs less than a wrong "en": the English rendering of an English unit is still a verbatim transcript."""
    return "en" if dict(probs).get("en", 0.0) >= min_en else native


def runs(units, langs):
    """Group neighbouring units of one language: [(lang, [units])], so each run is decoded in one pass."""
    out = []
    for u, lang in zip(units, langs):
        if out and out[-1][0] == lang:
            out[-1][1].append(u)
        else:
            out.append((lang, [u]))
    return out


def _by_unit(words, units):
    """Each word goes to the last unit (start, end) starting before its middle."""
    out = [[] for _ in units]
    starts = [u[0] for u in units]
    for w in words:
        out[max(0, bisect_right(starts, (w.start + w.end) / 2) - 1)].append(w)
    return out


def _text(words):
    return "".join(w.word for w in words).strip()


def _transcribe_mixed(model, native_model, wav, language, on_progress, detector=None):
    """Code-switched speech ("ne+en"): per pause-separated unit, decide English or the native language; English is
    transcribed verbatim. The native language is transcribed by `native_model` (a fine-tuned model, native script)
    with an English rendering in seg["en"] for the editing AI, or without one by the multilingual model forced to
    English (a rough translation, still marked with its spoken language)."""
    native = language.split("+")[0]
    audio = _read_wav(wav)
    duration = len(audio) / SR
    units = speech_units(audio)
    detector = detector or model
    langs = [pick_language(detector.detect_language(audio=audio[int(s * SR):int(e * SR)])[2], native)
             for s, e in units]
    out = []
    for lang, group in runs(units, langs):
        t0, t1 = group[0][0], group[-1][1]
        x = audio[int(t0 * SR):int(t1 * SR)]
        english = _flat(_decode(model, x, "en", t0)[0])
        if lang == "en" or native_model is None:
            out += [{**s, "lang": lang} for s in split_sentences(english)]
        else:  # Whisper's own segment text: words split Devanagari bytes and can carry "\ufffd"
            own = [s for s in _decode(native_model, x, lang, t0)[0] if s.text]
            for s, en in zip(own, _by_unit(english, [(s.start, s.end) for s in own])):
                out.append({"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text,
                            "words": [[w.word.strip(), round(w.start, 2), round(w.end, 2)] for w in s.words],
                            "lang": lang, "en": _text(en)})
        on_progress(min(1.0, t1 / max(duration, 1e-6)))
    on_progress(1.0)
    return out, SimpleNamespace(duration=duration, language=language)


def language_share(segments):
    """{lang: seconds of speech} of a transcript, for the log line after a mixed-language run."""
    share = {}
    for s in segments:
        share[s.get("lang", "?")] = share.get(s.get("lang", "?"), 0.0) + s["end"] - s["start"]
    return share


SCORE_FIELDS = ("score", "reason", "scored_by", "keep", "auto")


def merge_fragments(segments, min_dur=4.0, max_gap=1.5, max_dur=20.0):
    """Join short or unfinished pieces ("I" / "have to") into editable thoughts. Idempotent, so it is safe to run
    on cached transcripts. A long pause (> max_gap) always stays a cut point. Segments that are not merged keep
    their cached AI scores; merged ones lose them, since their text changed."""
    out = []
    for s in segments:
        cur = out[-1] if out else None
        if (cur and s["start"] - cur["end"] <= max_gap and s["end"] - cur["start"] <= max_dur
                and (cur["end"] - cur["start"] < min_dur or not cur["text"].endswith(ENDS))):
            if "en" in cur or "en" in s:  # English rendering of native-script lines (mixed languages)
                cur["en"] = f"{cur.get('en', cur['text'])} {s.get('en', s['text'])}"
            if "lang" in cur and s.get("lang", cur["lang"]) not in cur["lang"].split("+"):
                cur["lang"] += "+" + s["lang"]
            cur["end"], cur["text"] = s["end"], f"{cur['text']} {s['text']}"
            if "words" in cur or "words" in s:
                cur["words"] = (cur.get("words") or []) + (s.get("words") or [])
            for k in SCORE_FIELDS:
                cur.pop(k, None)
        else:
            out.append(dict(s))
    return out


def _words(text):  # keeps combining marks, so Devanagari words (नमस्ते) aren't cut at every vowel sign
    return "".join(c if unicodedata.category(c)[0] in "LMN" else " " for c in text.lower()).split()


def collapse_loops(text):
    """'She is the type of child. She is the type of child?' -> 'She is the type of child.' (Whisper repeat loops)."""
    out = []
    for sentence in re.split(r"(?<=[.!?।])\s+", text.strip()):
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
        if nxt is None or w.word.strip().endswith(ENDS) or nxt.start - w.end > max_pause:
            text = "".join(x.word for x in cur).strip()
            if text:  # word times are kept for exact profanity bleeps
                out.append({"start": round(cur[0].start, 2), "end": round(cur[-1].end, 2), "text": text,
                            "words": [[x.word.strip(), round(x.start, 2), round(x.end, 2)] for x in cur]})
            cur = []
    return out


def transcribe_files(size, wav_paths, language="auto", log=print, on_progress=lambda i, f: None, native=None):
    """Yield (index, segments) per WAV. Models are loaded once and released afterwards to free VRAM for the LLM.

    size: a size name, a Hugging Face repo id or a local folder of a CTranslate2-converted (e.g. fine-tuned) model.
    native: only for a mixed language ("ne+en"): a model fine-tuned for the non-English half (repo id or folder);
    None transcribes that half as rough English with `size`."""
    mixed = language in MIXED
    native = native if mixed else None

    def load(device):
        return (_load(size, device, log), native and _load(native, device, log),
                mixed and size != DETECTOR and _load(DETECTOR, device, log) or None)

    def run(wav, progress):
        if mixed:
            return _transcribe_mixed(model, nat, wav, language, progress, detector)
        return _transcribe_one(model, wav, language, progress)
    model, nat, detector = load("auto")
    try:
        for i, wav in enumerate(wav_paths):
            progress = lambda f, i=i: on_progress(i, f)
            try:
                segs, info = run(wav, progress)
            except Exception as e:
                if not _gpu_error(e):
                    raise
                log(f"GPU error during transcription ({e}). Retrying on CPU.")
                model = nat = detector = None
                gc.collect()
                model, nat, detector = load("cpu")
                segs, info = run(wav, progress)
            if language == "auto":
                log(f"Detected language: {LANGUAGES.get(info.language, info.language)} "
                    f"({info.language_probability:.0%} sure). If that's wrong, set the language explicitly.")
            elif mixed:
                share = language_share(segs)
                total = sum(share.values()) or 1
                log("Spoken: " + ", ".join(f"{share[k] / total:.0%} {LANGUAGES.get(k, k)}"
                                           for k in sorted(share, key=share.get, reverse=True)))
            yield i, segs
    finally:
        del model, nat, detector
        gc.collect()

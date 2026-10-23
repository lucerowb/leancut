"""Mixed-language (code-switching) transcription tests. Offline: fake Whisper models, no downloads, no VAD model.
Run: python -m pytest -q tests/test_transcriber.py"""
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import transcriber as T  # noqa: E402

SR = 16000
EN, NE = 0.5, 0.25  # the fake "audio": a block of samples at this level is English / Nepali speech
# what each fake model says for a block: (model, language asked for, language spoken) -> word
SAYS = {("multi", "en", EN): "hello", ("multi", "en", NE): "greetings", ("native", "ne", NE): "नमस्ते",
        ("native", "ne", EN): "हेलो"}


def _blocks(audio):
    """[(start s, end s, level)] of the non-silent runs in the fake audio."""
    out, i = [], 0
    while i < len(audio):
        if audio[i] == 0:
            i += 1
            continue
        j = i
        while j < len(audio) and audio[j] == audio[i]:
            j += 1
        out.append((i / SR, j / SR, float(audio[i])))
        i = j
    return out


class FakeModel:
    def __init__(self, name):
        self.name, self.calls = name, []

    def detect_language(self, audio=None, **kw):
        level = max((b[2] for b in _blocks(audio)), default=0.0)
        # like real Whisper on Nepali: "ne" gets almost nothing, Hindi and English share the rest
        return ("en", 0.9, [("en", 0.9), ("hi", 0.05)]) if level == EN else (
            "hi", 0.5, [("hi", 0.5), ("en", 0.2), ("mr", 0.1), ("ne", 0.01)])

    def transcribe(self, audio, language=None, **kw):
        self.calls.append(language)
        assert kw["word_timestamps"] and kw["vad_filter"]
        segs = [SimpleNamespace(start=s, end=e, no_speech_prob=0.1, text=" " + SAYS[(self.name, language, lvl)],
                                words=[SimpleNamespace(word=" " + SAYS[(self.name, language, lvl)], start=s, end=e)])
                for s, e, lvl in _blocks(audio)]
        return iter(segs), SimpleNamespace(duration=len(audio) / SR, language=language, language_probability=1.0)


def _audio(*blocks, total=10.0):
    a = np.zeros(int(total * SR), dtype=np.float32)
    for s, e, lvl in blocks:
        a[int(s * SR):int(e * SR)] = lvl
    return a


def _run(monkeypatch, audio, units, native=True):
    monkeypatch.setattr(T, "_read_wav", lambda wav: audio)
    monkeypatch.setattr(T, "speech_units", lambda a: units)
    multi, nat = FakeModel("multi"), FakeModel("native") if native else None
    seen = []
    segs, info = T._transcribe_mixed(multi, nat, "clip.wav", "ne+en", seen.append)
    assert seen and seen[-1] == 1.0
    return segs, multi, nat


def test_pick_language_only_compares_english_with_the_native_language():
    assert T.pick_language([("hi", 0.6), ("en", 0.3), ("ne", 0.01)], "ne") == "ne"  # Nepali heard as Hindi
    assert T.pick_language([("ko", 0.4), ("en", 0.2)], "ne") == "ne"
    assert T.pick_language([("en", 0.8), ("ne", 0.1)], "ne") == "en"
    assert T.pick_language([], "hi") == "hi"


def test_merge_chunks_joins_short_pieces_but_keeps_long_stretches_apart():
    assert T.merge_chunks([(0, 1), (1.5, 2)]) == [(0, 2)]  # two short pieces
    assert T.merge_chunks([(0, 5), (5.5, 6)]) == [(0, 6)]  # short tail joins the stretch before it
    assert T.merge_chunks([(0, 5), (5.5, 10)]) == [(0, 5), (5.5, 10)]  # both long: a language switch may be here
    assert T.merge_chunks([(0, 1), (4, 5)]) == [(0, 1), (4, 5)]  # gap too long
    assert T.merge_chunks([(0, 24), (24.5, 26)]) == [(0, 24), (24.5, 26)]  # would exceed max_len
    assert T.merge_chunks([]) == []


def test_runs_group_neighbours_of_one_language():
    units = [(0, 1), (2, 3), (4, 5), (6, 7)]
    assert T.runs(units, ["en", "en", "ne", "en"]) == [("en", [(0, 1), (2, 3)]), ("ne", [(4, 5)]), ("en", [(6, 7)])]


def test_mixed_with_native_model_gives_native_script_plus_english_and_words(monkeypatch):
    audio = _audio((0.5, 2, EN), (3, 4, NE), (4.8, 6, NE), (7, 9, EN))
    segs, multi, nat = _run(monkeypatch, audio, [(0.4, 2.1), (2.9, 4.1), (4.7, 6.1), (6.9, 9.1)])
    assert [(s["lang"], s["text"], s.get("en")) for s in segs] == [
        ("en", "hello", None), ("ne", "नमस्ते", "greetings"), ("ne", "नमस्ते", "greetings"), ("en", "hello", None)]
    assert segs[1]["words"] == [["नमस्ते", 3.0, 4.0]] and segs[2]["words"] == [["नमस्ते", 4.8, 6.0]]
    assert segs[0]["words"] == [["hello", 0.5, 2.0]] and segs[3]["start"] == 7.0  # source-clip time, not run time
    for s in segs:
        assert set(s) >= {"start", "end", "text", "words", "lang"}
        assert all(isinstance(w, str) and s["start"] <= a <= b <= s["end"] for w, a, b in s["words"])
    assert multi.calls == ["en", "en", "en"] and nat.calls == ["ne"]  # one decode per run, the Nepali run twice


def test_mixed_without_native_model_falls_back_to_english_text_marked_nepali(monkeypatch):
    audio = _audio((0.5, 2, EN), (3, 5, NE))
    segs, multi, _ = _run(monkeypatch, audio, [(0.4, 2.1), (2.9, 5.1)], native=False)
    assert [(s["lang"], s["text"]) for s in segs] == [("en", "hello"), ("ne", "greetings")]
    assert all("en" not in s for s in segs)  # the text already is English


def test_merge_fragments_carries_english_rendering_and_languages():
    segs = [{"start": 0, "end": 1, "text": "नमस्ते", "en": "hello", "lang": "ne", "words": [["नमस्ते", 0, 1]]},
            {"start": 1.2, "end": 2, "text": "let's go.", "lang": "en", "words": [["let's", 1.2, 1.5], ["go.", 1.6, 2]]},
            {"start": 9, "end": 12, "text": "Wow.", "lang": "en"}]
    out = T.merge_fragments(segs)
    assert out[0]["text"] == "नमस्ते let's go." and out[0]["en"] == "hello let's go." and out[0]["lang"] == "ne+en"
    assert len(out[0]["words"]) == 3 and out[1] == segs[2]
    assert T.merge_fragments(out) == out
    plain = T.merge_fragments([{"start": 0, "end": 1, "text": "I"}, {"start": 1.2, "end": 5, "text": "go."}])
    assert plain == [{"start": 0, "end": 5, "text": "I go."}]  # single-language transcripts get no new fields


def test_devanagari_words_and_danda_sentences():
    assert T._words("नमस्ते साथीहरू, आज!") == ["नमस्ते", "साथीहरू", "आज"]
    assert T.collapse_loops("म जान्छु। म जान्छु। ठिक छ।") == "म जान्छु। ठिक छ।"
    W = SimpleNamespace
    out = T.split_sentences([W(word=" म", start=0, end=0.3), W(word=" जान्छु।", start=0.4, end=0.9),
                             W(word=" ठिक", start=1.0, end=1.3)])
    assert [s["text"] for s in out] == ["म जान्छु।", "ठिक"]


def test_transcribe_files_loads_the_native_model_only_for_mixed_languages(monkeypatch):
    loaded = []

    def load(name, device, log):
        loaded.append(name)
        return FakeModel("native" if name == "nep-model" else "multi")
    audio = _audio((0.5, 2, EN), (3, 5, NE))
    monkeypatch.setattr(T, "_load", load)
    monkeypatch.setattr(T, "_read_wav", lambda wav: audio)
    monkeypatch.setattr(T, "speech_units", lambda a: [(0.4, 2.1), (2.9, 5.1)])
    logs = []
    (i, segs), = T.transcribe_files("turbo", ["a.wav"], "ne+en", logs.append, native="nep-model")
    assert loaded == ["turbo", "nep-model", T.DETECTOR] and [s["lang"] for s in segs] == ["en", "ne"]
    assert any("Nepali" in m and "English" in m for m in logs)
    loaded.clear()
    monkeypatch.setattr(T, "_transcribe_one", lambda m, w, lang, p: ([], SimpleNamespace(language="en")))
    list(T.transcribe_files("turbo", ["a.wav"], "en", logs.append, native="nep-model"))
    assert loaded == ["turbo"]

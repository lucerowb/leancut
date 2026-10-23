"""Voice-over tests. Offline, no Chatterbox needed: the "server" engine talks to a fake server, "system" skips when
the OS has no voice. Run: python -m pytest -q tests/test_voice.py"""
import importlib.util
import json
import sys
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import voice  # noqa: E402

SR = 16000


def wav_bytes(seconds, rate=24000, freq=220.0):
    """A mono 16-bit sine WAV, as a TTS server would return it."""
    import io
    t = np.arange(int(seconds * rate)) / rate
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((0.3 * np.sin(2 * np.pi * freq * t) * 32767).astype("<i2").tobytes())
    return buf.getvalue()


def wav_info(path):
    with wave.open(str(path), "rb") as w:
        return w.getframerate(), w.getnchannels(), w.getnframes() / w.getframerate()


def speechlike(seconds, level):
    """Syllable-like sound: a 150 Hz buzz with harmonics, pulsing 4 times a second."""
    t = np.arange(int(seconds * SR)) / SR
    buzz = sum(np.sin(2 * np.pi * 150 * k * t) / k for k in (1, 2, 3))
    return level * buzz * (0.6 + 0.4 * np.sin(2 * np.pi * 4 * t) ** 2) / 1.8


def test_pick_voice_sample_prefers_steady_clean_speech(tmp_path):
    rng = np.random.default_rng(0)
    x = rng.normal(0, 0.002, 60 * SR)  # quiet room tone between lines

    def put(start, sig):
        x[int(start * SR):int(start * SR) + len(sig)] = sig

    shout = speechlike(14, 0.2)
    shout[4 * SR:int(4.5 * SR)] = np.clip(speechlike(0.5, 3.0), -1, 1)  # a clipped shout mid-line
    put(1, shout)
    put(20, speechlike(12, 0.2))  # B: steady, the one to pick
    put(34, np.concatenate([speechlike(1, lvl) for lvl in [0.01, 0.6] * 7 + [0.01]]))  # pumping gain
    put(55, speechlike(2, 0.2))  # too short alone
    src = tmp_path / "clip.wav"
    voice._write_wav(src, x, SR)
    segs = [{"start": 1, "end": 15, "text": "a", "score": 5}, {"start": 20, "end": 32, "text": "b", "score": 4},
            {"start": 34, "end": 49, "text": "c", "score": 3}, {"start": 55, "end": 57, "text": "d", "score": 5}]

    got = voice.pick_voice_sample(segs, src, tmp_path / "me.wav")
    assert (got["start"], got["end"], got["seconds"]) == (20, 32, 12)
    rate, ch, dur = wav_info(got["path"])
    assert (rate, ch) == (SR, 1) and dur == pytest.approx(12, abs=0.01)


def test_pick_voice_sample_falls_back_to_longest_and_rejects_silence(tmp_path):
    src = voice._write_wav(tmp_path / "clip.wav", speechlike(30, 0.2), SR)
    short = [{"start": 1, "end": 3, "text": "hi"}, {"start": 26, "end": 29.5, "text": "bye"}]
    assert voice.pick_voice_sample(short, src, tmp_path / "me.wav")["seconds"] == 3.5
    with pytest.raises(voice.VoiceError, match="No speech"):
        voice.pick_voice_sample([], src, tmp_path / "me.wav")


@pytest.fixture
def fake_server():
    """A minimal OpenAI-compatible TTS server returning 1.5 s of audio and remembering the last request."""
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen["path"] = self.path
            seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            data = wav_bytes(1.5)
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1", seen
    srv.shutdown()


def test_server_engine_writes_48k_stereo(tmp_path, fake_server):
    url, seen = fake_server
    assert voice.engines_available(url)["server"] is True
    dur = voice.synthesize("Hello there.", tmp_path / "vo.wav", "server", server_url=url, server_voice="me")
    assert dur == pytest.approx(1.5, abs=0.02)
    assert wav_info(tmp_path / "vo.wav")[:2] == (48000, 2)
    assert seen["path"] == "/v1/audio/speech"
    assert seen["body"] == {"model": voice.SERVER_MODEL, "input": "Hello there.", "voice": "me",
                            "response_format": "wav"}
    # speed is applied locally; a sample path is sent as the voice when no server voice is named
    sample = voice._write_wav(tmp_path / "me.wav", speechlike(1, 0.2), SR)
    assert voice.synthesize("Hi.", tmp_path / "fast.wav", "server", voice_sample=sample, server_url=url,
                            speed=2.0) == pytest.approx(0.75, abs=0.03)
    assert seen["body"]["voice"] == str(sample.resolve())


def test_server_engine_explains_a_missing_server(tmp_path):
    dead = "http://127.0.0.1:9/v1"  # discard port: nothing listens
    assert voice.engines_available(dead)["server"] is False
    assert voice.engines_available()["server"] is False
    with pytest.raises(voice.VoiceError, match="voice_server.py"):
        voice.synthesize("Hi.", tmp_path / "vo.wav", "server", server_url=dead)


def test_synthesize_validates_input(tmp_path):
    for kwargs, msg in [({"text": " ", "engine": "system"}, "text"), ({"text": "hi", "engine": "elevenlabs"}, "Unknown"),
                        ({"text": "hi", "engine": "system", "speed": 3}, "Speed")]:
        with pytest.raises(voice.VoiceError, match=msg):
            voice.synthesize(out_wav=tmp_path / "x.wav", **kwargs)


@pytest.mark.skipif(importlib.util.find_spec("chatterbox") is not None, reason="Chatterbox is installed")
def test_chatterbox_missing_says_how_to_install(tmp_path):
    sample = voice._write_wav(tmp_path / "me.wav", speechlike(1, 0.2), SR)
    assert voice.engines_available()["chatterbox"] is False
    with pytest.raises(voice.VoiceError, match="requirements-voice.txt"):
        voice.synthesize("Hi.", tmp_path / "vo.wav", "chatterbox", voice_sample=sample)


@pytest.mark.skipif(not voice.engines_available()["system"], reason="no OS voice (say / SAPI / espeak)")
def test_system_engine_writes_48k_stereo(tmp_path):
    dur = voice.synthesize("Testing one two three.", tmp_path / "vo.wav", "system")
    rate, ch, length = wav_info(tmp_path / "vo.wav")
    assert (rate, ch) == (48000, 2) and length == pytest.approx(dur) and 0.5 < dur < 10


def test_voice_server_routes(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import voice_server

    def fake_clone(text, sample, out):
        Path(out).write_bytes(wav_bytes(1.0))
        return Path(out)

    monkeypatch.setattr(voice, "chatterbox_wav", fake_clone)
    monkeypatch.setitem(voice_server.VOICES, "default", voice._write_wav(tmp_path / "me.wav", speechlike(1, .2), SR))
    c = TestClient(voice_server.api, base_url="http://127.0.0.1:8880")
    r = c.post("/v1/audio/speech", json={"model": "tts-1", "input": "Hello.", "voice": "default"})
    assert r.status_code == 200 and r.content[:4] == b"RIFF"
    assert c.post("/v1/audio/speech", json={"input": "Hello.", "voice": str(tmp_path / "me.wav")}).status_code == 200
    assert c.post("/v1/audio/speech", json={"input": "Hello.", "voice": "nobody"}).status_code == 400
    assert c.post("/v1/audio/speech", json={"input": " "}).status_code == 400
    assert c.post("/v1/voices?name=../x", files={"file": ("a.wav", wav_bytes(1))}).status_code == 400
    assert c.post("/v1/voices?name=pal", files={"file": ("a.wav", wav_bytes(1))}).json() == {"voice": "pal"}
    assert "pal" in c.get("/v1/voices").json()["voices"]
    evil = TestClient(voice_server.api, base_url="http://attacker.example")
    assert evil.post("/v1/audio/speech", json={"input": "Hello."}).status_code == 403

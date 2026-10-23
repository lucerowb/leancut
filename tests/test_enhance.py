"""Speech enhancement tests. The DSP runs offline with dummy "models"; tests that need DeepFilterNet3 run only when
LEANCUT_ENHANCE_MODELS points at a folder where enhance.ensure_model() has already downloaded it.
Run: python -m pytest -q tests/test_enhance.py"""
import os
import shutil
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import enhance  # noqa: E402
import video_engine  # noqa: E402

SR = enhance.SR
MODELS = os.environ.get("LEANCUT_ENHANCE_MODELS")
needs_model = pytest.mark.skipif(
    not MODELS or not all((Path(MODELS) / enhance.MODEL["name"] / f).exists() for f in enhance.MODEL["files"]),
    reason="DeepFilterNet3 not downloaded (set LEANCUT_ENHANCE_MODELS to a folder holding it)")


def write_wav(path, x):
    x = np.atleast_2d(x)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(len(x))
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(np.clip(np.round(x.T * 32768), -32768, 32767).astype("<i2").tobytes())
    return path


def read_wav(path):
    with wave.open(str(path), "rb") as w:
        assert w.getframerate() == SR and w.getsampwidth() == 2
        return np.frombuffer(w.readframes(w.getnframes()), "<i2").reshape(-1, w.getnchannels()).T.astype(np.int32)


def noise(channels, seconds, seed=0):
    return np.random.default_rng(seed).normal(0, 0.1, (channels, int(seconds * SR))).clip(-1, 1)


def identity(feat_erb, feat_spec):
    """A "model" that changes nothing: unit ERB gains, and a deep filter that passes the current frame."""
    coefs = np.zeros((enhance.DF_ORDER, len(feat_erb), enhance.NB_DF), np.complex64)
    coefs[enhance.LOOKAHEAD] = 1
    return np.ones((len(feat_erb), enhance.NB_ERB), np.float32), coefs


def framewise(feat_erb, feat_spec):
    """A memoryless "model" that does change the sound, driven by the real features (lookahead shift included)."""
    coefs = np.zeros((enhance.DF_ORDER, len(feat_erb), enhance.NB_DF), np.complex64)
    coefs[1] = 0.3 * np.tanh(feat_erb[:, :1])
    coefs[2] = 0.5
    coefs[3] = 0.1 * feat_spec
    return 1 / (1 + np.exp(-4 * feat_erb)), coefs


def test_erb_bands_match_deepfilternet3():
    w = enhance.erb_widths()
    assert len(w) == enhance.NB_ERB and w.sum() == enhance.FFT // 2 + 1 and w.min() >= 2
    # libDF's erb_fb(48000, 960, 32, 2); the full pipeline matched the PyTorch reference to 1 LSB with these
    assert w.tolist() == [2] * 13 + [5, 5, 7, 7, 8, 10, 12, 13, 15, 18, 20, 24, 28, 31, 37, 42, 50, 56, 67]


def test_stft_istft_reconstruct_perfectly_across_blocks():
    x = noise(2, 1.0).astype(np.float32)[:, :100 * enhance.HOP]
    mem_a = mem_s = np.zeros((2, enhance.FFT - enhance.HOP), np.float32)
    out = []
    for blk in np.split(x, [7 * enhance.HOP, 8 * enhance.HOP, 60 * enhance.HOP], axis=1):  # uneven blocks
        spec, mem_a = enhance.stft(blk, mem_a)
        assert spec.shape == (2, blk.shape[1] // enhance.HOP, enhance.FFT // 2 + 1)
        y, mem_s = enhance.istft(spec, mem_s)
        out.append(y)
    y = np.concatenate(out, axis=1)
    d = enhance.FFT - enhance.HOP  # the STFT's algorithmic delay
    np.testing.assert_allclose(y[:, d:], x[:, :-d], atol=1e-5)


def test_feature_shapes_and_state_carry_over():
    spec, _ = enhance.stft(noise(2, 0.5).astype(np.float32)[:, :50 * enhance.HOP],
                           np.zeros((2, enhance.FFT - enhance.HOP), np.float32))
    widths = enhance.erb_widths()
    erb, cplx, state = enhance.features(spec, enhance.init_state(2), widths)
    assert erb.shape == (2, 50, enhance.NB_ERB) and cplx.shape == (2, 50, enhance.NB_DF)
    assert np.isfinite(erb).all() and np.isfinite(cplx).all()
    erb2, cplx2, _ = enhance.features(spec[:, 20:], enhance.features(spec[:, :20], enhance.init_state(2), widths)[2],
                                      widths)
    np.testing.assert_allclose(erb2, erb[:, 20:], atol=1e-5)  # a split stream normalises like a whole one
    np.testing.assert_allclose(cplx2, cplx[:, 20:], atol=1e-5)
    _, coefs = identity(erb[0, :10], cplx[0, :10])
    sp = spec[0, :14]  # 10 frames plus LOOKAHEAD frames of context either side
    np.testing.assert_allclose(enhance.apply(sp, np.ones((10, enhance.NB_ERB), np.float32), coefs, widths), sp[2:12])
    shut = enhance.apply(sp, np.zeros((10, enhance.NB_ERB), np.float32), coefs, widths)
    assert not shut[:, enhance.NB_DF:].any()  # the ERB mask owns the high bins
    np.testing.assert_allclose(shut[:, :enhance.NB_DF], sp[2:12, :enhance.NB_DF])  # the deep filter owns the low ones


@pytest.mark.parametrize("channels", [1, 2])
def test_identity_model_is_transparent_at_every_block_size(tmp_path, channels):
    src = write_wav(tmp_path / "in.wav", noise(channels, 1.0)[:, :SR + 123])  # not a whole number of frames
    for block, warmup in [(1, 2), (7, 3), (40, 10), (enhance.BLOCK, enhance.WARMUP)]:
        out = enhance._process(src, tmp_path / "out.wav", identity, 1.0, lambda f: None, block=block, warmup=warmup)
        np.testing.assert_array_equal(read_wav(out), read_wav(src))
    assert not list(tmp_path.glob("*.part.wav"))


def test_block_boundaries_do_not_change_the_output(tmp_path):
    src = write_wav(tmp_path / "in.wav", noise(2, 1.2))
    progress = []
    whole = read_wav(enhance._process(src, tmp_path / "a.wav", framewise, 0.8, progress.append, block=10_000))
    assert progress[-1] == 1.0
    for block in (1, 5, 33):
        np.testing.assert_array_equal(
            read_wav(enhance._process(src, tmp_path / "b.wav", framewise, 0.8, lambda f: None, block=block, warmup=2)),
            whole)
    dry = read_wav(src)
    assert np.abs(whole - dry).max() > 1000  # the dummy really does change the sound


def test_strength_blends_dry_and_wet(tmp_path):
    src = write_wav(tmp_path / "in.wav", noise(1, 0.5))
    run = lambda s: read_wav(enhance._process(src, tmp_path / f"{s}.wav", framewise, s, lambda f: None))  # noqa: E731
    dry, wet, half = read_wav(src), run(1.0), run(0.5)
    assert np.abs(half - (dry + wet) / 2).max() <= 2
    out = enhance.enhance_wav(src, tmp_path / "zero.wav", tmp_path / "no-model-needed", strength=0)
    np.testing.assert_array_equal(read_wav(out), dry)


def test_rejects_other_sample_rates(tmp_path):
    p = tmp_path / "16k.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\0\0" * 1600)
    with pytest.raises(ValueError, match="48000"):
        enhance._process(p, tmp_path / "o.wav", identity, 1.0, lambda f: None)


def test_cancel_stops_and_leaves_no_partial_file(tmp_path):
    src = write_wav(tmp_path / "in.wav", noise(1, 2.0))

    def cancel_midway(f):
        if f > 0.3:
            video_engine._cancel.set()
    try:
        with pytest.raises(video_engine.Cancelled):
            enhance._process(src, tmp_path / "o.wav", identity, 1.0, cancel_midway, block=20)
    finally:
        video_engine.reset_cancel()
    assert not (tmp_path / "o.wav").exists() and not list(tmp_path.glob("*.part.wav"))


def spoken(tmp_path):
    """A real voice from macOS text-to-speech at 48 kHz (DeepFilterNet ignores synthetic buzzes: they aren't speech)."""
    if not shutil.which("say"):
        pytest.skip("needs macOS `say` for a voice recording")
    aiff = tmp_path / "voice.aiff"
    subprocess.run(["say", "-v", "Daniel", "-o", str(aiff), "The road is still wet from this morning, so I'm taking "
                    "the hairpins slowly. The next village should have a pump in maybe twenty kilometres."], check=True)
    video_engine._ffmpeg(["-y", "-i", str(aiff), "-ac", "1", "-ar", str(SR), "-c:a", "pcm_s16le",
                          str(tmp_path / "voice.wav")])
    return read_wav(tmp_path / "voice.wav")[0] / 32768


def engine(n, seed=2):
    """Brown noise plus a drifting ~45 Hz firing hum with harmonics: road rumble and engine."""
    rng = np.random.default_rng(seed)
    t = np.arange(n) / SR
    brown = np.cumsum(rng.normal(0, 1, n))
    brown -= np.convolve(brown, np.ones(4801) / 4801, "same")
    ph = 2 * np.pi * np.cumsum(45 + 6 * np.sin(2 * np.pi * 0.1 * t)) / SR
    hum = sum(np.sin(k * ph + k) / k for k in range(1, 20))
    return brown / brown.std() + hum / hum.std()


def snr(clean, x):
    return 10 * np.log10(np.sum(clean ** 2) / np.sum((clean - x) ** 2))


@needs_model
def test_deepfilternet_lifts_voice_out_of_engine_noise(tmp_path):
    clean = spoken(tmp_path)
    n = engine(len(clean))
    n *= np.sqrt(np.mean(clean ** 2) / np.mean(n ** 2))  # 0 dB SNR
    g = 0.5 / np.abs(clean + n).max()
    clean, noisy = clean * g, (clean + n) * g
    src = write_wav(tmp_path / "noisy.wav", np.stack([noisy, noisy]))
    out = read_wav(enhance.enhance_wav(src, tmp_path / "enh.wav", MODELS)) / 32768
    assert out.shape == (2, len(clean))
    gain = snr(clean, out[0]) - snr(clean, noisy)
    assert gain > 5, f"SNR improved by only {gain:.1f} dB"


@needs_model
def test_enhance_video_audio_round_trip(tmp_path):
    src = write_wav(tmp_path / "a.wav", np.stack([noise(1, 2.0)[0], noise(1, 2.0, seed=3)[0]]) * 0.3)
    video = tmp_path / "ride.mp4"
    video_engine._ffmpeg(["-y", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=2", "-i", str(src), "-shortest",
                          "-c:v", "libx264", *video_engine.AUDIO_ARGS, str(video)])
    out = enhance.enhance_video_audio(video, tmp_path / "out" / "voice.wav", MODELS, strength=0.7)
    with wave.open(str(out), "rb") as w:
        assert (w.getframerate(), w.getnchannels()) == (SR, 2)
        assert abs(w.getnframes() / SR - 2.0) < 0.1
    assert sorted(p.name for p in out.parent.iterdir()) == ["voice.wav"]  # the extracted source is cleaned up

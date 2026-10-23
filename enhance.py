"""Neural speech enhancement: lift the rider's voice out of engine rumble and wind, on this machine.

DeepFilterNet3 (Schröter et al., MIT/Apache-2.0) through its official ONNX export and onnxruntime. The networks only
see features; everything around them (STFT, ERB features, running normalisation, ERB mask, deep filtering, ISTFT) is
reimplemented in numpy after the reference libDF (Rust) and df/enhance.py. Full band at 48 kHz, so the voice keeps its
air. Audio is streamed in 10 s blocks, so memory stays flat however long the ride (~0.6 GB peak, mostly the
networks' activations) and a 30-minute stereo file takes well under a minute on a laptop CPU.
"""
import hashlib
import shutil
import tarfile
import urllib.error
import urllib.request
import wave
from pathlib import Path

import video_engine

_COMMIT = "d375b2d8309e0935d165700c91da9de862a99c31"  # pinned so the checksum below stays valid
MODEL = {
    "name": "DeepFilterNet3",
    "license": "MIT OR Apache-2.0",
    "urls": [f"https://raw.githubusercontent.com/Rikorose/DeepFilterNet/{_COMMIT}/models/DeepFilterNet3_onnx.tar.gz"],
    "sha256": "c94d91f70911001c946e0fabb4aa9adc37045f45a03b56008cb0c8244cb63616",
    "files": ["enc.onnx", "erb_dec.onnx", "df_dec.onnx", "config.ini"],
    "size_mb": 8.0,
}

# The model's [df] config; fixed, since the download is pinned by checksum.
SR, FFT, HOP = 48000, 960, 480
NB_ERB, NB_DF, DF_ORDER = 32, 96, 5
LOOKAHEAD = 2  # frames the model and the deep filter peek ahead (conv_lookahead = df_lookahead = 2)
ALPHA = 0.99  # running-normalisation decay, exp(-hop/sr / 1 s) rounded like libDF's calc_norm_alpha
BLOCK = 1000  # frames per model call (10 s); peak memory grows ~0.3 MB per frame of a call
WARMUP = 500  # earlier frames re-run before each block so the GRUs start warm (5 s)


def ensure_model(models_dir) -> Path:
    """Download the ONNX export once (≈8 MB) into models_dir/DeepFilterNet3; later calls work offline."""
    d = Path(models_dir) / MODEL["name"]
    if all((d / f).exists() for f in MODEL["files"]):
        return d
    d.mkdir(parents=True, exist_ok=True)
    tgz = d / "download.tar.gz.part"
    try:
        req = urllib.request.Request(MODEL["urls"][0], headers={"User-Agent": "LeanCut"})
        with urllib.request.urlopen(req, timeout=60) as r, open(tgz, "wb") as f:
            shutil.copyfileobj(r, f)
        if hashlib.sha256(tgz.read_bytes()).hexdigest() != MODEL["sha256"]:
            raise RuntimeError("The voice enhancement model download is corrupt (checksum mismatch). Try again.")
        with tarfile.open(tgz, "r:gz") as tar:
            for m in tar.getmembers():  # only known names, written by us: no path tricks from the archive
                name = Path(m.name).name
                if m.isfile() and name in MODEL["files"]:
                    part = d / (name + ".part")
                    part.write_bytes(tar.extractfile(m).read())
                    part.replace(d / name)
    except urllib.error.URLError as e:
        raise RuntimeError(f"Couldn't download the voice enhancement model ({e.reason}). "
                           "Check the internet connection; it is only needed once.") from e
    finally:
        tgz.unlink(missing_ok=True)
    missing = [f for f in MODEL["files"] if not (d / f).exists()]
    if missing:
        raise RuntimeError(f"The voice enhancement model archive lacks {', '.join(missing)}.")
    return d


# ---------------------------------------------------------------- signal processing (libDF in numpy)

def erb_widths(sr=SR, fft=FFT, nb=NB_ERB, min_freqs=2):
    """FFT bins per ERB band (libDF erb_fb): equal steps on the ERB scale, at least min_freqs bins each."""
    import numpy as np
    f32 = np.float32  # libDF computes in f32; same precision, same rounding at band edges
    erb_high = f32(9.265) * np.log1p(f32(sr // 2) / f32(24.7 * 9.265))
    step = erb_high / f32(nb)
    widths, prev, over = [], 0, 0
    for i in range(1, nb + 1):
        f = f32(24.7 * 9.265) * (np.exp(f32(i) * step / f32(9.265)) - f32(1))
        fb = int(np.floor(f / f32(sr / fft) + f32(0.5)))  # Rust round(): half away from zero
        n = fb - prev - over
        over, n = (min_freqs - n, min_freqs) if n < min_freqs else (0, n)
        widths.append(n)
        prev = fb
    widths[-1] += 1  # the Nyquist bin
    widths[-1] -= max(0, sum(widths) - (fft // 2 + 1))
    return np.array(widths)


def window(fft=FFT):
    """Vorbis window: w² overlap-adds to 1 at 50 % overlap, so analysis + synthesis reconstruct perfectly."""
    import numpy as np
    return np.sin(0.5 * np.pi * np.sin(0.5 * np.pi * (np.arange(fft) + 0.5) / (fft // 2)) ** 2).astype(np.float32)


def stft(x, mem):
    """[C, n*HOP] new samples + [C, FFT-HOP] carried samples -> ([C, n, F] spectrum, new carry). libDF scaling."""
    import numpy as np
    buf = np.concatenate([mem, x], axis=1)
    frames = np.lib.stride_tricks.sliding_window_view(buf, FFT, axis=1)[:, ::HOP]
    spec = (np.fft.rfft(frames * window(), axis=-1) / FFT).astype(np.complex64)
    return spec, buf[:, buf.shape[1] - (FFT - HOP):]


def istft(spec, mem):
    """[C, n, F] spectrum + [C, FFT-HOP] overlap carry -> ([C, n*HOP] samples, new carry)."""
    import numpy as np
    x = np.fft.irfft(spec, n=FFT, axis=-1).astype(np.float32) * (FFT * window())
    out = x[:, :, :HOP].copy()
    out[:, 0] += mem
    out[:, 1:] += x[:, :-1, HOP:]
    return out.reshape(len(out), -1), x[:, -1, HOP:]


def features(spec, state, widths):
    """ERB band energies in dB and the low NB_DF bins, each normalised by a slow running mean (libDF
    band_mean_norm_erb / band_unit_norm). state = (mean [C, NB_ERB], unit [C, NB_DF]) carries across blocks."""
    import numpy as np
    power = spec.real ** 2 + spec.imag ** 2
    erb = 10 * np.log10(np.add.reduceat(power, np.cumsum(widths) - widths, axis=-1) / widths + 1e-10)
    mag = np.sqrt(power[..., :NB_DF])
    mean, unit = state
    means, units = np.empty_like(erb), np.empty_like(mag)
    for t in range(spec.shape[1]):  # a recursive filter; 100 frames/s, so cheap next to the networks
        mean = erb[:, t] * (1 - ALPHA) + mean * ALPHA
        unit = mag[:, t] * (1 - ALPHA) + unit * ALPHA
        means[:, t], units[:, t] = mean, unit
    return (erb - means) / 40, spec[..., :NB_DF] / np.sqrt(units), (mean, unit)


def init_state(channels):
    import numpy as np
    return (np.tile(np.linspace(-60, -90, NB_ERB, dtype=np.float32), (channels, 1)),
            np.tile(np.linspace(0.001, 0.0001, NB_DF, dtype=np.float32), (channels, 1)))


def apply(spec, mask, coefs, widths):
    """Stage 1: ERB gains on every bin. Stage 2: deep filtering of the low NB_DF bins, a complex filter over DF_ORDER
    frames (t-2 … t+2) that rebuilds the voice's harmonics instead of only attenuating noise.
    spec [T + 2*LOOKAHEAD, F] (context frames each side), mask [T, NB_ERB], coefs [DF_ORDER, T, NB_DF] -> [T, F]."""
    t = len(mask)
    out = spec[LOOKAHEAD:LOOKAHEAD + t] * mask.repeat(widths, axis=-1)
    out[:, :NB_DF] = sum(spec[n:n + t, :NB_DF] * coefs[n] for n in range(DF_ORDER))
    return out


def load_model(models_dir):
    """The three ONNX networks as one function: (feat_erb [T, NB_ERB], feat_spec [T, NB_DF] complex) ->
    (mask [T, NB_ERB], coefs [DF_ORDER, T, NB_DF] complex). Inputs must already be shifted by LOOKAHEAD frames."""
    import numpy as np
    import onnxruntime as ort
    d = ensure_model(models_dir)
    opts = ort.SessionOptions()
    opts.log_severity_level = 3
    enc, erb_dec, df_dec = (ort.InferenceSession(str(d / f"{n}.onnx"), opts, providers=["CPUExecutionProvider"])
                            for n in ("enc", "erb_dec", "df_dec"))

    def run(feat_erb, feat_spec):
        spec_in = np.stack([feat_spec.real, feat_spec.imag])[None].astype(np.float32)
        e0, e1, e2, e3, emb, c0, _ = enc.run(None, {"feat_erb": feat_erb[None, None].astype(np.float32),
                                                    "feat_spec": spec_in})
        mask = erb_dec.run(None, {"emb": emb, "e3": e3, "e2": e2, "e1": e1, "e0": e0})[0][0, 0]
        c = df_dec.run(["coefs"], {"emb": emb, "c0": c0})[0][0].reshape(len(mask), NB_DF, DF_ORDER, 2)
        return mask, (c[..., 0] + 1j * c[..., 1]).transpose(2, 0, 1).astype(np.complex64)
    return run


# ---------------------------------------------------------------- streaming

def _rows(buf, base, lo, hi):
    """Frames [lo, hi) of a [C, frames, X] buffer whose first row is frame `base`; zeros past its end (the signal's
    tail padding) and before frame 0."""
    import numpy as np
    have = buf.shape[1]
    rows = buf[:, max(lo - base, 0):max(min(hi - base, have), 0)]
    return np.pad(rows, ((0, 0), (max(base - lo, 0), max(hi - base - have, 0)), (0, 0)))


def _process(in_wav, out_wav, model, strength, on_progress, block=BLOCK, warmup=WARMUP):
    """Stream in_wav through STFT -> features -> model -> mask + deep filter -> ISTFT into out_wav.

    Same result as df/enhance.py on the whole file (checked to 1 LSB with one block), except that each block's
    networks start from `warmup` frames of earlier audio instead of the whole past. The output is aligned with the
    input (the FFT-HOP STFT delay is dropped): same length, rate and channels.
    """
    # shortcut: the ONNX export has no GRU state in/out, so every block re-runs a warm-up and differs from one long
    # pass by ~-40 dB (inaudible under the residual noise); export stateful GRUs if exact streaming is ever needed.
    import numpy as np
    widths = erb_widths()
    out_wav = Path(out_wav)
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_wav.with_suffix(".part.wav")
    try:
        with wave.open(str(in_wav), "rb") as r:
            rate, width, ch, n_in = r.getframerate(), r.getsampwidth(), r.getnchannels(), r.getnframes()
        if rate != SR or width != 2:
            raise ValueError(f"Expected a {SR} Hz 16-bit WAV, got {rate} Hz {8 * width}-bit.")
        with wave.open(str(in_wav), "rb") as r, wave.open(str(tmp), "wb") as w:
            w.setnchannels(ch)
            w.setsampwidth(2)
            w.setframerate(SR)
            total = (n_in + FFT) // HOP  # like df/enhance.py: FFT zeros flush the last samples out of the STFT
            mem_a = mem_s = np.zeros((ch, FFT - HOP), np.float32)
            state = init_state(ch)
            spec = np.zeros((ch, 0, FFT // 2 + 1), np.complex64)
            f_erb, f_spec = np.zeros((ch, 0, NB_ERB), np.float32), np.zeros((ch, 0, NB_DF), np.complex64)
            base = done = avail = 0  # buffers hold frames [base, avail); frames before `done` are written
            skip, left = FFT - HOP, n_in  # output samples still to drop (STFT delay) / to write
            while done < total:
                video_engine.check_cancel()
                want = min(block, total - avail) * HOP
                x = np.frombuffer(r.readframes(want), dtype="<i2").reshape(-1, ch).T.astype(np.float32) / 32768
                s, mem_a = stft(np.pad(x, ((0, 0), (0, want - x.shape[1]))), mem_a)  # zeros past the end
                fe, fs, state = features(s, state, widths)
                spec, f_erb, f_spec = (np.concatenate(p, axis=1) for p in ((spec, s), (f_erb, fe), (f_spec, fs)))
                avail += s.shape[1]
                end = total if avail == total else avail - LOOKAHEAD  # frames whose look-ahead has arrived
                if end > done:
                    a = max(base, done - warmup)  # first frame fed to the networks
                    fe, fs = (_rows(f, base, a + LOOKAHEAD, end + LOOKAHEAD) for f in (f_erb, f_spec))
                    sp = _rows(spec, base, done - LOOKAHEAD, end + LOOKAHEAD)
                    y = np.empty((ch, end - done, FFT // 2 + 1), np.complex64)
                    for c in range(ch):  # shortcut: stereo runs the networks per channel (2x time), fine at RTF 0.02
                        mask, coefs = model(fe[c], fs[c])
                        y[c] = apply(sp[c], mask[done - a:], coefs[:, done - a:], widths)
                    if strength < 1:  # mix the dry signal back: an attenuation limit of -20·log10(1 - strength) dB
                        y = strength * y + (1 - strength) * sp[:, LOOKAHEAD:LOOKAHEAD + end - done]
                    out, mem_s = istft(y, mem_s)
                    out = out[:, skip:skip + left]
                    skip -= min(skip, y.shape[1] * HOP)
                    left -= out.shape[1]
                    w.writeframes(np.clip(np.round(out.T * 32768), -32768, 32767).astype("<i2").tobytes())
                    done = end
                    drop = max(0, done - max(warmup, LOOKAHEAD) - base)  # keep what the next block looks back on
                    spec, f_erb, f_spec, base = spec[:, drop:], f_erb[:, drop:], f_spec[:, drop:], base + drop
                on_progress(done / total)
    except BaseException:  # cancelled or failed: never leave a half-written WAV that a later step would trust
        tmp.unlink(missing_ok=True)
        raise
    return tmp.replace(out_wav)


def enhance_wav(in_wav, out_wav, models_dir, strength=1.0, on_progress=lambda f: None) -> Path:
    """48 kHz 16-bit WAV (mono or stereo) -> same rate/channels/length with the voice lifted out of the noise.

    strength 0..1 blends dry and enhanced: 1 = full suppression, 0.75 keeps the noise 12 dB down, 0.5 keeps 6 dB.
    Some road noise left under the voice usually sounds more natural in a vlog than dead silence."""
    strength = min(max(float(strength), 0.0), 1.0)
    if strength == 0:
        Path(out_wav).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(in_wav, out_wav)
        on_progress(1.0)
        return Path(out_wav)
    return _process(in_wav, out_wav, load_model(models_dir), strength, on_progress)


def enhance_video_audio(video_in, wav_out, models_dir, strength=1.0, on_progress=lambda f: None) -> Path:
    """Extract a rendered video's audio at 48 kHz, enhance it and write wav_out; the caller muxes it back."""
    wav_out = Path(wav_out)
    wav_out.parent.mkdir(parents=True, exist_ok=True)
    src = wav_out.with_name(wav_out.stem + ".src.wav")
    try:
        video_engine._ffmpeg(["-y", *video_engine.inp(video_in), "-vn", "-map", "0:a:0", "-ar", str(SR),
                              "-c:a", "pcm_s16le", str(src)])
        return enhance_wav(src, wav_out, models_dir, strength, on_progress)
    finally:
        src.unlink(missing_ok=True)

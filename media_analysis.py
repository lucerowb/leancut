"""Signals beyond the transcript: scenic B-roll, loud moments, camera sync, thumbnails, Shorts and ride-through
connectors. Everything uses FFmpeg + numpy only — no extra models to download.

These are heuristics, not classifiers: "interesting" footage = steady movement + colour + sane exposure,
a "moment" = a sudden loudness spike (shout, laugh, engine rev) relative to the rest of that clip.
"""
import json
import re
from pathlib import Path

import video_engine as ve


def _np():
    import numpy as np  # numpy ships with faster-whisper; imported lazily so the API starts fast
    return np


# ---------------------------------------------------------------- scenic B-roll

def keyframe_stats(src, cache_file=None):
    """[{t, motion, y, sat}] for every keyframe. Decoding keyframes only keeps a 4K hour to seconds or minutes."""
    if cache_file and Path(cache_file).exists():
        return json.loads(Path(cache_file).read_text(encoding="utf-8"))
    # format=yuv420p: 10-bit footage (DJI, GoPro HEVC) would otherwise report brightness on a 0-1023 scale
    out = ve._ffmpeg(["-skip_frame", "nokey", "-i", str(src), "-an", "-vf",
                      "scale=160:-2,format=yuv420p,select='gte(scene,0)',signalstats,metadata=print:file=-",
                      "-f", "null", "-"]).stdout
    rows, cur = [], None
    for line in out.splitlines():
        if line.startswith("frame:"):
            m = re.search(r"pts_time:([\d.]+)", line)
            cur = {"t": float(m.group(1))} if m else None
            if cur:
                rows.append(cur)
        elif cur is not None and "=" in line:
            key, val = line.split("=", 1)
            name = {"lavfi.scene_score": "motion", "lavfi.signalstats.YAVG": "y",
                    "lavfi.signalstats.SATAVG": "sat"}.get(key.strip())
            if name:
                cur[name] = float(val)
    if cache_file:
        Path(cache_file).write_text(json.dumps(rows), encoding="utf-8")
    return rows


def interest(stats):
    """Per-keyframe 0..1 'worth watching' score, ranked within the clip so it adapts to each camera."""
    np = _np()
    if not stats:
        return []
    motion = np.array([r.get("motion", 0) for r in stats])
    sat = np.array([r.get("sat", 0) for r in stats])
    y = np.array([r.get("y", 128) for r in stats])
    def rank(a):  # percentile rank where equal values share a rank, so results don't depend on sort order
        s = np.sort(a)
        return (np.searchsorted(s, a, "left") + np.searchsorted(s, a, "right") - 1) / 2 / max(len(a) - 1, 1)
    pm, ps = rank(motion), rank(sat)
    moving = np.where(pm > 0.95, 0.4, pm)  # the shakiest 5% is usually a bump or a pan, not a view
    exposed = (y > 40) & (y < 225)
    return list(np.where(exposed, 0.6 * moving + 0.4 * ps, 0.0))


def broll_candidates(duration, speech, stats, win=5.0, min_gap=6.0, per_gap=2):
    """Windows inside the silent stretches of a clip, best first: [{start, end, interest}]."""
    scores = interest(stats)
    times = [r["t"] for r in stats]
    edges, t = [], 0.0
    for s in sorted(speech, key=lambda s: s["start"]):
        edges.append((t, s["start"] - 0.5))
        t = s["end"] + 0.5
    edges.append((t, duration))
    out = []
    for a, b in edges:
        if b - a < min_gap:
            continue
        wins = []
        x = a
        while x + win <= b:
            vals = [sc for tt, sc in zip(times, scores) if x <= tt < x + win]
            if len(vals) >= 2:
                wins.append({"start": round(x, 2), "end": round(x + win, 2), "interest": round(sum(vals) / len(vals), 3)})
            x += win / 2
        picked = []
        for w in sorted(wins, key=lambda w: -w["interest"]):  # a few non-overlapping windows per silent stretch
            if len(picked) < per_gap and all(w["end"] <= p["start"] or w["start"] >= p["end"] for p in picked):
                picked.append(w)
        out += picked
    return sorted(out, key=lambda w: -w["interest"])


def pick_broll(candidates, budget):
    """candidates: [(clip_id, window)] best first; take windows until `budget` seconds are used."""
    picked, used = [], 0.0
    for cid, w in candidates:
        if used >= budget:
            break
        if w["interest"] <= 0:
            continue
        picked.append((cid, w))
        used += w["end"] - w["start"]
    return picked


# ---------------------------------------------------------------- audio moments & sync

def _envelope(wav, rate=100, max_seconds=None):
    """Loudness envelope (dB) of a 16 kHz mono WAV at `rate` frames per second."""
    from transcriber import _read_wav
    np = _np()
    x = _read_wav(wav)
    if max_seconds:
        x = x[:int(max_seconds * 16000)]
    hop = 16000 // rate
    n = len(x) // hop
    rms = np.sqrt((x[:n * hop].reshape(n, hop) ** 2).mean(axis=1) + 1e-10)
    return 20 * np.log10(rms)


def loud_moments(wav, jump_db=8.0, rate=10, context=10.0):
    """Sudden loudness spikes: ≥ jump_db louder than the surrounding ±context seconds and among the clip's
    loudest 10%, for at least half a second: [{start, end, jump}]. A local baseline matters on rides, where
    road noise and talk/silence swings make any whole-clip baseline useless."""
    np = _np()
    from numpy.lib.stride_tricks import sliding_window_view
    db = _envelope(wav, rate)
    if len(db) < rate * 5:
        return []
    half = int(context * rate)
    base = np.median(sliding_window_view(np.pad(db, half, mode="edge"), 2 * half + 1)[:, ::5], axis=1)
    jump = db - base
    hot = (jump > jump_db) & (db > np.percentile(db, 90))
    out, start = [], None
    for i, v in enumerate(list(hot) + [False]):
        if v and start is None:
            start = i
        elif not v and start is not None:
            if i - start >= rate // 2:
                out.append({"start": start / rate, "end": i / rate, "jump": round(float(jump[start:i].max()), 1)})
            start = None
    return out


def sync_offset(wav_ref, wav_other, rate=100, max_seconds=900):
    """Where `other` sits on `ref`'s timeline: t_ref = t_other + offset. Returns (offset_seconds, confidence).
    Cross-correlates loudness envelopes, so it works across cameras with different mics and gain."""
    np = _np()
    a, b = (_envelope(w, rate, max_seconds) for w in (wav_ref, wav_other))
    a, b = a - a.mean(), b - b.mean()
    n = 1 << int(np.ceil(np.log2(len(a) + len(b))))
    corr = np.fft.irfft(np.fft.rfft(a, n) * np.conj(np.fft.rfft(b, n)), n)
    lag = int(np.argmax(corr))
    confidence = float((corr[lag] - corr.mean()) / (corr.std() + 1e-9))
    if lag > n // 2:
        lag -= n
    return round(lag / rate, 3), round(confidence, 1)


# ---------------------------------------------------------------- thumbnails, Shorts, connectors

def sharpness(gray):
    np = _np()
    g = gray.astype(np.float32)
    lap = g[1:-1, 2:] + g[1:-1, :-2] + g[2:, 1:-1] + g[:-2, 1:-1] - 4 * g[1:-1, 1:-1]
    return float(lap.var())


def thumbnails(candidates, out_dir, n=6, name="thumb"):
    """candidates: [(src, t, score 1-5)]. Saves the n sharpest, well-exposed frames as JPEGs; returns paths."""
    rated = []
    for src, t, score in candidates[:30]:
        g = ve.frame_gray(src, t)
        if g is None:
            continue
        mean = float(g.mean())
        exposure = 1.0 if 50 < mean < 200 else 0.4
        rated.append((sharpness(g) * exposure * (1 + 0.15 * score), src, t))
    rated.sort(reverse=True)
    paths = []
    for i, (_, src, t) in enumerate(rated[:n], 1):
        paths.append(ve.save_frame(src, t, Path(out_dir) / f"{name}_{i}.jpg"))
    return paths


def shorts_windows(clips, n=3, min_len=15.0, max_len=58.0, max_gap=3.0):
    """Best continuous stretches for vertical Shorts: [(clip, start, end, value)], value = time-weighted score."""
    cands = []
    for c in clips:
        segs = sorted([s for s in c.get("segments") or [] if s.get("score")], key=lambda s: s["start"])
        for i in range(len(segs)):
            total = weighted = 0.0
            for j in range(i, len(segs)):
                if j > i and segs[j]["start"] - segs[j - 1]["end"] > max_gap:
                    break
                if segs[j]["end"] - segs[i]["start"] > max_len:
                    break
                d = segs[j]["end"] - segs[j]["start"]
                total, weighted = total + d, weighted + d * segs[j]["score"]
                span = segs[j]["end"] - segs[i]["start"]
                if span >= min_len:
                    cands.append((weighted / total + 0.002 * span, c, segs[i]["start"], segs[j]["end"]))
    picked = []
    for value, c, a, b in sorted(cands, key=lambda x: -x[0]):
        if len(picked) == n:
            break
        if all(c is not pc or b <= pa or a >= pb for pc, pa, pb, _ in picked):
            picked.append((c, a, b, round(value, 2)))
    return picked


def connectors(kept_ranges, duration, min_gap=40.0, length=6.0, max_speed=64.0):
    """Fast-forward 'ride-through' ranges for long dropped stretches between kept ranges of one clip:
    [(start, end, speed)]. Keeps the sense of the journey instead of jumping straight to the next stop."""
    out, prev = [], 0.0
    for a, b in sorted(kept_ranges) + [(duration, duration)]:
        gap = a - prev
        if gap >= min_gap:
            speed = min(max_speed, gap / length)
            span = speed * length
            mid = (prev + a) / 2
            out.append((round(mid - span / 2, 3), round(mid + span / 2, 3), round(speed, 2)))
        prev = max(prev, b)
    return out

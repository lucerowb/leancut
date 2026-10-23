"""GoPro telemetry (GPMF): speed, accelerometer and GPS track -> ride highlights and boring stretches.

GoPro MP4s carry a 'gpmd' data stream ("GoPro MET") of GPMF records: KLV items (4-char key, type char, struct size,
big-endian repeat count, payload padded to 4 bytes), nested DEVC -> STRM -> samples, one DEVC payload per ~second.
The ride analysis uses the GPS track only (see `_signals`); ACCL is decoded for callers that want it.
"""
import re
import struct

import video_engine as ve

DT = 0.1  # analysis grid step (s)
_FMT = {"b": "b", "B": "B", "s": "h", "S": "H", "l": "i", "L": "I", "f": "f", "d": "d", "j": "q", "J": "Q"}


def _gpmd_stream(path):
    """Absolute index of the GoPro metadata stream from the `ffmpeg -i` listing, or None."""
    err = ve._ffmpeg(["-i", str(path)], check=False).stderr
    for block in re.split(r"\n\s*Stream #", err)[1:]:  # a block = stream line + its metadata (handler_name)
        if "gpmd" in block or "GoPro MET" in block:
            return int(re.match(r"\d+:(\d+)", block).group(1))
    return None


def has_gpmf(path) -> bool:
    return _gpmd_stream(path) is not None


def read_gopro(path) -> dict:
    """{"speed": [(t, m/s)], "accel": [(t, ax, ay, az)], "gps": [(t, lat, lon, alt)]}, or {} without telemetry."""
    idx = _gpmd_stream(path)
    if idx is None:
        return {}
    raw = ve._ffmpeg(["-i", str(path), "-map", f"0:{idx}", "-c", "copy", "-f", "rawvideo", "-"], binary=True).stdout
    return parse_gpmf(raw)


def _klv(data):
    """Yield (key, type, struct size, payload) for each GPMF item in a buffer; stops cleanly on truncation."""
    pos = 0
    while pos + 8 <= len(data):
        key, typ, size, repeat = struct.unpack_from(">4scBH", data, pos)
        n = size * repeat
        yield key.decode("latin-1"), typ.decode("latin-1"), size, data[pos + 8:pos + 8 + n]
        pos += 8 + (n + 3) // 4 * 4


def _values(typ, size, payload):
    """A string for 'c', else one tuple per sample ([] for types we don't need, e.g. nested or complex)."""
    if typ == "c":
        return payload.decode("latin-1").rstrip("\0")
    fmt = _FMT.get(typ)
    if not fmt or not size:
        return []
    k = size // struct.calcsize(fmt)
    return [struct.unpack_from(f">{k}{fmt}", payload, i) for i in range(0, len(payload) - size + 1, size)]


def _scaled(rows, scal):
    """Apply SCAL: one divisor for every element, or one per element."""
    scal = [s or 1 for row in scal for s in row] or [1]
    return [[v / s for v, s in zip(r, scal if len(scal) > 1 else scal * len(r))] for r in rows]


def parse_gpmf(data: bytes) -> dict:
    """Decode a raw gpmd stream (GPS5 + ACCL); t = seconds from clip start. {} if it holds nothing usable."""
    out = {"speed": [], "accel": [], "gps": []}
    devcs = [p for key, _, _, p in _klv(data) if key == "DEVC"]
    for i, devc in enumerate(devcs):
        # shortcut: payload i is taken to span [i, i+1) s (GoPro writes one every ~1.001 s, so <=0.7 s drift by the
        # end of a 12-min chapter), upgrade to the gpmd packet pts (`-f framecrc`) if sync must be frame-tight
        for key, _, _, strm in _klv(devc):
            if key != "STRM":
                continue
            s = {k: _values(typ, size, p) for k, typ, size, p in _klv(strm)}
            scal = s.get("SCAL") or [(1,)]
            gps, fix = s.get("GPS5"), (s.get("GPSF") or [(3,)])[0][0]
            if gps and fix >= 2:  # GPSF 0/1 = no lock: positions are stale or zero
                for j, (lat, lon, alt, speed2d, *_) in enumerate(_scaled(gps, scal)):
                    t = i + j / len(gps)
                    out["gps"].append((t, lat, lon, alt))
                    out["speed"].append((t, speed2d))
            if s.get("ACCL"):
                # channel order: ORIN when present (HERO8+, lowercase = negated axis), else Z,X,Y as documented
                # for older cameras; the result is the camera frame (x, y, z), not the bike's
                orin = s.get("ORIN") if isinstance(s.get("ORIN"), str) else ""
                orin = orin if sorted(orin.upper()) == ["X", "Y", "Z"] else "ZXY"
                rows = _scaled(s["ACCL"], scal)
                for j, r in enumerate(rows):
                    a = {c.upper(): v if c.isupper() else -v for c, v in zip(orin, r)}
                    out["accel"].append((i + j / len(rows), a["X"], a["Y"], a["Z"]))
    return out if any(out.values()) else {}


def _rolling(x, n, fn):
    """fn over a centred window of n samples (edges padded), e.g. np.median for a median filter."""
    import numpy as np
    from numpy.lib.stride_tricks import sliding_window_view
    n = max(1, int(n))
    return fn(sliding_window_view(np.pad(x, (n // 2, n - 1 - n // 2), mode="edge"), n), axis=1)


def _runs(mask, min_len, max_gap=0):
    """(start, end) index pairs of True runs; gaps <= max_gap samples are bridged, runs < min_len dropped."""
    import numpy as np
    edges = np.flatnonzero(np.diff(np.r_[0, mask.astype(int), 0]))
    out = []
    for a, b in zip(edges[::2], edges[1::2]):
        if out and a - out[-1][1] <= max_gap:
            out[-1][1] = b
        else:
            out.append([a, b])
    return [(int(a), int(b)) for a, b in out if b - a >= min_len]


def _span(t, a, b, duration, kind, score=None):
    d = {"start": round(float(t[a]), 2), "end": round(min(float(t[b - 1]) + DT, float(duration)), 2), "kind": kind}
    if score is not None:
        d["score"] = round(max(0.0, min(1.0, float(score))), 2)
    return d


def _signals(tel, duration):
    """(t, speed m/s, lateral accel m/s², altitude m) on a DT grid, or None without a GPS track.

    Lateral g comes from the GPS track (velocity x its rate of change), not ACCL: a leaning motorcycle's camera
    barely feels cornering force (the lean cancels it out) and the mount orientation is unknown anyway.
    """
    import numpy as np
    gps = np.array(tel.get("gps") or [], dtype=float).reshape(-1, 4)
    speed = np.array(tel.get("speed") or [], dtype=float).reshape(-1, 2)
    if len(gps) < 2 or len(speed) < 2:
        return None
    t = np.arange(max(gps[0, 0], 0.0), min(gps[-1, 0], duration), DT)
    if len(t) < 2:
        return None
    n = int(1 / DT)
    v = _rolling(np.interp(t, speed[:, 0], speed[:, 1]), n // 2, np.median)  # drops GPS speed spikes
    lat0 = np.radians(np.median(gps[:, 1]))
    x = np.interp(t, gps[:, 0], np.radians(gps[:, 2]) * 6371000 * np.cos(lat0))  # local metres
    y = np.interp(t, gps[:, 0], np.radians(gps[:, 1]) * 6371000)
    # each derivative is smoothed over 1 s: raw GPS jitter differentiated twice would swamp the signal
    vx, vy = (_rolling(np.gradient(_rolling(c, n, np.mean), DT), n, np.mean) for c in (x, y))
    ax, ay = (_rolling(np.gradient(c, DT), n, np.mean) for c in (vx, vy))
    lat = np.where(v > 3, (vx * ay - vy * ax) / np.maximum(np.hypot(vx, vy), 1.0), 0.0)
    alt = _rolling(np.interp(t, gps[:, 0], gps[:, 3]), 5 * n, np.median)  # GPS altitude is noisy (±5-10 m)
    return t, v, lat, alt


def _prominence(x, i):
    """How far x[i] stands above the higher of the lowest points on either side before higher ground (0 at edges)."""
    import numpy as np
    lows = []
    for side in (x[:i][::-1], x[i + 1:]):
        higher = np.flatnonzero(side > x[i])
        side = side[:higher[0]] if len(higher) else side
        if not len(side):
            return 0.0
        lows.append(side.min())
    return float(x[i] - max(lows))


def highlights(tel, duration) -> list[dict]:
    """Exciting moments: hard_braking, twisties, top_speed, summit; [{"start","end","kind","score" 0..1}] by start."""
    sig = _signals(tel, duration)
    if sig is None:
        return []
    import numpy as np
    t, v, lat, alt = sig
    n = int(1 / DT)
    out = []
    decel = -np.gradient(_rolling(v, n // 2, np.mean), DT)
    for a, b in _runs((decel > 4) & (v > 3), n // 2):  # > 0.4 g for >= 0.5 s; ~0.9 g is a full stop
        out.append(_span(t, a, b, duration, "hard_braking", decel[a:b].max() / 9))
    w = 8 * n  # twisties: hard left and hard right within 8 s, kept up for >= 10 s (one S-bend isn't a road)
    weave = (_rolling(lat, w, np.max) > 2.5) & (_rolling(lat, w, np.min) < -2.5)
    for a, b in _runs(weave, 10 * n, max_gap=2 * n):
        out.append(_span(t, a, b, duration, "twisties", np.sqrt(np.mean(lat[a:b] ** 2)) / 4))
    moving = v > 3
    if moving.any():
        top = moving & (v >= np.percentile(v[moving], 97))
        for a, b in _runs(top, n, max_gap=2 * n):
            out.append(_span(t, a, b, duration, "top_speed", v[a:b].max() / 40))  # 1.0 at ~145 km/h
    last = None
    peak = (alt == _rolling(alt, 60 * n, np.max)) & (np.diff(alt, prepend=-np.inf) != 0)  # top within ±30 s
    for i in np.flatnonzero(peak):
        prom = _prominence(alt, i)
        if prom >= 30 and (last is None or i - last > 60 * n):
            out.append(_span(t, max(0, i - 5 * n), min(len(t), i + 5 * n), duration, "summit", prom / 150))
            last = i
    return sorted(out, key=lambda h: h["start"])


def boring(tel, duration) -> list[dict]:
    """Stretches worth skipping: stopped (< 1.5 m/s for >= 8 s) and motorway (> 22 m/s, dead straight, >= 60 s)."""
    sig = _signals(tel, duration)
    if sig is None:
        return []
    import numpy as np
    t, v, lat, _ = sig
    n = int(1 / DT)
    out = [_span(t, a, b, duration, "stopped") for a, b in _runs(v < 1.5, 8 * n)]
    straight = _rolling(lat, 10 * n, np.std) < 0.8
    out += [_span(t, a, b, duration, "motorway") for a, b in _runs((v > 22) & straight, 60 * n, max_gap=3 * n)]
    return sorted(out, key=lambda s: s["start"])

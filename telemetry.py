"""GoPro telemetry (GPMF): speed, accelerometer and GPS track -> ride highlights and boring stretches.

GoPro MP4s carry a 'gpmd' data stream ("GoPro MET") of GPMF records: KLV items (4-char key, type char, struct size,
big-endian repeat count, payload padded to 4 bytes), nested DEVC -> STRM -> samples. Each gpmd packet (~1.001 s) holds
one DEVC per device (a Karma drone adds its own). GPS is GPS5 (HERO5-11) or GPS9 (HERO11+, per-sample fix and DOP).
The ride analysis uses the GPS track only (see `_signals`); ACCL is decoded for callers that want it.
Checked against GoPro's gpmf-parser on its sample clips (HERO5-8, MAX, Fusion, Karma), a HERO11 dump and a real
HERO13 bike ride (GPS9): values match to the last digit, sample times to the MP4 packet times.
"""
import re
import struct
from fractions import Fraction
from itertools import accumulate

import video_engine as ve

DT = 0.1  # analysis grid step (s)
# GPS dilution of precision cut-off (GoPro: GPSP = DOP x100, < 500 is "good", 9999 = no fix). Against a Garmin on a
# real HERO13 ride, positions were off by a median 4 m at DOP < 5, 10-45 m at 5-10 and 26 m+ above 10 (speed stays
# within ~0.3 m/s up to 20); much of that ride sits at DOP 5-10, so 5 would have dropped nearly all of it.
MAX_DOP = 10.0
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
    args = ["-i", str(path), "-map", f"0:{idx}", "-c", "copy"]
    raw = ve._ffmpeg([*args, "-f", "rawvideo", "-"], binary=True).stdout
    return parse_gpmf(raw, _packets(ve._ffmpeg([*args, "-f", "framecrc", "-"]).stdout))


def _packets(framecrc):
    """[(start s, duration s, size)] per gpmd packet from an `-f framecrc` listing (the MP4 sample table's times)."""
    tb = re.search(r"#tb 0: (\d+)/(\d+)", framecrc)
    tb = Fraction(int(tb.group(1)), int(tb.group(2))) if tb else Fraction(1)
    rows = [line.split(",") for line in framecrc.splitlines() if line[:1].isdigit()]
    out = [(float(int(r[2]) * tb), float(int(r[3]) * tb), int(r[4])) for r in rows]
    if len(out) > 1:
        # shortcut: FFmpeg stretches the last packet to the file's end, so it is capped at the one before; a short
        # final payload (stts 0.667 s) then has its samples up to 0.3 s late, read stts from the MP4 if that matters
        out[-1] = (out[-1][0], min(out[-1][1], out[-2][1]), out[-1][2])
    return out


def _klv(data):
    """Yield (key, type, struct size, payload) for each GPMF item in a buffer; stops cleanly on truncation."""
    pos = 0
    while pos + 8 <= len(data):
        key, typ, size, repeat = struct.unpack_from(">4scBH", data, pos)
        n = size * repeat
        yield key.decode("latin-1"), typ.decode("latin-1"), size, data[pos + 8:pos + 8 + n]
        pos += 8 + (n + 3) // 4 * 4


def _values(typ, size, payload, typedef=""):
    """A string for 'c', else one tuple per sample ([] for types we don't need, e.g. nested).

    Complex ('?') samples are decoded from the stream's TYPE string, e.g. GPS9 = "lllllllSS".
    """
    if typ == "c":
        return payload.decode("latin-1").rstrip("\0")
    fmt = "".join(_FMT.get(c, "?") for c in typedef) if typ == "?" else _FMT.get(typ)
    if not fmt or "?" in fmt or not size or (typ == "?" and struct.calcsize(">" + fmt) != size):
        return []
    k = size // struct.calcsize(fmt) if typ != "?" else 1
    return [struct.unpack_from(f">{k}{fmt}", payload, i) for i in range(0, len(payload) - size + 1, size)]


def _scaled(rows, scal):
    """Apply SCAL: one divisor for every element, or one per element."""
    scal = [s or 1 for row in scal for s in row] or [1]
    return [[v / s for v, s in zip(r, scal if len(scal) > 1 else scal * len(r))] for r in rows]


def _devcs(data):
    return [p for key, _, _, p in _klv(data) if key == "DEVC"]


def _streams(devcs):
    """{key: values} for every STRM of the given DEVC payloads."""
    out = []
    for devc in devcs:
        for k, _, _, strm in _klv(devc):
            if k == "STRM":
                items = list(_klv(strm))
                typedef = next((p.decode("latin-1").rstrip("\0") for k2, _, _, p in items if k2 == "TYPE"), "")
                out.append({k2: _values(typ, size, p, typedef) for k2, typ, size, p in items})
    return out


def parse_gpmf(data: bytes, packets=None) -> dict:
    """Decode a raw gpmd stream (GPS5/GPS9 + ACCL); t = seconds from clip start. {} if it holds nothing usable.

    `packets` = [(start, duration, size)] from the MP4 (`_packets`); each packet's samples are spread evenly over it.
    Without them every top-level DEVC is taken as one 1 s packet (a bare GPMF dump, as in GoPro's samples/*.raw).
    """
    out = {"speed": [], "accel": [], "gps": []}
    if packets and sum(p[2] for p in packets) == len(data):
        offs = accumulate((p[2] for p in packets), initial=0)
        chunks = [(t0, dur, _devcs(data[o:o + n])) for (t0, dur, n), o in zip(packets, offs)]
    else:
        chunks = [(i, 1.0, [devc]) for i, devc in enumerate(_devcs(data))]
    for t0, dur, devcs in chunks:
        streams = _streams(devcs)
        gps9 = any(s.get("GPS9") for s in streams)  # HERO11 also writes GPS5; GPS9 carries per-sample fix + DOP
        for s in streams:
            scal = s.get("SCAL") or [(1,)]
            if gps9 and s.get("GPS9"):
                rows = _scaled(s["GPS9"], scal)
                ok = [len(r) == 9 and r[8] >= 2 and r[7] <= MAX_DOP for r in rows]
            elif not gps9 and s.get("GPS5"):
                rows = _scaled(s["GPS5"], scal)
                fix, dop = (s.get("GPSF") or [(3,)])[0][0], (s.get("GPSP") or [(0,)])[0][0] / 100
                ok = [fix >= 2 and dop <= MAX_DOP] * len(rows)  # GPSF 0/1 = no lock: positions are stale or junk
            else:
                rows = ok = []
            for j, (r, good) in enumerate(zip(rows, ok)):
                if good:
                    t = t0 + dur * j / len(rows)
                    out["gps"].append((t, r[0], r[1], r[2]))
                    out["speed"].append((t, r[3]))
            if s.get("ACCL"):
                # channel order: ORIN when present (HERO7+, lowercase = negated axis; matches MTRX on real files),
                # else Z,X,Y as documented for older cameras; the result is the camera frame (x, y, z), not the bike's
                orin = s.get("ORIN") if isinstance(s.get("ORIN"), str) else ""
                orin = orin if sorted(orin.upper()) == ["X", "Y", "Z"] else "ZXY"
                rows = _scaled(s["ACCL"], scal)
                for j, r in enumerate(rows):
                    a = {c.upper(): v if c.isupper() else -v for c, v in zip(orin, r)}
                    out["accel"].append((t0 + dur * j / len(rows), a["X"], a["Y"], a["Z"]))
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
    # position and velocity are smoothed over 2 s: real GPS jitter (10-45 m at DOP 5-10 on a HERO13) differentiated
    # twice read as "twisties" on a straight city ride over 1 s; 2 s still passes a 6 s S-bend at 2/3 strength
    vx, vy = (_rolling(np.gradient(_rolling(c, 2 * n, np.mean), DT), 2 * n, np.mean) for c in (x, y))
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

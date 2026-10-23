"""GPMF parsing and ride analysis on a synthetic GoPro ride (no real footage needed)."""
import struct
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import telemetry  # noqa: E402
import video_engine  # noqa: E402

FMT = {"s": "h", "l": "i", "L": "I"}
RIDE = 305  # s: stop, pull away, hard brake, twisties, climb to a summit, motorway with an overtake, stop


def klv(key, typ, rows):
    """Encode one GPMF item: rows of tuples, a str for 'c', or nested KLV bytes for type 0."""
    if typ == "\0":
        payload, size = rows, 4
    elif typ == "c":
        payload, size = rows.encode("latin-1"), len(rows)
    else:
        payload = b"".join(struct.pack(f">{len(r)}{FMT[typ]}", *r) for r in rows)
        size = len(payload) // len(rows)
    return (key.encode("latin-1") + typ.encode("latin-1") + bytes([size]) + struct.pack(">H", len(payload) // size)
            + payload + b"\0" * (-len(payload) % 4))


def ride():
    """Ground truth at 18 Hz (GPS rate): t, speed, lat, lon, alt, longitudinal accel."""
    t = np.arange(0, RIDE, 1 / 18)
    v = np.interp(t, [0, 20, 30, 40, 43, 50, 170, 180, 220, 222, 227, 229, 280, 295, RIDE],
                  [0, 0, 25, 25, 7, 15, 15, 30, 30, 36, 36, 30, 30, 0, 0])
    yaw = np.where((t > 50) & (t < 80), 0.33 * np.sin(2 * np.pi * (t - 50) / 6), 0.0)  # ±5 m/s² lateral at 15 m/s
    heading = np.cumsum(yaw) / 18
    rng = np.random.default_rng(1)
    x = np.cumsum(v * np.cos(heading)) / 18 + rng.normal(0, 0.3, len(t))
    y = np.cumsum(v * np.sin(heading)) / 18 + rng.normal(0, 0.3, len(t))
    alt = np.interp(t, [0, 80, 140, 170, RIDE], [100, 100, 250, 150, 150]) + rng.normal(0, 3, len(t))
    lat = 46.5 + np.degrees(y / 6371000)
    lon = 8.0 + np.degrees(x / (6371000 * np.cos(np.radians(46.5))))
    return t, np.maximum(v + rng.normal(0, 0.2, len(t)), 0), lat, lon, alt, np.gradient(v, t)


def gpmf_bytes():
    """One DEVC per second: a GPS stream (no lock in second 0) and an ACCL stream (Z,X,Y order, 50 Hz)."""
    t, v, lat, lon, alt, acc = ride()
    out = b""
    for s in range(RIDE):
        g = (t >= s) & (t < s + 1)
        gps = [(round(a * 1e7), round(o * 1e7), round(h * 1000), round(sp * 1000), round(sp * 100))
               for a, o, h, sp in zip(lat[g], lon[g], alt[g], v[g])]
        acc_rows = [(round(9.81 * 418), round(a * 418), 0) for a in np.interp(np.arange(s, s + 1, 0.02), t, acc)]
        gps_strm = (klv("STNM", "c", "GPS (Lat., Long., Alt., 2D, 3D)") + klv("GPSF", "L", [(0 if s == 0 else 3,)])
                    + klv("SCAL", "l", [(10 ** 7,), (10 ** 7,), (1000,), (1000,), (100,)]) + klv("GPS5", "l", gps))
        acc_strm = klv("SCAL", "s", [(418,)]) + klv("ACCL", "s", acc_rows)
        out += klv("DEVC", "\0", klv("DVID", "L", [(1,)]) + klv("STRM", "\0", acc_strm) + klv("STRM", "\0", gps_strm))
    return out


TEL = telemetry.parse_gpmf(gpmf_bytes())


def overlaps(item, a, b):
    return item["start"] < b and item["end"] > a


def test_parse_gpmf_decodes_scaled_gps_and_accel():
    t, v, lat, lon, alt, _ = ride()
    assert set(TEL) == {"speed", "accel", "gps"}
    assert TEL["gps"][0][0] == 1.0  # second 0 had no GPS lock and is dropped
    assert len(TEL["gps"]) == (t >= 1).sum() and len(TEL["accel"]) == RIDE * 50
    i = np.argmax(t >= 35)
    tt, la, lo, al = TEL["gps"][i - 18]
    assert abs(tt - t[i]) < 0.06 and abs(la - lat[i]) < 1e-6 and abs(lo - lon[i]) < 1e-6 and abs(al - alt[i]) < 0.01
    assert abs(TEL["speed"][i - 18][1] - v[i]) < 0.01
    brake = [ax for tt, ax, ay, az in TEL["accel"] if 40.5 < tt < 42.5]
    assert abs(np.mean(brake) + 6) < 0.05  # ZXY order: second channel is x
    assert all(abs(az - 9.81) < 0.01 for _, _, _, az in TEL["accel"])


def test_parse_gpmf_honours_orin_and_survives_junk():
    data = klv("DEVC", "\0", klv("STRM", "\0", klv("ORIN", "c", "yXZ") + klv("SCAL", "s", [(10,)])
                                + klv("ACCL", "s", [(10, 20, 30)])))
    assert telemetry.parse_gpmf(data)["accel"] == [(0.0, 2.0, -1.0, 3.0)]
    assert telemetry.parse_gpmf(b"") == {} and telemetry.parse_gpmf(b"\0" * 7 + b"junk") == {}
    assert telemetry.parse_gpmf(data[:-5]) == {}  # truncated payload: no crash


def test_highlights_find_the_ride_events():
    hl = telemetry.highlights(TEL, RIDE)
    assert hl == sorted(hl, key=lambda h: h["start"]) and all(0 <= h["score"] <= 1 for h in hl)
    kinds = {}
    for h in hl:
        kinds.setdefault(h["kind"], []).append(h)
    assert set(kinds) == {"hard_braking", "twisties", "top_speed", "summit"}, hl
    assert len(kinds["hard_braking"]) == 1 and overlaps(kinds["hard_braking"][0], 40, 43)
    assert len(kinds["twisties"]) == 1 and kinds["twisties"][0]["start"] < 55 and kinds["twisties"][0]["end"] > 75
    assert all(overlaps(h, 220, 229) for h in kinds["top_speed"])
    assert len(kinds["summit"]) == 1 and kinds["summit"][0]["start"] <= 140 <= kinds["summit"][0]["end"]


def test_boring_finds_stops_and_motorway():
    bo = telemetry.boring(TEL, RIDE)
    assert [b["kind"] for b in bo] == ["stopped", "motorway", "stopped"], bo
    stop1, motorway, stop2 = bo
    assert stop1["start"] == 1.0 and 19 < stop1["end"] < 22
    assert motorway["start"] < 185 and motorway["end"] > 275
    assert stop2["start"] < 296 and stop2["end"] <= RIDE
    assert telemetry.boring({}, 10) == [] and telemetry.highlights({}, 10) == []


def test_read_gopro_from_files():
    with tempfile.TemporaryDirectory() as tmp:
        plain, gopro, raw = Path(tmp) / "plain.mp4", Path(tmp) / "gopro.mov", Path(tmp) / "gpmf.bin"
        video_engine._ffmpeg(["-y", "-f", "lavfi", "-i", "testsrc=d=1:s=64x64", "-c:v", "mpeg4", str(plain)])
        assert not telemetry.has_gpmf(plain) and telemetry.read_gopro(plain) == {}
        # a real GoPro track is MP4; FFmpeg only muxes a gpmd track into MOV, which parses the same way
        raw.write_bytes(gpmf_bytes())
        video_engine._ffmpeg(["-y", "-i", str(plain), "-f", "data", "-i", str(raw), "-map", "0", "-map", "1",
                              "-c", "copy", "-tag:d", "gpmd", "-bsf:d", "setts=ts=N*100:duration=100", str(gopro)])
        assert telemetry.has_gpmf(gopro)
        assert telemetry.read_gopro(gopro) == TEL


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")

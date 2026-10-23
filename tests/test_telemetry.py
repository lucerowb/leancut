"""GPMF parsing and ride analysis: a synthetic GoPro ride, built GPMF snippets and a real HERO13 fixture (offline)."""
import struct
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import telemetry  # noqa: E402
import video_engine  # noqa: E402

FMT = {"s": "h", "S": "H", "l": "i", "L": "I"}
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "gopro13_ride_gpmd.mov"  # see fixtures/LICENSE.md
RIDE = 305  # s: stop, pull away, hard brake, twisties, climb to a summit, motorway with an overtake, stop


def klv(key, typ, rows):
    """Encode one GPMF item: rows of tuples, a str for 'c', or nested KLV bytes for type 0."""
    if typ == "\0":
        payload, size = rows, 4
    elif typ == "c":
        payload, size = rows.encode("latin-1"), len(rows)
    elif typ == "?":  # complex: rows are (struct format, tuples), e.g. GPS9 "lllllllSS"
        fmt, rows = rows
        payload = b"".join(struct.pack(">" + "".join(FMT[c] for c in fmt), *r) for r in rows)
        size = len(payload) // len(rows)
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
    """One DEVC per second: a GPS stream (no lock in second 0) and an ACCL stream (Z,X,Y order, 50 Hz).

    Each DEVC is padded to 1024 bytes, the packet size of FFmpeg's raw data demuxer, so a muxed file keeps one DEVC
    per packet like a camera does.
    """
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
        devc = klv("DVID", "L", [(1,)]) + klv("STRM", "\0", acc_strm) + klv("STRM", "\0", gps_strm)
        out += klv("DEVC", "\0", devc + klv("FILL", "L", [(0,)] * ((1024 - 16 - len(devc)) // 4)))
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
        # a real GoPro track is MP4; FFmpeg only muxes a gpmd track into MOV, which parses the same way.
        # Packets are 1.001 s apart, as on a camera: times follow them instead of drifting 1 ms per second
        raw.write_bytes(gpmf_bytes())
        video_engine._ffmpeg(["-y", "-i", str(plain), "-f", "data", "-i", str(raw), "-map", "0", "-map", "1",
                              "-c", "copy", "-tag:d", "gpmd", "-bsf:d", "setts=ts=N*1.001/TB:duration=1.001/TB",
                              str(gopro)])
        assert telemetry.has_gpmf(gopro)
        tel = telemetry.read_gopro(gopro)
        assert tel.keys() == TEL.keys() and all(len(tel[k]) == len(TEL[k]) for k in TEL)
        for k in TEL:
            assert all(abs(a[0] - b[0] * 1.001) < 1e-6 and a[1:] == b[1:] for a, b in zip(tel[k], TEL[k]))
        assert abs(tel["gps"][-1][0] - TEL["gps"][-1][0]) > 0.3  # the old 1 s/packet guess was 0.3 s off by 5 min


def test_packets_from_framecrc():
    crc = "#tb 0: 1/1000\n#media_type 0: data\n0, 0, 0, 1001, 9960, 0x1\n0, 1001, 1001, 1001, 10004, 0x2\n" \
          "0, 2002, 2002, 1126, 7628, 0x3\n"
    # FFmpeg stretches the last packet to the file's end (the MP4 says 0.667 s): capped at the previous duration
    assert telemetry._packets(crc) == [(0.0, 1.001, 9960), (1.001, 1.001, 10004), (2.002, 1.001, 7628)]
    data = klv("DEVC", "\0", klv("STRM", "\0", klv("ACCL", "s", [(1, 2, 3)] * 4)))
    assert telemetry.parse_gpmf(data, [(5.0, 2.0, len(data) + 1)])["accel"][1][0] == 0.25  # sizes disagree: 1 s


def test_gps9_preferred_over_gps5_with_per_sample_fix_and_dop():
    """HERO11 writes GPS5 and GPS9 side by side (HERO13: GPS9 only); GPS9 carries fix and DOP per sample."""
    scal9 = [(10 ** 7,), (10 ** 7,), (1000,), (1000,), (100,), (1,), (1000,), (100,), (1,)]
    # lat, lon, alt, speed2d, speed3d, days since 2000, s since midnight, DOP x100, fix
    rows = [(424261529, -86499795, 34098, 8132, 816, 8298, 48576899, 179, 3),
            (424261530, -86499790, 34100, 8195, 813, 8298, 48576999, 9999, 3),  # DOP 99.99: junk
            (424261531, -86499785, 34102, 8194, 809, 8298, 48577099, 150, 0),  # no fix
            (424261532, -86499780, 34104, 8200, 820, 8298, 48577199, 268, 2)]
    gps9 = klv("STRM", "\0", klv("TYPE", "c", "lllllllSS") + klv("SCAL", "l", scal9)
               + klv("GPS9", "?", ("lllllllSS", rows)))
    gps5 = klv("STRM", "\0", klv("GPSF", "L", [(3,)]) + klv("GPSP", "S", [(150,)]) + klv("SCAL", "l", scal9[:5])
               + klv("GPS5", "l", [r[:5] for r in rows]))
    tel = telemetry.parse_gpmf(klv("DEVC", "\0", gps5 + gps9))
    assert tel["gps"] == [(0.0, 42.4261529, -8.6499795, 34.098), (0.75, 42.4261532, -8.649978, 34.104)]
    assert tel["speed"] == [(0.0, 8.132), (0.75, 8.2)]
    # GPS5 alone: one GPSF/GPSP for the whole packet
    assert len(telemetry.parse_gpmf(klv("DEVC", "\0", gps5))["gps"]) == 4
    bad = gps5.replace(struct.pack(">H", 150), struct.pack(">H", 9999))
    assert telemetry.parse_gpmf(klv("DEVC", "\0", bad)) == {}


def test_several_devc_per_packet_share_its_time():
    """A Karma drone (or a BLE sensor) adds its own DEVC to each packet: one packet is still one span of time."""
    cam = klv("DEVC", "\0", klv("DVID", "L", [(1,)]) + klv("STRM", "\0", klv("ACCL", "s", [(1, 2, 3)] * 2)))
    drone = klv("DEVC", "\0", klv("DVID", "L", [(16835857,)]) + klv("STRM", "\0", klv("EMPT", "L", [(0,)])))
    data = (cam + drone) * 2
    tel = telemetry.parse_gpmf(data, [(0.0, 1.001, len(cam + drone)), (1.001, 1.001, len(cam + drone))])
    assert [round(a[0], 4) for a in tel["accel"]] == [0.0, 0.5005, 1.001, 1.5015]


def test_real_hero13_ride_matches_gpmf_parser():
    """3 s of a real HERO13 bike ride; expected values are GoPro's gpmf-parser (GPMF_ScaledData) on the same file."""
    assert telemetry.has_gpmf(FIXTURE)
    tel = telemetry.read_gopro(FIXTURE)
    # 30 GPS9 samples in 3 packets of 1.001 s; the first two have DOP 99.99 and 10.68 and are dropped
    assert len(tel["gps"]) == 28 and len(tel["accel"]) == 602
    t, lat, lon, alt = tel["gps"][0]
    assert abs(t - 0.2002) < 1e-9 and (lat, lon, alt) == (40.8509104, -73.9480974, 98.759)
    assert tel["gps"][-1][1:] == (40.8508839, -73.9478393, 73.675) and abs(tel["gps"][-1][0] - 2.9029) < 1e-9
    assert tel["speed"][0][1] == 8.132 and all(7.5 < v < 9 for _, v in tel["speed"])  # ~30 km/h on a bike
    # gpmf-parser's first ACCL sample in its Z,X,Y output order: -8.79376499 -4.94244604 -1.9616307
    _, ax, ay, az = tel["accel"][0]
    assert abs(ax + 4.94244604) < 1e-7 and abs(ay + 1.9616307) < 1e-7 and abs(az + 8.79376499) < 1e-7


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")

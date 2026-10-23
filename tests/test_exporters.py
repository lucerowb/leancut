"""Edit-list exports carrying the look of the cut: transitions, speed, titles, overlays and intro/outro cards."""
import json
import sys
import tempfile
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import exporters  # noqa: E402

A = {"id": "a", "path": "/r/A.MP4", "duration": 100.0, "timecode": "10:00:00:00"}
B = {"id": "b", "path": "/r/B.MP4", "duration": 60.0}
FMT = {"width": 1920, "height": 1080, "fps": 25}


def rich():
    """card 4 s → A (crossfade 1 s, title, B-roll, B&W, grade) → A slow-mo (wipe) → B 2× (dip) → 8× connector
    (left out) → outro card over B (crossfade). At 25 fps: 100 + 250 + 250 + 125 + 75 frames − 24 − 12 − 14 − 14."""
    tl = exporters.timeline([(A, 10, 20, 1), (A, 30, 35, 0.5), (B, 0, 10, 2.0), (A, 40, 50, 8.0)])
    tl.insert(0, {"card": {"title": "Ride", "subtitle": "Day 1", "duration": 4, "style": "bold"}, "clip": None,
                  "speed": 1})
    tl[1].update(transition={"type": "crossfade", "dur": 1.0}, effect="bw", grade={"exposure": 0.3, "lut": "/x/l.cube"},
                 texts=[{"text": "Hello", "style": "lower_third", "t0": 1, "t1": 4}],
                 overlays=[{"clip": B, "start": 5, "end": 8, "at": 2, "dur": 3}])
    tl[2].update(transition={"type": "wipe_left", "dur": 0.5}, effect="slowmo")
    tl[3].update(transition={"type": "dip_black", "dur": 0.6}, effect="speedup")
    tl.append({"card": {"title": "Thanks", "duration": 3, "style": "minimal"}, "clip": B, "start": 20, "end": 23,
               "speed": 1, "transition": {"type": "crossfade", "dur": 0.6}})
    return tl


TOTAL = 736


def sec(v):
    return Fraction(v[:-1]) if "/" in v else Fraction(v[:-1] or 0)


def test_layout_mirrors_the_renderer_and_clamps_transitions():
    lay, total = exporters._layout(rich(), 25)
    assert total == TOTAL and [L["d_in"] for L in lay] == [0, 24, 12, 14, 14]
    assert [L["n"] for L in lay] == [100, 250, 250, 125, 75]  # slow-mo doubles, 2× halves, 8× connector is out
    short = exporters.timeline([(A, 0, 4, 1), (A, 10, 11, 1)])
    short[1]["transition"] = {"type": "dissolve", "dur": 2.0}
    assert exporters._layout(short, 25)[0][1]["d_in"] == 12  # a 1 s part takes at most half of itself, even frames
    short[1]["transition"] = {"type": "jcut", "dur": 0.4}
    assert exporters._layout(short, 25)[1] == 125  # J-cuts don't overlap pictures


def test_edl_carries_dissolves_wipes_and_speed_and_notes_the_rest():
    with tempfile.TemporaryDirectory() as d:
        lines = exporters.write_edl(rich(), Path(d) / "x.edl", 25, "x").read_text().splitlines()
    assert lines[3] == "001  BL       V     C        00:00:00:00 00:00:03:01 01:00:00:00 01:00:03:01"  # card = black
    assert "002  AX       AA/V  D    024 10:00:10:00 10:00:19:13 01:00:03:01 01:00:12:14" in lines
    assert "003  AX       AA/V  W001 012 10:00:30:00 10:00:39:11 01:00:12:14 01:00:22:00" in lines
    assert "M2   AX             012.5                10:00:30:00" in lines  # 0.5 × 25 fps
    assert "M2   AX             050.0                00:00:00:00" in lines
    events = [x for x in lines if x[:3].isdigit()]
    assert events[-1].endswith("01:00:26:11 01:00:29:11") and events[-1][:3] == "005"  # 736 frames; 8× part is out
    for note in ('title text "Ride"', 'lower_third text "Hello" 01:00:04:01-01:00:07:01', "B-roll over 01:00:05:01",
                 "effect Black & white", "LUT l.cube", "dip black transition", "minimal title card over this clip"):
        assert any(x.startswith("* LeanCut: ") and note in x for x in lines), note


def test_fcpxml_is_structurally_sound():
    with tempfile.TemporaryDirectory() as d:
        root = ET.parse(exporters.write_fcpxml(rich(), Path(d) / "x.fcpxml", FMT, "x", (), [(5.0, "Go")])).getroot()
    fx = {e.get("id"): e.get("uid") for e in root.iter("effect")}
    assert fx["fx1"] == "FxPlug:4731E73A-8DAC-4113-9A30-AE85B1761265" and fx["fx3"].endswith("Basic Title.moti")
    ids = {e.get("id") for e in root.find("resources")}
    spine = list(root.find(".//spine"))
    assert [e.tag for e in spine] == ["gap"] + ["transition", "asset-clip"] * 4
    end = Fraction(0)
    for k, e in enumerate(spine):
        off, dur = sec(e.get("offset")), sec(e.get("duration"))
        if e.tag == "transition":  # centred on the cut, overlapping both neighbours' handles
            assert off + dur / 2 == end == sec(spine[k + 1].get("offset"))
            assert all(f.get("ref") in ids for f in e)
            continue
        assert off == end
        start = sec(e.get("start"))
        for child in e:
            if child.get("lane"):  # connected items start inside their parent and point at real resources
                assert start <= sec(child.get("offset")) < start + dur and child.get("ref") in ids
        end = off + dur
    assert sec(root.find(".//sequence").get("duration")) == end == Fraction(TOTAL, 25)
    clip = spine[2]
    lanes = {c.tag: c.get("lane") for c in clip if c.get("lane")}
    assert lanes == {"asset-clip": "1", "title": "2"} and clip.find("asset-clip").get("srcEnable") == "video"
    assert clip.find("title/text/text-style").text == "Hello"
    assert "Black & white" in clip.find("marker").get("value") and clip.find("marker").get("completed") == "0"
    tm = [(sec(p.get("time")), sec(p.get("value"))) for p in spine[4].find("timeMap")]
    assert tm[1][0] - tm[0][0] == 2 * (tm[1][1] - tm[0][1])  # slow motion: 10 s of timeline for 5 s of media
    assert [t.find("text/text-style").text for t in spine[0].findall("title")] == ["Ride", "Day 1"]
    assert spine[0].find("chapter-marker") is None and spine[2].find("chapter-marker").get("value") == "Go"
    assert spine[-1].get("srcEnable") == "video" and spine[-1].find("title").get("lane") == "1"


def test_otio_loads_with_transitions_warps_markers_and_overlays():
    with tempfile.TemporaryDirectory() as d:
        p = exporters.write_otio(rich(), Path(d) / "x.otio", FMT, "x")
        doc = json.loads(p.read_text())
        try:
            import opentimelineio as otio
        except ImportError:
            otio = None
        if otio:
            tl = otio.adapters.read_from_file(str(p))
            assert tl.duration() == otio.opentime.RationalTime(TOTAL, 25)
            assert [t.name for t in tl.tracks] == ["V1", "V2", "A1"] and tl.tracks[-1].duration() == tl.duration()
            assert tl.video_tracks()[1][1].trimmed_range_in_parent().start_time.value == 76 + 50  # B-roll at +2 s
    v1 = doc["tracks"]["children"][0]["children"]
    assert [x["OTIO_SCHEMA"] for x in v1] == ["Gap.1"] + ["Transition.1", "Clip.2"] * 4
    assert {x["transition_type"] for x in v1[1::2]} == {"SMPTE_Dissolve"} and v1[3]["name"] == "wipe_left"
    assert v1[1]["in_offset"]["value"] == v1[1]["out_offset"]["value"] == 12
    assert [m["name"] for m in v1[0]["markers"]] == ["Ride", "Day 1"]
    assert v1[2]["markers"][0]["metadata"]["leancut"]["style"] == "lower_third"
    assert v1[2]["metadata"]["leancut"]["grade"]["lut"] == "/x/l.cube" and v1[2]["effects"][0]["effect_name"] == "bw"
    assert [e["time_scalar"] for e in v1[4]["effects"]] == [0.5] and v1[6]["effects"][0]["time_scalar"] == 2.0
    assert doc["tracks"]["children"][-1]["children"][-1]["OTIO_SCHEMA"] == "Gap.1"  # cards are silent


def test_old_style_entries_stay_plain():
    tl = exporters.timeline([(A, 1.0, 2.5, 1), (A, 50.0, 60.0, 8.0), (B, 3.0, 9.0, 1)])
    with tempfile.TemporaryDirectory() as d:
        edl = exporters.write_edl(tl, Path(d) / "x.edl", 25, "x").read_text()
        xml = exporters.write_fcpxml(tl, Path(d) / "x.fcpxml", FMT, "x").read_text()
        doc = json.loads(exporters.write_otio(tl, Path(d) / "x.otio", FMT, "x").read_text())
    assert "LeanCut" not in edl and "M2" not in edl and edl.count("  C        ") == 2
    for tag in ("<transition", "<title", "<timeMap", "<effect", "<marker", "<gap", "srcEnable"):
        assert tag not in xml
    assert 'duration="187/25s"' in xml  # 37 + 150 frames
    v1 = doc["tracks"]["children"][0]["children"]
    assert [x["OTIO_SCHEMA"] for x in v1] == ["Clip.2", "Clip.2"]
    assert all(not x["effects"] and not x["markers"] and not x["metadata"] for x in v1)

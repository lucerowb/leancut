"""Tests for the edit model, render specs and chapter joining. Run: python -m pytest -q tests/test_editing.py"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("LEANCUT_HOME", tempfile.mkdtemp())

import editing  # noqa: E402
import production  # noqa: E402
import video_engine as ve  # noqa: E402


def project():
    seg = lambda a, b, t, keep=True: {"start": a, "end": b, "text": t, "keep": keep, "score": 4}
    return {"name": "Ride", "clips": [
        {"id": "a", "name": "a.mp4", "path": "/x/a.mp4", "duration": 60, "width": 1280, "height": 720, "fps": 30,
         "segments": [seg(1, 4, "Hello there."), seg(4.2, 8, "We ride today."), seg(20, 25, "Um so", False)],
         "broll": [{"start": 30, "end": 35, "keep": True, "interest": 0.7}]},
        {"id": "b", "name": "b.mp4", "path": "/x/b.mp4", "duration": 30, "width": 1280, "height": 720, "fps": 30,
         "segments": [seg(2, 6, "Look at that!")], "broll": []}]}


def test_actions_validate_apply_and_undo():
    p = project()
    before = editing.snapshot(p)
    done, errors = editing.apply_actions(p, [
        {"type": "effect", "ids": ["c0.s1"], "value": "zoom"},
        {"type": "transition", "ids": ["c1.s0"], "value": "dip_black", "duration": 9},
        {"type": "text", "ids": ["c0.s0"], "text": "Day 1", "value": "title"},
        {"type": "cut", "ids": ["c0.s0"]}, {"type": "keep", "ids": ["c0.s2"]},
        {"type": "grade", "value": "warm", "params": {"saturation": 1.2}},
        {"type": "intro", "text": "Mountain Pass", "value": "Day 1"},
        {"type": "effect", "ids": ["c9.s9"], "value": "zoom"},
        {"type": "effect", "ids": ["c0.s0"], "value": "explode"}, {"type": "nonsense"}])
    assert len(done) == 7 and len(errors) == 3
    assert p["clips"][1]["segments"][0]["transition"] == {"type": "dip_black", "dur": 2.0}  # clamped
    assert p["grade"] == {"preset": "warm", "manual": {"saturation": 1.2}}
    assert p["intro"]["subtitle"] == "Day 1" and not p["clips"][0]["segments"][0]["keep"]
    editing.restore(p, before)
    assert p["clips"][0]["segments"][0]["keep"] and "effect" not in p["clips"][0]["segments"][1] and not p.get("intro")


def test_parts_merge_only_matching_looks():
    p = project()
    parts = editing.build_parts(p, {})
    assert [x["ids"] for x in parts] == [["c0.s0", "c0.s1"], ["c0.b0"], ["c1.s0"]]
    editing.apply_actions(p, [{"type": "effect", "ids": ["c0.s1"], "value": "slowmo"}])
    parts = editing.build_parts(p, {})
    assert [x["ids"] for x in parts][:2] == [["c0.s0"], ["c0.s1"]] and parts[1]["speed"] == 0.5
    assert abs(editing.part_duration(parts[1]) - 2 * (8.15 - 4.05)) < 1e-6


def test_grade_combines_auto_preset_manual_and_renders():
    g = editing.combine({"exposure": 0.2, "contrast": 1.1}, editing.GRADE_PRESETS["cinematic"], {"contrast": 1.1})
    assert abs(g["contrast"] - 1.1 * 1.1 * 1.1) < 1e-9 and g["vignette"] == 0.5
    f = editing.grade_filter(g)
    assert "colortemperature" in f and "eq=" in f and "vignette" in f and "colorbalance" in f
    assert editing.grade_filter(editing.combine()) == ""
    dark = editing.auto_grade_params([{"y": 60, "ylow": 10, "yhigh": 120, "sat": 16}])
    assert dark == {"exposure": 0.35, "contrast": 1.18, "saturation": 1.25}  # capped: night stays night


def test_filters_are_valid_ffmpeg():
    """Every look renders on a real (generated) frame, so a typo in a filter string fails here, not in an export."""
    with tempfile.TemporaryDirectory() as d:
        wd, font = production.prepare(d)
        vf = [production.base_vf({"width": 320, "height": 180}), editing.grade_filter(editing.combine(
            editing.GRADE_PRESETS["teal_orange"], editing.GRADE_PRESETS["vintage"]))]
        for eff in ("zoom", "push_in", "bw", "vignette", "dreamy"):
            vf.append(editing.effect_filter(eff, 320, 180, 2.0)[0])
        if font:
            for style in editing.TEXT_STYLES:
                vf.append(editing.text_filter(production.text_file(wd, f"It's 100% {style}: 'quoted'"), font, style, 0.1, 1.5, 320, 180))
        ve._ffmpeg(["-f", "lavfi", "-i", "testsrc=s=320x180:d=2:r=10", "-vf", ",".join(x for x in vf if x),
                    "-f", "null", "-"], cwd=str(wd))


def test_transitions_and_jcut_specs():
    p = project()
    editing.apply_actions(p, [{"type": "cut_style", "value": "jcut", "duration": 0.4},
                              {"type": "transition", "ids": ["c1.s0"], "value": "wipe_left"}])
    parts = editing.build_parts(p, {})
    specs = production.with_transitions([{} for _ in parts], parts, p)
    assert "transition" not in specs[0]
    assert specs[1]["transition"] == {"jcut": True, "dur": 0.4} and specs[1]["preroll"] == 0.4
    assert specs[2]["transition"] == {"xfade": "wipeleft", "dur": 0.6}


def test_crop_expression_follows_subject():
    e = production.crop_x_expr([(10, 0.2), (12, 0.8)], 5, start=10)
    assert e.startswith("'max(0,min(iw-ow,") and "if(lt(t,2.000)" in e
    ve._ffmpeg(["-f", "lavfi", "-i", "testsrc=s=640x360:d=3:r=10", "-vf", f"crop=200:360:{e}:0", "-f", "null", "-"])


def test_profanity_mask_and_words():
    assert production.mask("well fuck that shit, nice") == "well f*** that s***, nice"
    seg = {"start": 0, "end": 4, "text": "oh fuck yes now"}
    words = production.segment_words(seg)
    assert words[1] == ("fuck", 1.0, 2.0) and production.is_profane(words[1][0])
    assert not production.is_profane("scunthorpe")


def test_chapters_are_detected_and_joined(tmp_path):
    import app
    for k, tc in enumerate(("01:00:00:00", "01:00:02:00")):
        ve._ffmpeg(["-y", "-f", "lavfi", "-i", "testsrc=s=320x180:d=2:r=30", "-f", "lavfi", "-i", "sine=d=2",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-timecode", tc,
                    str(tmp_path / f"ride_{k}.mp4")])
    app.new_project("chapters")
    a = app.add_by_path({"path": str(tmp_path / "ride_0.mp4")})
    b = app.add_by_path({"path": str(tmp_path / "ride_1.mp4")})
    assert a["id"] == b["id"] and len(app.project["clips"]) == 1
    c = app.project["clips"][0]
    assert c["path"].endswith(".ffconcat") and abs(c["duration"] - 4.0) < 0.1 and len(c["chapters"]) == 2
    assert app.locate(c, 3.0) == (str(tmp_path / "ride_1.mp4"), 1.0)
    assert abs(ve.probe(c["path"])["duration"] - 4.0) < 0.15  # FFmpeg reads the joined clip
    tl = app.expand_chapters([{"clip": c, "start": 1.0, "end": 3.0, "speed": 1, "rec_start": 0, "rec_end": 2}])
    assert [(e["clip"]["path"][-10:], e["start"], e["end"]) for e in tl] == [("ride_0.mp4", 1.0, 2.0), ("ride_1.mp4", 0.0, 1.0)]


def test_api_actions_plan_undo_redo():
    import app
    from fastapi.testclient import TestClient
    app.new_project("api")
    app.project["clips"] = project()["clips"]
    c = TestClient(app.api, headers={"host": "127.0.0.1:8765"})
    st = c.get("/api/state").json()
    assert "zoom" in st["effects"] and "jcut" in st["transitions"] and "cinematic" in st["presets"]
    r = c.post("/api/actions", json={"actions": [{"type": "effect", "ids": ["c0.s0"], "value": "slowmo"},
                                                 {"type": "intro", "text": "Day 1"}]}).json()
    assert r["can_undo"] and len(r["done"]) == 2
    plan = c.get("/api/plan").json()
    assert plan["intro"]["title"] == "Day 1" and plan["parts"][0]["rec_start"] == 4.0
    assert plan["parts"][0]["speed"] == 0.5 and plan["parts"][0]["captions"] == []
    assert c.post("/api/actions", json={"actions": [{"type": "nonsense"}]}).status_code == 400
    r = c.post("/api/undo").json()
    assert r["project"]["intro"] is None and r["can_redo"]
    r = c.post("/api/redo").json()
    assert r["project"]["intro"]["title"] == "Day 1"
    assert c.post("/api/chat", json={"message": ""}).status_code == 400

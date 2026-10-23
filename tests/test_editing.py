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


def test_split_and_word_cuts_stay_cut():
    p = project()
    seg = p["clips"][0]["segments"][1]
    seg["words"] = [["We", 4.2, 4.5], ["ride", 4.6, 5.0], ["um", 5.2, 5.6], ["today.", 6.0, 8.0]]
    seg["texts"] = [{"text": "Late", "style": "label", "offset": 3.0, "duration": 2}]
    done, errors = editing.apply_actions(p, [{"type": "cut_words", "ids": ["c0.s1"], "start": 5.2, "end": 5.6}])
    assert not errors, errors
    a, b = p["clips"][0]["segments"][1], p["clips"][0]["segments"][-1]
    assert (a["text"], a["end"], a["cut_out"]) == ("We ride", 5.2, True)
    assert (b["text"], b["start"], b["cut_in"]) == ("today.", 5.6, True)
    assert b["texts"][0]["offset"] == round(4.2 + 3.0 - 5.6, 2) and not a["texts"]  # the title moved with its words
    parts = editing.build_parts(p, {})
    ranges = [(x["start"], x["end"]) for x in parts if x["clip"]["id"] == "a"][:2]
    assert ranges[0][1] == 5.2 and ranges[1][0] == 5.6  # no padding re-includes "um"
    # split: no lost or doubled frames at the split point
    editing.apply_actions(p, [{"type": "split", "ids": ["c1.s0"], "start": 4.0}])
    parts = [x for x in editing.build_parts(p, {}) if x["clip"]["id"] == "b"]
    assert [(x["start"], x["end"]) for x in parts] == [(1.85, 4.0), (4.0, 6.15)]
    assert editing.apply_actions(p, [{"type": "split", "ids": ["c1.s0"], "start": 3.9}])[1]  # too close to the edge


def test_broll_stays_in_its_scene_and_cuts_away():
    p = project()
    p["clips"][0]["broll"] = [{"start": 9, "end": 13, "keep": True, "interest": 0.8},
                              {"start": 55, "end": 59, "keep": True, "interest": 0.9}]
    p["clips"][0]["duration"] = 300
    p["clips"][0]["broll"].append({"start": 200, "end": 204, "keep": True, "interest": 0.9})  # far from any talking
    over, dropped = editing.place_broll(p, "auto", scene=60)
    b = p["clips"][0]["broll"]
    assert dropped == 1 and not b[2]["keep"]
    assert "over" not in b[0] and "over" not in b[1] and b[1]["keep"]  # 4 s of B-roll needs a line >= 5.5 s
    p["clips"][0]["segments"][1]["end"] = 15  # a long line now
    editing.place_broll(p, "auto", scene=60)
    assert b[0]["over"] == "c0.s1" and "over" not in b[1]  # one cutaway per line; the other stays a shot
    parts = editing.build_parts(p, {})
    host = next(x for x in parts if "c0.s1" in x["ids"])
    assert host["overlays"] and host["overlays"][0]["id"] == "c0.b0" and not any("c0.b0" in x["ids"] for x in parts)
    assert host["overlays"][0]["at"] == 4.2 + 1.0
    # the creator's own B-roll is never dropped, even far away
    p["clips"][0]["broll"][2].update(keep=True, reason="you said a marker phrase")
    assert editing.place_broll(p, "insert", scene=60)[1] == 0 and b[2]["keep"] and not b[0].get("over")


def test_intro_over_video_and_placed_text_render():
    p = project()
    editing.apply_actions(p, [{"type": "intro", "text": "Day 1", "value": "Pokhara", "over": True},
                              {"type": "text", "ids": ["c0.s0"], "text": "Here", "value": "plain", "x": 0.3, "y": 0.8, "size": 0.08}])
    assert p["intro"]["over"] is True
    t = p["clips"][0]["segments"][0]["texts"][0]
    assert (t["x"], t["y"], t["size"]) == (0.3, 0.8, 0.08)
    editing.apply_actions(p, [{"type": "edit_text", "ids": ["c0.s0"], "index": 0, "x": 0.5, "text": "There"}])
    assert (t["x"], t["text"]) == (0.5, "There")
    f = editing.text_filter("texts/a.txt", "fonts/f.ttf", "plain", 0, 2, 1280, 720, 0.5, 0.8, 0.08)
    assert "x=w*0.5000-text_w/2:y=h*0.8000-text_h/2" in f and "fontsize=57" in f


def test_cutaway_render(tmp_path):
    """A B-roll cutaway over a talking part renders in one FFmpeg pass, keeping the part's length and sound."""
    src = tmp_path / "talk.mp4"
    ve._ffmpeg(["-y", "-f", "lavfi", "-i", "testsrc=s=320x180:d=6:r=25", "-f", "lavfi", "-i", "sine=f=440:d=6",
                "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(src)])
    fmt = {"width": 320, "height": 180, "fps": 25}
    vf = production.base_vf(fmt) + ",fps=25,format=yuv420p"
    spec = {"src": str(src), "start": 0.5, "end": 4.5, "vf": production.base_vf(fmt), "top": "fps=25,format=yuv420p",
            "af": "", "overlays": [{"src": str(src), "start": 5.0, "end": 6.0, "at": 1.0, "vf": vf}]}
    out, starts = ve.render_parts([spec], tmp_path / "out.mp4", fmt, "software", tmp_path / "parts", str(tmp_path))
    info = ve.probe(out)
    assert abs(info["duration"] - 4.0) < 0.15 and info["has_audio"]

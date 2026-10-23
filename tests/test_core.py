"""Self-checks for the pure logic. Run: python tests/test_core.py  (or pytest)"""
import json
import os
import sys
import tempfile
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Never touch the real workspace: point the app at a throwaway data folder holding a v0.1-style project.
HOME = Path(tempfile.mkdtemp())
os.environ["LEANCUT_HOME"] = str(HOME)
(HOME / "output").mkdir()
(HOME / "output" / "old_cut.mp4").write_bytes(b"x")
(HOME / "project.json").write_text(json.dumps({"clips": [], "settings": {"model": "qwen2.5:14b", "target_length": "12"}}))

import app  # noqa: E402  (runs the v0.1 -> v0.2 migration on import)
import exporters  # noqa: E402
import llm_agent  # noqa: E402
import media_analysis  # noqa: E402
import transcriber  # noqa: E402
import updates  # noqa: E402
import video_engine  # noqa: E402
from app import parse_target  # noqa: E402


def test_merge_ranges_pads_merges_and_clamps():
    segs = [{"start": 5.0, "end": 6.0}, {"start": 0.1, "end": 2.0}, {"start": 2.5, "end": 3.0}]
    assert video_engine.merge_ranges(segs, duration=6.05) == [(0.0, 3.15), (4.85, 6.05)]


def test_parse_scores_tolerates_noise():
    reply = ('<think>hmm {"x":1}</think>```json\n{"scores": [{"id": 3, "s": 5, "r": "hook"}, {"id": "4", "s": "9"},'
             ' {"id": 99, "s": 1}, {"id": "x", "s": 2}, {"id": 5}]}\n```')
    assert llm_agent.parse_scores(reply, {3, 4, 5}) == {3: (5, "hook"), 4: (5, "")}
    for bad in ("sorry, I can't", '{"scores": []}'):
        try:
            llm_agent.parse_scores(bad, {1})
            raise AssertionError("expected LLMParseError")
        except llm_agent.LLMParseError:
            pass


def test_select_enforces_budget_whatever_the_scores():
    dur = {0: 10, 1: 10, 2: 10, 3: 10, 4: 10}
    harsh = {i: 1 for i in dur}  # a model that calls everything "redundant"
    assert len(llm_agent.select(dur, harsh, ratio=1.5)) == 4  # still keeps >= 80%
    scores = {0: 5, 1: 1, 2: 4, 3: 2, 4: 5}
    assert llm_agent.select(dur, scores, ratio=0.6) == {0, 2, 4}  # best first, exactly the target
    assert llm_agent.select(dur, scores, ratio=None) == {0, 2, 4}  # score >= 3
    assert llm_agent.select(dur, scores, ratio=1.2) == {0, 2, 3, 4}  # only the 1 goes


CLIP_DJI = {"id": "c1", "path": "/rides/DJI_0001.MP4", "duration": 100.0, "fps": 29.97, "timecode": "21:01:21;28",
            "width": 3840, "height": 2160, "segments": []}


def test_drop_frame_timecode_round_trips():
    assert exporters.frames_to_tc(1800, 29.97, True) == "00:01:00;02"
    assert exporters.frames_to_tc(1799, 29.97, True) == "00:00:59;29"
    assert exporters.frames_to_tc(17982, 29.97, True) == "00:10:00;00"
    assert exporters.tc_to_frames("00:10:00;00", 29.97, True) == 17982
    for n in (0, 1, 1799, 1800, 1801, 17981, 17982, 1234567):
        assert exporters.tc_to_frames(exporters.frames_to_tc(n, 29.97, True), 29.97, True) == n
    assert exporters.frames_to_tc(90, 25, False) == "00:00:03:15"


def test_edl_uses_the_clips_own_timecode():
    tl = exporters.timeline([(CLIP_DJI, 1.0, 2.5, 1), (CLIP_DJI, 50.0, 60.0, 8.0), (CLIP_DJI, 70.0, 71.0, 1)])
    with tempfile.TemporaryDirectory() as d:
        lines = exporters.write_edl(tl, Path(d) / "x.edl", 29.97, "ride").read_text().splitlines()
    assert lines[1] == "FCM: DROP FRAME"
    assert lines[3] == "001  AX       AA/V  C        21:01:22;28 21:01:24;13 01:00:00;00 01:00:01;15"
    assert lines[4] == "* FROM CLIP NAME: DJI_0001.MP4"
    assert lines[6].startswith("002  AX       AA/V  C        21:02:31;")  # the 8x connector is left out of the EDL
    assert tl[1]["rec_end"] - tl[1]["rec_start"] == 1.25  # 10 s at 8x


def test_split_sentences_on_punctuation_and_pauses():
    from types import SimpleNamespace as W
    from transcriber import split_sentences
    words = [W(word=" Hi", start=0.0, end=0.4), W(word=" there.", start=0.5, end=0.9),
             W(word=" Um", start=1.0, end=1.2), W(word=" look", start=3.0, end=3.3), W(word=" wow", start=3.4, end=3.8)]
    assert split_sentences(words) == [{"start": 0.0, "end": 0.9, "text": "Hi there."},
                                      {"start": 1.0, "end": 1.2, "text": "Um"},
                                      {"start": 3.0, "end": 3.8, "text": "look wow"}]


def test_merge_fragments_joins_short_pieces_but_not_long_pauses():
    from transcriber import merge_fragments
    segs = [{"start": 0.0, "end": 0.5, "text": "I"}, {"start": 0.8, "end": 1.5, "text": "have to"},
            {"start": 1.6, "end": 5.0, "text": "go now."}, {"start": 5.2, "end": 9.5, "text": "Look at that."},
            {"start": 12.0, "end": 12.4, "text": "Woo!"}]
    once = merge_fragments(segs)
    assert once == [{"start": 0.0, "end": 5.0, "text": "I have to go now."},
                    {"start": 5.2, "end": 9.5, "text": "Look at that."},
                    {"start": 12.0, "end": 12.4, "text": "Woo!"}]
    assert merge_fragments(once) == once


def test_parse_target():
    assert parse_target("10-15 minutes") == 750
    assert parse_target("12") == 720
    assert parse_target("") is None


def test_chat_streams_and_detects_stalls():
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.end_headers()
            if self.path.endswith("/chat/completions"):
                self.wfile.write(b'data: {"choices":[{"delta":{"content":"{\\"keep\\""}}]}\n\n'
                                 b'data: {"choices":[{"delta":{"content":":[1]}"}}]}\n\ndata: [DONE]\n\n')
                return
            self.wfile.write(b'{"message":{"content":"{\\"ke"}}\n{"message":{"content":"ep\\":[]}"}}\n')
            self.wfile.flush()
            if "stall" in self.path:
                time.sleep(3)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    assert llm_agent.chat("ollama", base, "m", "s", "u") == '{"keep":[]}'
    assert llm_agent.chat("lmstudio", base + "/v1", "m", "s", "u") == '{"keep":[1]}'
    try:
        llm_agent.chat("ollama", base + "/stall", "m", "s", "u", stall_timeout=1)
        raise AssertionError("expected a stall error")
    except llm_agent.LLMError as e:
        assert "no output" in str(e)
    srv.shutdown()


def test_decide_falls_back_to_keep_all_on_bad_replies():
    segs = [{"id": i, "clip": 1, "start": i, "end": i + 1, "text": "hi"} for i in range(3)]
    real = llm_agent.chat
    quiet = {"log": lambda m: None}
    llm_agent.chat = lambda *a: "not json"
    assert all(d["keep"] for d in llm_agent.decide(segs, "ollama", "", "m", **quiet).values())
    llm_agent.chat = lambda *a: '{"scores":[{"id":0,"s":2},{"id":1,"s":5,"r":"funny"},{"id":2,"s":1,"r":"repeat"}]}'
    assert llm_agent.decide(segs, "ollama", "", "m", **quiet) == {
        0: {"keep": False, "score": 2, "reason": ""}, 1: {"keep": True, "score": 5, "reason": "funny"},
        2: {"keep": False, "score": 1, "reason": "repeat"}}
    # target above the speech length: cutting the 1 would leave 67% < the 80% floor, so everything stays
    kept = {i for i, d in llm_agent.decide(segs, "ollama", "", "m", target_seconds=60, **quiet).items() if d["keep"]}
    assert kept == {0, 1, 2}
    llm_agent.chat = real


def test_v01_project_is_migrated_without_loss():
    projects = app.list_projects()
    assert [p["name"] for p in projects] == ["My first project"]
    pdir = app.PROJECTS / projects[0]["id"]
    assert (pdir / "output" / "old_cut.mp4").exists() and not (HOME / "project.json").exists()
    assert app.settings["model"] == "qwen2.5:14b" and app.project["target_length"] == "12"


def test_api_refuses_foreign_hosts_and_origins():
    from fastapi.testclient import TestClient
    c = TestClient(app.api)
    assert c.get("/api/job", headers={"host": "127.0.0.1:8765"}).status_code == 200
    assert c.get("/api/job", headers={"host": "evil.example:8765"}).status_code == 403  # DNS rebinding
    assert c.post("/api/cancel", headers={"host": "127.0.0.1:8765", "origin": "https://evil.example"}).status_code == 403


def test_retakes_loops_and_markers():
    segs = [{"start": 0, "end": 3, "text": "Welcome back to the channel guys."},
            {"start": 4, "end": 7, "text": "Welcome back to the channel, guys!"},
            {"start": 9, "end": 12, "text": "She is the type of child. She is the type of child? Okay."},
            {"start": 13, "end": 14, "text": "Oh wow, clip that!"}]
    transcriber.flag_retakes(segs)
    assert segs[0]["auto"] == "retake" and "auto" not in segs[1]
    assert segs[2]["text"] == "She is the type of child. Okay."
    assert transcriber.find_markers(segs, "clip that, mark this") == [14]
    assert transcriber.find_markers([{"end": 1, "text": "an eclipse that day"}], "clip that") == []
    assert transcriber.is_marker_only("Clip that!", "clip that, mark this")
    assert not transcriber.is_marker_only("Oh wow, clip that!", "clip that, mark this")


def test_merge_keeps_cached_scores_only_on_untouched_segments():
    segs = [{"start": 0, "end": 5, "text": "Whole sentence.", "score": 4, "scored_by": "m"},
            {"start": 6, "end": 6.5, "text": "and", "score": 2}, {"start": 6.6, "end": 7, "text": "more", "score": 3}]
    out = transcriber.merge_fragments(segs)
    assert out[0]["score"] == 4 and "score" not in out[1] and out[1]["text"] == "and more"


def _write_wav(path, samples):
    import numpy as np
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(16000)
        w.writeframes((np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes())


def test_audio_sync_offset_and_loud_moments():
    import numpy as np
    rng = np.random.default_rng(1)
    env = np.repeat(rng.random(400) ** 3, 1600)  # 40 s of bursty "speech" loudness
    ref = env * rng.standard_normal(len(env)) * 0.3
    other = ref[int(7.25 * 16000):] * 0.5 + rng.standard_normal(len(ref) - int(7.25 * 16000)) * 0.002
    with tempfile.TemporaryDirectory() as d:
        a, b = Path(d) / "a.wav", Path(d) / "b.wav"
        _write_wav(a, ref)
        _write_wav(b, other)
        offset, confidence = media_analysis.sync_offset(a, b)
        assert abs(offset - 7.25) < 0.02 and confidence > 8
        quiet = rng.standard_normal(16000 * 30) * 0.01
        quiet[16000 * 12:16000 * 13] *= 40  # a one-second shout
        _write_wav(a, quiet)
        moments = media_analysis.loud_moments(a)
        assert len(moments) == 1 and 11.9 <= moments[0]["start"] <= 12.1


def test_broll_windows_avoid_speech_and_prefer_motion():
    stats = [{"t": t, "motion": (0.3 if 20 <= t < 30 else 0.01), "sat": 50, "y": 120} for t in range(60)]
    speech = [{"start": 0, "end": 10}, {"start": 40, "end": 50}]
    wins = media_analysis.broll_candidates(60, speech, stats)
    assert wins and all(not (w["start"] < 50.5 and w["end"] > 39.5) for w in wins)
    assert 20 <= wins[0]["start"] < 30
    picked = media_analysis.pick_broll([("c", w) for w in wins], budget=6)
    assert len(picked) == 2  # 5 s windows until 6 s of budget is used


def test_shorts_and_connectors():
    segs = [{"start": i * 5, "end": i * 5 + 4.5, "score": 5 if 10 <= i < 16 else 2} for i in range(30)]
    wins = media_analysis.shorts_windows([{"segments": segs}], n=2)
    assert wins[0][1] == 50 and 15 <= wins[0][2] - wins[0][1] <= 58
    assert media_analysis.connectors([(0, 10), (200, 210)], 215) == [(10.0, 200.0, 31.67)]  # 190 s gap -> 6 s
    assert media_analysis.connectors([(0, 10), (1000, 1010)], 1015)[0][2] == 64.0  # capped speed, middle part only


def test_fcpxml_otio_and_srt_line_up():
    import xml.etree.ElementTree as ET
    clip = {**CLIP_DJI, "segments": [{"start": 1.0, "end": 2.4, "text": "Hey everyone.", "keep": True},
                                     {"start": 30, "end": 33, "text": "Look at that view.", "keep": True}]}
    angle = {**CLIP_DJI, "id": "c2", "path": "/rides/chest.MP4", "timecode": None}
    tl = exporters.timeline([(clip, 0.85, 2.55, 1), (clip, 29.85, 33.15, 1)])
    fmt = {"width": 3840, "height": 2160, "fps": 29.97}
    with tempfile.TemporaryDirectory() as d:
        x = ET.parse(exporters.write_fcpxml(tl, Path(d) / "a.fcpxml", fmt, "Ride", [(angle, "c1", 2.0)], [(1.0, "Start")]))
        spine = x.findall(".//spine/asset-clip")
        assert len(spine) == 2 and spine[0].get("offset") == "0/30000s"
        assert spine[0].find("asset-clip").get("lane") == "1"  # synced chest cam as a connected clip
        assert x.find(".//chapter-marker").get("value") == "Start"
        doc = json.loads(exporters.write_otio(tl, Path(d) / "a.otio", fmt, "Ride", [(angle, "c1", 2.0)]).read_text())
        assert [t["name"] for t in doc["tracks"]["children"]] == ["V1", "V2", "A1"]
        srt = exporters.write_srt(tl, Path(d) / "a.srt").read_text()
        assert "00:00:00,150 --> 00:00:01,550\nHey everyone." in srt
        assert "Look at that view." in srt and "00:00:01,850 -->" in srt
        try:
            import opentimelineio as otio  # optional: validates against the real OTIO library when installed
            assert len(otio.adapters.read_from_file(str(Path(d) / "a.otio")).video_tracks()[0]) == 2
        except ImportError:
            pass


def test_chapters_follow_youtube_rules():
    lines = [(0.0, "a"), (5.0, "b"), (40.0, "c"), (95.0, "d"), (300.0, "e")]
    assert exporters.clean_chapters([(3, "Intro"), (41, "Road"), (44, "Dup"), (299, "End")], lines, 600) == [
        (0.0, "Intro"), (40.0, "Road"), (300.0, "End")]
    assert exporters.clean_chapters([(0, "Only"), (40, "Two")], lines, 600) == []  # YouTube needs at least 3


def test_update_version_compare():
    assert updates.is_newer("0.10.0", "0.9.9") and updates.is_newer("v0.2.1", "0.2.0")
    assert not updates.is_newer("0.2.0", "0.2.0") and not updates.is_newer("0.1.9", "0.2.0")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok ", name)

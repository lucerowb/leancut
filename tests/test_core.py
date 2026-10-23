"""Self-checks for the pure logic. Run: python tests/test_core.py  (or pytest)"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import llm_agent
import video_engine
from app import parse_target


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


def test_edl_timecodes_are_continuous():
    with tempfile.TemporaryDirectory() as d:
        p = video_engine.write_edl([("/a/clip1.mp4", 1.0, 2.5), ("/b/clip2.mov", 3600.0, 3601.0)], Path(d) / "x.edl", fps=29.97)
        lines = p.read_text().splitlines()
    assert lines[3] == "001  AX       AA/V  C        00:00:01:00 00:00:02:15 01:00:00:00 01:00:01:15"
    assert lines[4] == "* FROM CLIP NAME: clip1.mp4"
    assert lines[6] == "002  AX       AA/V  C        01:00:00:00 01:00:01:00 01:00:01:15 01:00:02:15"


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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok ", name)

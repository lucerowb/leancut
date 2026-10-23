"""Tests for vision.py. The pure parts run anywhere (FFmpeg comes from imageio-ffmpeg); model tests run only when
the models are already downloaded into $LEANCUT_MODELS — tests never download."""
import math
import os
import sys
import tempfile
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import video_engine  # noqa: E402
import vision  # noqa: E402

MODELS_DIR = os.environ.get("LEANCUT_MODELS", "")


def needs(*names):
    missing = [n for n in names if not MODELS_DIR or not (
        Path(MODELS_DIR) / vision.MODELS[n]["repo"].replace("/", "--") / vision.MODELS[n]["file"]).exists()]
    if missing:
        pytest.skip(f"models not downloaded (set LEANCUT_MODELS): {', '.join(missing)}")


def make_video(path, colors, seconds=2, size="320x180"):
    """One solid-colour stretch per colour, 1 keyframe per second."""
    inputs = []
    for c in colors:
        inputs += ["-f", "lavfi", "-i", f"color=c={c}:s={size}:d={seconds}:r=30"]
    n = len(colors)
    video_engine._ffmpeg(["-y", *inputs, "-filter_complex", "".join(f"[{i}]" for i in range(n)) + f"concat=n={n}",
                          "-c:v", "libx264", "-g", "30", "-pix_fmt", "yuv420p", str(path)])
    return path


# ---------------------------------------------------------------- pure logic

def test_nms_keeps_best_and_drops_overlaps():
    boxes = [(0, 0, 10, 10), (1, 1, 10, 10), (50, 50, 10, 10), (0, 0, 10, 10)]
    assert vision.nms(boxes, [0.9, 0.95, 0.8, 0.3], iou=0.3, min_score=0.5) == [1, 2]
    assert vision.nms([], [], min_score=0.5) == []


def test_decode_yunet_maps_grid_cell_to_pixels():
    outs = {}
    for st in (8, 16, 32):
        n = (64 // st) ** 2
        outs[f"cls_{st}"], outs[f"obj_{st}"] = np.zeros((1, n, 1)), np.zeros((1, n, 1))
        outs[f"bbox_{st}"] = np.zeros((1, n, 4))
    idx = 2 * 8 + 3  # stride 8 on a 64 px input: 8 columns; row 2, column 3
    outs["cls_8"][0, idx, 0], outs["obj_8"][0, idx, 0] = 0.81, 1.5  # obj is clamped to 1
    outs["bbox_8"][0, idx] = (0.5, 0.5, math.log(2), math.log(3))
    boxes, scores = vision.decode_yunet(outs, 64, 64)
    assert len(boxes) == 64 + 16 + 4
    assert np.allclose(boxes[idx], (28 - 8, 20 - 12, 16, 24)) and math.isclose(scores[idx], 0.9)


def test_lpd_priors_match_the_model_head():
    pri = vision._lpd_priors(320, 240)
    assert pri.shape == (4385, 4)  # the plate model's output count at 320×240
    assert np.allclose(pri[0], (4 / 320, 4 / 240, 10 / 320, 10 / 240)) and np.allclose(pri[2, 2], 24 / 320)
    outs = {"loc": np.zeros((4385, 14)), "conf": np.full((4385, 2), 0.25), "iou": np.full((4385, 1), 4.0)}
    boxes, scores = vision.decode_lpd(outs, 320, 240)
    assert np.allclose(boxes[0], (4, 4, 0, 0)) and np.allclose(scores, 0.5)


def test_letterbox_scales_into_the_top_left():
    img = np.full((720, 1280, 3), 200, np.uint8)
    blob, s = vision.letterbox(img, 640, 640)
    assert blob.shape == (1, 3, 640, 640) and blob.dtype == np.float32 and s == 0.5
    assert blob[0, :, 359, 639].tolist() == [200] * 3 and blob[0, :, 360, 0].tolist() == [0] * 3


def test_merge_tracks_joins_bridges_and_splits():
    W, H = 1920, 1080
    face = [(t, "face", 100, 100, 50, 50) for t in (1.0, 1.5, 2.5)]  # one missed sample at 2.0 is bridged
    other = [(1.5, "face", 800, 100, 50, 50), (1.5, "plate", 100, 100, 50, 50)]  # elsewhere / other kind
    out = vision.merge_tracks(face + other, 0.5, W, H)
    assert out[0] == {"t0": 0.75, "t1": 2.75, "x": 90, "y": 90, "w": 71, "h": 71}  # padded by 20%
    assert len(out) == 3
    late = vision.merge_tracks([(1.0, "face", 100, 100, 50, 50), (3.0, "face", 100, 100, 50, 50)], 0.5, W, H)
    assert len(late) == 2  # too long a gap: separate appearances
    edge = vision.merge_tracks([(0.0, "face", 1900, -5, 50, 50)], 0.5, W, H)[0]
    assert edge["t0"] == 0.0 and edge["x"] + edge["w"] <= W and edge["y"] == 0


def test_merge_tracks_follows_a_moving_face_in_overlapping_steps():
    dets = [(t, "face", 100 + 120 * t, 100, 50, 50) for t in (0.0, 0.5, 1.0, 1.5)]  # 60 px per sample
    out = vision.merge_tracks(dets, 0.5, 1920, 1080, pad=0)
    assert len(out) > 1
    for t in np.arange(0, 1.51, 0.05):  # wherever the face is, some active box contains it
        x = 100 + 120 * t
        assert any(b["t0"] <= t <= b["t1"] and b["x"] <= x and x + 50 <= b["x"] + b["w"] + 1 for b in out), t
    assert max(b["w"] for b in out) < 150  # never one box over the whole 230 px path


def test_blur_filter_string_and_ffmpeg_accepts_it(tmp_path):
    assert vision.blur_filter([], 0, 320, 180) == ""
    boxes = [{"t0": 9.0, "t1": 10.5, "x": 11, "y": 21, "w": 61, "h": 41},
             {"t0": 0.0, "t1": 4.0, "x": 300, "y": 160, "w": 100, "h": 100},  # ends before the part: dropped
             {"t0": 11.0, "t1": 12.0, "x": 280, "y": 150, "w": 100, "h": 100}]  # clipped to the frame
    vf = vision.blur_filter(boxes, 10.0, 320, 180)
    assert vf.count("overlay=") == 2 and "between(t,0.000,0.500)" in vf and "between(t,1.000,2.000)" in vf
    assert "crop=62:42:10:20" in vf and "crop=40:30:280:150" in vf
    out = tmp_path / "blurred.mp4"
    src = make_video(tmp_path / "src.mp4", ["red", "blue"])
    # as render() uses it: a chain element in front of the normal scale/pad chain
    video_engine._ffmpeg(["-y", "-ss", "1", "-i", str(src), "-t", "2", "-vf", f"{vf},scale=160:90,setsar=1",
                          "-c:v", "libx264", str(out)])
    assert video_engine.probe(out)["width"] == 160


def test_clip_pixels_normalises_like_clip():
    x = vision.clip_pixels(np.full((2, 224, 224, 3), 255, np.uint8))
    assert x.shape == (2, 3, 224, 224) and x.dtype == np.float32 and x.flags.c_contiguous
    assert np.allclose(x[0, :, 0, 0], [(1 - m) / s for m, s in zip(vision.CLIP_MEAN, vision.CLIP_STD)])


def test_smooth_track_drops_outliers_and_keeps_length():
    pts = [(i * 0.5, 0.3) for i in range(10)]
    pts[4] = (2.0, 0.95)  # one-frame jump
    out = vision.smooth_track(pts)
    assert [t for t, _ in out] == [t for t, _ in pts] and all(abs(v - 0.3) < 1e-9 for _, v in out)
    assert vision.smooth_track([]) == [] and vision.smooth_track([(0.0, 0.7)]) == [(0.0, 0.7)]


def test_saliency_points_at_the_odd_object():
    rng = np.random.default_rng(0)
    frame = rng.normal(100, 3, (180, 320))
    frame[60:100, 40:80] = 250
    assert vision.saliency_cx(frame) < 0.4
    assert vision.saliency_cx(frame[:, ::-1]) > 0.6
    assert vision.saliency_cx(np.zeros((180, 320))) == 0.5


def test_frames_are_timed_and_chunked(tmp_path):
    src = make_video(tmp_path / "rgb.mp4", ["red", "blue"])
    got = [(t, int(f[..., 2].mean()) > 200) for t, f in vision._frames(src, 0.0, 4.0, 0.5, 32, 18, chunk=3)]
    assert [t for t, _ in got] == [i * 0.5 for i in range(8)]  # chunk seams neither drop nor repeat a sample
    assert [red for _, red in got] == [True] * 4 + [False] * 4


def test_work_size_is_even_and_bounded():
    assert vision._work_size(3840, 2160) == (640, 360)
    assert vision._work_size(1080, 1920) == (360, 640)
    assert vision._work_size(320, 180) == (320, 180)


# ---------------------------------------------------------------- with models (skipped unless downloaded)

def test_detect_runs_on_real_models():
    needs("face", "plate")
    blank = np.zeros((360, 640, 3), np.uint8)
    assert vision.detect("face", blank, MODELS_DIR) == [] and vision.detect("plate", blank, MODELS_DIR) == []
    with pytest.raises(ValueError):
        vision.detect("cat", blank, MODELS_DIR)


def test_clip_search_finds_the_red_frame_and_caches(tmp_path):
    needs("clip_vision", "clip_text", "clip_tokenizer")
    src = make_video(tmp_path / "rgb.mp4", ["blue", "red", "green"])
    cache = tmp_path / "index.npz"
    index = vision.embed_frames(src, cache, MODELS_DIR, every=1.0)
    assert index["emb"].shape == (len(index["t"]), 512) and np.allclose(np.linalg.norm(index["emb"], axis=1), 1)
    top_t, _ = vision.search("a red image", index, MODELS_DIR, top=3)[0]
    assert 2.0 <= top_t < 4.0
    again = vision.embed_frames(src, cache, tempfile.gettempdir(), every=1.0)  # from cache: no model needed
    assert again["t"] == index["t"] and np.allclose(again["emb"], index["emb"])
    scores = vision.scenic_score(index, MODELS_DIR)
    assert [t for t, _ in scores] == index["t"] and all(0 <= s <= 1 for _, s in scores)


def test_subject_track_and_privacy_on_a_faceless_clip(tmp_path):
    needs("face", "plate")
    src = make_video(tmp_path / "plain.mp4", ["gray"], seconds=3)
    track = vision.subject_track(src, 0.0, 3.0, MODELS_DIR)
    assert [t for t, _ in track] == [0.0, 0.5, 1.0, 1.5, 2.0, 2.5] and all(0 <= x <= 1 for _, x in track)
    assert vision.privacy_boxes(src, 0.5, 2.5, MODELS_DIR) == []

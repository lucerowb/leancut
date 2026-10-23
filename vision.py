"""On-device vision: privacy blur (faces, plates), CLIP frame search and scenic scores, subject tracking for 9:16.

Models are downloaded on first use into a folder the caller chooses (never bundled) and run with onnxruntime, on a GPU
when onnx_runtime finds one. Decoding, NMS, tracking and smoothing are plain numpy so they can be tested without any model.
"""
import functools
from pathlib import Path

import video_engine as ve

# Pinned to a commit so a changed upstream file can't silently swap the weights we run.
MODELS = {
    "face": {  # YuNet, https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet
        "repo": "opencv/face_detection_yunet", "file": "face_detection_yunet_2023mar.onnx",
        "revision": "3cc26e7f1014a5ee5d74a42acee58bafc9d0a310", "license": "MIT", "size_mb": 0.2,
    },
    "plate": {  # LPD-YuNet, https://github.com/opencv/opencv_zoo/tree/main/models/license_plate_detection_yunet
        "repo": "opencv/license_plate_detection_yunet", "file": "license_plate_detection_lpd_yunet_2023mar.onnx",
        "revision": "962e8d8849878106cb84039dab61cf97bb683b31", "license": "Apache-2.0", "size_mb": 4.0,
        "coreml": True,  # M5 Pro: 1.9-2.4 ms vs 3.5 ms on the CPU, outputs within 1.5e-5 (YuNet face: no gain, so CPU)
    },
    # OpenAI CLIP ViT-B/32 (MIT, https://github.com/openai/CLIP), int8 ONNX export by Xenova (transformers.js)
    "clip_vision": {
        "repo": "Xenova/clip-vit-base-patch32", "file": "onnx/vision_model_quantized.onnx",
        "revision": "d15189d7028b43f1d3e65039190477f6af591c2a", "license": "MIT", "size_mb": 85,
    },
    "clip_text": {
        "repo": "Xenova/clip-vit-base-patch32", "file": "onnx/text_model_quantized.onnx",
        "revision": "d15189d7028b43f1d3e65039190477f6af591c2a", "license": "MIT", "size_mb": 62,
    },
    "clip_tokenizer": {
        "repo": "Xenova/clip-vit-base-patch32", "file": "tokenizer.json",
        "revision": "d15189d7028b43f1d3e65039190477f6af591c2a", "license": "MIT", "size_mb": 2.2,
    },
}
# Below the demos' 0.8-0.9: for privacy a missed face costs more than a stray blur.
# The plate model is trained on Chinese plates; other countries' plates scored ~0.45-0.5 in testing.
THRESHOLD = {"face": 0.5, "plate": 0.45}
CLIP_MEAN, CLIP_STD = (0.48145466, 0.4578275, 0.40821073), (0.26862954, 0.26130258, 0.27577711)
SCENIC = ["a scenic mountain road", "a beautiful landscape view", "a winding road through nature",
          "a sunset over the hills"]
NOT_SCENIC = ["a close-up of a face", "a parking lot", "a blurry dark photo", "an indoor room", "a busy city street"]


def _np():
    import numpy as np  # imported lazily so the API starts fast
    return np


def ensure_model(name, models_dir) -> Path:
    """Local path of a model file, downloading it once. Existing files are used without touching the network."""
    m = MODELS[name]
    folder = Path(models_dir) / m["repo"].replace("/", "--")
    path = folder / m["file"]
    if not path.exists():
        from huggingface_hub import hf_hub_download  # writes to a temp name and renames, so no half files
        hf_hub_download(m["repo"], m["file"], revision=m["revision"], local_dir=folder)
    return path


@functools.cache
def _session(name, models_dir):
    import onnx_runtime
    return onnx_runtime.session(ensure_model(name, models_dir), coreml=MODELS[name].get("coreml", False))


@functools.cache
def _tokenizer(path):
    from tokenizers import Tokenizer
    return Tokenizer.from_file(str(path))


# ---------------------------------------------------------------- detection (faces, plates)

def nms(boxes, scores, iou=0.3, min_score=0.5):
    """Greedy non-maximum suppression on (x, y, w, h) boxes: kept indices, best first."""
    np = _np()
    b, s = np.asarray(boxes, float).reshape(-1, 4), np.asarray(scores, float).reshape(-1)
    x1, y1, x2, y2 = b[:, 0], b[:, 1], b[:, 0] + b[:, 2], b[:, 1] + b[:, 3]
    area = b[:, 2] * b[:, 3]
    order = np.array([i for i in np.argsort(-s) if s[i] >= min_score], int)
    keep = []
    while order.size:
        i, rest = order[0], order[1:]
        keep.append(int(i))
        inter = (np.clip(np.minimum(x2[i], x2[rest]) - np.maximum(x1[i], x1[rest]), 0, None)
                 * np.clip(np.minimum(y2[i], y2[rest]) - np.maximum(y1[i], y1[rest]), 0, None))
        order = rest[inter / (area[i] + area[rest] - inter + 1e-9) <= iou]
    return keep


def letterbox(img, W, H):
    """Fit an h×w×3 uint8 image into W×H (top-left, black padding): (float32 1×3×H×W blob, scale)."""
    np = _np()
    h, w = img.shape[:2]
    s = min(W / w, H / h)
    nw, nh = max(1, round(w * s)), max(1, round(h * s))
    # shortcut: nearest-neighbour resize (no image library); callers grab frames near the model size anyway
    ys = np.minimum((np.arange(nh) / s).astype(int), h - 1)
    xs = np.minimum((np.arange(nw) / s).astype(int), w - 1)
    canvas = np.zeros((H, W, 3), np.float32)
    canvas[:nh, :nw] = img[ys[:, None], xs]
    return canvas.transpose(2, 0, 1)[None], s


def decode_yunet(outs, W, H):
    """YuNet face head (strides 8/16/32, anchor-free) → (x,y,w,h boxes, scores), as OpenCV's FaceDetectorYN does."""
    np = _np()
    boxes, scores = [], []
    for st in (8, 16, 32):
        cols = W // st
        cls, obj = outs[f"cls_{st}"].reshape(-1), outs[f"obj_{st}"].reshape(-1)
        bb = outs[f"bbox_{st}"].reshape(-1, 4)
        r, c = np.divmod(np.arange(len(cls)), cols)
        cx, cy = (c + bb[:, 0]) * st, (r + bb[:, 1]) * st
        w, h = np.exp(bb[:, 2]) * st, np.exp(bb[:, 3]) * st
        boxes.append(np.stack([cx - w / 2, cy - h / 2, w, h], 1))
        scores.append(np.sqrt(np.clip(cls, 0, 1) * np.clip(obj, 0, 1)))
    return np.concatenate(boxes), np.concatenate(scores)


@functools.cache
def _lpd_priors(W, H):
    np = _np()
    fh, fw = (H + 1) // 2 // 2, (W + 1) // 2 // 2
    out = []
    for step, sizes in zip((8, 16, 32, 64), ([10, 16, 24], [32, 48], [64, 96], [128, 192, 256])):
        fh, fw = fh // 2, fw // 2
        i, j = np.divmod(np.repeat(np.arange(fh * fw), len(sizes)), fw)  # row-major cells, every anchor size per cell
        sz = np.tile(np.array(sizes, float), fh * fw)
        out.append(np.stack([(j + 0.5) * step / W, (i + 0.5) * step / H, sz / W, sz / H], 1))
    return np.concatenate(out).astype(np.float32)


def decode_lpd(outs, W, H):
    """LPD-YuNet (SSD-style priors) → axis-aligned boxes around the 4 predicted plate corners, and scores."""
    np = _np()
    pri, loc = _lpd_priors(W, H), outs["loc"]
    corners = [(pri[:, :2] + loc[:, k:k + 2] * 0.1 * pri[:, 2:]) * (W, H) for k in (4, 6, 10, 12)]
    xs, ys = np.stack([c[:, 0] for c in corners], 1), np.stack([c[:, 1] for c in corners], 1)
    x1, y1 = xs.min(1), ys.min(1)
    boxes = np.stack([x1, y1, xs.max(1) - x1, ys.max(1) - y1], 1)
    return boxes, np.sqrt(outs["conf"][:, 1] * np.clip(outs["iou"][:, 0], 0, 1))


def detect(name, image_bgr_uint8, models_dir):
    """Faces or licence plates in one BGR image: [(x, y, w, h, score)] in that image's pixels, best first."""
    if name not in ("face", "plate"):
        raise ValueError(f"Unknown detector '{name}' (use 'face' or 'plate').")
    sess = _session(name, models_dir)
    inp = sess.get_inputs()[0]
    H, W = inp.shape[2], inp.shape[3]  # both models have a fixed input: face 640×640, plate 320×240
    blob, s = letterbox(image_bgr_uint8, W, H)
    outs = dict(zip([o.name for o in sess.get_outputs()], sess.run(None, {inp.name: blob})))
    boxes, scores = (decode_yunet if name == "face" else decode_lpd)(outs, W, H)
    return [(float(x / s), float(y / s), float(w / s), float(h / s), round(float(scores[i]), 3))
            for i in nms(boxes, scores, 0.3, THRESHOLD[name]) for x, y, w, h in [boxes[i]]]


# ---------------------------------------------------------------- frames

def _frames(src, start, end, step, w, h, pix_fmt="bgr24", fill=False, keyframes=False, chunk=64):
    """Yield (t, h×w×3 uint8 frame) every `step` s over [start, end), via rawvideo pipes of `chunk` frames each
    so an hour of footage never sits in memory. fill=True scales to cover and centre-crops (CLIP preprocessing).
    keyframes=True decodes keyframes only (much faster; each sample shows the nearest earlier keyframe)."""
    np = _np()
    size = (f"scale={w}:{h}:force_original_aspect_ratio=increase:flags=bicubic,crop={w}:{h}" if fill
            else f"scale={w}:{h}")
    vf = f"fps=fps=1/{step}:start_time=0,{size}"  # start_time=0 pads with the first frame, so frame k is at a + k·step
    a = start
    while a < end - 1e-6:
        b = min(end, a + chunk * step)
        # reads one step past b: the fps filter drops the final frame at EOF; frames at or after b are skipped below
        raw = ve._ffmpeg(["-hwaccel", "auto", *(["-skip_frame", "nokey"] if keyframes else []),
                          "-ss", f"{a:.3f}", "-t", f"{b - a + step:.3f}", "-i", str(src), "-map", "0:v:0", "-an",
                          "-vf", vf, "-f", "rawvideo", "-pix_fmt", pix_fmt, "-"], binary=True).stdout
        n = len(raw) // (w * h * 3)
        arr = np.frombuffer(raw[:n * w * h * 3], np.uint8).reshape(n, h, w, 3)
        for k in range(n):
            if a + k * step < b - 1e-6:
                yield round(a + k * step, 3), arr[k]
        a = b


def _work_size(width, height, longest=640):
    s = min(1.0, longest / max(width, height))
    return max(2, round(width * s / 2) * 2), max(2, round(height * s / 2) * 2)


# ---------------------------------------------------------------- privacy blur

def merge_tracks(dets, step, width, height, pad=0.2, grow=2.0):
    """[(t, kind, x, y, w, h)] → [{"t0","t1","x","y","w","h"}]: consecutive detections of one object (same kind,
    at most one missed sample, centre within `grow` box sizes) become one interval with a padded union box.
    When the union would outgrow `grow`× the box (a face crossing the frame), a new box starts that covers the move
    since the previous sample, so the blur follows in overlapping steps instead of covering half the frame."""
    tracks = []
    for t, kind, x, y, w, h in sorted(dets, key=lambda d: d[0]):
        b = (x, y, x + w, y + h)
        for tr in reversed(tracks):
            p = tr["last"]
            sw, sh = grow * max(p[2] - p[0], w), grow * max(p[3] - p[1], h)
            if (tr["kind"] != kind or not tr["t1"] < t <= tr["t1"] + 2.5 * step
                    or abs(p[0] + p[2] - 2 * x - w) / 2 > sw or abs(p[1] + p[3] - 2 * y - h) / 2 > sh):
                continue
            u = (min(tr["box"][0], x), min(tr["box"][1], y), max(tr["box"][2], b[2]), max(tr["box"][3], b[3]))
            if u[2] - u[0] <= sw and u[3] - u[1] <= sh:
                tr.update(t1=t, last=b, box=u)
            else:
                tracks.append({"kind": kind, "t0": tr["t1"], "t1": t, "last": b,
                               "box": (min(p[0], x), min(p[1], y), max(p[2], b[2]), max(p[3], b[3]))})
            break
        else:
            tracks.append({"kind": kind, "t0": t, "t1": t, "last": b, "box": b})
    out = []
    for tr in tracks:
        x1, y1, x2, y2 = tr["box"]
        px, py = pad * (x2 - x1), pad * (y2 - y1)
        x1, y1 = max(0, int(x1 - px)), max(0, int(y1 - py))
        x2, y2 = min(width, int(x2 + px + 1)), min(height, int(y2 + py + 1))
        out.append({"t0": round(max(0.0, tr["t0"] - step / 2), 3), "t1": round(tr["t1"] + step / 2, 3),
                    "x": x1, "y": y1, "w": x2 - x1, "h": y2 - y1})
    return sorted(out, key=lambda b: b["t0"])


def privacy_boxes(src, start, end, models_dir, kinds=("face", "plate"), fps=2.0):
    """Boxes to blur over [start, end] s of `src`, in source pixels: [{"t0","t1","x","y","w","h"}]."""
    info = ve.probe(src)
    # shortcut: assumes the decoded frame has the probed size; phone clips with rotation metadata would need swapping
    W, H = info["width"], info["height"]
    gw, gh = _work_size(W, H)
    # shortcut: one 640 px pass misses faces under ~10 px there (~60 px in 4K); tile the frame if distant faces matter
    sx, sy = W / gw, H / gh
    step = 1.0 / fps
    dets = []
    for t, frame in _frames(src, start, end + step, step, gw, gh):  # a sample at `end` covers the last half step
        for kind in kinds:
            dets += [(t, kind, x * sx, y * sy, w * sx, h * sy) for x, y, w, h, _ in detect(kind, frame, models_dir)]
    boxes = merge_tracks(dets, step, W, H)
    for b in boxes:
        b["t0"], b["t1"] = max(b["t0"], start), min(b["t1"], end)
    return boxes


def blur_filter(boxes, part_start, width, height) -> str:
    """FFmpeg filter fragment that blurs `boxes` (source pixels, source seconds) in a part that starts at
    `part_start` s of the source. It reads and writes one unlabelled stream at source resolution, so it chains
    with commas like any filter: "<blur>,scale=…". Empty string when nothing needs blurring."""
    parts = []
    for b in boxes:
        a, z = b["t0"] - part_start, b["t1"] - part_start
        x, y = max(0, int(b["x"]) // 2 * 2), max(0, int(b["y"]) // 2 * 2)
        w = min(int(b["w"]) + 1, width - x) // 2 * 2  # even sizes/offsets keep 4:2:0 chroma aligned
        h = min(int(b["h"]) + 1, height - y) // 2 * 2
        if z <= 0 or w < 8 or h < 8:
            continue
        i = len(parts)
        on = f"enable='between(t,{max(0.0, a):.3f},{z:.3f})'"
        r = max(1, min(w, h) // 8)  # chroma planes are half size: radius must stay ≤ min(w, h) / 4
        parts.append(f"split=2[pv{i}][pc{i}];[pc{i}]crop={w}:{h}:{x}:{y},boxblur={r}:3:{on}[pb{i}];"
                     f"[pv{i}][pb{i}]overlay={x}:{y}:format=auto:{on}")
    return ",".join(parts)


# ---------------------------------------------------------------- CLIP search & scenic score

def clip_pixels(rgb):
    """(n, 224, 224, 3) uint8 RGB (already resized + centre-cropped) → CLIP's normalised (n, 3, 224, 224) float32."""
    np = _np()
    x = (np.asarray(rgb, np.float32) / 255.0 - np.array(CLIP_MEAN, np.float32)) / np.array(CLIP_STD, np.float32)
    return np.ascontiguousarray(x.transpose(0, 3, 1, 2))


def _l2(x):
    np = _np()
    return x / (np.linalg.norm(x, axis=1, keepdims=True) + 1e-9)


def embed_frames(src, cache_file, models_dir, every=2.0) -> dict:
    """CLIP image embeddings of one frame every `every` s: {"t": [...], "emb": (n, 512) L2-normalised}.
    Cached in cache_file (.npz); keyframe-only decoding keeps a 4K hour to minutes."""
    np = _np()
    cache_file = Path(cache_file) if cache_file else None
    if cache_file and cache_file.exists():
        with np.load(cache_file) as d:
            return {"t": d["t"].tolist(), "emb": d["emb"]}
    sess = _session("clip_vision", models_dir)
    ts, embs, batch = [], [], []

    def flush():
        if batch:
            embs.append(sess.run(None, {"pixel_values": clip_pixels(np.stack(batch))})[0])
            batch.clear()
    for t, frame in _frames(src, 0.0, ve.probe(src)["duration"], every, 224, 224, "rgb24", fill=True, keyframes=True):
        ts.append(t)
        batch.append(frame)
        if len(batch) == 16:
            flush()
    flush()
    emb = _l2(np.concatenate(embs)).astype(np.float32) if embs else np.zeros((0, 512), np.float32)
    if cache_file:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_file.with_name(cache_file.name + ".tmp")
        with open(tmp, "wb") as f:
            np.savez(f, t=np.array(ts, float), emb=emb)
        tmp.replace(cache_file)  # never leave a half-written index that a later run would trust
    return {"t": ts, "emb": emb}


def embed_text(texts, models_dir):
    """CLIP text embeddings, (n, 512) L2-normalised."""
    np = _np()
    tok = _tokenizer(ensure_model("clip_tokenizer", models_dir))
    sess = _session("clip_text", models_dir)
    out = []
    for text in texts:  # one at a time: no padding, so the model's end-of-text pooling is unambiguous
        ids = tok.encode(text).ids
        ids = ids[:76] + ids[-1:] if len(ids) > 77 else ids  # CLIP's 77-token context, keep the end token
        out.append(sess.run(None, {"input_ids": np.array([ids], np.int64)})[0][0])
    return _l2(np.array(out, np.float32))


def search(query, index, models_dir, top=12):
    """Frames that best match a text query: [(t, cosine similarity)], best first."""
    np = _np()
    if not len(index["t"]):
        return []
    sims = index["emb"] @ embed_text([query], models_dir)[0]
    return [(index["t"][i], round(float(sims[i]), 4)) for i in np.argsort(-sims)[:top]]


def scenic_score(index, models_dir):
    """[(t, 0..1)]: how much each frame looks like a view rather than a face, car park or blur.
    Zero-shot CLIP: best scenic prompt vs best non-scenic prompt, at CLIP's logit scale of 100."""
    np = _np()
    if not len(index["t"]):
        return []
    sims = index["emb"] @ embed_text(SCENIC + NOT_SCENIC, models_dir).T
    diff = sims[:, :len(SCENIC)].max(1) - sims[:, len(SCENIC):].max(1)
    p = 1 / (1 + np.exp(-100 * diff))
    return [(t, round(float(v), 3)) for t, v in zip(index["t"], p)]


# ---------------------------------------------------------------- subject tracking for 9:16

def saliency_cx(gray):
    """Horizontal centre (0..1) of a spectral-residual saliency map (Hou & Zhang 2007) of a grayscale frame."""
    np = _np()
    g = gray.astype(np.float32)
    k = max(1, g.shape[1] // 64)  # work at ~64 px wide, the scale the method is designed for
    g = g[:g.shape[0] // k * k, :g.shape[1] // k * k].reshape(g.shape[0] // k, k, -1, k).mean((1, 3))
    if g.std() < 1:  # flat frame (black, fog): no subject, keep the crop centred
        return 0.5
    f = np.fft.fft2(g)
    amp = np.log(np.abs(f) + 1e-6)
    box = lambda a: sum(np.roll(np.roll(a, dy, 0), dx, 1) for dy in (-1, 0, 1) for dx in (-1, 0, 1)) / 9  # noqa: E731
    sal = box(box(np.abs(np.fft.ifft2(np.exp(amp - box(amp) + 1j * np.angle(f)))) ** 2))
    col = sal.sum(0)
    return float((col * (np.arange(len(col)) + 0.5)).sum() / (col.sum() * len(col) + 1e-9))


def smooth_track(points, win=5):
    """[(t, cx)] → median filter (drops one-frame jumps) then moving average (no jitter), edges held."""
    np = _np()
    if not points:
        return []
    t, x = zip(*points)
    half = win // 2
    p = np.pad(np.array(x, float), half, mode="edge")
    med = np.array([np.median(p[i:i + win]) for i in range(len(x))])
    avg = np.convolve(np.pad(med, half, mode="edge"), np.ones(win) / win, mode="valid")
    return [(ti, round(float(v), 3)) for ti, v in zip(t, avg)]


def subject_track(src, start, end, models_dir):
    """Horizontal subject centre over [start, end]: [(t, cx 0..1)], smoothed, for steering a 9:16 crop.
    The largest confident face wins; otherwise the centre of visual saliency."""
    info = ve.probe(src)
    gw, gh = _work_size(info["width"], info["height"])
    raw = []
    for t, frame in _frames(src, start, end, 0.5, gw, gh):
        faces = detect("face", frame, models_dir)
        if faces:
            x, _, w, h, s = max(faces, key=lambda f: f[2] * f[3] * f[4])
            raw.append((t, min(1.0, max(0.0, (x + w / 2) / gw))))
        else:
            # shortcut: saliency on a riding shot often locks onto the road edge or horizon; add a person/bike
            # detector if Shorts crops wander
            raw.append((t, saliency_cx(frame.mean(2))))
    return smooth_track(raw)

"""FFmpeg layer: probing, audio isolation, cutting/concatenation, audio finishing and frame grabs.

Only the `ffmpeg` binary is needed (no ffprobe): probing parses `ffmpeg -i` output.
If ffmpeg is not on PATH, the binary bundled by `imageio-ffmpeg` is used.
Every ffmpeg process is registered so a running job can be cancelled instantly.
"""
import functools
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

VIDEO_EXTS = {".mp4", ".mov", ".mkv"}
HW_ENCODERS = ["h264_videotoolbox", "h264_nvenc", "h264_qsv", "h264_amf"]  # first that works wins

_procs = set()
_cancel = threading.Event()


class Cancelled(Exception):
    pass


def cancel():
    _cancel.set()
    for p in list(_procs):
        p.kill()


def reset_cancel():
    _cancel.clear()


def check_cancel():
    if _cancel.is_set():
        raise Cancelled("Cancelled.")


def ffmpeg_exe() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:
        raise RuntimeError(
            "FFmpeg not found. Run `pip install imageio-ffmpeg` (bundled binary) or install it system-wide "
            "(macOS: brew install ffmpeg, Windows: winget install ffmpeg, Linux: apt install ffmpeg)."
        ) from e


def _ffmpeg(args, check=True, binary=False) -> subprocess.CompletedProcess:
    check_cancel()
    p = subprocess.Popen([ffmpeg_exe(), "-hide_banner", "-nostdin", *args],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    _procs.add(p)
    try:
        out, err = p.communicate()
    finally:
        _procs.discard(p)
    check_cancel()  # a cancel kills the process; report that instead of a confusing ffmpeg error
    err = err.decode("utf-8", errors="replace")
    proc = subprocess.CompletedProcess(p.args, p.returncode, out if binary else out.decode("utf-8", "replace"), err)
    if check and proc.returncode != 0:
        raise RuntimeError("ffmpeg failed: " + err.strip()[-800:])
    return proc


def probe(path) -> dict:
    err = _ffmpeg(["-i", str(path)], check=False).stderr  # exits 1 ("no output file") by design
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", err)
    if not m:
        raise ValueError(f"Not a readable video file: {Path(path).name}")
    h, mi, s = m.groups()
    video = re.search(r"Stream #.*Video:.*", err)
    size = re.search(r"\b(\d{2,5})x(\d{2,5})\b", video.group(0)) if video else None
    fps = re.search(r"([\d.]+) fps", video.group(0)) if video else None
    return {
        "duration": int(h) * 3600 + int(mi) * 60 + float(s),
        "width": int(size.group(1)) if size else 1920,
        "height": int(size.group(2)) if size else 1080,
        "fps": float(fps.group(1)) if fps else 30.0,
        "has_audio": bool(re.search(r"Stream #.*Audio:", err)),
        # cameras embed a start timecode (DJI: time of day, drop-frame); NLEs place clips by it
        "timecode": (re.search(r"timecode\s*:\s*(\d\d:\d\d:\d\d[:;.]\d\d)", err) or [None, None])[1],
    }


def extract_audio(src, dst) -> Path:
    """16 kHz mono WAV for Whisper; the high-pass filter knocks out most wind/engine rumble."""
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".part.wav")
    _ffmpeg(["-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", "-af", "highpass=f=120",
             "-c:a", "pcm_s16le", str(tmp)])
    tmp.replace(dst)  # never leave a half-written WAV that a later run would trust
    return dst


def merge_ranges(segments, duration, pad=0.15, max_gap=0.6):
    """Turn kept segments into padded, merged (start, end) cut ranges."""
    out = []
    for s in sorted(segments, key=lambda s: s["start"]):
        a, b = max(0.0, s["start"] - pad), min(duration, s["end"] + pad)
        if out and a - out[-1][1] <= max_gap:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(round(a, 3), round(b, 3)) for a, b in out if b - a > 0.05]


@functools.cache
def hardware_encoder():
    """The first hardware H.264 encoder that actually encodes on this machine, or None."""
    for enc in HW_ENCODERS:
        test = ["-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.1", "-c:v", enc, "-b:v", "1M", "-f", "null", "-"]
        if _ffmpeg(test, check=False).returncode == 0:
            return enc
    return None


def video_codec_args(encoder, width, height, fps):
    """encoder: 'auto' (hardware if available), 'hardware' (required) or 'software' (libx264 CRF 18)."""
    enc = hardware_encoder() if encoder in ("auto", "hardware") else None
    if encoder == "hardware" and not enc:
        raise RuntimeError("No working hardware H.264 encoder was found. Choose 'Auto' or 'Software' encoding.")
    if enc:
        rate = int(width * height * fps * 0.12)  # ≈30 Mbps at 4K30, ≈7.5 Mbps at 1080p30
        return ["-c:v", enc, "-b:v", str(rate), "-maxrate", str(int(rate * 1.5)), "-bufsize", str(rate * 2),
                "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p"]


AUDIO_ARGS = ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]


def render(ranges, out_path, accurate=True, fmt=None, on_progress=lambda f: None, encoder="auto",
           vf=None, cache_dir=None) -> Path:
    """Cut every (src, start, end[, speed]) range then concat losslessly.

    speed > 1 renders a silent fast-forward (ride-through connector); it needs accurate mode and is skipped otherwise.
    Every part gets 30 ms audio fades so cuts over a constant engine/wind drone don't click.
    accurate=True re-encodes each part (frame-accurate, normalises resolution/fps so mixed cameras concat cleanly).
    accurate=False stream-copies video (fast, but cuts snap to keyframes; all clips must share codec/resolution).
    fmt: {"width","height","fps"} of the output. vf: a custom video filter (forces re-encoding), e.g. a 9:16 crop.
    cache_dir: rendered parts are kept there and reused, so a cancelled or slightly changed export resumes.
    """
    if not ranges:
        raise ValueError("Nothing to render: no segments are marked as kept.")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if accurate or vf:
        w, h, fps = fmt["width"], fmt["height"], fmt["fps"]
        vf = vf or (f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,"
                    f"setsar=1,fps={fps}")
        codec = video_codec_args(encoder, w, h, fps)
        video = ["-vf", vf, *codec]
        decode = ["-hwaccel", "auto"]  # GPU decoding: ~1.5x faster on 4K HEVC; FFmpeg falls back to CPU by itself
    else:
        video = ["-c:v", "copy", "-avoid_negative_ts", "make_zero"]
        decode = []
        ranges = [r for r in ranges if len(r) < 4 or r[3] <= 1]

    with tempfile.TemporaryDirectory(dir=out_path.parent) as tmp:
        parts_dir = Path(cache_dir) if cache_dir else Path(tmp)
        parts_dir.mkdir(parents=True, exist_ok=True)
        parts = []
        for i, (src, start, end, *rest) in enumerate(ranges):
            speed = rest[0] if rest else 1
            st = Path(src).stat()
            key = hashlib.sha1(json.dumps([str(src), st.st_mtime, st.st_size, start, end, speed, video]).encode()).hexdigest()
            part = parts_dir / f"{key[:20]}.mp4"
            if not part.exists():
                partial = Path(tmp) / f"partial_{i}.mp4"
                dur = end - start
                if speed > 1:  # silent fast-forward
                    out_dur = dur / speed
                    args = [*decode, "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(src),
                            "-f", "lavfi", "-t", f"{out_dur:.3f}", "-i", "anullsrc=r=48000:cl=stereo",
                            "-map", "0:v:0", "-map", "1:a:0", "-vf", f"setpts=PTS/{speed},{vf}", *codec, "-shortest"]
                else:
                    fade = f"afade=t=in:d=0.03,afade=t=out:st={max(0.0, dur - 0.03):.3f}:d=0.03"
                    args = [*decode, "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{dur:.3f}",
                            "-map", "0:v:0", "-map", "0:a:0?", *video, "-af", fade]
                _ffmpeg(["-y", *args, *AUDIO_ARGS, str(partial)])
                shutil.move(partial, part)
            parts.append(part)
            on_progress((i + 1) / (len(ranges) + 1))
        listing = Path(tmp) / "concat.txt"
        listing.write_text("".join("file '%s'\n" % p.resolve().as_posix().replace("'", "'\\''") for p in parts),
                           encoding="utf-8")
        # the joined file is a new edit: don't inherit the first clip's camera timecode
        _ffmpeg(["-y", "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", "-map_metadata", "-1",
                 "-write_tmcd", "0", "-movflags", "+faststart", str(out_path)])
    on_progress(1.0)
    return out_path


def finish_audio(src, dst, loudness=True, denoise=False, music=None, music_volume=0.15) -> Path:
    """Final audio pass on a rendered video; the video stream is copied untouched.

    denoise: high-pass + FFT noise reduction (wind, road roar). music: a file looped under the voice and
    ducked whenever someone speaks. loudness: two-pass EBU R128 normalisation to YouTube's −14 LUFS.
    """
    src, dst = Path(src), Path(dst)
    if not (loudness or denoise or music):
        if src != dst:
            shutil.move(src, dst)
        return dst
    inputs = ["-i", str(src)]
    chain = "[0:a]" + ("highpass=f=80,afftdn=nr=12:nf=-35" if denoise else "anull") + "[v]"
    if music:
        inputs += ["-stream_loop", "-1", "-i", str(music)]
        chain += (f";[1:a]aformat=sample_rates=48000:channel_layouts=stereo,volume={float(music_volume):.3f}[m]"
                  ";[v]asplit=2[v1][sc];[m][sc]sidechaincompress=threshold=0.02:ratio=10:attack=15:release=400[d]"
                  ";[v1][d]amix=inputs=2:duration=first:normalize=0[v]")
    if loudness:
        target = "I=-14:TP=-1.5:LRA=11"
        probe_run = _ffmpeg(["-y", *inputs, "-filter_complex", chain + f";[v]loudnorm={target}:print_format=json[o]",
                             "-map", "[o]", "-f", "null", "-"])
        m = json.loads(probe_run.stderr[probe_run.stderr.rfind("{"):probe_run.stderr.rfind("}") + 1])
        chain += (f";[v]loudnorm={target}:measured_I={m['input_i']}:measured_TP={m['input_tp']}"
                  f":measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}"
                  f":offset={m['target_offset']}:linear=true[v]")
    tmp = dst.with_name(dst.stem + ".audio.tmp" + dst.suffix)
    _ffmpeg(["-y", *inputs, "-filter_complex", chain, "-map", "0:v:0", "-map", "[v]", "-c:v", "copy",
             *AUDIO_ARGS, "-movflags", "+faststart", str(tmp)])
    tmp.replace(dst)
    if src != dst:
        src.unlink(missing_ok=True)
    return dst


def frame_gray(src, t, w=320, h=180):
    """One frame at t seconds as a h×w uint8 grayscale numpy array (no image library needed)."""
    import numpy as np
    raw = _ffmpeg(["-ss", f"{t:.3f}", "-i", str(src), "-frames:v", "1", "-vf", f"scale={w}:{h}",
                   "-f", "rawvideo", "-pix_fmt", "gray", "-"], binary=True).stdout
    return np.frombuffer(raw[:w * h], dtype=np.uint8).reshape(h, w) if len(raw) >= w * h else None


def save_frame(src, t, dst) -> Path:
    _ffmpeg(["-y", "-ss", f"{t:.3f}", "-i", str(src), "-frames:v", "1", "-q:v", "2", str(dst)])
    return Path(dst)

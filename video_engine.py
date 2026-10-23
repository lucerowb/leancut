"""FFmpeg layer: probing, audio isolation, cutting/concatenation and EDL export.

Only the `ffmpeg` binary is needed (no ffprobe): probing parses `ffmpeg -i` output.
If ffmpeg is not on PATH, the binary bundled by `imageio-ffmpeg` is used.
"""
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

VIDEO_EXTS = {".mp4", ".mov", ".mkv"}


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


def _ffmpeg(args, check=True) -> subprocess.CompletedProcess:
    proc = subprocess.run([ffmpeg_exe(), "-hide_banner", "-nostdin", *args],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and proc.returncode != 0:
        raise RuntimeError("ffmpeg failed: " + proc.stderr.strip()[-800:])
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
    }


def extract_audio(src, dst) -> Path:
    """16 kHz mono WAV for Whisper; the high-pass filter knocks out most wind/engine rumble."""
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    _ffmpeg(["-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", "-af", "highpass=f=120",
             "-c:a", "pcm_s16le", str(dst)])
    return dst


def merge_ranges(segments, duration, pad=0.15, max_gap=0.6):
    """Turn kept transcript segments into padded, merged (start, end) cut ranges."""
    out = []
    for s in sorted(segments, key=lambda s: s["start"]):
        a, b = max(0.0, s["start"] - pad), min(duration, s["end"] + pad)
        if out and a - out[-1][1] <= max_gap:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(round(a, 3), round(b, 3)) for a, b in out if b - a > 0.05]


def render(ranges, out_path, accurate=True, fmt=None, on_progress=lambda f: None) -> Path:
    """Cut every (src, start, end) range then concat losslessly.

    accurate=True re-encodes each part (frame-accurate, normalises resolution/fps so mixed cameras concat cleanly).
    accurate=False stream-copies video (fast, but cuts snap to keyframes; all clips must share codec/resolution).
    fmt: {"width","height","fps"} of the output when accurate.
    """
    if not ranges:
        raise ValueError("Nothing to render: no segments are marked as kept.")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    audio = ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]
    if accurate:
        w, h, fps = fmt["width"], fmt["height"], fmt["fps"]
        vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,"
              f"setsar=1,fps={fps}")
        video = ["-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p"]
    else:
        video = ["-c:v", "copy", "-avoid_negative_ts", "make_zero"]

    with tempfile.TemporaryDirectory(dir=out_path.parent) as tmp:
        parts = []
        for i, (src, start, end) in enumerate(ranges):
            part = Path(tmp) / f"part_{i:05d}.mp4"
            _ffmpeg(["-y", "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{end - start:.3f}",
                     "-map", "0:v:0", "-map", "0:a:0?", *video, *audio, str(part)])
            parts.append(part)
            on_progress((i + 1) / (len(ranges) + 1))
        listing = Path(tmp) / "concat.txt"
        listing.write_text("".join("file '%s'\n" % p.as_posix().replace("'", "'\\''") for p in parts),
                           encoding="utf-8")
        _ffmpeg(["-y", "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy",
                 "-movflags", "+faststart", str(out_path)])
    on_progress(1.0)
    return out_path


def _tc(frames: int, fps: int) -> str:
    s, f = divmod(frames, fps)
    return f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}:{f:02d}"


def write_edl(ranges, out_path, fps=30.0, title="final_youtube_cut") -> Path:
    """CMX3600 EDL (imports in DaVinci Resolve and Premiere Pro; clips are matched by the FROM CLIP NAME)."""
    fps = max(1, round(fps))  # shortcut: non-drop-frame only; 29.97 is written as 30 NDF
    lines = [f"TITLE: {title}", "FCM: NON-DROP FRAME", ""]
    rec = 3600 * fps  # record timeline starts at 01:00:00:00
    for n, (src, start, end) in enumerate(ranges, 1):
        a, b = round(start * fps), round(end * fps)
        lines += [f"{n:03d}  AX       AA/V  C        {_tc(a, fps)} {_tc(b, fps)} {_tc(rec, fps)} {_tc(rec + b - a, fps)}",
                  f"* FROM CLIP NAME: {Path(src).name}", ""]
        rec += b - a
    Path(out_path).write_text("\n".join(lines), encoding="utf-8")
    return Path(out_path)

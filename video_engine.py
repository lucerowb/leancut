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


@functools.cache
def ffmpeg_exe() -> str:
    """LEANCUT_FFMPEG if set, else the bundled imageio-ffmpeg build (a known feature set: drawtext, vid.stab, x264…),
    else whatever ffmpeg is on PATH. System builds vary a lot; some lack the filters titles need."""
    import os
    if os.environ.get("LEANCUT_FFMPEG"):
        return os.environ["LEANCUT_FFMPEG"]
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    raise RuntimeError(
        "FFmpeg not found. Run `pip install imageio-ffmpeg` (bundled binary) or install it system-wide "
        "(macOS: brew install ffmpeg, Windows: winget install ffmpeg, Linux: apt install ffmpeg).")


def _ffmpeg(args, check=True, binary=False, cwd=None) -> subprocess.CompletedProcess:
    check_cancel()
    p = subprocess.Popen([ffmpeg_exe(), "-hide_banner", "-nostdin", *args],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd)
    _procs.add(p)
    try:
        out, err = p.communicate()
    finally:
        _procs.discard(p)
    check_cancel()  # a cancel kills the process; report that instead of a confusing ffmpeg error
    err = err.decode("utf-8", errors="replace")
    proc = subprocess.CompletedProcess(p.args, p.returncode, out if binary else out.decode("utf-8", "replace"), err)
    if check and proc.returncode != 0:
        first = [l for l in err.splitlines() if re.search(r"error|invalid|failed|no such|not found|unable", l, re.I)][:4]
        raise RuntimeError("ffmpeg failed: " + " | ".join(first)[:600] + " … " + err.strip()[-300:])
    return proc


@functools.cache
def has_filter(name) -> bool:
    out = _ffmpeg(["-filters"], check=False).stdout
    return bool(re.search(rf"\s{re.escape(name)}\s", out))


def inp(src):
    """Input arguments for a source; joined camera chapters are .ffconcat lists read by FFmpeg's concat demuxer."""
    return ["-f", "concat", "-safe", "0", "-i", str(src)] if str(src).endswith(".ffconcat") else ["-i", str(src)]


def probe(path) -> dict:
    err = _ffmpeg([*inp(path)], check=False).stderr  # exits 1 ("no output file") by design
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
        "created": (re.search(r"creation_time\s*:\s*(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)", err) or [None, None])[1],
    }


def extract_audio(src, dst) -> Path:
    """16 kHz mono WAV for Whisper; the high-pass filter knocks out most wind/engine rumble."""
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".part.wav")
    _ffmpeg(["-y", *inp(src), "-vn", "-ac", "1", "-ar", "16000", "-af", "highpass=f=120",
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
                    args = [*decode, "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", *inp(src),
                            "-f", "lavfi", "-t", f"{out_dur:.3f}", "-i", "anullsrc=r=48000:cl=stereo",
                            "-map", "0:v:0", "-map", "1:a:0", "-vf", f"setpts=PTS/{speed},{vf}", *codec, "-shortest"]
                else:
                    fade = f"afade=t=in:d=0.03,afade=t=out:st={max(0.0, dur - 0.03):.3f}:d=0.03"
                    args = [*decode, "-ss", f"{start:.3f}", *inp(src), "-t", f"{dur:.3f}",
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


def _part_video(sp, dur, still_at=None):
    """Inputs and video filter args for one part spec. Overlays (cutaway B-roll, rendered graphics with alpha) are
    composited between the look (`vf`) and the titles (`top`), so text stays on top. still_at: one frame at that
    part-local time instead of the whole part (the agents' `look`)."""
    src_in = ["-hwaccel", "auto", "-ss", f"{sp['start']:.3f}", "-t", f"{dur:.3f}", *inp(sp["src"])]
    pick = f",select='gte(t\\,{still_at:.3f})'" if still_at is not None else ""
    if not sp.get("overlays"):
        return src_in, ["-vf", sp["vf"] + pick, "-map", "0:v:0"]
    graph, last = [f"[0:v]{sp['vf']}[m0]"], "m0"
    for k, o in enumerate(sp["overlays"], 1):
        od = o["end"] - o["start"]
        src_in += ["-ss", f"{o['start']:.3f}", "-t", f"{od:.3f}", *inp(o["src"])]
        fade = (f",fade=t=in:d=0.2,fade=t=out:st={max(0.0, od - 0.2):.3f}:d=0.2:alpha=1"
                if o.get("fade", True) else "")
        graph.append(f"[{k}:v]{o['vf']},format=yuva420p{fade},setpts=PTS-STARTPTS+{o['at']:.3f}/TB[o{k}]")
        graph.append(f"[{last}][o{k}]overlay=eof_action=pass:enable='between(t,{o['at']:.3f},"
                     f"{o['at'] + od:.3f})'[m{k}]")
        last = f"m{k}"
    graph.append(f"[{last}]{sp.get('top') or 'null'}{pick}[v]")
    return src_in, ["-filter_complex", ";".join(graph), "-map", "[v]"]


def still_of(sp, t, width, workdir=None):
    """JPEG of a part spec at part-local time t, rendered through the same filters as the export."""
    src_in, video = _part_video(sp, min(sp["end"] - sp["start"], t + 0.5), t)
    return _ffmpeg(["-y", *src_in, *video, "-frames:v", "1", "-s", f"{width}x{int(width * 9 / 16) // 2 * 2}",
                    "-f", "mjpeg", "-"], binary=True, cwd=workdir).stdout


def render_parts(specs, out_path, fmt, encoder="auto", cache_dir=None, workdir=None, on_progress=lambda f: None):
    """Two-stage render for edits with looks and transitions.

    Stage 1 renders every part to its own cached file with its filters baked in. A spec is
      {"src", "start", "end", "vf", "af", "silent": bool, "preroll": seconds (J-cut audio lead-in, 0 = none),
       "transition": {"xfade": name | None, "jcut": bool, "dur": s} (into this part), "card": bool}
    `vf` must end at the output size/fps; text, LUT and stabilisation files in it are relative to `workdir`.
    A "card" spec (intro/outro) renders `vf` over `src` footage with silent audio.
    Stage 2 assembles: a lossless concat when every cut is a hard cut, otherwise one pass chaining xfade /
    acrossfade / J-cuts. Returns (path, [output start time of each part]).
    """
    if not specs:
        raise ValueError("Nothing to render: no segments are marked as kept.")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    w, h, fps = fmt["width"], fmt["height"], fmt["fps"]
    codec = video_codec_args(encoder, w, h, fps)
    cache = Path(cache_dir or out_path.parent / ".parts")
    cache.mkdir(parents=True, exist_ok=True)
    files, durs, prerolls = [], [], []
    for i, sp in enumerate(specs):
        st = Path(sp["src"]).stat() if Path(sp["src"]).exists() else None
        sig = [str(sp["src"]), st and st.st_mtime, st and st.st_size, sp["start"], sp["end"], sp.get("vf"),
               sp.get("af"), sp.get("silent"), sp.get("card"), codec]
        if sp.get("overlays"):
            sig += [sp.get("top"), sp["overlays"]]
        key = hashlib.sha1(json.dumps(sig, default=str).encode()).hexdigest()[:20]
        part = cache / f"{key}.mp4"
        dur = sp["end"] - sp["start"]
        if not part.exists():
            partial = cache / f".{key}.partial.mp4"
            src_in, video = _part_video(sp, dur)
            if sp.get("overlays"):  # cutaway B-roll / graphics: other pictures over this part while its sound runs on
                af = ",".join(x for x in (sp.get("af"), "aresample=48000") if x)
                args = [*src_in, *video, "-map", "0:a:0?", "-af", af]
            elif sp.get("silent") or sp.get("card"):
                args = [*src_in, "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", *video, "-map", "1:a:0", "-shortest"]
            else:
                af = ",".join(x for x in (sp.get("af"), "aresample=48000") if x)
                args = [*src_in, *video, "-map", "0:a:0?", "-af", af]
            _ffmpeg(["-y", *args, *codec, *AUDIO_ARGS, str(partial)], cwd=workdir)
            partial.replace(part)
        files.append(part)
        durs.append(probe(part)["duration"])
        pre = None
        if sp.get("preroll") and not sp.get("silent"):
            pre = cache / f"{key}.pre{sp['preroll']:.2f}.wav"
            if not pre.exists():
                lead = min(sp["preroll"], sp["start"])
                _ffmpeg(["-y", "-ss", f"{sp['start'] - lead:.3f}", "-t", f"{lead:.3f}", *inp(sp["src"]), "-vn",
                         "-af", f"afade=t=in:d={lead:.3f}", "-ar", "48000", "-ac", "2", str(pre)])
        prerolls.append(pre)
        on_progress((i + 1) / (len(specs) + 1))

    starts, t = [], 0.0
    plain = all(not (sp.get("transition") or {}).get("xfade") and not (sp.get("transition") or {}).get("jcut")
                for sp in specs[1:])
    for i, sp in enumerate(specs):
        tr = sp.get("transition") or {}
        d = min(tr.get("dur", 0), durs[i] / 2, durs[i - 1] / 2) if i and tr.get("xfade") else 0
        t -= d
        starts.append(round(t, 3))
        t += durs[i]
    if plain:
        listing = cache / f".{out_path.stem}.concat.txt"
        listing.write_text("".join("file '%s'\n" % p.resolve().as_posix().replace("'", "'\\''") for p in files),
                           encoding="utf-8")
        # the joined file is a new edit: don't inherit the first clip's camera timecode
        _ffmpeg(["-y", "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", "-map_metadata", "-1",
                 "-write_tmcd", "0", "-movflags", "+faststart", str(out_path)])
        on_progress(1.0)
        return out_path, starts

    inputs, graph, n_in = [], [], 0
    for f in files:
        inputs += ["-i", str(f)]
    pre_idx = {}
    for i, p in enumerate(prerolls):
        if p is not None:
            pre_idx[i] = len(files) + len(pre_idx)
            inputs += ["-i", str(p)]
    for i in range(len(files)):  # xfade/concat need identical timebases and clean timestamps on every input
        graph.append(f"[{i}:v]settb=AVTB,setpts=PTS-STARTPTS,fps={fps},format=yuv420p[n{i}]")
        graph.append(f"[{i}:a]asettb=AVTB,asetpts=PTS-STARTPTS,aresample=48000[m{i}]")
    v, a, cum = "[n0]", "[m0]", durs[0]
    for i in range(1, len(files)):
        tr = specs[i].get("transition") or {}
        vi, ai = f"[n{i}]", f"[m{i}]"
        if tr.get("xfade"):
            d = min(tr["dur"], durs[i] / 2, durs[i - 1] / 2)
            graph.append(f"{v}{vi}xfade=transition={tr['xfade']}:duration={d:.3f}:offset={cum - d:.3f},"
                         f"settb=AVTB,fps={fps}[v{i}]")
            graph.append(f"{a}{ai}acrossfade=d={d:.3f}[a{i}]")
            cum += durs[i] - d
        else:
            graph.append(f"{v}{vi}concat=n=2:v=1:a=0,settb=AVTB,fps={fps}[v{i}]")  # xfade needs a constant rate
            if tr.get("jcut") and i in pre_idx:  # the next line's sound starts under the previous picture
                lead = min(tr["dur"], durs[i - 1] / 2)
                graph.append(f"[{pre_idx[i]}:a]{ai}concat=n=2:v=0:a=1[p{i}]")
                graph.append(f"{a}[p{i}]acrossfade=d={lead:.3f}:c1=tri:c2=tri[a{i}]")
            else:
                graph.append(f"{a}{ai}concat=n=2:v=0:a=1[a{i}]")
            cum += durs[i]
        v, a = f"[v{i}]", f"[a{i}]"
    _ffmpeg(["-y", *inputs, "-filter_complex", ";".join(graph), "-map", v, "-map", a, *codec, *AUDIO_ARGS,
             "-map_metadata", "-1", "-movflags", "+faststart", str(out_path)])
    on_progress(1.0)
    return out_path, starts


def finish_audio(src, dst, loudness=True, denoise=False, music=None, music_volume=0.15, voiceovers=(),
                 bleeps=(), voice_wav=None, workdir=None) -> Path:
    """Final audio pass on a rendered video; the video stream is copied untouched.

    voice_wav: a replacement for the video's audio track (e.g. the speech-enhanced version of it).
    denoise: high-pass + FFT noise reduction (wind, road roar). bleeps: [(t0, t1)] output-time windows muted under a
    1 kHz tone. voiceovers: [(wav, at_seconds)] mixed in, with the programme ducked under them. music: a file looped
    under everything and ducked whenever someone speaks. loudness: two-pass EBU R128 normalisation to −14 LUFS.
    """
    src, dst = Path(src), Path(dst)
    if not (loudness or denoise or music or voiceovers or bleeps or voice_wav):
        if src != dst:
            shutil.move(src, dst)
        return dst
    inputs = ["-i", str(src)] + (["-i", str(voice_wav)] if voice_wav else [])
    pre = ["highpass=f=80", "afftdn=nr=12:nf=-35"] if denoise else []
    pre += [f"volume=0:enable='between(t,{a:.3f},{b:.3f})'" for a, b in bleeps]
    chain = f"[{1 if voice_wav else 0}:a]aformat=sample_rates=48000:channel_layouts=stereo," + (",".join(pre) or "anull") + "[v]"
    if bleeps:
        tone = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in bleeps)
        chain += (f";sine=f=1000:sample_rate=48000,volume=0.25,volume=0:enable='not({tone})',"
                  f"aformat=channel_layouts=stereo[bl];[v][bl]amix=inputs=2:duration=first:normalize=0[v]")
    if voiceovers:
        n0 = 2 if voice_wav else 1
        mix = []
        for k, (wav, at) in enumerate(voiceovers):
            inputs += ["-i", str(wav)]
            ms = int(at * 1000)
            chain += f";[{n0 + k}:a]aformat=sample_rates=48000:channel_layouts=stereo,adelay={ms}|{ms}[vo{k}]"
            mix.append(f"[vo{k}]")
        chain += (f";{''.join(mix)}amix=inputs={len(mix)}:duration=longest:normalize=0,asplit=2[vo][vosc]"
                  ";[v][vosc]sidechaincompress=threshold=0.02:ratio=8:attack=20:release=500[vd]"
                  ";[vd][vo]amix=inputs=2:duration=first:normalize=0[v]")
    if music:
        k = len(inputs) // 2
        inputs += ["-stream_loop", "-1", "-i", str(music)]
        chain += (f";[{k}:a]aformat=sample_rates=48000:channel_layouts=stereo,volume={float(music_volume):.3f}[m]"
                  ";[v]asplit=2[v1][sc];[m][sc]sidechaincompress=threshold=0.02:ratio=10:attack=15:release=400[d]"
                  ";[v1][d]amix=inputs=2:duration=first:normalize=0[v]")
    if loudness:
        target = "I=-14:TP=-1.5:LRA=11"
        probe_run = _ffmpeg(["-y", *inputs, "-filter_complex", chain + f";[v]loudnorm={target}:print_format=json[o]",
                             "-map", "[o]", "-f", "null", "-"], cwd=workdir)
        m = json.loads(probe_run.stderr[probe_run.stderr.rfind("{"):probe_run.stderr.rfind("}") + 1])
        chain += (f";[v]loudnorm={target}:measured_I={m['input_i']}:measured_TP={m['input_tp']}"
                  f":measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}"
                  f":offset={m['target_offset']}:linear=true[v]")
    tmp = dst.with_name(dst.stem + ".audio.tmp" + dst.suffix)
    _ffmpeg(["-y", *inputs, "-filter_complex", chain, "-map", "0:v:0", "-map", "[v]", "-c:v", "copy",
             *AUDIO_ARGS, "-movflags", "+faststart", str(tmp)], cwd=workdir)
    tmp.replace(dst)
    if src != dst:
        src.unlink(missing_ok=True)
    return dst


def stabilise_detect(src, start, end, trf, workdir):
    """vid.stab pass 1 on a part's source range; writes motion vectors to `trf` (relative to workdir)."""
    if not (Path(workdir) / trf).exists():
        _ffmpeg(["-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", *inp(src), "-an",
                 "-vf", f"vidstabdetect=shakiness=6:accuracy=12:result={trf}", "-f", "null", "-"], cwd=workdir)
    return trf


def frame_gray(src, t, w=320, h=180):
    """One frame at t seconds as a h×w uint8 grayscale numpy array (no image library needed)."""
    import numpy as np
    raw = _ffmpeg(["-ss", f"{t:.3f}", *inp(src), "-frames:v", "1", "-vf", f"scale={w}:{h}",
                   "-f", "rawvideo", "-pix_fmt", "gray", "-"], binary=True).stdout
    return np.frombuffer(raw[:w * h], dtype=np.uint8).reshape(h, w) if len(raw) >= w * h else None


def save_frame(src, t, dst) -> Path:
    _ffmpeg(["-y", "-ss", f"{t:.3f}", *inp(src), "-frames:v", "1", "-q:v", "2", str(dst)])
    return Path(dst)

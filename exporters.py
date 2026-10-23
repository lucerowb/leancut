"""Hand-off files for the finished cut: EDL, FCPXML, OpenTimelineIO, SRT subtitles and YouTube metadata.

All of them are built from one `timeline()`: the ordered list of kept ranges with their position in the output.
Source times respect each clip's embedded start timecode (DJI writes time-of-day drop-frame timecode), because
Resolve, Premiere and Final Cut place media by it.
"""
import json
import re
from pathlib import Path
from xml.sax.saxutils import quoteattr


# ---------------------------------------------------------------- time & timecode

def rate(fps):
    """29.97 -> (30000, 1001), 25 -> (25, 1): exact frame rates for rational times."""
    if abs(fps - round(fps)) > 0.01:
        return round(fps) * 1000, 1001
    return round(fps), 1


def frames(seconds, fps):
    num, den = rate(fps)
    return round(seconds * num / den)


def is_drop_frame(fps, tc=None):
    """The file's own timecode decides (';' = drop-frame); without one, 29.97/59.94 default to drop-frame."""
    if tc:
        return ";" in tc
    return rate(fps)[1] == 1001 and round(fps) in (30, 60)


def tc_to_frames(tc, fps, drop):
    hh, mm, ss, ff = (int(x) for x in re.split(r"[:;.]", tc))
    base = round(fps)
    total = (hh * 3600 + mm * 60 + ss) * base + ff
    if drop:
        d = 2 * base // 30  # 2 frames per minute at 29.97, 4 at 59.94
        minutes = hh * 60 + mm
        total -= d * (minutes - minutes // 10)
    return total


def frames_to_tc(n, fps, drop):
    base = round(fps)
    if drop:
        d = 2 * base // 30
        per10, per1 = base * 600 - d * 9, base * 60 - d
        tens, rem = divmod(n, per10)
        n += d * 9 * tens + (d * ((rem - d) // per1) if rem > d else 0)
    sep = ";" if drop else ":"
    return f"{n // (base * 3600) % 24:02d}:{n // (base * 60) % 60:02d}:{n // base % 60:02d}{sep}{n % base:02d}"


def clip_tc_frames(clip, fps, drop):
    """Start timecode of a clip as a frame count on the sequence's timecode scale (0 if the file has none)."""
    tc = clip.get("timecode")
    return tc_to_frames(tc, fps, drop) if tc else 0


# ---------------------------------------------------------------- timeline

def timeline(entries):
    """entries: [(clip, start, end, speed)] in output order -> same with record start/end seconds added."""
    out, rec = [], 0.0
    for clip, start, end, speed in entries:
        dur = (end - start) / speed
        out.append({"clip": clip, "start": start, "end": end, "speed": speed, "rec_start": rec, "rec_end": rec + dur})
        rec += dur
    return out


def _uri(path):
    return Path(path).resolve().as_uri()


# ---------------------------------------------------------------- EDL

def write_edl(tl, path, fps, title):
    """CMX3600 EDL. Fast-forward connectors are left out (EDL speed effects don't round-trip reliably)."""
    drop = is_drop_frame(fps, next((e["clip"].get("timecode") for e in tl), None))
    lines = [f"TITLE: {title}", f"FCM: {'DROP FRAME' if drop else 'NON-DROP FRAME'}", ""]
    rec = tc_to_frames("01:00:00:00", fps, drop)
    n = 0
    for e in tl:
        if e["speed"] != 1:
            continue
        n += 1
        tc0 = clip_tc_frames(e["clip"], fps, drop)
        a, b = tc0 + frames(e["start"], fps), tc0 + frames(e["end"], fps)
        lines += [f"{n:03d}  AX       AA/V  C        {frames_to_tc(a, fps, drop)} {frames_to_tc(b, fps, drop)} "
                  f"{frames_to_tc(rec, fps, drop)} {frames_to_tc(rec + b - a, fps, drop)}",
                  f"* FROM CLIP NAME: {Path(e['clip']['path']).name}", ""]
        rec += b - a
    Path(path).write_text("\n".join(lines), encoding="utf-8")
    return Path(path)


# ---------------------------------------------------------------- FCPXML

def write_fcpxml(tl, path, fmt, title, angles=(), chapters=()):
    """FCPXML 1.10 for Final Cut Pro and DaVinci Resolve.

    angles: [(angle_clip, primary_clip_id, offset)] from audio sync (t_primary = t_angle + offset); each synced
    angle is attached under the primary's clips as a connected clip on lane 1, ready to switch to in the NLE.
    chapters: [(record_seconds, title)] become chapter markers.
    """
    fps = fmt["fps"]
    num, den = rate(fps)
    drop = is_drop_frame(fps, next((e["clip"].get("timecode") for e in tl), None))
    t = lambda f: f"{f * den}/{num}s" if den != 1 else f"{f}/{num}s"
    clips = {}
    for e in tl:
        clips.setdefault(e["clip"]["id"], e["clip"])
    for angle, _, _ in angles:
        clips.setdefault(angle["id"], angle)
    ids = {cid: f"a{i}" for i, cid in enumerate(clips)}
    res = [f'<format id="r0" frameDuration="{t(1)}" width="{fmt["width"]}" height="{fmt["height"]}"/>']
    for cid, c in clips.items():
        res.append(f'<asset id="{ids[cid]}" name={quoteattr(Path(c["path"]).name)} '
                   f'start="{t(clip_tc_frames(c, fps, drop))}" duration="{t(frames(c["duration"], fps))}" '
                   f'hasVideo="1" hasAudio="1" format="r0" audioSources="1" audioChannels="2" audioRate="48000">'
                   f'<media-rep kind="original-media" src={quoteattr(_uri(c["path"]))}/></asset>')
    spine, rec = [], 0
    marks = sorted(chapters)
    for e in tl:
        if e["speed"] != 1:
            continue
        c = e["clip"]
        tc0 = clip_tc_frames(c, fps, drop)
        a, b = frames(e["start"], fps), frames(e["end"], fps)
        inner = []
        for angle, primary_id, offset in angles:
            if primary_id != c["id"]:
                continue
            sa = max(0, frames(e["start"] - offset, fps))
            sb = min(frames(angle["duration"], fps), frames(e["end"] - offset, fps))
            if sb - sa > 0:
                lead = sa - frames(e["start"] - offset, fps)
                inner.append(f'<asset-clip ref="{ids[angle["id"]]}" lane="1" offset="{t(tc0 + a + lead)}" '
                             f'name={quoteattr(Path(angle["path"]).name)} '
                             f'start="{t(clip_tc_frames(angle, fps, drop) + sa)}" duration="{t(sb - sa)}"/>')
        for sec, name in marks:
            f = frames(sec, fps)
            if rec <= f < rec + b - a:
                inner.append(f'<chapter-marker start="{t(tc0 + a + f - rec)}" duration="{t(1)}" value={quoteattr(name)}/>')
        spine.append(f'<asset-clip ref="{ids[c["id"]]}" offset="{t(rec)}" name={quoteattr(Path(c["path"]).name)} '
                     f'start="{t(tc0 + a)}" duration="{t(b - a)}" format="r0" tcFormat="{"DF" if drop else "NDF"}">'
                     + "".join(inner) + "</asset-clip>")
        rec += b - a
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n<fcpxml version="1.10">\n<resources>\n'
           + "\n".join(res) + f'\n</resources>\n<library><event name="LeanCut"><project name={quoteattr(title)}>'
           f'<sequence format="r0" duration="{t(rec)}" tcStart="0s" tcFormat="{"DF" if drop else "NDF"}" '
           f'audioLayout="stereo" audioRate="48k"><spine>\n' + "\n".join(spine)
           + "\n</spine></sequence></project></event></library>\n</fcpxml>\n")
    Path(path).write_text(xml, encoding="utf-8")
    return Path(path)


# ---------------------------------------------------------------- OpenTimelineIO

def _rt(value, r):
    return {"OTIO_SCHEMA": "RationalTime.1", "rate": r, "value": float(value)}


def _range(start, dur, r):
    return {"OTIO_SCHEMA": "TimeRange.1", "start_time": _rt(start, r), "duration": _rt(dur, r)}


def _otio_clip(c, start_f, dur_f, r, tc0):
    return {"OTIO_SCHEMA": "Clip.2", "name": Path(c["path"]).name, "source_range": _range(tc0 + start_f, dur_f, r),
            "effects": [], "markers": [], "enabled": True, "metadata": {},
            "media_references": {"DEFAULT_MEDIA": {
                "OTIO_SCHEMA": "ExternalReference.1", "name": Path(c["path"]).name, "target_url": _uri(c["path"]),
                "available_range": _range(tc0, frames(c["duration"], r), r), "metadata": {}}},
            "active_media_reference_key": "DEFAULT_MEDIA"}


def _gap(dur_f, r):
    return {"OTIO_SCHEMA": "Gap.1", "name": "", "source_range": _range(0, dur_f, r), "effects": [], "markers": [],
            "enabled": True, "metadata": {}}


def write_otio(tl, path, fmt, title, angles=()):
    """OpenTimelineIO JSON (Resolve 18.5+, and anything with an OTIO adapter): V1/A1 = the cut, V2 = synced angles."""
    num, den = rate(fmt["fps"])
    r = num / den
    drop = is_drop_frame(fmt["fps"], next((e["clip"].get("timecode") for e in tl), None))
    v1, a1, v2, rec, v2_at = [], [], [], 0, 0
    for e in tl:
        if e["speed"] != 1:
            continue
        c = e["clip"]
        a, b = frames(e["start"], r), frames(e["end"], r)
        tc0 = clip_tc_frames(c, fmt["fps"], drop)
        v1.append(_otio_clip(c, a, b - a, r, tc0))
        a1.append(_otio_clip(c, a, b - a, r, tc0))
        for angle, primary_id, offset in angles:
            if primary_id != c["id"]:
                continue
            sa = max(0, frames(e["start"] - offset, r))
            sb = min(frames(angle["duration"], r), frames(e["end"] - offset, r))
            if sb - sa > 0:
                at = rec + sa - frames(e["start"] - offset, r)
                if at > v2_at:
                    v2.append(_gap(at - v2_at, r))
                v2.append(_otio_clip(angle, sa, sb - sa, r, clip_tc_frames(angle, fmt["fps"], drop)))
                v2_at = at + sb - sa
                break
        rec += b - a
    track = lambda name, kind, kids: {"OTIO_SCHEMA": "Track.1", "name": name, "kind": kind, "children": kids,
                                      "source_range": None, "effects": [], "markers": [], "enabled": True,
                                      "metadata": {}}
    tracks = [track("V1", "Video", v1)] + ([track("V2", "Video", v2)] if v2 else []) + [track("A1", "Audio", a1)]
    doc = {"OTIO_SCHEMA": "Timeline.1", "name": title, "global_start_time": None, "metadata": {},
           "tracks": {"OTIO_SCHEMA": "Stack.1", "name": "tracks", "children": tracks, "source_range": None,
                      "effects": [], "markers": [], "enabled": True, "metadata": {}}}
    Path(path).write_text(json.dumps(doc, indent=1), encoding="utf-8")
    return Path(path)


# ---------------------------------------------------------------- subtitles & YouTube text

def _srt_time(s):
    ms = round(s * 1000)
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def caption_cues(tl, max_chars=84, mask=None):
    """[(start, end, text)] on the output timeline from the kept speech, split into readable two-line cues.
    Entries may be slowed down or sped up (speed); silent fast-forwards carry no speech. mask: text -> text."""
    cues = []
    for e in tl:
        if e.get("silent") or e["speed"] > 4:
            continue
        sp = e["speed"]
        for s in e["clip"].get("segments") or []:
            mid = (s["start"] + s["end"]) / 2
            if not s.get("keep") or not e["start"] <= mid <= e["end"]:
                continue
            a = e["rec_start"] + (max(s["start"], e["start"]) - e["start"]) / sp
            b = e["rec_start"] + (min(s["end"], e["end"]) - e["start"]) / sp
            words, chunks, cur = (mask(s["text"]) if mask else s["text"]).split(), [], ""
            for w in words:
                if cur and len(cur) + 1 + len(w) > max_chars:
                    chunks.append(cur)
                    cur = w
                else:
                    cur = f"{cur} {w}".strip()
            chunks += [cur] if cur else []
            total = sum(len(c) for c in chunks) or 1
            t = a
            for c in chunks:
                d = (b - a) * len(c) / total
                cues.append((t, t + d, c))
                t += d
    return cues


def write_srt(tl, path, mask=None):
    lines = []
    for i, (a, b, text) in enumerate(caption_cues(tl, mask=mask), 1):
        lines += [str(i), f"{_srt_time(a)} --> {_srt_time(b)}", text, ""]
    Path(path).write_text("\n".join(lines), encoding="utf-8")
    return Path(path)


def chapter_lines(tl):
    """[(record_seconds, text)] of the kept speech, for the LLM to place chapters on."""
    out = []
    for e in tl:
        if e.get("silent") or e["speed"] > 4:
            continue
        for s in e["clip"].get("segments") or []:
            if s.get("keep") and e["start"] <= (s["start"] + s["end"]) / 2 <= e["end"]:
                out.append((e["rec_start"] + (max(s["start"], e["start"]) - e["start"]) / e["speed"], s["text"]))
    return out


def clean_chapters(chapters, lines, total, min_gap=10.0):
    """Snap LLM chapter times to real lines and apply YouTube's rules: first at 0:00, ≥10 s apart, at least 3."""
    times = [t for t, _ in lines] or [0.0]
    out = []
    for sec, name in sorted(chapters):
        snapped = min(times, key=lambda x: abs(x - sec))
        if out and snapped - out[-1][0] < min_gap or snapped > total - min_gap:
            continue
        out.append((snapped, name))
    if out:
        out[0] = (0.0, out[0][1])
    return out if len(out) >= 3 else []


def mmss(s):
    s = int(s)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def write_youtube_txt(meta, chapters, path):
    parts = [f"TITLE\n{meta.get('title', '')}", "DESCRIPTION\n" + meta.get("description", "").strip()]
    if chapters:
        parts.append("CHAPTERS (paste into the description)\n" + "\n".join(f"{mmss(t)} {n}" for t, n in chapters))
    parts.append("TAGS\n" + ", ".join(meta.get("tags", [])))
    Path(path).write_text("\n\n".join(parts) + "\n", encoding="utf-8")
    return Path(path)

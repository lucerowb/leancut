"""Turns an edit (parts + looks) into render specs for video_engine.render_parts, plus the audio events
(voice-overs, profanity bleeps) and the programme timeline that subtitles and chapters follow.

All helper files (title text, fonts, LUTs, stabilisation data) live in one working folder and are referenced by
relative paths, so filter strings never need Windows drive letters or quotes escaped.
"""
import hashlib
import re
import shutil
from pathlib import Path

import editing

PROFANITY = ("fuck", "fucking", "fucked", "shit", "bullshit", "bitch", "asshole", "bastard", "dick", "cunt",
             "motherfucker", "damn", "piss", "crap")


def prepare(workdir):
    workdir = Path(workdir)
    for d in ("texts", "fonts", "luts", "trf"):
        (workdir / d).mkdir(parents=True, exist_ok=True)
    font = editing.find_font()
    target = workdir / "fonts" / ("title" + (Path(font).suffix if font else ".ttf"))
    if font and not target.exists():
        shutil.copy(font, target)
    return workdir, (f"fonts/{target.name}" if font else None)


def text_file(workdir, text):
    rel = f"texts/{editing.text_key(text)}.txt"
    p = Path(workdir) / rel
    if not p.exists():
        p.write_text(str(text).replace("%", "%%"), encoding="utf-8")  # drawtext treats % as an expansion
    return rel


def lut_file(workdir, lut):
    if not lut or not Path(lut).is_file():
        return None
    rel = f"luts/{hashlib.sha1(str(lut).encode()).hexdigest()[:12]}{Path(lut).suffix.lower()}"
    if not (Path(workdir) / rel).exists():
        shutil.copy(lut, Path(workdir) / rel)
    return rel


def base_vf(fmt):
    w, h = fmt["width"], fmt["height"]
    return f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1"


def segment_words(seg):
    """[(word, t0, t1)]: Whisper word times when stored, else spread evenly across the line."""
    if seg.get("words"):
        return [(w, a, b) for w, a, b in seg["words"]]
    words = seg["text"].split()
    if not words:
        return []
    step = (seg["end"] - seg["start"]) / len(words)
    return [(w, seg["start"] + k * step, seg["start"] + (k + 1) * step) for k, w in enumerate(words)]


def is_profane(word, extra=()):
    w = re.sub(r"[^\w']", "", word.lower())
    return bool(w) and (w in PROFANITY or w in extra or any(w.startswith(p) for p in ("fuck", "shit")))


def mask(text, extra=()):
    """'well fuck that' -> 'well f*** that' (letters only, so punctuation stays)."""
    return re.sub(r"[\w']+", lambda m: m[0][0] + "*" * (len(m[0]) - 1) if is_profane(m[0], extra) else m[0], text)


def part_spec(part, project, settings, fmt, workdir, font, blur="", trf=None):
    """Render spec for one part, filters in order: privacy blur → stabilise → speed → scale → grade → effect →
    texts → burned-in captions → fps."""
    c, w, h, fps = part["clip"], fmt["width"], fmt["height"], fmt["fps"]
    speed, dur = part["speed"], editing.part_duration(part)
    vf = [x for x in (blur, trf and f"vidstabtransform=input={trf}:smoothing=20:zoom=1,unsharp=5:5:0.6") if x]
    eff_v, eff_a = editing.effect_filter(part["effect"], w, h, dur)
    if speed != 1:  # setpts first, so later filters (text fades) run on output time
        vf.append(eff_v if eff_v.startswith("setpts") else f"setpts=PTS/{speed}")
    vf.append(base_vf(fmt))
    g = editing.project_grade(project, c, part.get("grade"))
    g["lut"] = lut_file(workdir, g.get("lut"))
    vf += [x for x in (editing.grade_filter(g), eff_v if speed == 1 else "") if x]
    if font:
        for t in part["texts"]:
            t0 = max(0.0, (t["at"] - part["start"]) / speed)
            vf.append(editing.text_filter(text_file(workdir, t["text"]), font, t["style"], t0,
                                          min(dur, t0 + float(t.get("duration", 4))), w, h))
        if project.get("captions"):
            words = settings.get("profanity_words", "").split(",") if settings.get("bleep") else ()
            for iid in part["ids"]:
                hit = editing.find(project, iid)
                if hit and hit[1] == "segments":
                    seg = hit[3]
                    t0 = max(0.0, (seg["start"] - part["start"]) / speed)
                    t1 = min(dur, (seg["end"] - part["start"]) / speed)
                    txt = mask(seg["text"], [x.strip() for x in words if x.strip()]) if settings.get("bleep") else seg["text"]
                    vf.append(editing.text_filter(text_file(workdir, editing.wrap(txt, 42)), font, "caption", t0, t1, w, h))
    vf += [f"fps={fps}", "format=yuv420p"]
    af = [eff_a] if eff_a else []
    af += ["afade=t=in:d=0.03", f"afade=t=out:st={max(0.0, dur - 0.03):.3f}:d=0.03"]
    return {"src": c["path"], "start": part["start"], "end": part["end"], "vf": ",".join(x for x in vf if x),
            "af": ",".join(af)}


def card_spec(card, fmt, workdir, font, bg):
    """Intro/outro: a title over the best scenic footage (bg = (src, start)), darkened and softened."""
    w, h, fps = fmt["width"], fmt["height"], fmt["fps"]
    dur = float(card.get("duration", 4))
    vf = [base_vf(fmt), "eq=brightness=-0.08:saturation=0.9", "gblur=sigma=3"]
    style = card.get("style", "bold")
    if style == "cinematic":
        bar = int(h * 0.12)
        vf.append(f"drawbox=x=0:y=0:w=iw:h={bar}:color=black:t=fill,drawbox=x=0:y=ih-{bar}:w=iw:h={bar}:color=black:t=fill")
    if font:
        vf.append(editing.text_filter(text_file(workdir, card["title"]), font,
                                      "lower_third" if style == "minimal" else "title", 0.2, dur - 0.1, w, h))
        if card.get("subtitle"):
            sub = editing.text_filter(text_file(workdir, card["subtitle"]), font, "caption", 0.6, dur - 0.1, w, h)
            vf.append(sub.replace("y=h*0.92-text_h", "y=h*0.62") if style != "minimal" else sub)
    vf += [f"fps={fps}", "format=yuv420p", f"fade=t=in:d=0.4,fade=t=out:st={dur - 0.5:.3f}:d=0.5"]
    src, start = bg
    return {"src": src, "start": start, "end": start + dur, "vf": ",".join(vf), "card": True}


def with_transitions(specs, parts, project):
    """Attach the transition into each part (its own or the project's cut style)."""
    for i, (sp, p) in enumerate(zip(specs, parts)):
        tr = editing.transition_of(project, p, i)
        if not tr:
            continue
        name = editing.TRANSITIONS.get(tr["type"])
        if tr["type"] == "jcut":
            sp["transition"] = {"jcut": True, "dur": tr.get("dur", 0.4)}
            sp["preroll"] = tr.get("dur", 0.4)
        elif name:
            sp["transition"] = {"xfade": name, "dur": tr.get("dur", 0.6)}
    return specs


def bleep_windows(parts, starts, settings, project):
    """Output-time windows to bleep, from the kept lines in each part."""
    if not settings.get("bleep"):
        return []
    extra = [x.strip().lower() for x in settings.get("profanity_words", "").split(",") if x.strip()]
    out = []
    for p, s0 in zip(parts, starts):
        if p.get("card"):
            continue
        for iid in p["ids"]:
            hit = editing.find(project, iid)
            seg = hit[3] if hit and hit[1] == "segments" else None
            if not seg:
                continue
            for word, a, b in segment_words(seg):
                if is_profane(word, extra) and p["start"] <= a < p["end"]:
                    out.append((round(s0 + (a - p["start"]) / p["speed"], 3), round(s0 + (b - p["start"]) / p["speed"], 3)))
    return out


def programme(parts, starts):
    """Timeline entries (as exporters.timeline makes them) at their real output times, transitions included."""
    return [{"clip": p["clip"], "start": p["start"], "end": p["end"], "speed": p["speed"], "rec_start": s,
             "rec_end": s + editing.part_duration(p), "silent": p.get("silent", False)}
            for p, s in zip(parts, starts) if not p.get("card")]


def crop_x_expr(track, dur, start=None):
    """FFmpeg crop x expression following a subject: track = [(t, cx 0..1)] (t in the window's source time when
    `start` is given, else relative), piecewise-linear between points, clamped to the frame."""
    pts = [((t - start) if start is not None else t, cx) for t, cx in track] or [(0, 0.5)]
    pts = sorted(p for p in pts if 0 <= p[0] <= dur) or [(0, 0.5)]
    if pts[0][0] > 0:
        pts.insert(0, (0.0, pts[0][1]))
    pos = lambda cx: f"({cx:.4f}*iw-ow/2)"
    expr = pos(pts[-1][1])
    for (t0, c0), (t1, c1) in reversed(list(zip(pts, pts[1:]))):
        seg = f"({pos(c0)}+({pos(c1)}-{pos(c0)})*(t-{t0:.3f})/{max(t1 - t0, 1e-3):.3f})"
        expr = f"if(lt(t,{t1:.3f}),{seg},{expr})"
    return f"'max(0,min(iw-ow,{expr}))'"

"""The edit model: addressable items, rendered parts, looks (grade/effects/text), and the action vocabulary.

Every change to an edit — from the UI, a per-part suggestion or the chat assistant — is an *action* validated and
applied here, so all three behave the same and every change can be undone.

Items are the transcript lines and B-roll windows of a project, addressed as "c<clip>.s<n>" / "c<clip>.b<n>".
Each item can carry: keep, start/end (trim), effect, transition (into it), grade (override), texts, voiceovers.
"""
import copy
import hashlib
import math
import os
import re
import sys
from pathlib import Path

EFFECTS = {
    "none": "No effect", "zoom": "Punch-in zoom", "push_in": "Slow push-in", "slowmo": "Slow motion (½×)",
    "speedup": "Speed up (2×)", "bw": "Black & white", "vignette": "Vignette", "dreamy": "Soft glow",
    "stabilise": "Stabilise",
}
# LeanCut name -> FFmpeg xfade transition (None = handled specially)
TRANSITIONS = {
    "cut": None, "jcut": None, "crossfade": "fade", "dissolve": "dissolve", "dip_black": "fadeblack",
    "dip_white": "fadewhite", "wipe_left": "wipeleft", "wipe_right": "wiperight", "slide_left": "slideleft",
    "slide_right": "slideright", "circle": "circleopen", "radial": "radial", "zoom": "zoomin",
    "pixelize": "pixelize", "smooth": "smoothleft",
}
TEXT_STYLES = ("title", "lower_third", "caption", "label", "plain")
INTRO_STYLES = ("bold", "minimal", "cinematic")
NEUTRAL = {"exposure": 0.0, "contrast": 1.0, "saturation": 1.0, "gamma": 1.0, "temperature": 0.0, "vignette": 0.0,
           "teal_orange": 0.0, "curves": None, "lut": None}
GRADE_PRESETS = {
    "none": {},
    "punchy": {"contrast": 1.12, "saturation": 1.25},
    "warm": {"temperature": 0.5, "saturation": 1.05},
    "cool": {"temperature": -0.5, "saturation": 0.97},
    "cinematic": {"contrast": 1.1, "saturation": 0.92, "temperature": 0.2, "vignette": 0.5, "teal_orange": 0.5},
    "teal_orange": {"teal_orange": 1.0, "saturation": 1.1, "contrast": 1.05},
    "moody": {"exposure": -0.3, "contrast": 1.15, "saturation": 0.8, "vignette": 0.6},
    "vintage": {"curves": "vintage", "saturation": 0.85, "vignette": 0.4},
    "bright": {"exposure": 0.3, "contrast": 0.96, "saturation": 1.08},
    "bw": {"saturation": 0.0, "contrast": 1.12},
}


# ---------------------------------------------------------------- items

def item_id(ci, kind, i):
    return f"c{ci}.{'s' if kind == 'segments' else 'b'}{i}"


def items(project):
    """Yield (id, clip, kind, index, item) for every line and B-roll window of the main (non-angle) clips."""
    for ci, c in enumerate(project["clips"]):
        if c.get("angle_of"):
            continue
        for kind in ("segments", "broll"):  # source order (split pieces are appended, so ids aren't sorted)
            for i, it in sorted(enumerate(c.get(kind) or []), key=lambda x: x[1]["start"]):
                yield item_id(ci, kind, i), c, kind, i, it


def find(project, iid):
    m = re.fullmatch(r"c(\d+)\.([sb])(\d+)", str(iid).strip())
    if not m:
        return None
    ci, kind, i = int(m[1]), "segments" if m[2] == "s" else "broll", int(m[3])
    if ci >= len(project["clips"]):
        return None
    lst = project["clips"][ci].get(kind) or []
    return (project["clips"][ci], kind, i, lst[i]) if i < len(lst) else None


def look(project, it):
    """Everything that decides how an item renders; adjacent items merge into one part only if this matches."""
    return (it.get("effect", "none"), repr(sorted((it.get("grade") or {}).items())))


# ---------------------------------------------------------------- parts

def cutaway_target(project, it):
    """The kept line a B-roll window is laid over (a cutaway: picture changes, the talking continues), or None."""
    hit = find(project, it.get("over")) if it.get("over") else None
    return hit if hit and hit[1] == "segments" and hit[3].get("keep") and not hit[0].get("angle_of") else None


def build_parts(project, settings, pad=0.15, max_gap=0.6):
    """Kept items -> ordered render parts: [{clip, start, end, speed, effect, grade, transition, texts, voiceovers,
    overlays, ids}].

    Clips play in project order and each clip in source order, so the story stays chronological. Adjacent kept items
    of a clip merge into one part unless their look differs, the later one has its own transition, or they were
    separated by a split / word cut (`cut_in` / `cut_out`: a hard edge with no padding, so cut words stay cut).
    Cutaway B-roll (`over` = a kept line's id) isn't a part of its own: it rides on the part holding that line.
    Texts/voice-overs anchored to an item keep their source time, so they move with trims."""
    parts, cutaways = [], []
    for c in project["clips"]:
        if c.get("angle_of"):
            continue
        ci = project["clips"].index(c)
        kept = []
        for kind in ("segments", "broll"):
            for i, it in enumerate(c.get(kind) or []):
                if not it.get("keep"):
                    continue
                if kind == "broll" and cutaway_target(project, it):
                    cutaways.append((item_id(ci, kind, i), c, it))
                else:
                    kept.append((item_id(ci, kind, i), it))
        kept.sort(key=lambda x: x[1]["start"])
        cur = None
        for iid, it in kept:
            a = it["start"] if it.get("cut_in") else max(0.0, it["start"] - pad)
            b = it["end"] if it.get("cut_out") else min(c["duration"], it["end"] + pad)
            tr = it.get("transition")
            if (cur and a - cur["end"] <= max_gap and cur["look"] == look(project, it) and not tr
                    and not it.get("cut_in") and not cur["hard_end"]):
                cur["end"] = max(cur["end"], b)
                cur["ids"].append(iid)
            else:
                if cur and cur["end"] > a:  # padding must never replay frames the previous part already showed
                    cur["end"] = max(cur["start"] + 0.05, a)
                cur = {"clip": c, "start": a, "end": b, "speed": 1.0, "effect": it.get("effect", "none"),
                       "grade": it.get("grade") or {}, "transition": tr, "ids": [iid], "look": look(project, it),
                       "texts": [], "voiceovers": [], "overlays": [], "graphics": []}
                parts.append(cur)
            cur["hard_end"] = bool(it.get("cut_out"))
            for k, t in enumerate(it.get("texts") or []):
                cur["texts"].append({**t, "at": it["start"] + float(t.get("offset", 0)), "item": iid, "index": k})
            for v in it.get("voiceovers") or []:
                cur["voiceovers"].append({**v, "at": it["start"] + float(v.get("offset", 0))})
            for g in it.get("graphics") or []:
                cur["graphics"].append({**g, "at": it["start"] + float(g.get("offset", 0)), "item": iid})
        for p in parts:
            if p["clip"] is c:
                p["start"], p["end"] = round(p["start"], 3), round(p["end"], 3)
                p["speed"] = {"slowmo": 0.5, "speedup": 2.0}.get(p["effect"], 1.0)
    for iid, c, it in cutaways:
        line = cutaway_target(project, it)[3]
        p = next((p for p in parts if it["over"] in p["ids"]), None)
        if not p:
            continue
        at = max(p["start"], line["start"] + float(it.get("offset", 0.5)))
        dur = min(it["end"] - it["start"], p["end"] - at)
        if dur >= 0.5:
            p["overlays"].append({"id": iid, "clip": c, "start": it["start"], "end": round(it["start"] + dur, 3),
                                  "at": round(at, 3)})
    return parts


def part_duration(p):
    return (p["end"] - p["start"]) / p["speed"]


def transition_of(project, part, index):
    """The transition into a part: its own, else the project's cut style; never into the very first part."""
    if index == 0:
        return None
    t = part.get("transition") or project.get("cut_style") or {"type": "cut"}
    return t if t.get("type", "cut") != "cut" else None


# ---------------------------------------------------------------- looks: grade, effects, text

def combine(*grades):
    out = dict(NEUTRAL)
    for g in grades:
        for k, v in (g or {}).items():
            if v is None or k not in out:
                continue
            if k in ("contrast", "saturation", "gamma"):
                out[k] *= float(v)
            elif k in ("exposure", "temperature"):
                out[k] += float(v)
            elif k in ("vignette", "teal_orange"):
                out[k] = max(out[k], float(v))
            else:
                out[k] = v
    return out


def project_grade(project, clip=None, override=None):
    """auto (per clip) → preset → manual → per-part override."""
    g = project.get("grade") or {}
    auto = clip.get("auto_grade") if (clip and g.get("auto")) else None
    return combine(auto, GRADE_PRESETS.get(g.get("preset", "none"), {}), g.get("manual"), override)


def auto_grade_params(stats):
    """Exposure/contrast/saturation corrections from keyframe stats (8-bit YAVG, YLOW, YHIGH, SATAVG)."""
    if not stats:
        return {}
    med = lambda k, d: sorted(r.get(k, d) for r in stats)[len(stats) // 2]
    y, lo, hi, sat = med("y", 118), med("ylow", 30), med("yhigh", 200), med("sat", 30)
    # gentle caps: night rides are meant to look like night, not like a flat, noisy day
    return {"exposure": round(max(-0.35, min(0.35, (118 - y) / 110)), 3),
            "contrast": round(max(0.94, min(1.18, 170 / max(hi - lo, 1))), 3),
            "saturation": round(max(0.92, min(1.25, 32 / max(sat, 1))), 3)}


def grade_filter(g):
    """FFmpeg filters for a combined grade (no-op returns '')."""
    f = []
    if g.get("curves"):
        f.append(f"curves=preset={g['curves']}")
    if g["teal_orange"]:
        k = g["teal_orange"]
        f.append(f"colorbalance=rs={-0.08 * k:.3f}:bs={0.08 * k:.3f}:rh={0.08 * k:.3f}:bh={-0.06 * k:.3f}")
    if g["temperature"]:
        f.append(f"colortemperature=temperature={6500 - 2000 * max(-1.0, min(1.0, g['temperature'])):.0f}")
    if any(abs(g[k] - n) > 1e-3 for k, n in (("exposure", 0), ("contrast", 1), ("saturation", 1), ("gamma", 1))):
        f.append(f"eq=brightness={0.12 * g['exposure']:.3f}:contrast={g['contrast']:.3f}"
                 f":saturation={g['saturation']:.3f}:gamma={g['gamma']:.3f}")
    if g.get("lut"):
        f.append(f"lut3d=file={g['lut']}")  # relative path inside the render's working folder
    if g["vignette"]:
        f.append(f"vignette=angle={0.25 + 0.5 * min(1.0, g['vignette']):.3f}")
    return ",".join(f)


def effect_filter(effect, w, h, dur):
    """(video filter, audio filter) for an effect on a part `dur` seconds long (after speed change)."""
    return {
        "zoom": (f"crop=iw/1.15:ih/1.15,scale={w}:{h}", ""),
        "push_in": (f"scale=w='{w}*(1+0.12*t/{max(dur, 0.1):.3f})':h=-2:eval=frame,crop={w}:{h}", ""),
        "slowmo": ("setpts=2*PTS", "atempo=0.5"),
        "speedup": ("setpts=0.5*PTS", "atempo=2.0"),
        "bw": ("hue=s=0", ""),
        "vignette": ("vignette=angle=PI/4", ""),
        "dreamy": ("split[d0][d1];[d1]gblur=sigma=14[d2];[d0][d2]blend=all_mode=screen:all_opacity=0.35", ""),
    }.get(effect, ("", ""))


def find_font():
    """A bold system font for titles (no font is bundled)."""
    for f in ("/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/System/Library/Fonts/Helvetica.ttc",
              os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "segoeuib.ttf"),
              os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "arialbd.ttf"),
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"):
        if Path(f).exists():
            return f
    return None


def wrap(text, width=40):
    lines, cur = [], ""
    for w in str(text).split():
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    return "\n".join(lines + ([cur] if cur else []))


TEXT_SIZES = {"title": 1 / 10, "lower_third": 1 / 20, "caption": 1 / 22, "label": 1 / 30, "plain": 1 / 14}


def text_filter(textfile, fontfile, style, t0, t1, w, h, x=None, y=None, size=None):
    """drawtext for one overlay. textfile/fontfile are paths relative to the render's working folder (no escaping
    trouble with Windows drive letters or quotes). Fades in and out over 0.3 s. x/y (0..1, the text's centre) and
    size (fraction of the frame height) override the style's placement, for text placed on the monitor."""
    fade = (f"if(lt(t,{t0:.3f}+0.3),(t-{t0:.3f})/0.3,if(gt(t,{t1:.3f}-0.3),({t1:.3f}-t)/0.3,1))")
    common = (f"drawtext=fontfile={fontfile}:textfile={textfile}:fontcolor=white:line_spacing=8"
              f":enable='between(t,{t0:.3f},{t1:.3f})':alpha='{fade}'")
    px = h * (size or TEXT_SIZES.get(style, 1 / 22))
    look = {"title": "borderw=4:bordercolor=black@0.55",
            "lower_third": f"box=1:boxcolor=black@0.55:boxborderw={int(h / 60)}",
            "caption": f"box=1:boxcolor=black@0.5:boxborderw={int(h / 90)}",
            "label": f"box=1:boxcolor=black@0.4:boxborderw={int(h / 120)}"}.get(style, "borderw=3:bordercolor=black@0.6")
    if x is not None and y is not None:
        pos = f"x=w*{float(x):.4f}-text_w/2:y=h*{float(y):.4f}-text_h/2"
    else:
        pos = {"title": "x=(w-text_w)/2:y=(h-text_h)/2", "lower_third": "x=w*0.06:y=h*0.74-text_h",
               "caption": "x=(w-text_w)/2:y=h*0.92-text_h", "label": "x=w*0.04:y=h*0.05"}.get(style, "x=(w-text_w)/2:y=(h-text_h)/2")
    return f"{common}:fontsize={int(px)}:{pos}:{look}"


def text_key(text):
    return hashlib.sha1(str(text).encode()).hexdigest()[:16]


# ---------------------------------------------------------------- actions

ACTION_TYPES = ("keep", "cut", "trim", "effect", "transition", "cut_style", "grade", "auto_grade", "text",
                "remove_text", "voiceover", "remove_voiceover", "intro", "outro", "target", "broll_share",
                "music_volume", "captions", "privacy_blur", "stabilise", "lut", "add_broll", "split", "cut_words",
                "edit_text", "broll_mode", "graphic", "edit_graphic", "remove_graphic")
PAGE_RE = re.compile(r"graphics/[\w\- .]{1,80}\.html")
SCENE = 75.0  # seconds: B-roll further than this from the talking it accompanies is from another scene


def _rejoin(words):
    return " ".join(w[0] for w in words).strip()


def remove_range(c, kind, it, a, b):
    """Take source time [a, b] out of an item; a == b just splits it. The piece after the cut becomes a new item
    appended to the clip's list (so existing ids never shift). Cut edges are hard (`cut_in`/`cut_out`): no padding,
    no merging back. Returns a description."""
    s, e = it["start"], it["end"]
    a, b = max(s, min(a, e)), max(s, min(b, e))
    if b < a:
        a, b = b, a
    head, tail = a - s >= 0.2, e - b >= 0.2
    if not head and not tail:
        it["keep"] = False
        return "cut the whole line"
    words = it.get("words") or []
    if not words and it.get("text"):  # older transcripts: spread the words evenly over the line
        ws = it["text"].split()
        step = (e - s) / max(1, len(ws))
        words = [[w, round(s + k * step, 2), round(s + (k + 1) * step, 2)] for k, w in enumerate(ws)]
    before = [w for w in words if (w[1] + w[2]) / 2 < a]  # a word goes where most of it is
    after = [w for w in words if (w[1] + w[2]) / 2 >= b]
    later = {"texts": [], "voiceovers": []}
    for key in later:
        keep_here = []
        for x in it.get(key) or []:
            at = s + float(x.get("offset", 0))
            (later[key] if at >= b else keep_here).append(
                {**x, "offset": round(max(0.0, at - b), 2)} if at >= b else x)
        if it.get(key) is not None:
            it[key] = keep_here
    if not head:  # cut from the start: trim, and titles there move to the new start
        for key in later:
            if it.get(key) is not None:
                it[key] = [{**x, "offset": 0.0} for x in it[key]] + later[key]
        it.update(start=round(b, 2), cut_in=True)
        if words:
            it["words"], it["text"] = after, _rejoin(after) or it["text"]
        return f"trimmed the start to {b:.1f} s"
    new = None
    if tail:
        new = {k: copy.deepcopy(v) for k, v in it.items() if k not in ("texts", "voiceovers", "transition", "cut_in")}
        new.update(start=round(b, 2), end=e, cut_in=True, **{k: v for k, v in later.items() if v})
        if words:
            new["words"], new["text"] = after, _rejoin(after) or new["text"]
        c[kind].append(new)
    else:  # cut to the end: titles that started in the cut stay, just earlier
        for key in later:
            if later[key]:
                it[key] = (it.get(key) or []) + [{**x, "offset": round(max(0.0, a - s - 2), 2)} for x in later[key]]
    it.update(end=round(a, 2), cut_out=True)
    if words:
        it["words"], it["text"] = before, _rejoin(before) or it["text"]
    return f"split at {a:.1f} s" if a == b else f"cut {a:.1f}–{b:.1f} s"


def nearest_line(project, c, a, b, min_len=0.0, taken=()):
    """The kept line of clip c closest to [a, b] (gap in seconds) that is at least min_len long: (gap, id, line)."""
    ci = project["clips"].index(c)
    best = None
    for i, s in enumerate(c.get("segments") or []):
        iid = item_id(ci, "segments", i)
        if not s.get("keep") or s["end"] - s["start"] < min_len or iid in taken:
            continue
        gap = max(0.0, s["start"] - b, a - s["end"])
        if best is None or gap < best[0]:
            best = (gap, iid, s)
    return best


def place_broll(project, style="auto", scene=SCENE):
    """Put every kept B-roll window where the story is: laid over (cutaway) or next to the talking of its own scene.

    A window more than `scene` seconds from any kept line in its clip shows a different place and time from what's
    being said, so it is dropped (unticked) rather than dropped in at random. With style "auto"/"cutaway" a window
    covers the nearest long-enough kept line (one cutaway per line, starting 1 s in so the speaker is seen first);
    "insert" (or no long-enough line) keeps it as its own part between the lines, in source order."""
    moved = dropped = 0
    for c in project["clips"]:
        if c.get("angle_of"):
            continue
        taken = set()
        for w in sorted(c.get("broll") or [], key=lambda w: w["start"]):
            w.pop("over", None)
            if not w.get("keep"):
                continue
            near = nearest_line(project, c, w["start"], w["end"])
            if not near or near[0] > scene:
                if w.get("reason", "").startswith("you"):  # added or marked by the creator: their call
                    continue
                w["keep"] = False
                dropped += 1
                continue
            dur = w["end"] - w["start"]
            line = nearest_line(project, c, w["start"], w["end"], dur + 1.5, taken) if style != "insert" else None
            if line and line[0] <= scene:
                w["over"], w["offset"] = line[1], 1.0
                taken.add(line[1])
                moved += 1
    return moved, dropped

# The JSON schema the LLM's output is constrained to (Ollama `format` / LM Studio `json_schema`), so it can't
# invent fields or return something unparseable.
ACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": list(ACTION_TYPES)},
        "ids": {"type": "array", "items": {"type": "string"}},
        "value": {"type": "string"},
        "text": {"type": "string"},
        "start": {"type": "number"}, "end": {"type": "number"},
        "offset": {"type": "number"}, "duration": {"type": "number"}, "amount": {"type": "number"},
        "index": {"type": "number"}, "x": {"type": "number"}, "y": {"type": "number"}, "size": {"type": "number"},
        "over": {"type": "string"},
        "params": {"type": "object", "properties": {k: {"type": "number"} for k in
                                                    ("exposure", "contrast", "saturation", "temperature", "vignette")}},
    },
    "required": ["type"],
}


def snapshot(project):
    """The undoable part of a project (everything an action can change)."""
    keys = ("grade", "intro", "outro", "cut_style", "target_length", "captions", "privacy_blur", "broll_share",
            "music_volume")
    return copy.deepcopy({"clips": [{k: c.get(k) for k in ("segments", "broll")} for c in project["clips"]],
                          **{k: project.get(k) for k in keys}})


def restore(project, snap):
    for c, s in zip(project["clips"], snap["clips"]):
        c.update(s)
    for k, v in snap.items():
        if k != "clips":
            project[k] = v


def _ids(project, a, need=True):
    out, bad = [], []
    for iid in a.get("ids") or []:
        hit = find(project, iid)
        (out if hit else bad).append((iid, hit))
    if bad:
        raise ValueError(f"unknown part id(s): {', '.join(i for i, _ in bad)}")
    if need and not out:
        raise ValueError("needs at least one part id")
    return out


def all_graphics(project):
    """(item id, kind, item, graphic) for every graphic in the edit."""
    for iid, c, kind, i, it in items(project):
        for g in it.get("graphics") or []:
            yield iid, kind, it, g


def _placement(a):
    """Position on the frame for text placed on the monitor: x/y = centre (0..1), size = fraction of the height."""
    out = {k: round(max(0.0, min(1.0, float(a[k]))), 4) for k in ("x", "y") if a.get(k) is not None}
    if a.get("size") is not None:
        out["size"] = round(max(0.02, min(0.3, float(a["size"]))), 4)
    return out


def _onoff(a):
    return str(a.get("value", "on")).lower() not in ("off", "false", "no", "0", "none")


def apply_action(project, a, taste=None):
    """Validate and apply one action; returns a short description. Raises ValueError with a clear reason."""
    t = a.get("type")
    if t not in ACTION_TYPES:
        raise ValueError(f"unknown action '{t}'")
    if t in ("keep", "cut"):
        hits = _ids(project, a)
        for _, (c, kind, i, it) in hits:
            it["keep"] = t == "keep"
            if taste is not None and kind == "segments":
                taste.append({"text": it["text"], "kept": t == "keep"})
        return f"{'Kept' if t == 'keep' else 'Cut'} {len(hits)} part(s)"
    if t == "trim":
        (iid, (c, kind, i, it)), = _ids(project, a)[:1]
        a0 = max(0.0, float(a.get("start", it["start"])))
        b0 = min(c["duration"], float(a.get("end", it["end"])))
        if b0 - a0 < 0.2:
            raise ValueError("a trimmed part must stay at least 0.2 s long")
        it["start"], it["end"] = round(a0, 2), round(b0, 2)
        return f"Trimmed {iid} to {a0:.1f}–{b0:.1f} s"
    if t == "effect":
        v = a.get("value", "none")
        if v not in EFFECTS:
            raise ValueError(f"effect must be one of {', '.join(EFFECTS)}")
        hits = _ids(project, a)
        for _, (c, kind, i, it) in hits:
            it["effect"] = v
        return f"{EFFECTS[v]} on {len(hits)} part(s)"
    if t in ("transition", "cut_style"):
        v = a.get("value", "crossfade")
        if v not in TRANSITIONS:
            raise ValueError(f"transition must be one of {', '.join(TRANSITIONS)}")
        d = max(0.1, min(2.0, float(a.get("duration") or (0.4 if v == "jcut" else 0.6))))
        tr = {"type": v, "dur": round(d, 2)}
        if t == "cut_style":
            project["cut_style"] = tr
            return f"Every cut now uses: {v.replace('_', ' ')}"
        hits = _ids(project, a)
        for _, (c, kind, i, it) in hits:
            it["transition"] = None if v == "cut" else tr
        return f"{v.replace('_', ' ').capitalize()} into {len(hits)} part(s)"
    if t == "grade":
        preset, params = a.get("value"), a.get("params") or {}
        if preset and preset not in GRADE_PRESETS:
            raise ValueError(f"grade preset must be one of {', '.join(GRADE_PRESETS)}")
        clean = {k: float(v) for k, v in params.items() if k in NEUTRAL and isinstance(v, (int, float))}
        if a.get("ids"):
            hits = _ids(project, a)
            for _, (c, kind, i, it) in hits:
                it["grade"] = combine(GRADE_PRESETS.get(preset or "none"), clean) if (preset or clean) else None
                if it["grade"]:
                    it["grade"] = {k: v for k, v in it["grade"].items() if v != NEUTRAL[k]}
            return f"Graded {len(hits)} part(s)" + (f" ({preset})" if preset else "")
        g = project.setdefault("grade", {})
        if preset:
            g["preset"] = preset
        if clean:
            g["manual"] = {**(g.get("manual") or {}), **clean}
        return "Look: " + ", ".join(filter(None, [preset, ", ".join(f"{k} {v:+.2f}" for k, v in clean.items())]))
    if t == "auto_grade":
        project.setdefault("grade", {})["auto"] = _onoff(a)
        return f"Auto color {'on' if _onoff(a) else 'off'}"
    if t in ("text", "voiceover"):
        (iid, (c, kind, i, it)), = _ids(project, a)[:1]
        text = str(a.get("text", "")).strip()
        if not text:
            raise ValueError(f"{t} needs text")
        key = "texts" if t == "text" else "voiceovers"
        entry = {"text": text[:300], "offset": max(0.0, float(a.get("offset") or 0))}
        if t == "text":
            style = a.get("value") or "lower_third"
            if style not in TEXT_STYLES:
                raise ValueError(f"text style must be one of {', '.join(TEXT_STYLES)}")
            entry.update(style=style, duration=max(0.5, min(30.0, float(a.get("duration") or 4))), **_placement(a))
        it.setdefault(key, []).append(entry)
        return f"{'Text' if t == 'text' else 'Voice-over'} on {iid}: “{text[:40]}”"
    if t in ("remove_text", "remove_voiceover"):
        hits = _ids(project, a)
        for _, (c, kind, i, it) in hits:
            it.pop("texts" if t == "remove_text" else "voiceovers", None)
        return f"Removed {'texts' if t == 'remove_text' else 'voice-overs'} from {len(hits)} part(s)"
    if t in ("intro", "outro"):
        title = str(a.get("text", "")).strip()
        if not title:
            project[t] = None
            return f"Removed the {t}"
        style = a.get("style") or project.get(t, {}) and project[t].get("style") or "bold"
        over = a.get("over") if "over" in a else (project.get(t) or {}).get("over", False)
        project[t] = {"title": title[:80], "subtitle": str(a.get("value", ""))[:120], "style": style
                      if style in INTRO_STYLES else "bold",
                      "duration": max(2.0, min(10.0, float(a.get("duration") or 4))),
                      "over": str(over).lower() in ("true", "1", "on", "yes")}
        return f"{t.capitalize()}: “{title}”" + (" over the video" if project[t]["over"] else "")
    if t == "target":
        project["target_length"] = str(a.get("value", ""))
        return f"Target length: {project['target_length'] or 'none'}"
    if t in ("broll_share", "music_volume"):
        lim = (0, 60) if t == "broll_share" else (0, 0.6)
        project[t] = max(lim[0], min(lim[1], float(a.get("amount", 0))))
        return f"{t.replace('_', ' ').capitalize()}: {project[t]:g}"
    if t in ("captions", "privacy_blur"):
        project[t] = _onoff(a)
        return f"{'Burned-in captions' if t == 'captions' else 'Face & plate blur'} {'on' if project[t] else 'off'}"
    if t == "lut":
        path = str(a.get("value", "")).strip()
        if path and not (Path(path).is_file() and Path(path).suffix.lower() in (".cube", ".3dl")):
            raise ValueError("LUT must be an existing .cube or .3dl file")
        manual = project.setdefault("grade", {}).setdefault("manual", {})
        if path:
            manual["lut"] = path
        else:
            manual.pop("lut", None)
        return f"LUT: {Path(path).name if path else 'none'}"
    if t == "add_broll":
        cid = str(a.get("value", ""))
        c = next((c for c in project["clips"] if c["id"] == cid and not c.get("angle_of")), None)
        if not c:
            raise ValueError("add_broll needs value = a clip id")
        a0 = max(0.0, float(a.get("start", 0)))
        b0 = min(c["duration"], float(a.get("end", a0 + 5)))
        if b0 - a0 < 0.5:
            raise ValueError("B-roll must be at least 0.5 s long")
        c.setdefault("broll", []).append({"start": round(a0, 2), "end": round(b0, 2), "keep": True, "interest": 1.0,
                                          "reason": a.get("text") or "added by you"})
        c["broll"].sort(key=lambda w: w["start"])
        return f"Added {b0 - a0:.1f} s of B-roll from {c['name']}"
    if t in ("split", "cut_words"):
        (iid, (c, kind, i, it)), = _ids(project, a)[:1]
        a0 = float(a.get("start", it["start"]))
        b0 = a0 if t == "split" else float(a.get("end", a0))
        if t == "split" and not it["start"] + 0.2 <= a0 <= it["end"] - 0.2:
            raise ValueError(f"split point must be inside {iid} ({it['start']:.1f}–{it['end']:.1f} s)")
        if t == "cut_words" and b0 - a0 < 0.05:
            raise ValueError("cut_words needs start < end (source seconds)")
        return f"{iid}: {remove_range(c, kind, it, a0, b0)}"
    if t == "edit_text":
        (iid, (c, kind, i, it)), = _ids(project, a)[:1]
        texts, k = it.get("texts") or [], int(a.get("index", 0))
        if not 0 <= k < len(texts):
            raise ValueError(f"{iid} has no text #{k}")
        x = texts[k]
        if "text" in a:
            if not str(a["text"]).strip():
                texts.pop(k)
                return f"Removed a text from {iid}"
            x["text"] = str(a["text"]).strip()[:300]
        if a.get("value") in TEXT_STYLES:
            x["style"] = a["value"]
        if "offset" in a:
            x["offset"] = round(max(0.0, float(a["offset"])), 2)
        if "duration" in a:
            x["duration"] = round(max(0.5, min(30.0, float(a["duration"]))), 2)
        x.update(_placement(a))
        return f"Edited “{x['text'][:30]}” on {iid}"
    if t == "graphic":  # an animated page (graphics/*.html in the project folder) over this part
        (iid, (c, kind, i, it)), = _ids(project, a)[:1]
        page = str(a.get("value", "")).replace("\\", "/")
        if not PAGE_RE.fullmatch(page):
            raise ValueError("graphic needs value = a page in the project's graphics folder, e.g. graphics/title.html")
        gid = "g" + hashlib.sha1(f"{page}{iid}{len(list(all_graphics(project)))}".encode()).hexdigest()[:6]
        it.setdefault("graphics", []).append({"id": gid, "page": page, "offset": round(max(0.0, float(a.get("offset") or 0)), 2),
                                              "duration": round(max(0.3, min(120.0, float(a.get("duration") or 4))), 2),
                                              **({"template": str(a["text"])[:40]} if a.get("text") else {})})
        return f"Graphic {gid} ({Path(page).stem}) on {iid}"
    if t in ("edit_graphic", "remove_graphic"):
        gid = str(a.get("value", ""))
        hit = next(((it, g) for _, _, it, g in all_graphics(project) if g["id"] == gid), None)
        if not hit:
            raise ValueError(f"no graphic '{gid}'")
        it, g = hit
        if t == "remove_graphic":
            it["graphics"].remove(g)
            return f"Removed graphic {gid}"
        if "offset" in a:
            g["offset"] = round(max(0.0, float(a["offset"])), 2)
        if "duration" in a:
            g["duration"] = round(max(0.3, min(120.0, float(a["duration"]))), 2)
        if a.get("ids"):  # move it onto another part
            (iid, (c, kind, i, new)), = _ids(project, a)[:1]
            it["graphics"].remove(g)
            new.setdefault("graphics", []).append(g)
        return f"Moved graphic {gid}"
    if t == "broll_mode":
        hits = _ids(project, a)
        mode = a.get("value", "cutaway")
        for iid, (c, kind, i, it) in hits:
            if kind != "broll":
                raise ValueError(f"{iid} is not B-roll")
            it.pop("over", None)
            if mode == "cutaway":
                target = a.get("over") or (nearest_line(project, c, it["start"], it["end"]) or (0, None))[1]
                hit = find(project, target) if target else None
                if not hit or hit[1] != "segments":
                    raise ValueError(f"no kept line to lay {iid} over")
                it.update(over=target, offset=max(0.0, float(a.get("offset", 1.0))), keep=True)
        return f"B-roll {'over the talking' if mode == 'cutaway' else 'as its own shot'}: {len(hits)} window(s)"
    if t == "stabilise":
        hits = _ids(project, a)
        for _, (c, kind, i, it) in hits:
            it["effect"] = "stabilise" if _onoff(a) else "none"
        return f"Stabilise {'on' if _onoff(a) else 'off'} for {len(hits)} part(s)"


def apply_actions(project, actions, taste=None):
    """Apply a list of actions; returns ([descriptions], [errors]). Invalid actions are skipped, not fatal."""
    done, errors = [], []
    for a in actions or []:
        try:
            done.append(apply_action(project, a, taste))
        except (ValueError, TypeError) as e:
            errors.append(f"{a.get('type', '?')}: {e}")
    return done, errors


# ---------------------------------------------------------------- context & suggestions

def fmt_t(s):
    s = max(0, int(s))
    return f"{s // 60}:{s % 60:02d}"


def context(project, max_text=70):
    """Compact, line-per-part description of the edit for the language model."""
    g = project.get("grade") or {}
    head = [f"Project: {project['name']}. Target length: {project.get('target_length') or 'none'}. "
            f"Cut style: {(project.get('cut_style') or {}).get('type', 'cut')}. "
            f"Look: preset {g.get('preset', 'none')}, auto color {'on' if g.get('auto') else 'off'}. "
            f"Intro: {(project.get('intro') or {}).get('title', 'none')}. Outro: {(project.get('outro') or {}).get('title', 'none')}.",
            "Clips: " + "; ".join(f"c{ci} = clip id {c['id']} ({c['name']}, {fmt_t(c['duration'])})"
                                  for ci, c in enumerate(project["clips"]) if not c.get("angle_of")),
            "Parts (id | source time | length | KEEP/cut | score | effects | content):"]
    rows = []
    for iid, c, kind, i, it in items(project):
        fx = [x for x in (it.get("effect") not in (None, "none") and it.get("effect"),
                          (it.get("transition") or {}).get("type"),
                          it.get("texts") and f"{len(it['texts'])} text", it.get("voiceovers") and "voice-over",
                          it.get("grade") and "own grade",
                          it.get("graphics") and "graphics " + ",".join(f"{g['id']}={g['page']}" for g in it["graphics"])) if x]
        what = it["text"][:max_text] if kind == "segments" else f"[B-roll, no talking] {it.get('desc', it.get('reason', ''))}"
        rows.append(f"{iid} | {fmt_t(it['start'])}-{fmt_t(it['end'])} | {it['end'] - it['start']:.0f}s | "
                    f"{'KEEP' if it.get('keep') else 'cut'} | {('★%s' % it['score']) if it.get('score') else '-'} | "
                    f"{', '.join(fx) or '-'} | {what}")
    return "\n".join(head + rows)


def heuristic_suggestions(project):
    """Cheap, deterministic suggestions; the LLM adds creative ones on top."""
    out = []
    add = lambda ids, title, why, action: out.append(
        {"ids": ids, "title": title, "why": why, "action": action, "source": "rules"})
    kept = [(iid, c, kind, i, it) for iid, c, kind, i, it in items(project) if it.get("keep")]
    unsure = sorted(((it["conf"], iid, it) for iid, c, kind, i, it in items(project)
                     if kind == "segments" and it.get("conf") is not None and it["conf"] < 0.55
                     and it.get("auto") is None and 2.0 <= (it.get("score") or 0) <= 4.0), key=lambda x: x[0])
    for conf, iid, it in unsure[:6]:  # the closest calls only; a long list of coin-flips helps nobody
        flip = "cut" if it.get("keep") else "keep"
        add([iid], f"Your call: {flip} “{it['text'][:40]}”?", f"The AI is only {conf:.0%} sure about ★{it.get('score')}.",
            {"type": flip, "ids": [iid]})
    if kept and not project.get("intro"):
        add([kept[0][0]], "Add an intro title", "Videos with a title card in the first seconds hold viewers better.",
            {"type": "intro", "text": project["name"], "value": "", "duration": 4})
    if not (project.get("grade") or {}).get("auto"):
        dark = [c for c in project["clips"] if (c.get("auto_grade") or {}).get("exposure", 0) > 0.15]
        if dark:
            add([], "Turn on auto color", f"{len(dark)} clip(s) are under-exposed; auto color balances them.",
                {"type": "auto_grade", "value": "on"})
    if len(kept) > 8 and (project.get("cut_style") or {}).get("type", "cut") == "cut":
        add([], "Smooth the cuts with J-cuts", "Letting the next line's sound start early hides jump cuts.",
            {"type": "cut_style", "value": "jcut", "duration": 0.4})
    run, start = 0.0, None
    for iid, c, kind, i, it in kept:
        if kind == "segments" and it.get("effect", "none") == "none" and it["end"] - it["start"] > 6:
            run += it["end"] - it["start"]
            if run > 30 and start is None:
                start = iid
                add([iid], "Punch in to break up a long talking stretch",
                    "Over 30 s of talking without a visual change; a zoom keeps attention.",
                    {"type": "effect", "ids": [iid], "value": "zoom"})
        else:
            run, start = 0.0, None
    for iid, c, kind, i, it in kept:
        if "loud moment" in (it.get("reason") or "") and it.get("effect", "none") == "none":
            add([iid], "Slow down the big reaction", "A loud moment reads better in slow motion.",
                {"type": "effect", "ids": [iid], "value": "slowmo"})
            break
    if any((c.get("shake") or 0) > 0.6 for c in project["clips"]):
        shaky = [iid for iid, c, kind, i, it in kept if (c.get("shake") or 0) > 0.6][:12]
        if shaky:
            add(shaky, "Stabilise the shaky shots", "These clips move a lot between frames.",
                {"type": "stabilise", "ids": shaky, "value": "on"})
    return out


def platform_font_note():
    return "" if find_font() else ("No system font found for titles; install DejaVu fonts on Linux." if
                                   sys.platform.startswith("linux") else "No system font found for titles.")


def ease(x):
    return 0.5 - 0.5 * math.cos(math.pi * max(0.0, min(1.0, x)))

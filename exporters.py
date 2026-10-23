"""Hand-off files for the finished cut: EDL, FCPXML, OpenTimelineIO, SRT subtitles and YouTube metadata.

All of them are built from one `timeline()`: the ordered list of kept ranges with their position in the output.
Entries may also carry the look of the cut (transition into the entry, effect, texts, grade, intro/outro card,
B-roll overlays); each edit-list format carries as much of it as it can and notes the rest (see each writer).
Source times respect each clip's embedded start timecode (DJI writes time-of-day drop-frame timecode), because
Resolve, Premiere and Final Cut place media by it.
"""
import json
import re
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

import editing


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


# ---------------------------------------------------------------- shared layout of the edit lists

# (size as a fraction of frame height, centre x, centre y) per text style; mirrors editing.text_filter
TEXT_LOOK = {"title": (1 / 10, 0.5, 0.5), "lower_third": (1 / 20, 0.3, 0.7), "caption": (1 / 22, 0.5, 0.88),
             "label": (1 / 30, 0.15, 0.07)}
EDL_WIPES = {"wipe_left": "W001", "wipe_right": "W001"}  # SMPTE 001 = horizontal wipe; its direction isn't portable
DISSOLVES = ("crossfade", "dissolve")  # carried exactly; every other mix is approximated by a dissolve (or W001)


def _drop(tl, fps):
    return is_drop_frame(fps, next((e["clip"].get("timecode") for e in tl if e.get("clip")), None))


def _layout(tl, fps):
    """Frame geometry of the entries that go into EDL, FCPXML and OTIO: ([part], total frames).

    Mirrors the renderer's xfade: a transition of d frames starts d frames before the outgoing part ends, on the
    incoming part's first frame, so the programme gets d frames shorter and the media a transition overlaps (its
    handles) is always kept media. d is clamped like video_engine (≤ half of either part) and kept even so FCPXML
    and OTIO can centre it on the cut. Fast-forward ride-throughs (silent, or faster than 4×) stay out."""
    out, rec = [], 0
    for e in tl:
        speed, card, c = e.get("speed", 1), e.get("card"), e.get("clip")
        if e.get("silent") or speed > 4:
            continue
        if card:
            speed, src = 1, frames(e.get("start", 0), fps) if c else 0
            n = srcn = frames(float(card.get("duration") or e["rec_end"] - e["rec_start"]), fps)
        else:
            src = frames(e["start"], fps)
            srcn = frames(e["end"], fps) - src
            n = round(srcn / speed)
        kind = (e.get("transition") or {}).get("type", "cut")
        d = 0
        if out and editing.TRANSITIONS.get(kind):
            d = min(frames(float(e["transition"].get("dur", 0.6)), fps), n // 2, out[-1]["n"] // 2) // 2 * 2
            out[-1]["d_out"] = d
        rec -= d
        out.append({"e": e, "clip": c, "card": card, "speed": speed, "src": src, "srcn": srcn, "n": n, "rec": rec,
                    "d_in": d, "d_out": 0, "kind": kind})
        rec += n
    return out, rec


def _texts(L, fps):
    """A part's texts (a card's title and subtitle included): f0/f1 in frames from the part's first frame, clipped
    to what's visible between its transitions; x, y = text centre (0..1); size = fraction of height (≤ 1) or px."""
    e, card, n = L["e"], L["card"], L["n"]
    items = list(e.get("texts") or [])
    if card:
        num, den = rate(fps)
        dur, minimal = n * den / num, card.get("style") == "minimal"
        sub = [{"text": card["subtitle"], "style": "caption", "t0": 0.6, "t1": dur - 0.1,
                **({} if minimal else {"y": 0.62})}] if card.get("subtitle") else []
        items[:0] = [{"text": card["title"], "style": "lower_third" if minimal else "title", "t0": 0.2,
                      "t1": dur - 0.1}] + sub
    out = []
    for t in items:
        size, x, y = TEXT_LOOK.get(t.get("style"), TEXT_LOOK["caption"])
        t0 = float(t.get("t0", 0))
        f0 = max(frames(t0, fps), L["d_in"] // 2)
        f1 = min(frames(float(t.get("t1", t0 + 4)), fps), n - L["d_out"] // 2)
        if f1 > f0 and str(t.get("text", "")).strip():
            out.append({"text": " / ".join(str(t["text"]).split("\n")), "style": t.get("style", "custom"),
                        "f0": f0, "f1": f1, "x": float(t.get("x", x)), "y": float(t.get("y", y)),
                        "size": float(t.get("size") or size)})
    return out


def _overlays(L, fps):
    """B-roll laid over a part: [(clip, source frame, part-relative frame, frames)], clipped to the visible span."""
    out = []
    for o in L["e"].get("overlays") or []:
        a, k = frames(o["start"], fps), frames(float(o.get("at", 0)), fps)
        m = frames(o["end"], fps) - a
        if o.get("dur"):
            m = min(m, frames(float(o["dur"]), fps))
        lo, hi = max(k, L["d_in"] // 2), min(k + m, L["n"] - L["d_out"] // 2)
        if hi > lo:
            out.append((o["clip"], a + lo - k, lo, hi - lo))
    return out


def _angle_span(L, angle, offset, fps):
    """(angle source frame, part-relative frame, frames) of a synced angle under a plain part, or None."""
    if L["card"] or L["speed"] != 1:
        return None
    a = frames(L["e"]["start"] - offset, fps)
    k0, k1 = max(L["d_in"] // 2, -a), min(L["n"] - L["d_out"] // 2, frames(angle["duration"], fps) - a)
    return (a + k0, k0, k1 - k0) if k1 > k0 else None


def _grade_text(g):
    out = []
    for k, v in (g or {}).items():
        if k == "lut" and v:
            out.append(f"LUT {Path(v).name}")
        elif k == "curves" and v:
            out.append(f"curves {v}")
        elif isinstance(v, (int, float)) and abs(v - editing.NEUTRAL.get(k, v)) > 1e-3:
            out.append(f"{k} {v:g}")
    return ", ".join(out)


def _notes(L, carried):
    """What a part asks for that the file format can't apply (carried = effects it does apply), as short notes."""
    e, notes = L["e"], []
    if L["d_in"] and L["kind"] not in DISSOLVES:
        notes.append(f"{L['kind'].replace('_', ' ')} transition (approximated here)")
    if L["kind"] == "jcut":
        notes.append(f"J-cut: sound leads the picture by {float(e['transition'].get('dur', 0.4)):g} s")
    eff = e.get("effect") or "none"
    if eff != "none" and eff not in carried:
        notes.append(f"effect {editing.EFFECTS.get(eff, eff)}")
    if _grade_text(e.get("grade")):
        notes.append("grade: " + _grade_text(e["grade"]))
    return notes


# ---------------------------------------------------------------- EDL

def write_edl(tl, path, fps, title):
    """CMX3600 EDL: one picture track plus its audio. Carries cuts, dissolves (D; dips, slides and the other mixes
    are written as dissolves), wipes (W001) and constant speed changes (M2). Titles, cards, effects, grades and
    B-roll overlays can't live in an EDL: they become `* LeanCut:` comments under their event (a second video
    track would need a second EDL). Fast-forward ride-throughs are left out."""
    drop = _drop(tl, fps)
    tc = lambda f: frames_to_tc(f, fps, drop)
    lines = [f"TITLE: {title}", f"FCM: {'DROP FRAME' if drop else 'NON-DROP FRAME'}", ""]
    rec0 = tc_to_frames("01:00:00:00", fps, drop)
    lay, _ = _layout(tl, fps)
    prev = None
    for n, L in enumerate(lay, 1):
        c, d = L["clip"], L["d_in"]
        reel, chan = ("AX", "V" if L["card"] else "AA/V") if c else ("BL", "V")
        name = Path(c["path"]).name if c else "BLACK"
        s0, r0, ln = (clip_tc_frames(c, fps, drop) + L["src"] if c else 0), rec0 + L["rec"], L["n"] - L["d_out"]
        ev = lambda reel_, how, dur, a, b, ra, rb: (f"{n:03d}  {reel_:<8} {chan:<5} {how:<4} "
                                                    f"{f'{dur:03d}' if dur else '':>3} {tc(a)} {tc(b)} {tc(ra)} {tc(rb)}")
        if d:  # dissolve/wipe: zero-length 'from' line at the outgoing out point, then the incoming event
            lines += [ev(prev[0], "C", 0, prev[1], prev[1], r0, r0),
                      ev(reel, EDL_WIPES.get(L["kind"], "D"), d, s0, s0 + ln, r0, r0 + ln)]
        else:
            lines.append(ev(reel, "C", 0, s0, s0 + ln, r0, r0 + ln))
        if L["speed"] != 1:  # CMX convention: source out = source in + record length, M2 gives the real rate
            lines.append(f"M2   {reel:<8}       {L['speed'] * fps:05.1f}                {tc(s0)}")
        lines += [f"* FROM CLIP NAME: {prev[2]}", f"* TO CLIP NAME: {name}"] if d else [f"* FROM CLIP NAME: {name}"]
        if L["card"]:
            lines.append(f"* LeanCut: {L['card'].get('style', 'bold')} title card over {'this clip' if c else 'black'}")
        for x in _texts(L, fps):
            lines.append(f"* LeanCut: {x['style']} text \"{x['text']}\" {tc(r0 + x['f0'])}-{tc(r0 + x['f1'])}")
        for oc, a, k, m in _overlays(L, fps):
            o0 = clip_tc_frames(oc, fps, drop) + a
            lines.append(f"* LeanCut: B-roll over {tc(r0 + k)}-{tc(r0 + k + m)}: {Path(oc['path']).name} "
                         f"{tc(o0)}-{tc(o0 + m)} (picture only, on V2)")
        lines += [f"* LeanCut: {x}" for x in _notes(L, ("slowmo", "speedup"))] + [""]
        prev = (reel, s0 + ln, name)
    Path(path).write_text("\n".join(lines), encoding="utf-8")
    return Path(path)


# ---------------------------------------------------------------- FCPXML

# Final Cut's own effect references, as Final Cut writes them into <resources>
FCP_EFFECTS = {
    "mix": ['<effect id="fx1" name="Cross Dissolve" uid="FxPlug:4731E73A-8DAC-4113-9A30-AE85B1761265"/>',
            '<effect id="fx2" name="Audio Crossfade" uid="FFAudioTransition"/>'],
    "title": ['<effect id="fx3" name="Basic Title" '
              'uid=".../Titles.localized/Bumper:Opener.localized/Basic Title.localized/Basic Title.moti"/>'],
}
FCP_CARRIED = ("slowmo", "speedup", "zoom", "push_in", "stabilise")


def _stack(spans, base):
    """Lowest lane (≥ base) per (f0, f1) span so connected items never overlap on one lane."""
    ends, out = {}, []
    for f0, f1 in spans:
        lane = base
        while ends.get(lane, f0) > f0:
            lane += 1
        ends[lane] = max(ends.get(lane, f1), f1)
        out.append(lane)
    return out


def write_fcpxml(tl, path, fmt, title, angles=(), chapters=()):
    """FCPXML 1.10 for Final Cut Pro and DaVinci Resolve.

    Transitions are Cross Dissolves (+ Audio Crossfade) centred on the cut; slow motion / speed-up are timeMaps;
    zoom and push-in are transforms, stabilise is Final Cut's stabilisation; texts and intro/outro cards are Basic
    Titles (cards over a gap, or over their background clip); B-roll overlays are picture-only connected clips.
    Lanes: synced angles 1, then overlays, then titles on top. Looks Final Cut can't take portably (B&W, vignette,
    glow, grades, LUTs, wipes, J-cuts) become to-do markers on the clip.

    angles: [(angle_clip, primary_clip_id, offset)] from audio sync (t_primary = t_angle + offset); each synced
    angle is attached under the primary's clips as a connected clip on lane 1, ready to switch to in the NLE.
    chapters: [(record_seconds, title)] become chapter markers.
    """
    fps, w, h = fmt["fps"], fmt["width"], fmt["height"]
    num, den = rate(fps)
    drop = _drop(tl, fps)
    t = lambda f: f"{f * den}/{num}s" if den != 1 else f"{f}/{num}s"
    lay, total = _layout(tl, fps)
    clips = {}
    for L in lay:
        for c in [L["clip"]] + [o["clip"] for o in L["e"].get("overlays") or []]:
            if c:
                clips.setdefault(c["id"], c)
    for angle, _, _ in angles:
        clips.setdefault(angle["id"], angle)
    ids = {cid: f"a{i}" for i, cid in enumerate(clips)}
    res = [f'<format id="r0" frameDuration="{t(1)}" width="{w}" height="{h}"/>']
    for cid, c in clips.items():
        res.append(f'<asset id="{ids[cid]}" name={quoteattr(Path(c["path"]).name)} '
                   f'start="{t(clip_tc_frames(c, fps, drop))}" duration="{t(frames(c["duration"], fps))}" '
                   f'hasVideo="1" hasAudio="1" format="r0" audioSources="1" audioChannels="2" audioRate="48000">'
                   f'<media-rep kind="original-media" src={quoteattr(_uri(c["path"]))}/></asset>')
    tcf = "DF" if drop else "NDF"
    named = lambda c: quoteattr(Path(c["path"]).name)
    spine, used, styles = [], set(), 0
    marks = sorted(chapters)
    for L in lay:
        e, c, n = L["e"], L["clip"], L["n"]
        h_in, h_out = L["d_in"] // 2, L["d_out"] // 2
        base = clip_tc_frames(c, fps, drop) + L["src"] if c else 0  # local time of the part's first frame
        if L["d_in"]:
            used.add("mix")
            spine.append(f'<transition name="Cross Dissolve" offset="{t(L["rec"])}" duration="{t(L["d_in"])}">'
                         '<filter-video ref="fx1" name="Cross Dissolve"/>'
                         '<filter-audio ref="fx2" name="Audio Crossfade"/></transition>')
        inner, eff = [], e.get("effect") or "none"
        if L["speed"] != 1:  # local time runs at output pace from `base`; value = source media time
            inner.append(f'<timeMap><timept time="{t(base)}" value="{t(base)}" interp="linear"/>'
                         f'<timept time="{t(base + n)}" value="{t(base + L["srcn"])}" interp="linear"/></timeMap>')
        if eff == "zoom":
            inner.append('<adjust-transform scale="1.15 1.15"/>')
        elif eff == "push_in":
            inner.append(f'<adjust-transform><param name="scale"><keyframeAnimation>'
                         f'<keyframe time="{t(base)}" value="1 1"/><keyframe time="{t(base + n)}" value="1.12 1.12"/>'
                         '</keyframeAnimation></param></adjust-transform>')
        elif eff == "stabilise":
            inner.append('<adjust-stabilization type="automatic"/>')
        lane = 1
        for angle, primary_id, offset in angles:
            span = _angle_span(L, angle, offset, fps) if c and primary_id == c["id"] else None
            if span:
                a, k, m = span
                inner.append(f'<asset-clip ref="{ids[angle["id"]]}" lane="1" offset="{t(base + k)}" '
                             f'name={named(angle)} start="{t(clip_tc_frames(angle, fps, drop) + a)}" duration="{t(m)}"/>')
                lane = 2
        ovs = _overlays(L, fps)
        lanes = _stack([(k, k + m) for _, _, k, m in ovs], lane)
        for (oc, a, k, m), ln in zip(ovs, lanes):
            inner.append(f'<asset-clip ref="{ids[oc["id"]]}" lane="{ln}" offset="{t(base + k)}" name={named(oc)} '
                         f'start="{t(clip_tc_frames(oc, fps, drop) + a)}" duration="{t(m)}" srcEnable="video"/>')
        txs = _texts(L, fps)
        for x, ln in zip(txs, _stack([(x["f0"], x["f1"]) for x in txs], max(lanes, default=lane - 1) + 1)):
            used.add("title")
            styles += 1
            size = x["size"] * h if x["size"] <= 1 else x["size"]
            inner.append(f'<title ref="fx3" lane="{ln}" offset="{t(base + x["f0"])}" name={quoteattr(x["text"][:60])} '
                         f'start="0s" duration="{t(x["f1"] - x["f0"])}">'
                         f'<param name="Position" key="9999/999166631/999166633/1/100/101" '
                         f'value="{(x["x"] - 0.5) * w:.1f} {(0.5 - x["y"]) * h:.1f}"/>'
                         f'<text><text-style ref="ts{styles}">{escape(x["text"])}</text-style></text>'
                         f'<text-style-def id="ts{styles}"><text-style font="Helvetica" fontSize="{size:.0f}" '
                         f'fontFace="Bold" fontColor="1 1 1 1" alignment="center"/></text-style-def></title>')
        for sec, name in marks:
            f = frames(sec, fps) - L["rec"]
            if h_in <= f < n - h_out:
                inner.append(f'<chapter-marker start="{t(base + f)}" duration="{t(1)}" value={quoteattr(name)}/>')
        notes = _notes(L, FCP_CARRIED)
        if notes:
            inner.append(f'<marker start="{t(base + h_in)}" duration="{t(1)}" '
                         f'value={quoteattr("LeanCut: " + "; ".join(notes))} completed="0"/>')
        off, st, du = t(L["rec"] + h_in), t(base + h_in), t(n - h_in - h_out)
        if c:
            spine.append(f'<asset-clip ref="{ids[c["id"]]}" offset="{off}" name={named(c)} start="{st}" duration="{du}" '
                         f'format="r0" tcFormat="{tcf}"' + (' srcEnable="video">' if L["card"] else ">")
                         + "".join(inner) + "</asset-clip>")
        else:
            spine.append(f'<gap name={quoteattr(L["card"].get("title") or "Card")} offset="{off}" start="{st}" '
                         f'duration="{du}">' + "".join(inner) + "</gap>")
    res += [x for k in ("mix", "title") if k in used for x in FCP_EFFECTS[k]]
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n<fcpxml version="1.10">\n<resources>\n'
           + "\n".join(res) + f'\n</resources>\n<library><event name="LeanCut"><project name={quoteattr(title)}>'
           f'<sequence format="r0" duration="{t(total)}" tcStart="0s" tcFormat="{tcf}" '
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
    """OpenTimelineIO JSON (Resolve 18.5+, and anything with an OTIO adapter): V1/A1 = the cut, V2 = synced angles,
    next V track = B-roll overlays (picture only).

    Transitions are SMPTE_Dissolves centred on the cut (the LeanCut type in their metadata); speed changes are
    LinearTimeWarps (source_range.duration = record length, as OTIO adapters expect); other effects are named
    Effects. OTIO has no title primitive: texts and cards become markers with the text, style and position in
    metadata["leancut"]; a card without a clip is a gap. Grades ride along in the clip's metadata."""
    num, den = rate(fmt["fps"])
    r = num / den
    drop = _drop(tl, fmt["fps"])
    lay, _ = _layout(tl, fmt["fps"])
    v1, a1, v2, v3, at = [], [], [], [], {"v2": 0, "v3": 0}

    def place(track, key, c, a, pos, m):  # clip c (source frame a, m frames) at track frame pos, after a gap
        if pos < at[key]:
            a, m, pos = a + at[key] - pos, m - (at[key] - pos), at[key]
        if m > 0:
            if pos > at[key]:
                track.append(_gap(pos - at[key], r))
            track.append(_otio_clip(c, a, m, r, clip_tc_frames(c, fmt["fps"], drop)))
            at[key] = pos + m

    for L in lay:
        e, c, n, speed = L["e"], L["clip"], L["n"], L["speed"]
        h_in, h_out = L["d_in"] // 2, L["d_out"] // 2
        vis = n - h_in - h_out
        if L["d_in"]:
            tr = {"OTIO_SCHEMA": "Transition.1", "name": L["kind"], "transition_type": "SMPTE_Dissolve",
                  "in_offset": _rt(h_in, r), "out_offset": _rt(h_in, r), "metadata": {"leancut": {"type": L["kind"]}}}
            v1.append(tr)
            a1.append(json.loads(json.dumps(tr)))
        warp = [{"OTIO_SCHEMA": "LinearTimeWarp.1", "name": "", "effect_name": "LinearTimeWarp", "enabled": True,
                 "time_scalar": float(speed), "metadata": {}}] if speed != 1 else []
        tc0 = clip_tc_frames(c, fmt["fps"], drop) if c else 0
        vid = _otio_clip(c, L["src"] + h_in * speed, vis, r, tc0) if c else _gap(vis, r)
        aud = _otio_clip(c, L["src"] + h_in * speed, vis, r, tc0) if c and not L["card"] else _gap(vis, r)
        eff = e.get("effect") or "none"
        vid["effects"] = warp + ([{"OTIO_SCHEMA": "Effect.1", "name": editing.EFFECTS.get(eff, eff), "effect_name": eff,
                                   "enabled": True, "metadata": {}}] if eff not in ("none", "slowmo", "speedup") else [])
        aud["effects"] = list(warp) if c and not L["card"] else []
        meta = {k: v for k, v in (("effect", eff), ("grade", e.get("grade")), ("transition", e.get("transition")),
                                  ("card", L["card"])) if v and v != "none"}
        vid["metadata"] = {"leancut": meta} if meta else {}
        s0 = vid["source_range"]["start_time"]["value"]  # markers live in the item's source time
        vid["markers"] = [{"OTIO_SCHEMA": "Marker.2", "name": x["text"], "color": "YELLOW", "comment": x["text"],
                           "marked_range": _range(s0 + x["f0"] - h_in, x["f1"] - x["f0"], r),
                           "metadata": {"leancut": {k: x[k] for k in ("text", "style", "x", "y", "size")}}}
                          for x in _texts(L, fmt["fps"])]
        v1.append(vid)
        a1.append(aud)
        for angle, primary_id, offset in angles:
            span = _angle_span(L, angle, offset, fmt["fps"]) if c and primary_id == c["id"] else None
            if span:
                place(v2, "v2", angle, span[0], L["rec"] + span[1], span[2])
                break
        for oc, a, k, m in _overlays(L, fmt["fps"]):
            place(v3, "v3", oc, a, L["rec"] + k, m)
    track = lambda name, kind, kids: {"OTIO_SCHEMA": "Track.1", "name": name, "kind": kind, "children": kids,
                                      "source_range": None, "effects": [], "markers": [], "enabled": True,
                                      "metadata": {}}
    tracks = [track(f"V{i}", "Video", kids) for i, kids in enumerate([v1] + [x for x in (v2, v3) if x], 1)]
    doc = {"OTIO_SCHEMA": "Timeline.1", "name": title, "global_start_time": None, "metadata": {},
           "tracks": {"OTIO_SCHEMA": "Stack.1", "name": "tracks", "children": tracks + [track("A1", "Audio", a1)],
                      "source_range": None, "effects": [], "markers": [], "enabled": True, "metadata": {}}}
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

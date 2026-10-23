"""LeanCut — local AI video editor. FastAPI backend, projects, settings and pipeline orchestration.

Run:  python app.py      (browser UI at http://127.0.0.1:8765)
      python desktop.py  (native desktop window)
"""
import atexit
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import agent_tools
import editing
import exporters
import llm_agent
import media_analysis
import production
import transcriber
import updates
import video_engine
from video_engine import Cancelled, check_cancel

APP_NAME = "LeanCut"
VERSION = "0.4.0"
ROOT = Path(__file__).parent
RESOURCES = Path(getattr(sys, "_MEIPASS", ROOT))  # PyInstaller unpacks bundled files here


def _user_data_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / APP_NAME


# Packaged app bundles are read-only, so the packaged app keeps projects in the per-user data folder;
# running from source keeps using ./workspace. LEANCUT_HOME overrides both.
WORK = Path(os.environ.get("LEANCUT_HOME") or (_user_data_dir() if getattr(sys, "frozen", False) else ROOT / "workspace"))
MEDIA, CACHE, PROJECTS = WORK / "media", WORK / "cache", WORK / "projects"
SETTINGS_FILE = WORK / "settings.json"
for d in (MEDIA, CACHE, PROJECTS):
    d.mkdir(parents=True, exist_ok=True)

SCORE_VERSION = "v3"  # bump when the scoring prompt changes, so cached AI scores are refreshed
DEFAULT_SETTINGS = {
    # AI
    "whisper_size": "large-v3-turbo",  # best multilingual quality/speed; small garbles non-English speech
    "custom_whisper": "",  # Hugging Face repo id or local folder of a CTranslate2 model (e.g. fine-tuned for Nepali)
    "language": "auto",
    "provider": "ollama",
    "base_url": llm_agent.DEFAULT_URLS["ollama"],
    "model": "",
    "markers": "clip that, mark this, mark that, keep this, bookmark",
    # rider features
    "broll": True, "broll_share": 15, "moments": True, "connectors": False,
    "broll_style": "auto",  # "auto"/"cutaway": B-roll over the talking of its scene when possible; "insert": own shots
    # video & audio
    "render_mode": "accurate", "encoder": "auto",
    "loudness": True, "denoise": False, "music_path": "", "music_volume": 0.15,
    # what to export and where
    "out_video": True, "out_edl": True, "out_fcpxml": True, "out_otio": True, "out_srt": True,
    "out_youtube": True, "out_thumbs": True, "out_shorts": False, "shorts_count": 3,
    "output_dir": "", "output_name": "{project}",
    # AI engines
    "engine": "auto",  # "auto"/"decision": token-probability scoring when the server supports it; "batch": JSON lists
    "vision_model": "",  # a local vision model in Ollama (e.g. gemma3:4b) for the visual judge; empty = off
    "telemetry": True, "auto_join": True,
    # voice
    "tts_engine": "auto", "tts_server": "", "tts_voice": "", "voice_sample": "", "voice_consent": False,
    "voice_enhance": False,
    # publishing
    "bleep": False, "profanity_words": "", "privacy_kinds": "face,plate",
    # app
    "update_check": True, "current_project": "",
}
PROJECT_DEFAULTS = {"grade": {}, "cut_style": {"type": "cut"}, "intro": None, "outro": None, "captions": False,
                    "privacy_blur": False, "chat": [], "suggestions": []}
def stamp(name):
    m = re.search(r"(20\d\d)(\d\d)(\d\d)[_-]?(\d\d)(\d\d)(\d\d)", Path(name).stem)
    return "%s-%s-%sT%s:%s:%s" % m.groups() if m else None


def recorded_at(name, created):
    """Sortable recording time. A timestamp in the file name (DJI_20261016210207…, PXL_20261016_210207…, VID_…) is
    local time on every camera and phone, so it wins over the container's creation time (UTC on phones, local on
    GoPro and DJI) when cameras are mixed."""
    return stamp(name) or created


SERVER_URL = ""  # where this LeanCut listens, for coding agents (see announce)
undo = {}  # project id -> {"back": [snapshots], "forward": [snapshots]}; in memory only


def _read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path, data):
    tmp = Path(path).with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(path)  # atomic: a crash mid-write never corrupts the file


def _migrate_single_project():
    """v0.1 kept one project in WORK/project.json (+ WORK/output). Move it into projects/ without copying or deleting."""
    old = WORK / "project.json"
    if not old.exists():
        return
    data = _read_json(old, {})
    pid = uuid.uuid4().hex[:8]
    pdir = PROJECTS / pid
    pdir.mkdir(parents=True)
    old_settings = data.get("settings", {})
    _write_json(pdir / "project.json", {"id": pid, "name": "My first project", "created": time.time(),
                                        "target_length": old_settings.get("target_length", ""),
                                        "clips": data.get("clips", [])})
    if (WORK / "output").is_dir():
        (WORK / "output").rename(pdir / "output")
    merged = {**DEFAULT_SETTINGS, **{k: v for k, v in old_settings.items() if k in DEFAULT_SETTINGS},
              "current_project": pid}
    _write_json(SETTINGS_FILE, merged)
    old.rename(WORK / "project.v0.1.json.bak")


_migrate_single_project()
settings = {**DEFAULT_SETTINGS, **_read_json(SETTINGS_FILE, {})}
project = {}
lock = threading.Lock()
job = {"running": False, "name": "", "step": "", "progress": 0.0, "logs": [], "error": None, "result": None}
_update_cache = {}


def project_dir(pid=None) -> Path:
    return PROJECTS / (pid or project["id"])


def save():
    _write_json(project_dir() / "project.json", project)


def save_settings():
    _write_json(SETTINGS_FILE, settings)


def open_project(pid):
    global project
    data = _read_json(project_dir(pid) / "project.json", None)
    if not data:
        raise HTTPException(404, "Project not found")
    project = data
    for k, v in PROJECT_DEFAULTS.items():
        project.setdefault(k, v.copy() if isinstance(v, (dict, list)) else v)
    for c in project["clips"]:  # clips added before v0.2 lack the start timecode NLE exports need
        if "timecode" not in c and Path(c["path"]).exists():
            c["timecode"] = video_engine.probe(c["path"]).get("timecode")
            save()
        if "recorded" not in c:  # v0.4: recording time, so clips can be put in story order
            info = video_engine.probe(c["path"]) if Path(c["path"]).exists() and not stamp(c["name"]) else {}
            c["recorded"] = recorded_at(c["name"], info.get("created"))
            save()
    settings["current_project"] = pid
    save_settings()


def new_project(name):
    pid = uuid.uuid4().hex[:8]
    project_dir(pid).mkdir(parents=True)
    _write_json(project_dir(pid) / "project.json",
                {"id": pid, "name": name.strip() or "Untitled ride", "created": time.time(), "target_length": "",
                 "clips": []})
    open_project(pid)


def list_projects():
    out = []
    for f in PROJECTS.glob("*/project.json"):
        p = _read_json(f, None)
        if p:
            out.append({"id": p["id"], "name": p["name"], "created": p.get("created", 0), "clips": len(p["clips"]),
                        "duration": sum(c["duration"] for c in p["clips"]),
                        "analyzed": any(c.get("segments") for c in p["clips"])})
    return sorted(out, key=lambda p: -p["created"])


if settings["current_project"] and (project_dir(settings["current_project"]) / "project.json").exists():
    open_project(settings["current_project"])
elif list_projects():
    open_project(list_projects()[0]["id"])
else:
    new_project("My first project")


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    job["logs"].append(line)


def step(name, progress):
    check_cancel()  # every progress report is also a cancel point
    job["step"], job["progress"] = name, round(progress, 4)


def start_job(name, fn):
    with lock:
        if job["running"]:
            raise HTTPException(409, f"'{job['name']}' is still running.")
        video_engine.reset_cancel()
        job.update(running=True, name=name, step="Starting…", progress=0.0, logs=[], error=None, result=None)

    def run():
        try:
            job["result"] = fn()
            job["step"], job["progress"] = "Done", 1.0
            log(f"{name} finished.")
        except Cancelled:
            job["error"], job["step"] = "Cancelled", "Cancelled"
            log("Cancelled. Finished work is kept: transcripts, AI scores and rendered parts are reused next time.")
        except Exception as e:  # surface every failure in the UI console instead of a silent dead thread
            job["error"] = str(e)
            job["step"] = "Failed"
            log(f"ERROR: {e}")
        finally:
            job["running"] = False

    threading.Thread(target=run, daemon=True).start()
    return {"ok": True}


def clip(clip_id):
    for c in project["clips"]:
        if c["id"] == clip_id:
            return c
    raise HTTPException(404, "Clip not found")


def main_clips():
    """Clips that make up the story; synced camera angles ride along in the NLE exports only."""
    return [c for c in project["clips"] if not c.get("angle_of")]


def parse_target(text):
    """'10-15 minutes' -> 750 s (midpoint); '12' -> 720 s; '' -> None."""
    nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", text or "")]
    return sum(nums) / len(nums) * 60 if nums else None


def add_clip(path: Path, name: str, owned: bool):
    """Add a clip in recording order: a clip shot earlier than ones already in the project goes before them."""
    info = video_engine.probe(path)
    c = {"id": uuid.uuid4().hex[:10], "name": name, "path": str(path), "owned": owned,
         "size": path.stat().st_size, **info, "segments": None, "whisper": None, "broll": [],
         "recorded": recorded_at(name, info.get("created"))}
    later = next((x for x in main_clips() if c["recorded"] and x.get("recorded") and x["recorded"] > c["recorded"]), None)
    project["clips"].insert(project["clips"].index(later) if later else len(project["clips"]), c)
    return c


def _last_chapter(c):
    return (c.get("chapters") or [{"path": c["path"], "name": c["name"], "duration": c["duration"],
                                   "timecode": c.get("timecode"), "offset": 0.0}])[-1]


def chapter_follows(prev, new):
    """Is `new` the next chapter of `prev`? Cameras split long recordings (GoPro at ~4 GB, DJI by time)."""
    if prev.get("angle_of") or (prev["width"], prev["height"]) != (new["width"], new["height"]) \
            or abs(prev["fps"] - new["fps"]) > 0.02:
        return False
    last = _last_chapter(prev)
    gp = lambda name: re.match(r"G[HXOP](\d\d)(\d{4})$", Path(name).stem.upper())
    a, b = gp(last["name"]), gp(new["name"])
    if a and b:  # GoPro: GH01 0042 -> GH02 0042
        return a[2] == b[2] and int(b[1]) == int(a[1]) + 1
    if last.get("timecode") and new.get("timecode"):  # timecode continues where the previous file ended
        drop = ";" in new["timecode"]
        end = exporters.tc_to_frames(last["timecode"], new["fps"], drop) + exporters.frames(last["duration"], new["fps"])
        return abs(exporters.tc_to_frames(new["timecode"], new["fps"], drop) - end) <= 3
    return False


def join_chapter(prev, new):
    """Treat two camera files as one continuous clip, read through an FFmpeg concat list."""
    chapters = prev.get("chapters") or [_last_chapter(prev)]
    off = chapters[-1]["offset"] + chapters[-1]["duration"]
    chapters.append({"path": new["path"], "name": new["name"], "duration": new["duration"],
                     "timecode": new.get("timecode"), "offset": off})
    listing = CACHE / f"{prev['id']}.ffconcat"
    listing.write_text("ffconcat version 1.0\n" + "".join(
        "file '%s'\nduration %.6f\n" % (Path(ch["path"]).resolve().as_posix().replace("'", "'\\''"), ch["duration"])
        for ch in chapters), encoding="utf-8")
    first = chapters[0]["name"]
    prev.update(chapters=chapters, path=str(listing), duration=off + new["duration"], size=prev["size"] + new["size"],
                name=f"{first} (+{len(chapters) - 1} chapter{'s' if len(chapters) > 2 else ''})", segments=None,
                whisper=None, broll=[])
    (CACHE / f"{prev['id']}.wav").unlink(missing_ok=True)
    project["clips"].remove(new)
    log(f"Joined {new['name']} onto {first} as chapter {len(chapters)}.")


def maybe_join(new):
    mains = main_clips()
    prev = mains[mains.index(new) - 1] if mains.index(new) > 0 else None  # the clip recorded just before
    if settings["auto_join"] and prev and chapter_follows(prev, new):
        join_chapter(prev, new)
        return prev
    return new


def wav_for(c):
    wav = CACHE / f"{c['id']}.wav"
    if not wav.exists():
        if not c["has_audio"]:
            raise RuntimeError(f"{c['name']} has no audio track.")
        log(f"Isolating audio from {c['name']}")
        video_engine.extract_audio(c["path"], wav)
    return wav


def out_dir() -> Path:
    d = Path(settings["output_dir"]).expanduser() if settings["output_dir"] else project_dir() / "output"
    d.mkdir(parents=True, exist_ok=True)
    return d


def out_base() -> str:
    slug = re.sub(r"[^\w-]+", "_", project["name"]).strip("_") or "leancut"
    name = (settings["output_name"] or "{project}").replace("{project}", slug).replace("{date}", time.strftime("%Y-%m-%d"))
    return re.sub(r'[\\/:*?"<>|]+', "_", name)


# ---------------------------------------------------------------- optional modules (models download on first use)

def _optional(name):
    try:
        return __import__(name)
    except ImportError:  # module or one of its dependencies missing in this build
        return None


MODELS = WORK / "models"


def taste():
    return _read_json(WORK / "taste.json", [])


def remember_taste(entries):
    """Lines the creator kept or cut by hand become few-shot examples for future scoring."""
    if entries:
        _write_json(WORK / "taste.json", (taste() + entries)[-200:])


def locate(c, t):
    """(file, local time) for a time on a clip; joined camera chapters map to the chapter file that holds t."""
    for ch in c.get("chapters") or []:
        if t < ch["offset"] + ch["duration"]:
            return ch["path"], t - ch["offset"]
    if c.get("chapters"):
        last = c["chapters"][-1]
        return last["path"], t - last["offset"]
    return c["path"], t


def clip_stats(c):
    stats = media_analysis.keyframe_stats(c["path"], CACHE / f"{c['id']}.keyframes9.json")
    motion = sorted(r.get("motion", 0) for r in stats) or [0]
    c["auto_grade"] = editing.auto_grade_params(stats)
    c["shake"] = round(max(0.0, min(1.0, (motion[int(len(motion) * 0.9)] - 0.15) / 0.3)), 2)
    return stats


def clip_telemetry(c):
    """GoPro GPS highlights and boring stretches (empty for cameras without GPMF telemetry)."""
    tel = _optional("telemetry")
    if not tel or not settings["telemetry"]:
        return {"highlights": [], "boring": []}
    out = {"highlights": [], "boring": []}
    for ch in c.get("chapters") or [{"path": c["path"], "offset": 0, "duration": c["duration"]}]:
        try:
            if not tel.has_gpmf(ch["path"]):
                continue
            data = tel.read_gopro(ch["path"])
        except RuntimeError:
            continue
        for k, fn in (("highlights", tel.highlights), ("boring", tel.boring)):
            out[k] += [{**w, "start": w["start"] + ch["offset"], "end": w["end"] + ch["offset"]}
                       for w in fn(data, ch["duration"])]
    return out


# ---------------------------------------------------------------- pipeline: analyze

def analyze():
    s = settings
    clips = main_clips()
    if not clips:
        raise RuntimeError("Add at least one clip first.")
    if not s["model"]:
        raise RuntimeError("Choose an LLM model in Settings › AI models.")

    step("Checking LLM server…", 0.0)
    models = llm_agent.list_models(s["provider"], s["base_url"])  # fail fast, before an hour of transcription
    if s["model"] not in models:
        raise RuntimeError(f"Model '{s['model']}' is not available on the server. Available: {', '.join(models) or 'none'}")
    llm_agent.check_fits(s["provider"], s["base_url"], s["model"])

    whisper = s["custom_whisper"].strip() or s["whisper_size"]
    whisper_key = f"{whisper}/{s['language']}"  # transcripts are cached per model + language
    todo = [c for c in clips if c["segments"] is None or c["whisper"] != whisper_key]
    for i, c in enumerate(todo):
        step(f"Extracting audio — {c['name']}", 0.06 * i / len(todo))
        wav_for(c)
    if todo:
        log(f"Transcribing {len(todo)} clip(s) with Whisper '{whisper}', language "
            f"{transcriber.LANGUAGES.get(s['language'], s['language'])} (VAD on) — first run downloads the model.")

        def prog(i, f):
            step(f"Transcribing clip {i + 1}/{len(todo)} — {todo[i]['name']}", 0.06 + 0.40 * (i + f) / len(todo))

        wavs = [CACHE / f"{c['id']}.wav" for c in todo]
        for i, segs in transcriber.transcribe_files(whisper, wavs, s["language"], log, prog):
            c = todo[i]
            c["segments"], c["whisper"] = segs, whisper_key
            talk = sum(x["end"] - x["start"] for x in segs)
            log(f"{c['name']}: {len(segs)} speech segments, {talk / 60:.1f} of {c['duration'] / 60:.1f} min is talking.")
            save()  # resume point: a cancel after this keeps the clip's transcript
    else:
        log("Transcripts are cached — skipping Whisper.")

    step("Measuring shots (color, motion, telemetry)…", 0.47)
    stats_by = {c["id"]: clip_stats(c) for c in clips}
    tel_by = {c["id"]: clip_telemetry(c) for c in clips}
    n_hl = sum(len(t["highlights"]) for t in tel_by.values())
    if n_hl:
        log(f"GoPro telemetry: {n_hl} highlight(s) (braking, twisties, top speed, summits).")

    # transcript clean-up: whole thoughts, retakes / Whisper repeat loops, spoken "mark this" cues
    known, boost, markers = {}, {}, {}
    model_key = f"{s['provider']}:{s['model']}:{SCORE_VERSION}"
    refs, flat = [], []
    for ci, c in enumerate(clips):
        c["segments"] = transcriber.flag_retakes(transcriber.merge_fragments(c["segments"]))
        markers[c["id"]] = transcriber.find_markers(c["segments"], s["markers"])
        for seg in c["segments"]:
            sid = len(flat)
            refs.append((c, seg))
            flat.append({"id": sid, "clip": ci + 1, **seg})
            if seg.get("auto") == "retake":
                known[sid] = (1, "earlier take of the next line")
            elif transcriber.is_marker_only(seg["text"], s["markers"]):
                known[sid] = (1, "spoken marker, not for viewers")
            elif any(t - 30 <= seg["start"] <= t for t in markers[c["id"]]):
                known[sid] = (5, "you marked this moment")
            elif seg.get("scored_by") == model_key and "score" in seg:
                known[sid] = (seg["score"], seg.get("reason", ""))
    if not flat:
        raise RuntimeError("No speech was detected in any clip, so there is nothing for the AI to edit.")
    n_markers = sum(map(len, markers.values()))
    retakes = sum(1 for v in known.values() if v[1].startswith("earlier take"))
    if n_markers or retakes:
        log(f"Found {n_markers} spoken marker(s) and {retakes} retake(s).")

    if s["moments"]:
        step("Listening for loud moments…", 0.5)
        for c in clips:
            c["moments"] = media_analysis.loud_moments(wav_for(c))
    for sid, (c, seg) in enumerate(refs):
        hit = [m for m in c.get("moments") or [] if m["start"] < seg["end"] and m["end"] > seg["start"]]
        hl = [h for h in tel_by[c["id"]]["highlights"] if h["start"] < seg["end"] and h["end"] > seg["start"]]
        if hit or hl:
            boost[sid] = (1, "loud moment" if hit else hl[0]["kind"].replace("_", " "))

    engine = s["engine"]
    log("AI scoring with the " + ("decision engine (token probabilities)" if engine != "batch" else "batch scorer"))
    decisions = llm_agent.decide(flat, s["provider"], s["base_url"], s["model"], None, log,
                                 lambda f: step("AI scoring…", 0.52 + 0.28 * f), known=known, engine=engine,
                                 taste=taste())
    for sid, d in decisions.items():  # loud moments / riding highlights nudge scores before the budget is applied
        if sid in boost and sid not in known:
            d["score"] = min(5, round(d["score"] + boost[sid][0], 1))
            d["reason"] = (d["reason"] + " · " + boost[sid][1]).strip(" ·")
    ratio_target = parse_target(project["target_length"])
    total = sum(x["end"] - x["start"] for x in flat)
    keep = llm_agent.select({x["id"]: x["end"] - x["start"] for x in flat},
                            {i: d["score"] for i, d in decisions.items()}, ratio_target / total if ratio_target else None)
    for sid, (c, seg) in enumerate(refs):
        d = decisions[sid]
        seg.update(keep=sid in keep, score=d["score"], reason=d["reason"])
        if "conf" in d:
            seg["conf"] = d["conf"]
        if sid not in known or known[sid][1] == seg.get("reason"):
            seg["scored_by"] = model_key
    save()
    speech_kept = sum(seg["end"] - seg["start"] for c, seg in refs if seg["keep"])

    # scenic B-roll for the silent stretches (plus anything the rider marked out loud)
    for c in clips:
        c["broll"] = []
    if s["broll"]:
        cands = []
        for c in clips:
            windows = media_analysis.broll_candidates(c["duration"], c["segments"], stats_by[c["id"]])
            for w in windows:
                if any(b["start"] < w["end"] and b["end"] > w["start"] for b in tel_by[c["id"]]["boring"]):
                    w["interest"] = 0.0  # motorway / red light
                if any(h["start"] < w["end"] and h["end"] > w["start"] for h in tel_by[c["id"]]["highlights"]):
                    w["interest"] = min(1.0, w["interest"] + 0.3)
            cands += [(c["id"], w) for w in windows]
        cands.sort(key=lambda x: -x[1]["interest"])
        by_clip = {c["id"]: c for c in clips}
        if s["vision_model"]:
            judge_broll(cands[:16], by_clip)
            cands.sort(key=lambda x: -x[1]["interest"])
        share = max(0, min(60, float(project.get("broll_share", s["broll_share"])))) / 100
        budget = speech_kept * share / (1 - share) if share < 1 else 0
        picked = {id(w) for _, w in media_analysis.pick_broll(cands, budget)}
        for cid, w in cands[:max(12, len(picked) * 2)]:  # keep some runners-up so you can tick them in
            by_clip[cid]["broll"].append({**w, "keep": id(w) in picked,
                                          "reason": w.get("desc") or f"scenic · {w['interest']:.0%}"})
    for c in clips:
        for t in markers[c["id"]]:
            c["broll"].append({"start": round(max(0.0, t - 30), 2), "end": t, "interest": 1.0, "keep": True,
                               "reason": "you said a marker phrase"})
        c["broll"].sort(key=lambda w: w["start"])
    over, dropped = editing.place_broll(project, s["broll_style"])
    if over or dropped:
        log(f"B-roll placed in story order: {over} shot(s) laid over the talking of their own scene"
            + (f", {dropped} dropped for being far from anything you say" if dropped else "") + ".")
    save()
    broll_kept = sum(w["end"] - w["start"] for c in clips for w in c["broll"] if w["keep"] and not w.get("over"))
    log(f"Kept {sum(1 for _, x in refs if x['keep'])}/{len(refs)} lines ≈ {speech_kept / 60:.1f} min of talking"
        + (f" + {broll_kept / 60:.1f} min of scenic B-roll" if broll_kept else "") + ".")
    step("Writing suggestions…", 0.92)
    make_suggestions()
    unsure = sum(1 for _, x in refs if x.get("conf") is not None and x["conf"] < 0.55)
    log(f"{len(project['suggestions'])} suggestion(s) ready" + (f", including {unsure} line(s) the AI is unsure about"
                                                                 if unsure else "") + ". Review them in the assistant.")


def frame_b64(c, t, width=448):
    import base64
    path, local = locate(c, t)
    raw = video_engine._ffmpeg(["-ss", f"{max(0.0, local):.3f}", *video_engine.inp(path), "-frames:v", "1",
                                "-vf", f"scale={width}:-2", "-f", "mjpeg", "-"], binary=True).stdout
    return base64.b64encode(raw).decode()


def judge_broll(cands, by_clip):
    """Local vision model looks at each candidate's middle frame: a description plus a scenic score that's blended
    into the motion/colour interest."""
    s = settings
    log(f"Visual judge ({s['vision_model']}) is looking at {len(cands)} B-roll candidate(s)…")
    for k, (cid, w) in enumerate(cands, 1):
        try:
            desc, scenic, conf = llm_agent.look_at(s["provider"], s["base_url"], s["vision_model"],
                                                   frame_b64(by_clip[cid], (w["start"] + w["end"]) / 2))
        except llm_agent.LLMError as e:
            log(f"Visual judge stopped: {e}")
            return
        w["desc"], w["interest"] = desc, round(0.5 * w["interest"] + 0.5 * (scenic - 1) / 4, 3)
        step("Visual judge…", 0.86 + 0.05 * k / len(cands))


def _noop(a):
    hits = [editing.find(project, i) for i in a.get("ids") or []]
    if not hits or None in hits:
        return False
    t = a.get("type")
    if t in ("keep", "cut"):
        return all(bool(h[3].get("keep")) == (t == "keep") for h in hits)
    if t == "effect":
        return all(h[3].get("effect", "none") == a.get("value") for h in hits)
    if t == "transition":
        return all((h[3].get("transition") or {}).get("type") == a.get("value") for h in hits)
    return False


def make_suggestions(use_llm=True):
    """Rule-based suggestions (always) + creative ones from the LLM (when it answers), minus no-ops."""
    out = editing.heuristic_suggestions(project)
    if use_llm:
        try:
            for x in llm_agent.suggest(editing.context(project), settings["provider"], settings["base_url"],
                                       settings["model"], editing.ACTION_SCHEMA):
                out.append({**x, "source": "ai"})
        except llm_agent.LLMError as e:
            log(f"AI suggestions skipped: {e}")
    seen, clean = set(), []
    for x in out:
        key = (x["action"].get("type"), tuple(x["action"].get("ids") or x.get("ids") or ()), x["action"].get("value"))
        if key in seen or _noop(x["action"]):
            continue
        seen.add(key)
        clean.append({**x, "sid": uuid.uuid4().hex[:8], "status": "open"})
    project["suggestions"] = clean
    save()
    return clean


# ---------------------------------------------------------------- pipeline: export

def program_parts():
    """Parts in output order, with fast-forward ride-throughs inserted when enabled."""
    parts = editing.build_parts(project, settings)
    if settings["connectors"]:
        out = []
        for c in main_clips():
            mine = [p for p in parts if p["clip"] is c]
            ff = [{"clip": c, "start": a, "end": b, "speed": sp, "effect": "none", "grade": {}, "transition": None,
                   "ids": [], "texts": [], "voiceovers": [], "overlays": [], "silent": True}
                  for a, b, sp in media_analysis.connectors([(p["start"], p["end"]) for p in mine], c["duration"])]
            out += sorted(mine + ff, key=lambda p: p["start"])
        parts = out
    for kind, p in (("intro", parts[0] if parts else None), ("outro", parts[-1] if parts else None)):
        card = project.get(kind)
        if p and card and card.get("over"):  # titles over the first / last shot instead of a card of their own
            d = min(float(card["duration"]), editing.part_duration(p))  # output seconds
            at = p["start"] if kind == "intro" else max(p["start"], p["end"] - d * p["speed"])
            style = "lower_third" if card.get("style") == "minimal" else "title"
            p["texts"] = [{"text": card["title"], "style": style, "at": at, "duration": d, "card": kind},
                          *([{"text": card["subtitle"], "style": "caption", "at": at + 0.4 * p["speed"], "y": 0.62,
                              "x": 0.5, "duration": d - 0.4, "card": kind}]
                            if card.get("subtitle") and style == "title" else []), *p["texts"]]
    return spread_graphics(parts)


def spread_graphics(parts):
    """A graphic is anchored to a line but lasts its own length, across cuts: each part gets the slice of every
    graphic that's on screen during it, as {..., at (source time in the part), in (seconds into the graphic),
    duration (of the slice), full (the graphic's whole length)}."""
    t, starts = 0.0, []
    for p in parts:
        starts.append(t)
        t += editing.part_duration(p)
    spans = [(starts[k] + (g["at"] - p["start"]) / p["speed"], g) for k, p in enumerate(parts) for g in p.get("graphics") or []]
    for k, p in enumerate(parts):
        p["graphics"] = []
        a0, b0 = starts[k], starts[k] + editing.part_duration(p)
        for g0, g in spans:
            full = float(g.get("duration", 4))
            a, b = max(g0, a0), min(g0 + full, b0)
            if b - a > 0.04:
                p["graphics"].append({**g, "at": p["start"] + (a - a0) * p["speed"], "in": round(a - g0, 3),
                                      "duration": round(b - a, 3), "full": full})
    return parts


def card_of(kind):
    """The intro/outro shown as a card of its own (None when it is laid over the video)."""
    c = project.get(kind)
    return c if c and not c.get("over") else None


def is_plain(parts):
    """Nothing to bake in: the lossless stream-copy path is allowed."""
    g = project.get("grade") or {}
    return not (g.get("auto") or g.get("preset", "none") != "none" or g.get("manual") or project.get("intro")
                or project.get("outro") or project.get("captions") or project.get("privacy_blur")
                or (project.get("cut_style") or {}).get("type", "cut") != "cut"
                or any(p["effect"] != "none" or p["transition"] or p["texts"] or p["grade"] or p.get("silent")
                       for p in parts))


def voice_track(text):
    """Synthesise a voice-over line (cached by text + voice), in the creator's cloned voice when available."""
    voice = _optional("voice")
    if not voice:
        raise RuntimeError("Voice-over needs the voice module (voice.py).")
    s = settings
    key = editing.text_key(f"{text}|{s['tts_engine']}|{s['voice_sample']}|{s['tts_server']}|{s['tts_voice']}")
    out = CACHE / "voice" / f"{key}.wav"
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        engine = s["tts_engine"]
        if engine == "auto":
            avail = voice.engines_available(s["tts_server"] or None)
            engine = next((e for e in ("chatterbox", "server", "system") if avail.get(e)), None)
            if not engine:
                raise RuntimeError("No voice engine is available. See Settings › Voice.")
        voice.synthesize(text, out, engine, voice_sample=s["voice_sample"] or None,
                         server_url=s["tts_server"] or None, server_voice=s["tts_voice"] or None)
    return out


def render_program(out_path, fmt, preview=False):
    """Render the current edit (looks, transitions, intro/outro, audio) to out_path. Returns (path, parts, starts)."""
    s = settings
    parts = program_parts()
    if not parts:
        raise RuntimeError("No segments are marked to keep. Run the auto-edit or tick some lines first.")
    wd, font = production.prepare(CACHE / "render")
    if not font and (project.get("captions") or project.get("intro") or any(p["texts"] for p in parts)):
        log("Titles and captions skipped: this FFmpeg has no drawtext filter or no font was found.")
    vision = _optional("vision") if project.get("privacy_blur") else None
    if project.get("privacy_blur") and not vision:
        log("Face & plate blur needs the vision module; skipped.")
    specs = []
    for k, p in enumerate(parts):
        step(f"Preparing part {k + 1}/{len(parts)}…", 0.02 + 0.08 * k / len(parts))
        blur = trf = ""
        path, local = locate(p["clip"], p["start"])
        if vision and not p.get("silent"):
            boxes = vision.privacy_boxes(path, local, local + p["end"] - p["start"], MODELS,
                                         kinds=tuple(x.strip() for x in s["privacy_kinds"].split(",") if x.strip()))
            blur = vision.blur_filter(boxes, local, p["clip"]["width"], p["clip"]["height"])
        if p["effect"] == "stabilise" and not preview:
            trf = video_engine.stabilise_detect(p["clip"]["path"], p["start"], p["end"],
                                                f"trf/{editing.text_key([p['clip']['path'], p['start'], p['end']])}.trf", wd)
        sp = production.part_spec(p, project, s, fmt, wd, font, blur, trf, locate, graphic_clip_for(fmt))
        if p.get("silent"):
            sp["silent"] = True
        specs.append(sp)
    specs = production.with_transitions(specs, parts, project)
    best = max((w for c in main_clips() for w in c.get("broll") or [] if w.get("keep")),
               key=lambda w: w.get("interest", 0), default=None)
    bg_clip = next((c for c in main_clips() if best and best in c.get("broll", [])), parts[0]["clip"])
    bg = (bg_clip["path"], best["start"] if best else parts[0]["start"])
    if card_of("intro"):
        specs.insert(0, production.card_spec(project["intro"], fmt, wd, font, bg))
        parts.insert(0, {"card": True, "clip": None, "start": 0, "end": project["intro"]["duration"], "speed": 1,
                         "voiceovers": [], "ids": []})
        specs[1]["transition"] = specs[1].get("transition") or {"xfade": "fade", "dur": 0.5}
    if card_of("outro"):
        last = parts[-1]
        out_bg = (last["clip"]["path"], max(0.0, last["end"] - project["outro"]["duration"])) if last.get("clip") else bg
        specs.append({**production.card_spec(project["outro"], fmt, wd, font, out_bg), "transition": {"xfade": "fade", "dur": 0.6}})
        parts.append({"card": True, "clip": None, "start": 0, "end": project["outro"]["duration"], "speed": 1,
                      "voiceovers": [], "ids": []})
    tmp = Path(out_path).with_name("." + Path(out_path).stem + ".joined.mp4")
    _, starts = video_engine.render_parts(specs, tmp, fmt, "auto" if preview else s["encoder"],
                                          CACHE / ("preview-parts" if preview else "parts"), str(wd),
                                          lambda f: step("Rendering…", 0.1 + f * (0.8 if preview else 0.6)))
    voiceovers = []
    for p, t0 in zip(parts, starts):
        for v in p.get("voiceovers") or []:
            try:
                voiceovers.append((voice_track(v["text"]), round(t0 + (v["at"] - p["start"]) / p["speed"], 3)))
            except RuntimeError as e:
                log(f"Voice-over skipped: {e}")
    bleeps = production.bleep_windows([p for p in parts], starts, s, project)
    voice_wav = None
    if s["voice_enhance"] and not preview:
        enhance = _optional("enhance")
        if enhance:
            step("Enhancing voice…", 0.72)
            voice_wav = enhance.enhance_video_audio(tmp, CACHE / "render" / "enhanced.wav", MODELS)
        else:
            log("Voice enhancement needs the enhance module; skipped.")
    music = s["music_path"] if s["music_path"] and Path(s["music_path"]).is_file() else None
    step("Finishing audio…", 0.8)
    video_engine.finish_audio(tmp, out_path, s["loudness"], s["denoise"], music,
                              project.get("music_volume", s["music_volume"]), voiceovers, bleeps, voice_wav, str(wd))
    if voice_wav:
        Path(voice_wav).unlink(missing_ok=True)
    return Path(out_path), parts, starts


def chapter_clip(c, k):
    ch = c["chapters"][k]
    return {**c, "id": f"{c['id']}#{k}", "path": ch["path"], "duration": ch["duration"], "timecode": ch.get("timecode")}


def nle_entries(parts):
    """Rendered parts -> edit-list entries that carry the look: transition into each, effect, speed, grade, titles
    (seconds from the entry's start), cutaway B-roll, and intro/outro cards."""
    body = [p for p in parts if not p.get("card")]
    out = []
    for i, p in enumerate(body):
        if p.get("silent"):  # fast-forward ride-throughs aren't footage an NLE editor wants back
            continue
        sp = p["speed"]
        overlays = []
        for o in p.get("overlays") or []:
            oc = o["clip"]
            if oc.get("chapters"):
                k = next(k for k, ch in enumerate(oc["chapters"]) if ch["path"] == locate(oc, o["start"])[0])
                oc, a = chapter_clip(oc, k), locate(oc, o["start"])[1]
            else:
                a = o["start"]
            overlays.append({"clip": oc, "start": a, "end": a + o["end"] - o["start"],
                             "at": (o["at"] - p["start"]) / sp, "dur": o["end"] - o["start"]})
        out.append({"clip": p["clip"], "start": p["start"], "end": p["end"], "speed": sp, "effect": p["effect"],
                    "transition": editing.transition_of(project, p, i),
                    "grade": editing.project_grade(project, p["clip"], p.get("grade")),
                    "texts": [{"text": t["text"], "style": t["style"], "t0": (t["at"] - p["start"]) / sp,
                               "t1": (t["at"] - p["start"]) / sp + float(t.get("duration", 4)),
                               **{k: t[k] for k in ("x", "y", "size") if t.get(k) is not None}} for t in p["texts"]],
                    "overlays": overlays})
    return out


def expand_chapters(tl):
    """Edit lists reference real files: split entries of joined clips at chapter boundaries, then add the
    intro/outro cards (they have no clip of their own)."""
    out = []
    for e in tl:
        c = e["clip"]
        if not c.get("chapters"):
            out.append(e)
            continue
        first = True
        for k, ch in enumerate(c["chapters"]):
            a, b = max(e["start"], ch["offset"]), min(e["end"], ch["offset"] + ch["duration"])
            if b - a > 0.05:
                piece = {**e, "clip": chapter_clip(c, k), "start": a - ch["offset"], "end": b - ch["offset"]}
                if not first:  # later pieces continue the same shot: no transition, titles and B-roll shift
                    shift = (a - e["start"]) / e["speed"]
                    piece.update(transition=None,
                                 texts=[{**t, "t0": t["t0"] - shift, "t1": t["t1"] - shift} for t in e.get("texts") or []],
                                 overlays=[{**o, "at": o["at"] - shift} for o in e.get("overlays") or []])
                out.append(piece)
                first = False
    rec = 0.0
    for e in out:
        d = (e["end"] - e["start"]) / e["speed"]
        e["rec_start"], e["rec_end"], rec = rec, rec + d, rec + d
    if card_of("intro"):
        if out:
            out[0]["transition"] = out[0].get("transition") or {"type": "crossfade", "dur": 0.5}
        out.insert(0, {"card": project["intro"], "clip": None, "speed": 1})
    if card_of("outro"):
        out.append({"card": project["outro"], "clip": None, "speed": 1, "transition": {"type": "crossfade", "dur": 0.6}})
    return out


def export():
    s = settings
    clips = main_clips()
    if not clips:
        raise RuntimeError("Add at least one clip first.")
    fmt = {k: clips[0][k] for k in ("width", "height", "fps")}
    folder, base = out_dir(), out_base()
    title = project["name"]
    files = []
    angles = [(c, c["angle_of"], c["sync_offset"]) for c in project["clips"] if c.get("angle_of")]
    parts = program_parts()
    if not parts:
        raise RuntimeError("No segments are marked to keep. Run the auto-edit or tick some lines first.")
    starts = None
    if s["out_video"]:
        fast = s["render_mode"] == "fast" and is_plain(parts)
        if s["render_mode"] == "fast" and not fast:
            log("Fast mode can't apply looks, titles or transitions; rendering frame-accurately instead.")
        enc = video_engine.hardware_encoder() if s["encoder"] != "software" else None
        log(f"Rendering {len(parts)} parts ({'fast stream copy' if fast else 'frame-accurate, ' + (enc or 'libx264')}) "
            f"at {fmt['width']}x{fmt['height']} @ {fmt['fps']:g} fps")
        if fast:
            tmp = folder / f".{base}.joined.mp4"
            video_engine.render([(p["clip"]["path"], p["start"], p["end"]) for p in parts], tmp, False, fmt,
                                lambda f: step("Stitching video…", f * 0.7), s["encoder"], cache_dir=CACHE / "parts")
            music = s["music_path"] if s["music_path"] and Path(s["music_path"]).is_file() else None
            files.append(video_engine.finish_audio(tmp, folder / f"{base}.mp4", s["loudness"], s["denoise"], music,
                                                   project.get("music_volume", s["music_volume"])))
        else:
            path, parts, starts = render_program(folder / f"{base}.mp4", fmt)
            files.append(path)
    if starts is None:  # no video rendered: lay the parts end to end
        starts, t = [], 0.0
        for p in parts:
            starts.append(t)
            t += editing.part_duration(p)
    prog = production.programme(parts, starts)
    nle = expand_chapters(nle_entries(parts))
    chapters = []
    if s["out_youtube"]:
        step("Writing title, description and chapters…", 0.9)
        lines = exporters.chapter_lines(prog)
        try:
            meta = llm_agent.youtube_metadata(lines, s["provider"], s["base_url"], s["model"])
            end = prog[-1]["rec_end"] if prog else 0
            chapters = exporters.clean_chapters(meta["chapters"], lines, end)
            if project.get("intro") and chapters:
                chapters[0] = (0.0, chapters[0][1])
            files.append(exporters.write_youtube_txt(meta, chapters, folder / f"{base}_youtube.txt"))
        except llm_agent.LLMError as e:
            log(f"Skipped YouTube metadata: {e}")
    if s["out_edl"]:
        files.append(exporters.write_edl(nle, folder / f"{base}.edl", fmt["fps"], title))
    if s["out_fcpxml"]:
        files.append(exporters.write_fcpxml(nle, folder / f"{base}.fcpxml", fmt, title, angles, chapters))
    if s["out_otio"]:
        files.append(exporters.write_otio(nle, folder / f"{base}.otio", fmt, title, angles))
    if s["out_srt"]:
        extra = [w.strip().lower() for w in s["profanity_words"].split(",") if w.strip()]
        files.append(exporters.write_srt(prog, folder / f"{base}.srt",
                                         (lambda t: production.mask(t, extra)) if s["bleep"] else None))
    if s["out_thumbs"]:
        step("Picking thumbnails…", 0.93)
        cands = sorted([(locate(c, (x["start"] + x["end"]) / 2)[0], locate(c, (x["start"] + x["end"]) / 2)[1],
                         x.get("score", 4)) for c in clips for x in (c["segments"] or []) + c.get("broll", [])
                        if x.get("keep")], key=lambda r: -r[2])
        files += media_analysis.thumbnails(cands, folder, name=f"{base}_thumb")
    if s["out_shorts"]:
        files += render_shorts(clips, fmt, folder, base)
    log("Saved to " + str(folder) + ": " + ", ".join(Path(f).name for f in files))
    return {"folder": str(folder), "files": [{"name": Path(f).name, "url": f"/api/output/{Path(f).name}"} for f in files]}


def render_shorts(clips, fmt, folder, base):
    """Vertical Shorts from the best continuous moments; the crop follows the subject when the vision module
    can track it, else stays centred."""
    s = settings
    files = []
    vision = _optional("vision")
    wins = media_analysis.shorts_windows(clips, n=int(s["shorts_count"]))
    for i, (c, a, b, value) in enumerate(wins, 1):
        step(f"Rendering Short {i}/{len(wins)}…", 0.94 + 0.05 * i / len(wins))
        path, local = locate(c, a)
        x = "(iw-ow)/2"
        if vision:
            try:
                track = vision.subject_track(path, local, local + (b - a), MODELS)
                x = production.crop_x_expr(track, b - a)
            except Exception as e:  # tracking is a nicety; never lose the Short over it
                log(f"Subject tracking skipped: {e}")
        blur = ""
        if vision and project.get("privacy_blur"):
            kinds = tuple(k.strip() for k in s["privacy_kinds"].split(",") if k.strip())
            boxes = vision.privacy_boxes(path, local, local + (b - a), MODELS, kinds=kinds)
            blur = vision.blur_filter(boxes, local, c["width"], c["height"])
        vf = ",".join(x for x in (blur, f"scale=-2:1920,crop=1080:1920:{x}:0,setsar=1,fps={fmt['fps']},format=yuv420p") if x)
        tmp = folder / f".{base}_short_{i}.mp4"
        video_engine.render([(path, local, local + (b - a))], tmp, True, {**fmt, "width": 1080, "height": 1920},
                            encoder=s["encoder"], vf=vf, cache_dir=CACHE / "parts")
        files.append(video_engine.finish_audio(tmp, folder / f"{base}_short_{i}.mp4", s["loudness"]))
        log(f"Short {i}: {c['name']} {a:.0f}–{b:.0f}s ({b - a:.0f}s, score {value})")
    return files


def run_queue(pids, steps):
    """Overnight batch: analyze and/or export several projects in a row; one failure doesn't stop the rest."""
    failed = []
    for n, pid in enumerate(pids, 1):
        open_project(pid)
        log(f"━━ Project {n}/{len(pids)}: {project['name']}")
        try:
            if "analyze" in steps:
                analyze()
            if "export" in steps:
                export()
        except Cancelled:
            raise
        except Exception as e:
            failed.append(project["name"])
            log(f"ERROR in {project['name']}: {e}")
    if failed:
        raise RuntimeError(f"{len(failed)} project(s) failed: {', '.join(failed)}")


# ---------------------------------------------------------------- API

api = FastAPI(title="LeanCut")


@api.middleware("http")
async def local_only(request, call_next):
    """The API has no login, so it only answers its own UI: a web page on another site can neither reach it through
    DNS rebinding (wrong Host) nor post to it cross-site (foreign Origin)."""
    from fastapi.responses import JSONResponse
    host = (request.headers.get("host") or "").rsplit(":", 1)[0]
    origin = request.headers.get("origin")
    own = {"127.0.0.1", "localhost", "[::1]"}
    if host not in own or (origin and origin.split("://")[-1].rsplit(":", 1)[0] not in own):
        return JSONResponse({"detail": "LeanCut only accepts requests from its own window."}, status_code=403)
    if not SERVER_URL and request.url.port:  # however the server was started (uvicorn CLI, tests), agents can find it
        announce(f"http://127.0.0.1:{request.url.port}")
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.endswith((".js", ".css", ".html")):
        response.headers.setdefault("Cache-Control", "no-cache")  # revalidate: after an update, never yesterday's UI
    return response


@api.get("/api/state")
def get_state():
    return {"version": VERSION, "project": project, "projects": list_projects(), "settings": settings,
            "sizes": transcriber.WHISPER_SIZES, "languages": transcriber.LANGUAGES,
            "platform": sys.platform, "data_dir": str(WORK), "default_output": str(project_dir() / "output"),
            "effects": editing.EFFECTS, "transitions": {k: k for k in editing.TRANSITIONS},
            "presets": {k: k for k in editing.GRADE_PRESETS}, "text_styles": editing.TEXT_STYLES}


@api.get("/api/job")
def get_job(since: int = 0):
    return {**job, "logs": job["logs"][since:], "log_count": len(job["logs"])}


@api.post("/api/cancel")
def cancel_job():
    if job["running"]:
        video_engine.cancel()
    return {"ok": True}


@api.post("/api/settings")
def set_settings(body: dict):
    if "target_length" in body:
        project["target_length"] = str(body.pop("target_length"))
        save()
    for k, v in body.items():
        if k in DEFAULT_SETTINGS and k != "current_project":
            if type(DEFAULT_SETTINGS[k]) in (int, float) and not isinstance(v, bool):
                v = float(v) if isinstance(DEFAULT_SETTINGS[k], float) else int(float(v))
            settings[k] = v
    save_settings()
    return settings


@api.get("/api/encoder")
def encoder_info():
    return {"hardware": video_engine.hardware_encoder()}


# --- projects

@api.post("/api/projects")
def create_project(body: dict):
    if job["running"]:
        raise HTTPException(409, "Wait for the running job to finish.")
    new_project(str(body.get("name", "")))
    return project


@api.post("/api/projects/{pid}/open")
def switch_project(pid: str):
    if job["running"]:
        raise HTTPException(409, "Wait for the running job to finish.")
    open_project(pid)
    return project


@api.patch("/api/projects/{pid}")
def rename_project(pid: str, body: dict):
    data = _read_json(project_dir(pid) / "project.json", None)
    if not data:
        raise HTTPException(404, "Project not found")
    data["name"] = str(body.get("name", data["name"])).strip() or data["name"]
    _write_json(project_dir(pid) / "project.json", data)
    if project.get("id") == pid:
        project["name"] = data["name"]
    return data


@api.delete("/api/projects/{pid}")
def delete_project(pid: str):
    """Moves the project folder aside (projects/.trash-<id>) instead of deleting it: exports may live there."""
    if job["running"]:
        raise HTTPException(409, "Wait for the running job to finish.")
    if not (project_dir(pid) / "project.json").exists():
        raise HTTPException(404, "Project not found")
    project_dir(pid).rename(PROJECTS / f".trash-{pid}")
    rest = list_projects()
    if project.get("id") == pid:
        open_project(rest[0]["id"]) if rest else new_project("My first project")
    return {"ok": True}


@api.post("/api/queue")
def queue(body: dict):
    pids = [p for p in body.get("projects", []) if (project_dir(p) / "project.json").exists()]
    steps = [x for x in body.get("steps", ["analyze", "export"]) if x in ("analyze", "export")]
    if not pids or not steps:
        raise HTTPException(400, "Pick at least one project and one step.")
    return start_job(f"Batch of {len(pids)} project(s)", lambda: run_queue(pids, steps))


# --- clips

@api.post("/api/clips/upload")
def upload(files: list[UploadFile]):
    added = []
    for f in files:
        name = Path(f.filename or "clip").name
        if Path(name).suffix.lower() not in video_engine.VIDEO_EXTS:
            raise HTTPException(400, f"{name}: only .mp4, .mov and .mkv are supported")
        dst = MEDIA / uuid.uuid4().hex[:8] / name  # keep the original filename so NLE exports relink by name
        dst.parent.mkdir()
        with dst.open("wb") as out:
            shutil.copyfileobj(f.file, out, 8 << 20)
        try:
            added.append(maybe_join(add_clip(dst, name, owned=True)))
        except ValueError as e:
            shutil.rmtree(dst.parent, ignore_errors=True)
            raise HTTPException(400, str(e))
    save()
    return added


@api.post("/api/clips/path")
def add_by_path(body: dict):
    """Reference a file in place — avoids copying multi-GB footage into the workspace."""
    p = Path(str(body.get("path", "")).strip().strip("'\"")).expanduser()
    if not p.is_file() or p.suffix.lower() not in video_engine.VIDEO_EXTS:
        raise HTTPException(400, f"Not an .mp4/.mov/.mkv file: {p}")
    try:
        c = maybe_join(add_clip(p, p.name, owned=False))
    except ValueError as e:
        raise HTTPException(400, str(e))
    save()
    return c


@api.post("/api/clips/{clip_id}/stabilised")
def use_gyroflow_export(clip_id: str):
    """Swap in the gyro-stabilised file Gyroflow wrote next to the original (<name>_stabilized.*); undo swaps back."""
    c = clip(clip_id)
    if c.get("original_path"):
        c["path"], c["original_path"] = c["original_path"], None
        save()
        return c
    orig = Path(c["path"])
    found = next(iter(sorted(orig.parent.glob(orig.stem + "_stabilized.*"))), None)
    if not found:
        raise HTTPException(404, f"No Gyroflow export found next to {orig.name} (expected {orig.stem}_stabilized.mp4). "
                                 "Stabilise the clip in Gyroflow first, then try again.")
    info = video_engine.probe(found)
    if abs(info["duration"] - c["duration"]) > 0.5:
        raise HTTPException(400, f"{found.name} is {info['duration']:.1f} s long, the original {c['duration']:.1f} s; "
                                 "export it from Gyroflow without trimming.")
    c["original_path"], c["path"] = c["path"], str(found)
    save()
    return c


@api.delete("/api/clips/{clip_id}")
def remove_clip(clip_id: str):
    if job["running"]:
        raise HTTPException(409, "Wait for the running job to finish.")
    c = clip(clip_id)
    project["clips"].remove(c)
    for other in project["clips"]:
        if other.get("angle_of") == clip_id:
            other.pop("angle_of", None)
    folder = Path(c["path"]).parent
    if c["owned"] and folder.parent == MEDIA:  # only delete copies we made; never the user's original footage
        shutil.rmtree(folder, ignore_errors=True)
    for f in (CACHE / f"{c['id']}.wav", CACHE / f"{c['id']}.keyframes9.json"):
        f.unlink(missing_ok=True)
    save()
    return {"ok": True}


@api.post("/api/clips/{clip_id}/move")
def move_clip(clip_id: str, body: dict):
    c = clip(clip_id)
    clips = project["clips"]
    i = clips.index(c)
    j = max(0, min(len(clips) - 1, i + int(body.get("delta", 0))))
    clips.insert(j, clips.pop(i))
    save()
    return {"ok": True}


@api.post("/api/clips/sort")
def sort_clips():
    """Put the story clips in the order they were recorded (clips with no known time stay at the end)."""
    mains = main_clips()
    order = sorted(mains, key=lambda c: (c.get("recorded") is None, c.get("recorded") or "", mains.index(c)))
    project["clips"] = order + [c for c in project["clips"] if c.get("angle_of")]
    save()
    return {"ok": True, "changed": order != mains}


STRIP_W, STRIP_H, STRIP_COLS = 160, 90, 20


@api.get("/api/clips/{clip_id}/strip")
def clip_strip(clip_id: str):
    """Filmstrip for the timeline: one JPEG sprite of thumbnails every `step` s (keyframes only, so it's quick)."""
    c = clip(clip_id)
    step_s = max(2.0, round(c["duration"] / 300, 1))
    n = max(1, int(c["duration"] // step_s) + 1)
    rows = -(-n // STRIP_COLS)
    out = CACHE / f"{c['id']}.strip{step_s:g}.jpg"
    if not out.exists():
        tmp = out.with_suffix(".part.jpg")
        video_engine._ffmpeg(["-y", "-skip_frame", "nokey", *video_engine.inp(c["path"]), "-an", "-vf",
                              f"fps=1/{step_s:g},scale={STRIP_W}:{STRIP_H}:force_original_aspect_ratio=decrease,"
                              f"pad={STRIP_W}:{STRIP_H}:-1:-1,format=yuvj420p,tile={STRIP_COLS}x{rows}",
                              "-frames:v", "1", "-q:v", "5", str(tmp)])
        tmp.replace(out)
    return {"url": f"/api/clips/{c['id']}/strip.jpg?step={step_s:g}", "step": step_s, "cols": STRIP_COLS,
            "w": STRIP_W, "h": STRIP_H, "n": n}


@api.get("/api/clips/{clip_id}/strip.jpg")
def clip_strip_file(clip_id: str, step: float):
    f = CACHE / f"{clip(clip_id)['id']}.strip{step:g}.jpg"
    if not f.exists():
        raise HTTPException(404, "Not generated yet")
    return FileResponse(f, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})


@api.get("/api/clips/{clip_id}/peaks")
def clip_peaks(clip_id: str):
    """Audio waveform for the timeline: peak level (0-255) every 1/20 s of the clip."""
    c = clip(clip_id)
    cache = CACHE / f"{c['id']}.peaks.json"
    if not cache.exists():
        import numpy as np
        x = np.abs(transcriber._read_wav(wav_for(c)))
        hop = 16000 // 20
        n = len(x) // hop
        pk = x[:n * hop].reshape(n, hop).max(axis=1) if n else np.zeros(0)
        pk = np.sqrt(pk / max(float(pk.max()) if n else 1.0, 1e-6))  # sqrt: quiet talk still visible next to shouts
        cache.write_text(json.dumps({"rate": 20, "peaks": (pk * 255).astype(int).tolist()}), encoding="utf-8")
    return json.loads(cache.read_text(encoding="utf-8"))


@api.post("/api/clips/{clip_id}/sync")
def sync_clip(clip_id: str, body: dict):
    """Mark a clip as a second camera angle of another clip, lining them up by their audio."""
    angle, primary = clip(clip_id), clip(str(body.get("to", "")))
    if angle is primary:
        raise HTTPException(400, "Pick a different clip to sync to.")

    def run():
        step("Comparing audio…", 0.3)
        offset, confidence = media_analysis.sync_offset(wav_for(primary), wav_for(angle))
        angle.update(angle_of=primary["id"], sync_offset=offset, sync_confidence=confidence)
        save()
        log(f"{angle['name']} sits at {offset:+.2f} s on {primary['name']}'s timeline (confidence {confidence:.0f}"
            + ("; low — check the result in your editor" if confidence < 8 else "") + ").")
    return start_job("Sync cameras", run)


@api.delete("/api/clips/{clip_id}/sync")
def unsync_clip(clip_id: str):
    c = clip(clip_id)
    for k in ("angle_of", "sync_offset", "sync_confidence"):
        c.pop(k, None)
    save()
    return c


@api.get("/api/clips/{clip_id}/file")
def clip_file(clip_id: str, chapter: int | None = None):
    c = clip(clip_id)
    chapters = c.get("chapters") or []
    if chapters:  # browsers can't play a concat list; serve one chapter file
        return FileResponse(chapters[max(0, min(chapter or 0, len(chapters) - 1))]["path"])
    return FileResponse(c["path"])


@api.patch("/api/clips/{clip_id}/{kind}/{index}")
def edit_item(clip_id: str, kind: str, index: int, body: dict):
    """Tick/untick a line or B-roll window, or drag its edges (start/end in seconds)."""
    c = clip(clip_id)
    items = {"segments": c.get("segments"), "broll": c.get("broll")}.get(kind) or []
    if not 0 <= index < len(items):
        raise HTTPException(404, "Not found")
    item = items[index]
    st = _stack()
    st["back"] = (st["back"] + [editing.snapshot(project)])[-40:]
    st["forward"] = []
    if "keep" in body:
        item["keep"] = bool(body["keep"])
        if kind == "segments":
            remember_taste([{"text": item["text"], "kept": item["keep"]}])
    if kind == "segments" and str(body.get("text") or "").strip():  # caption fix typed on the monitor/transcript
        item["text"] = str(body["text"]).strip()[:500]
        item.pop("words", None)  # word times no longer match; bleeps fall back to spreading words evenly
    if "start" in body or "end" in body:
        a = max(0.0, float(body.get("start", item["start"])))
        b = min(c["duration"], float(body.get("end", item["end"])))
        if b - a < 0.2:
            raise HTTPException(400, "A cut must be at least 0.2 s long.")
        item["start"], item["end"] = round(a, 2), round(b, 2)
    save()
    return item


# --- setup, models, updates

@api.get("/api/setup")
def setup_status():
    try:
        ffmpeg, ffmpeg_err = video_engine.ffmpeg_exe(), None
    except RuntimeError as e:
        ffmpeg, ffmpeg_err = None, str(e)
    try:
        whisper = {s: transcriber.is_downloaded(s) for s in transcriber.WHISPER_SIZES}
        whisper_err = None
    except ImportError:
        whisper, whisper_err = {}, "faster-whisper is not installed: pip install -r requirements.txt"
    return {"ffmpeg": ffmpeg, "ffmpeg_error": ffmpeg_err, "whisper": whisper, "whisper_error": whisper_err}


@api.post("/api/setup/whisper/{size}")
def download_whisper(size: str):
    if size not in transcriber.WHISPER_SIZES:
        raise HTTPException(400, "Unknown model size")

    def run():
        step(f"Downloading Whisper '{size}'…", 0.1)
        log(f"Downloading Whisper '{size}' from Hugging Face (one-time)…")
        log(f"Stored at {transcriber.download(size)}")

    return start_job(f"Download Whisper {size}", run)


@api.get("/api/llm/models")
def llm_models(provider: str, base_url: str):
    try:
        return {"models": llm_agent.list_models(provider, base_url)}
    except llm_agent.LLMError as e:
        raise HTTPException(502, str(e))


@api.get("/api/update")
def check_update(force: bool = False):
    if not force and not settings["update_check"]:
        return {"current": VERSION, "disabled": True}
    if force or time.time() - _update_cache.get("at", 0) > 6 * 3600:
        try:
            _update_cache.update(at=time.time(), data=updates.check(VERSION))
        except Exception as e:
            return {"current": VERSION, "error": f"Couldn't reach GitHub ({e})."}
    return _update_cache["data"]


@api.post("/api/open-url")
def open_url(body: dict):
    url = str(body.get("url", ""))
    if not url.startswith(f"https://github.com/{updates.REPO}/"):  # only our own release pages
        raise HTTPException(400, "Not a LeanCut link.")
    webbrowser.open(url)
    return {"ok": True}


# --- editing: actions, undo, chat, suggestions

def _stack():
    return undo.setdefault(project["id"], {"back": [], "forward": []})


def apply_edit(actions, source):
    """Validate + apply actions as one undoable step; kept/cut overrides teach the scorer the creator's taste."""
    st = _stack()
    before = editing.snapshot(project)
    learned = []
    done, errors = editing.apply_actions(project, actions, learned if source != "ai" else None)
    if done:
        st["back"] = (st["back"] + [before])[-40:]
        st["forward"] = []
        remember_taste(learned)
        save()
    return done, errors


def edit_state(extra=None):
    st = _stack()
    return {"project": project, "can_undo": bool(st["back"]), "can_redo": bool(st["forward"]), **(extra or {})}


@api.post("/api/actions")
def post_actions(body: dict):
    done, errors = apply_edit(body.get("actions") or [], body.get("source", "ui"))
    if errors and not done:
        raise HTTPException(400, "; ".join(errors))
    return edit_state({"done": done, "errors": errors})


@api.post("/api/undo")
def post_undo():
    st = _stack()
    if not st["back"]:
        raise HTTPException(400, "Nothing to undo.")
    st["forward"].append(editing.snapshot(project))
    editing.restore(project, st["back"].pop())
    save()
    return edit_state()


@api.post("/api/redo")
def post_redo():
    st = _stack()
    if not st["forward"]:
        raise HTTPException(400, "Nothing to redo.")
    st["back"].append(editing.snapshot(project))
    editing.restore(project, st["forward"].pop())
    save()
    return edit_state()


@api.post("/api/chat")
def post_chat(body: dict):
    """The editing assistant: a request in plain words becomes validated, undoable actions."""
    msg = str(body.get("message", "")).strip()
    if not msg:
        raise HTTPException(400, "Type an instruction.")
    if not any(it.get("score") is not None or it.get("keep") is not None for _, _, _, _, it in editing.items(project)):
        raise HTTPException(400, "Run the auto-edit first, so the assistant has parts to work with.")
    s = settings
    try:
        r = llm_agent.assistant(editing.context(project), project["chat"], msg, s["provider"], s["base_url"],
                                s["model"], editing.ACTION_SCHEMA)
    except llm_agent.LLMError as e:
        raise HTTPException(502, str(e))
    done, errors = apply_edit(r["actions"], "chat") if r["actions"] else ([], [])
    project["chat"] = (project["chat"] + [{"role": "user", "text": msg, "ts": time.time()},
                                          {"role": "assistant", "text": r["reply"] or "Done.", "done": done,
                                           "errors": errors, "ts": time.time()}])[-60:]
    save()
    return edit_state({"reply": r["reply"], "done": done, "errors": errors})


@api.post("/api/suggest")
def post_suggest():
    if job["running"]:
        raise HTTPException(409, "Wait for the running job to finish.")
    return edit_state({"suggestions": make_suggestions()})


@api.post("/api/suggestions/{sid}/{verb}")
def suggestion_verb(sid: str, verb: str):
    sug = next((x for x in project["suggestions"] if x["sid"] == sid), None)
    if not sug or verb not in ("apply", "dismiss"):
        raise HTTPException(404, "Suggestion not found")
    done, errors = apply_edit([sug["action"]], "suggestion") if verb == "apply" else ([], [])
    if verb == "apply" and errors:
        raise HTTPException(400, "; ".join(errors))
    sug["status"] = "applied" if verb == "apply" else "dismissed"
    save()
    return edit_state({"done": done})


def chapter_of(c, t):
    path = locate(c, t)[0]
    return next((i for i, ch in enumerate(c.get("chapters") or []) if ch["path"] == path), None)


@api.get("/api/plan")
def get_plan():
    """The programme for the instant preview: parts in output order with their looks, texts and transitions."""
    parts = program_parts()
    out, t = [], float((card_of("intro") or {}).get("duration", 0) or 0)
    for k, p in enumerate(parts):
        c = p["clip"]
        tr = editing.transition_of(project, p, k)
        path, local = locate(c, p["start"])
        chapter = chapter_of(c, p["start"])
        d = editing.part_duration(p)
        g = editing.project_grade(project, c, p.get("grade"))
        out.append({"clip": c["id"], "chapter": chapter, "src_start": local, "src_end": local + p["end"] - p["start"],
                    "start": p["start"], "end": p["end"], "speed": p["speed"], "effect": p["effect"],
                    "grade": {k2: v for k2, v in g.items() if k2 not in ("lut",)}, "transition": tr, "ids": p["ids"],
                    "silent": p.get("silent", False), "rec_start": round(t, 3), "rec_end": round(t + d, 3),
                    "texts": [{"text": x["text"], "style": x["style"], "t0": (x["at"] - p["start"]) / p["speed"],
                               "t1": (x["at"] - p["start"]) / p["speed"] + float(x.get("duration", 4)),
                               **{k: x[k] for k in ("x", "y", "size", "card", "item", "index") if x.get(k) is not None}}
                              for x in p["texts"]],
                    "graphics": [{"id": g["id"], "page": g["page"], "item": g["item"], "template": g.get("template"),
                                  "in": g.get("in", 0), "full": g.get("full", g.get("duration", 4)),
                                  "t0": (g["at"] - p["start"]) / p["speed"],
                                  "t1": (g["at"] - p["start"]) / p["speed"] + float(g.get("duration", 4))}
                                 for g in p.get("graphics") or []],
                    "overlays": [{"id": o["id"], "clip": o["clip"]["id"], "start": o["start"], "end": o["end"],
                                  "src_start": locate(o["clip"], o["start"])[1],
                                  "chapter": chapter_of(o["clip"], o["start"]),
                                  "t0": (o["at"] - p["start"]) / p["speed"],
                                  "t1": (o["at"] - p["start"]) / p["speed"] + o["end"] - o["start"]} for o in p.get("overlays") or []],
                    "voiceovers": [{"text": v["text"], "t0": (v["at"] - p["start"]) / p["speed"]} for v in p["voiceovers"]],
                    "captions": [{"id": i, "text": h[3]["text"], "t0": max(0.0, (h[3]["start"] - p["start"]) / p["speed"]),
                                  "t1": (h[3]["end"] - p["start"]) / p["speed"]}
                                 for i, h in ((i, editing.find(project, i)) for i in p["ids"]) if h and h[1] == "segments"]
                                if project.get("captions") else []})
        t += d
    return {"parts": out, "duration": round(t + float((card_of("outro") or {}).get("duration", 0) or 0), 3),
            "intro": card_of("intro"), "outro": card_of("outro"), "captions": project.get("captions"),
            "titles_over": {k: project[k] for k in ("intro", "outro") if project.get(k) and project[k].get("over")}}


@api.post("/api/preview")
def render_preview():
    """An accurate, low-resolution render of the current edit (every look, transition and voice-over)."""
    clips = main_clips()
    if not clips:
        raise HTTPException(400, "Add clips first.")
    c0 = clips[0]
    h = 540
    w = int(c0["width"] * h / c0["height"]) // 2 * 2
    fmt = {"width": w, "height": h, "fps": c0["fps"]}

    def run():
        out = CACHE / "preview" / f"{project['id']}.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)
        render_program(out, fmt, preview=True)
        return {"preview": f"/api/preview-file?v={int(time.time())}"}
    return start_job("Preview render", run)


@api.get("/api/preview-file")
def preview_file():
    p = CACHE / "preview" / f"{project['id']}.mp4"
    if not p.exists():
        raise HTTPException(404, "No preview rendered yet")
    return FileResponse(p)


@api.get("/api/frame")
def frame(clip_id: str, t: float, w: int = 320):
    from fastapi.responses import Response
    return Response(__import__("base64").b64decode(frame_b64(clip(clip_id), t, max(64, min(w, 1280)))),
                    media_type="image/jpeg")


# --- footage search (on-device CLIP)

@api.post("/api/search")
def search(body: dict):
    vision = _optional("vision")
    if not vision:
        raise HTTPException(400, "Search needs the vision module.")
    q = str(body.get("query", "")).strip()
    if not q:
        raise HTTPException(400, "Type what you're looking for, e.g. “lake” or “hairpin”.")
    files = [(c, ch) for c in main_clips() for ch in (c.get("chapters") or [{"path": c["path"], "offset": 0}])]
    idx_path = lambda c, ch: CACHE / f"{c['id']}.{editing.text_key(ch['path'])}.clip.npz"

    def run():
        hits = []
        for k, (c, ch) in enumerate(files, 1):
            step(f"Indexing footage {k}/{len(files)} (once per clip)…", k / (len(files) + 1))
            index = vision.embed_frames(ch["path"], idx_path(c, ch), MODELS)
            hits += [{"clip": c["id"], "name": c["name"], "t": round(t + ch["offset"], 2), "score": round(float(sc), 3)}
                     for t, sc in vision.search(q, index, MODELS)]
        hits.sort(key=lambda h: -h["score"])
        return {"search": {"query": q, "hits": hits[:24]}}
    return start_job(f"Search: {q}", run)


# --- voice (cloned voice-over)

@api.get("/api/voice")
def voice_status():
    voice = _optional("voice")
    if not voice:
        return {"available": {}, "error": "The voice module isn't installed in this build."}
    return {"available": voice.engines_available(settings["tts_server"] or None), "sample": settings["voice_sample"],
            "consent": settings["voice_consent"]}


@api.post("/api/voice/sample")
def make_voice_sample(body: dict):
    """Create the voice reference: from a file the user picked, or the cleanest stretch of their own speech."""
    voice = _optional("voice")
    if not voice:
        raise HTTPException(400, "The voice module isn't installed in this build.")
    if not body.get("consent"):
        raise HTTPException(400, "Confirm that this is your own voice or that you have permission to clone it.")
    settings["voice_consent"] = True
    if body.get("path"):
        p = Path(str(body["path"])).expanduser()
        if not p.is_file():
            raise HTTPException(400, f"Not a file: {p}")
        settings["voice_sample"] = str(p)
        save_settings()
        return {"sample": str(p)}

    def run():
        best = max(main_clips(), key=lambda c: sum(x["end"] - x["start"] for x in c.get("segments") or []), default=None)
        if not best or not best.get("segments"):
            raise RuntimeError("Transcribe a clip first; your voice sample is taken from your own speech.")
        out = WORK / "voices" / f"{project['id']}.wav"
        out.parent.mkdir(parents=True, exist_ok=True)
        info = voice.pick_voice_sample(best["segments"], wav_for(best), out)
        settings["voice_sample"] = str(out)
        save_settings()
        log(f"Voice sample: {info['seconds']:.1f} s of your speech from {best['name']} ({info['start']:.0f}–{info['end']:.0f} s).")
        return {"voice": info}
    return start_job("Create voice sample", run)


@api.post("/api/voice/test")
def voice_test(body: dict):
    text = str(body.get("text", "")).strip() or "This is how my voice-over will sound."

    def run():
        p = voice_track(text)
        return {"voice_test": f"/api/voice/file/{p.name}"}
    return start_job("Voice test", run)


@api.get("/api/voice/file/{name}")
def voice_file(name: str):
    p = CACHE / "voice" / Path(name).name
    if not p.is_file():
        raise HTTPException(404, "Not found")
    return FileResponse(p, media_type="audio/wav")


# --- updates (download + install in place)

@api.post("/api/update/install")
def install_update():
    def run():
        info = updates.check(VERSION)
        if not (info["newer"] and info.get("can_install")):
            return {"update": {"restart": False, "message": "No update to install here; use the download link."}}
        dest = CACHE / updates.ASSETS[sys.platform]
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            updates.download(info["url"], dest, lambda f: step(f"Downloading LeanCut {info['latest']}…", f * 0.9))
            step("Installing…", 0.9)
            return {"update": updates.install(dest, on_log=log)}
        finally:
            dest.unlink(missing_ok=True)
    return start_job("Install update", run)


@api.post("/api/update/restart")
def restart_for_update():
    if job["running"]:
        raise HTTPException(409, f"'{job['name']}' is still running.")
    try:
        updates.restart()
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    threading.Timer(0.5, lambda: os._exit(0)).start()  # reply first; the helper waits for this PID to exit
    return {"ok": True}


# --- pipeline & outputs

@api.post("/api/run")
def run_analysis():
    return start_job("AI edit", analyze)


@api.post("/api/export")
def run_export():
    return start_job("Export", export)


@api.get("/api/output/{name}")
def output_file(name: str):
    p = out_dir() / Path(name).name
    if not p.is_file():
        raise HTTPException(404, "Not rendered yet")
    return FileResponse(p, filename=p.name)


@api.post("/api/reveal")
def reveal_output():
    """Open the output folder in Finder / Explorer (the native window has no download manager)."""
    opener = {"darwin": ["open"], "win32": ["explorer"]}.get(sys.platform, ["xdg-open"])
    subprocess.Popen([*opener, str(out_dir())])
    return {"ok": True}



# ---------------------------------------------------------------- agents: tools, built-in local agent, coding agents

def announce(url):
    """Tell agents' MCP servers / the CLI where this LeanCut listens (server.json in the data folder)."""
    global SERVER_URL
    SERVER_URL = url
    f = WORK / "server.json"
    try:
        _write_json(f, {"url": url, "pid": os.getpid(), "version": VERSION})
    except OSError:
        return

    def remove():  # on a clean exit, unless another LeanCut has taken over the file
        try:
            if json.loads(f.read_text(encoding="utf-8")).get("pid") == os.getpid():
                f.unlink()
        except (OSError, ValueError):
            pass
    atexit.register(remove)


def look_format():
    c = (main_clips() or [{"fps": 30}])[0]
    return {"width": 1280, "height": 720, "fps": c.get("fps", 30)}


def chrome():
    """The graphics module, with a headless browser to draw pages: an installed Chrome/Edge/Brave/Chromium, else
    Chrome for Testing's headless shell, downloaded once (about 110 MB) into the models folder."""
    gm = _optional("graphics")
    if not gm:
        raise RuntimeError("Motion graphics need graphics.py.")
    if not gm.find_browser():
        log("No Chrome, Edge, Brave or Chromium found: downloading a headless Chrome (about 110 MB, once)…")
        os.environ["LEANCUT_CHROME"] = str(gm.ensure_browser(MODELS, log))
    return gm


def graphic_page(g):
    return project_dir() / g["page"]


def graphic_clip_for(fmt):
    """Render a graphic page to a video with alpha (cached) for the export."""
    if not _optional("graphics"):
        return None

    def render(g, d):
        page = graphic_page(g)
        if not page.exists():
            raise RuntimeError(f"Graphic page {g['page']} is missing from the project folder.")
        out = CACHE / "graphics"  # a folder: the file is named by its content, so changed pages re-render
        out.mkdir(parents=True, exist_ok=True)
        full = float(g.get("full", d))  # the whole graphic once; each part overlays its slice of it
        log(f"Drawing graphic {g['id']} ({Path(g['page']).stem}, {full:.1f} s)…")
        return chrome().render_clip(page.as_uri(), out, fmt["fps"], full, 1920, 1080, on_progress=lambda f: check_cancel())
    return render


def programme_still(t, width=960):
    """(JPEG, what's there) of the finished video at programme time t, through the export's own filters."""
    fmt = look_format()
    wd, font = production.prepare(CACHE / "render")
    parts = program_parts()
    t0 = float((card_of("intro") or {}).get("duration", 0) or 0)
    if t < t0:
        best = max((w for c in main_clips() for w in c.get("broll") or [] if w.get("keep")), key=lambda w: w.get("interest", 0), default=None)
        c = next((c for c in main_clips() if best and best in c.get("broll", [])), parts[0]["clip"])
        sp = production.card_spec(project["intro"], fmt, wd, font, (c["path"], best["start"] if best else parts[0]["start"]))
        return video_engine.still_of(sp, t, width, str(wd)), "intro card"
    for p in parts:
        d = editing.part_duration(p)
        if t < t0 + d:
            lt = t - t0
            sp = production.part_spec(p, project, settings, fmt, wd, font, "", None, locate)
            jpeg = video_engine.still_of(sp, lt, width, str(wd))
            for g in p.get("graphics") or []:  # one still of each graphic on screen, laid over the frame
                gt = lt - (g["at"] - p["start"]) / p["speed"]
                if _optional("graphics") and 0 <= gt < float(g["duration"]) and graphic_page(g).exists():
                    gt += g.get("in", 0)
                    gm = chrome()
                    png = CACHE / "look-graphic.png"
                    png.write_bytes(gm.still(graphic_page(g).as_uri(), gt, 1920, 1080))
                    base = CACHE / "look-base.jpg"
                    base.write_bytes(jpeg)
                    jpeg = video_engine._ffmpeg(["-y", "-i", str(base), "-i", str(png), "-filter_complex",
                                                 f"[1:v]scale={width}:-2[g];[0:v][g]overlay", "-f", "mjpeg", "-"],
                                                binary=True).stdout
            what = "; ".join(filter(None, [f"part {parts.index(p) + 1}: {', '.join(p['ids']) or 'ride-through'}",
                                           ", ".join(f"graphic {g['id']}" for g in p.get("graphics") or []
                                                     if 0 <= lt - (g["at"] - p["start"]) / p["speed"] < g["duration"])]))
            return jpeg, what
        t0 += d
    raise ValueError(f"t is past the end of the video ({t0:.1f} s)")


def contact_sheet(times, width=320):
    """Quick strip of source frames at programme times (no looks or graphics), 4 per row."""
    plan = get_plan()
    tiles = []
    for t in times:
        p = next((p for p in plan["parts"] if p["rec_start"] <= t < p["rec_end"]), None)
        if p:
            c = clip(p["clip"])
            tiles.append(base64.b64decode(frame_b64(c, p["start"] + (t - p["rec_start"]) * p["speed"], width)))
    if not tiles:
        raise ValueError("no frames in that range")
    d = CACHE / "sheet"
    d.mkdir(parents=True, exist_ok=True)
    for k, jpg in enumerate(tiles):
        (d / f"{k:03d}.jpg").write_bytes(jpg)
    cols = min(4, len(tiles))
    rows = -(-len(tiles) // cols)
    return video_engine._ffmpeg(["-y", "-framerate", "1", "-i", str(d / "%03d.jpg"), "-vf",
                                 f"scale={width}:-2,pad={width}:ih,tile={cols}x{rows}:padding=4", "-frames:v", "1",
                                 "-f", "mjpeg", "-"], binary=True).stdout


def check_graphic(path):
    return chrome().check(Path(path).as_uri(), None, 1920, 1080)


def telemetry_series(ci):
    """GoPro GPS of clip ci as series for data graphics: {t, speed_kmh, alt, lat, lon, highlights} (or None)."""
    tel = _optional("telemetry")
    c = project["clips"][ci]
    if not tel:
        return None
    out = {"t": [], "speed_kmh": [], "alt": [], "lat": [], "lon": []}
    for ch in c.get("chapters") or [{"path": c["path"], "offset": 0}]:
        if not tel.has_gpmf(ch["path"]):
            continue
        data = tel.read_gopro(ch["path"])
        speed = data.get("speed") or []
        k = 0
        for t, lat, lon, alt in data.get("gps") or []:
            while k + 1 < len(speed) and speed[k + 1][0] <= t:
                k += 1
            out["t"].append(round(t + ch["offset"], 2))
            out["lat"].append(lat)
            out["lon"].append(lon)
            out["alt"].append(round(alt, 1))
            out["speed_kmh"].append(round(speed[k][1] * 3.6, 1) if speed else 0.0)
    if not out["t"]:
        return None
    step_ = max(1, len(out["t"]) // 2000)  # plenty for a gauge or a line, small enough to embed in a page
    out = {k: v[::step_] for k, v in out.items()}
    out["highlights"] = clip_telemetry(c)["highlights"]
    return out


def search_hits(query):
    vision = _optional("vision")
    if not vision:
        raise RuntimeError("Search needs the vision module.")
    hits = []
    for c in main_clips():
        for ch in c.get("chapters") or [{"path": c["path"], "offset": 0}]:
            index = vision.embed_frames(ch["path"], CACHE / f"{c['id']}.{editing.text_key(ch['path'])}.clip.npz", MODELS)
            hits += [{"clip": c["id"], "name": c["name"], "t": round(t + ch["offset"], 2), "score": round(float(sc), 3)}
                     for t, sc in vision.search(query, index, MODELS)]
    return sorted(hits, key=lambda h: -h["score"])[:12]


def mcp_command():
    mcp = _optional("mcp_server")
    if mcp and hasattr(mcp, "mcp_command"):
        return mcp.mcp_command()
    return [sys.executable, "--mcp"] if getattr(sys, "frozen", False) else [sys.executable, str(ROOT / "mcp_server.py")]


AGENT_PROMPT = """You are LeanCut's editing agent. You edit the creator's vlog with tools; every change is validated and
undoable. Work in small steps: get_edit first, act, then look at the result before you say you're done. Never export
unless asked. Keep replies short: say what you changed and what the creator should look at.

""" + agent_tools.MANUAL

CODING_AGENTS = {  # name -> (label, executable names)
    "codex": ("Codex", ("codex",)), "codex_local": ("Codex with a local gpt-oss model", ("codex",)),
    "claude": ("Claude Code", ("claude",)), "gemini": ("Gemini CLI", ("gemini",)), "opencode": ("OpenCode", ("opencode",)),
}


def find_cli(names):
    """Coding agents' CLIs, also where installers put them but a desktop app's PATH doesn't look."""
    home = Path.home()
    extra = [home / ".local/bin", home / ".npm-global/bin", home / ".bun/bin", home / ".opencode/bin",
             Path("/opt/homebrew/bin"), Path("/usr/local/bin"), home / "AppData/Roaming/npm", home / ".claude/local"]
    for n in names:
        found = shutil.which(n) or next((str(d / x) for d in extra for x in (n, n + ".cmd", n + ".exe")
                                         if (d / x).is_file()), None)
        if found:
            return found
    return None


def run_local_agent(message):
    s = settings
    caps = llm_agent.capabilities(s["provider"], s["base_url"], s["model"])
    if not caps["tools"]:
        raise RuntimeError(f"{s['model']} can't call tools. Pick a model that can (e.g. qwen2.5:14b, llama3.1:8b, "
                           "qwen3:8b) in Settings › AI models, or use the quick assistant.")
    steps = []

    def event(kind, text):
        steps.append({"kind": kind, "text": text[:400]})
        log(("💬 " if kind == "say" else "🔧 " if kind == "tool" else "   ↳ ") + text[:300])

    def tool(name, args):
        res = agent_tools.call(sys.modules[__name__], name, args)
        pics = [x for x in res["content"] if x["type"] == "image"]
        if pics and not caps["vision"] and s["vision_model"]:  # a text-only model gets the vision model's eyes
            try:
                desc = llm_agent.look_at(s["provider"], s["base_url"], s["vision_model"], pics[0]["data"])[0]
                res["content"].append({"type": "text", "text": f"What's in the frame: {desc}"})
            except llm_agent.LLMError:
                pass
        return res

    history = [{"role": m["role"], "content": m["text"]} for m in project["chat"][-8:] if m["role"] in ("user", "assistant")]
    step("Agent is working…", 0.1)
    reply, _ = llm_agent.agent_loop(s["provider"], s["base_url"], s["model"], AGENT_PROMPT,
                                    history + [{"role": "user", "content": message}], agent_tools.tool_list(), tool,
                                    event, check_cancel=check_cancel, vision=caps["vision"])
    return reply, steps


def run_coding_agent(kind, message):
    label, names = CODING_AGENTS[kind]
    exe = find_cli(names)
    if not exe:
        raise RuntimeError(f"{label} isn't installed (or LeanCut can't find it). Install it, sign in once in a "
                           "terminal, then try again. See Settings › Agents.")
    cmd = mcp_command()
    folder = project_dir()
    (folder / "graphics").mkdir(exist_ok=True)
    prompt = (f"You are editing the LeanCut video project in this folder through the `leancut` MCP tools (motion "
              f"graphics pages go in ./graphics). Call get_edit first and follow the LeanCut manual (MCP prompt "
              f"'leancut'). Use look to check visual changes. Don't export unless asked.\n\nTask: {message}")
    last = folder / ".agent-last.txt"
    if kind.startswith("codex"):
        args = [exe, "exec", "--skip-git-repo-check", "-C", str(folder), "-s", "workspace-write", "-o", str(last),
                "-c", f"mcp_servers.leancut.command={json.dumps(cmd[0])}",
                "-c", f"mcp_servers.leancut.args={json.dumps(cmd[1:])}",
                "-c", "mcp_servers.leancut.tool_timeout_sec=900"]
        if kind == "codex_local":  # Codex's local mode is made for OpenAI's open gpt-oss models
            local = "lmstudio" if settings["provider"] == "lmstudio" else "ollama"
            names = llm_agent.list_models(settings["provider"], settings["base_url"])
            model = next((m for m in names if "gpt-oss" in m), None)
            if not model:
                raise RuntimeError("Codex with a local model needs OpenAI's open gpt-oss model: run `ollama pull "
                                   "gpt-oss:20b` (13 GB; needs about 16 GB of free memory), then try again. Other "
                                   "local models can't drive Codex's tools; use “Agent with tools” for them.")
            args += ["--oss", "--local-provider", local, "-m", model, "--color", "never"]
        else:
            args += ["--color", "never"]
        args.append(prompt)
    elif kind == "claude":
        cfg = json.dumps({"mcpServers": {"leancut": {"command": cmd[0], "args": cmd[1:]}}})
        args = [exe, "-p", prompt, "--mcp-config", cfg, "--allowedTools", "mcp__leancut", "Read", "Write", "Edit",
                "--output-format", "stream-json", "--verbose"]
    elif kind == "gemini":
        (folder / ".gemini").mkdir(exist_ok=True)
        _write_json(folder / ".gemini" / "settings.json", {"mcpServers": {"leancut": {"command": cmd[0], "args": cmd[1:], "trust": True}}})
        args = [exe, "-p", prompt, "--yolo"]
    else:  # opencode
        _write_json(folder / "opencode.json", {"$schema": "https://opencode.ai/config.json",
                                                "mcp": {"leancut": {"type": "local", "command": cmd, "enabled": True}}})
        args = [exe, "run", prompt]
    log(f"Starting {label} in {folder}")
    step(f"{label} is working…", 0.1)
    env = {**os.environ, "LEANCUT_URL": SERVER_URL} if SERVER_URL else None  # this LeanCut, not another one
    proc = subprocess.Popen(args, cwd=folder, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            text=True, encoding="utf-8", errors="replace", env=env)
    final, tail = "", []
    try:
        for line in proc.stdout:
            check_cancel()
            line = line.rstrip()
            if kind == "claude" and line.startswith("{"):
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    ev = {}
                if ev.get("type") == "result":
                    final = ev.get("result") or final
                for c in (ev.get("message") or {}).get("content") or [] if ev.get("type") == "assistant" else []:
                    if c.get("type") == "text":
                        log("💬 " + c["text"][:300])
                    elif c.get("type") == "tool_use":
                        log(f"🔧 {c.get('name')} {json.dumps(c.get('input'))[:200]}")
                continue
            line = re.sub(r"\x1b\[[0-9;]*m", "", line)  # terminal colours
            if line:
                log(line[:300])
                tail = (tail + [line])[-20:]
    except Cancelled:
        proc.kill()
        raise
    finally:
        proc.wait()
    if last.exists():
        final = last.read_text(encoding="utf-8").strip()
        last.unlink()
    if proc.returncode and not final:
        raise RuntimeError(f"{label} stopped with an error: " + " ".join(tail[-3:])[:400])
    return final or "\n".join(tail[-6:]), []


@api.get("/api/agent/manual")
def agent_manual():
    return {"text": agent_tools.MANUAL}


@api.get("/api/agent/tools")
def agent_tool_list():
    return {"tools": agent_tools.tool_list()}


@api.post("/api/agent/call")
def agent_call(body: dict):
    return agent_tools.call(sys.modules[__name__], str(body.get("name", "")), body.get("arguments") or {})


@api.get("/api/agent/agents")
def agent_choices():
    cli = _optional("leancut_cli")
    snippets = cli.connect_snippets(mcp_command()) if cli else {}
    return {"agents": [{"id": "assistant", "label": "Quick assistant (your local model)", "ready": True},
                       {"id": "local", "label": "Agent with tools (your local model)", "ready": bool(settings["model"])},
                       *({"id": k, "label": lab, "ready": bool(find_cli(n))} for k, (lab, n) in CODING_AGENTS.items())],
            "mcp_command": mcp_command(), "connect": snippets}


@api.post("/api/agent/run")
def agent_run(body: dict):
    msg, kind = str(body.get("message", "")).strip(), str(body.get("agent", "local"))
    if not msg:
        raise HTTPException(400, "Type what you'd like the agent to do.")
    if kind != "local" and kind not in CODING_AGENTS:
        raise HTTPException(400, f"Unknown agent '{kind}'.")
    label = "Agent" if kind == "local" else CODING_AGENTS[kind][0]
    project["chat"].append({"role": "user", "text": msg, "agent": kind, "ts": time.time()})
    save()

    def run():
        try:
            reply, steps = run_local_agent(msg) if kind == "local" else run_coding_agent(kind, msg)
        except Cancelled:
            project["chat"].append({"role": "assistant", "text": "Stopped.", "agent": kind, "ts": time.time()})
            save()
            raise
        except Exception as e:
            project["chat"].append({"role": "assistant", "text": "", "errors": [str(e)], "agent": kind, "ts": time.time()})
            save()
            raise
        project["chat"] = (project["chat"] + [{"role": "assistant", "text": reply or "Done.", "steps": steps,
                                               "agent": kind, "ts": time.time()}])[-60:]
        save()
        return {"agent": True}
    return start_job(f"{label}: {msg[:40]}", run)


# --- motion graphics: templates and page files (served with the preview host for the monitor)

@api.get("/api/graphics/templates")
def graphic_templates():
    gm = _optional("graphics")
    return {"templates": gm.list_templates() if gm else [], "available": bool(gm)}


@api.post("/api/graphics/add")
def graphic_add(body: dict):
    r = agent_tools.call(sys.modules[__name__], "add_graphic", body)
    if r["isError"]:
        raise HTTPException(400, r["content"][0]["text"])
    return edit_state({"done": [r["content"][0]["text"]]})


@api.get("/api/graphics/page/{pid}/{path:path}")
def graphic_file(pid: str, path: str):
    """A project's graphics files for the live preview; pages get the preview host script injected."""
    from fastapi.responses import HTMLResponse
    if pid != project["id"]:
        raise HTTPException(404, "Not the open project")
    root = (project_dir() / "graphics").resolve()
    f = (root / path).resolve()
    if root not in f.parents or not f.is_file():
        raise HTTPException(404, "Not found")
    if f.suffix.lower() == ".html":
        gm = _optional("graphics")
        html = f.read_text(encoding="utf-8")
        return HTMLResponse(gm.inject_host(html, "/graphics-host.js") if gm else html,
                            headers={"Cache-Control": "no-store"})
    return FileResponse(f, headers={"Cache-Control": "no-store"})


api.mount("/", StaticFiles(directory=RESOURCES / "static", html=True), name="static")

if __name__ == "__main__":
    import uvicorn

    url = "http://127.0.0.1:8765"
    announce(url)
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run(api, host="127.0.0.1", port=8765, log_level="warning")

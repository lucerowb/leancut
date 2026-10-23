"""LeanCut — local AI video editor. FastAPI backend, projects, settings and pipeline orchestration.

Run:  python app.py      (browser UI at http://127.0.0.1:8765)
      python desktop.py  (native desktop window)
"""
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

import exporters
import llm_agent
import media_analysis
import transcriber
import updates
import video_engine
from video_engine import Cancelled, check_cancel

APP_NAME = "LeanCut"
VERSION = "0.2.0"
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

SCORE_VERSION = "v2"  # bump when the scoring prompt changes, so cached AI scores are refreshed
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
    # video & audio
    "render_mode": "accurate", "encoder": "auto",
    "loudness": True, "denoise": False, "music_path": "", "music_volume": 0.15,
    # what to export and where
    "out_video": True, "out_edl": True, "out_fcpxml": True, "out_otio": True, "out_srt": True,
    "out_youtube": True, "out_thumbs": True, "out_shorts": False, "shorts_count": 3,
    "output_dir": "", "output_name": "{project}",
    # app
    "update_check": True, "current_project": "",
}


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
    for c in project["clips"]:  # clips added before v0.2 lack the start timecode NLE exports need
        if "timecode" not in c and Path(c["path"]).exists():
            c["timecode"] = video_engine.probe(c["path"]).get("timecode")
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
    info = video_engine.probe(path)
    c = {"id": uuid.uuid4().hex[:10], "name": name, "path": str(path), "owned": owned,
         "size": path.stat().st_size, **info, "segments": None, "whisper": None, "broll": []}
    project["clips"].append(c)
    return c


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


# ---------------------------------------------------------------- pipeline: analyze

def analyze():
    s = settings
    clips = main_clips()
    if not clips:
        raise RuntimeError("Add at least one clip first.")
    if not s["model"]:
        raise RuntimeError("Choose an LLM model in the Models & API panel.")

    step("Checking LLM server…", 0.0)
    models = llm_agent.list_models(s["provider"], s["base_url"])  # fail fast, before an hour of transcription
    if s["model"] not in models:
        raise RuntimeError(f"Model '{s['model']}' is not available on the server. Available: {', '.join(models) or 'none'}")
    llm_agent.check_fits(s["provider"], s["base_url"], s["model"])

    whisper = s["custom_whisper"].strip() or s["whisper_size"]
    whisper_key = f"{whisper}/{s['language']}"  # transcripts are cached per model + language
    todo = [c for c in clips if c["segments"] is None or c["whisper"] != whisper_key]
    for i, c in enumerate(todo):
        step(f"Extracting audio — {c['name']}", 0.08 * i / len(todo))
        wav_for(c)

    if todo:
        log(f"Step 1–2: transcribing {len(todo)} clip(s) with Whisper '{whisper}', language "
            f"{transcriber.LANGUAGES.get(s['language'], s['language'])} (VAD on) — first run downloads the model.")

        def prog(i, f):
            step(f"Transcribing clip {i + 1}/{len(todo)} — {todo[i]['name']}", 0.08 + 0.47 * (i + f) / len(todo))

        wavs = [CACHE / f"{c['id']}.wav" for c in todo]
        for i, segs in transcriber.transcribe_files(whisper, wavs, s["language"], log, prog):
            c = todo[i]
            c["segments"], c["whisper"] = segs, whisper_key
            talk = sum(x["end"] - x["start"] for x in segs)
            log(f"{c['name']}: {len(segs)} speech segments, {talk / 60:.1f} of {c['duration'] / 60:.1f} min is talking.")
            save()  # resume point: a cancel after this keeps the clip's transcript
    else:
        log("Transcripts are cached — skipping Whisper.")

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
        step("Listening for loud moments…", 0.56)
        for c in clips:
            c["moments"] = media_analysis.loud_moments(wav_for(c))
        for sid, (c, seg) in enumerate(refs):
            if any(m["start"] < seg["end"] and m["end"] > seg["start"] for m in c["moments"]):
                boost[sid] = 1
        log(f"Loud moments (shouts, laughs, revs): {sum(len(c['moments']) for c in clips)}; "
            f"{len(boost)} line(s) get a +1 boost.")

    log("Step 3: AI scoring")
    decisions = llm_agent.decide(flat, s["provider"], s["base_url"], s["model"], None, log,
                                 lambda f: step("Querying local LLM…", 0.58 + 0.30 * f), known=known)
    for sid, d in decisions.items():  # loud moments nudge scores before the length budget is applied
        if sid in boost and sid not in known:
            d["score"] = min(5, d["score"] + 1)
            d["reason"] = (d["reason"] + " · loud moment").strip(" ·")
    ratio_target = parse_target(project["target_length"])
    total = sum(x["end"] - x["start"] for x in flat)
    keep = llm_agent.select({x["id"]: x["end"] - x["start"] for x in flat},
                            {i: d["score"] for i, d in decisions.items()}, ratio_target / total if ratio_target else None)
    for sid, (c, seg) in enumerate(refs):
        d = decisions[sid]
        seg.update(keep=sid in keep, score=d["score"], reason=d["reason"])
        if sid not in known or known[sid][1] == seg.get("reason"):
            seg["scored_by"] = model_key
    save()
    speech_kept = sum(seg["end"] - seg["start"] for c, seg in refs if seg["keep"])

    # scenic B-roll for the silent stretches (plus anything the rider marked out loud)
    for c in clips:
        c["broll"] = []
    if s["broll"]:
        cands = []
        for i, c in enumerate(clips):
            step(f"Looking for scenic B-roll — {c['name']}", 0.88 + 0.1 * i / len(clips))
            stats = media_analysis.keyframe_stats(c["path"], CACHE / f"{c['id']}.keyframes8.json")
            windows = media_analysis.broll_candidates(c["duration"], c["segments"], stats)
            cands += [(c["id"], w) for w in windows]
        share = max(0, min(60, float(s["broll_share"]))) / 100
        budget = speech_kept * share / (1 - share) if share < 1 else 0
        cands.sort(key=lambda x: -x[1]["interest"])
        picked = {id(w) for _, w in media_analysis.pick_broll(cands, budget)}
        by_clip = {c["id"]: c for c in clips}
        for cid, w in cands[:max(12, len(picked) * 2)]:  # keep some runners-up so you can tick them in
            by_clip[cid]["broll"].append({**w, "keep": id(w) in picked, "reason": f"scenic · {w['interest']:.0%}"})
    for c in clips:
        for t in markers[c["id"]]:
            c["broll"].append({"start": round(max(0.0, t - 30), 2), "end": t, "interest": 1.0, "keep": True,
                               "reason": "you said a marker phrase"})
        c["broll"].sort(key=lambda w: w["start"])
    save()
    broll_kept = sum(w["end"] - w["start"] for c in clips for w in c["broll"] if w["keep"])
    log(f"Kept {sum(1 for _, x in refs if x['keep'])}/{len(refs)} lines ≈ {speech_kept / 60:.1f} min of talking"
        + (f" + {broll_kept / 60:.1f} min of scenic B-roll" if broll_kept else "") + ". Review the timeline, then export.")


# ---------------------------------------------------------------- pipeline: export

def cut_entries():
    """[(clip, start, end, speed)] in output order for the current project."""
    entries = []
    accurate = settings["render_mode"] == "accurate"
    for c in main_clips():
        kept = [x for x in (c["segments"] or []) + c.get("broll", []) if x.get("keep")]
        ranges = video_engine.merge_ranges(kept, c["duration"])
        rows = [(c, a, b, 1) for a, b in ranges]
        if settings["connectors"] and accurate and ranges:
            rows += [(c, a, b, sp) for a, b, sp in media_analysis.connectors(ranges, c["duration"])]
        entries += sorted(rows, key=lambda r: r[1])
    return entries


def export():
    s = settings
    entries = cut_entries()
    if not entries:
        raise RuntimeError("No segments are marked to keep. Run the auto-edit or tick some lines first.")
    clips = main_clips()
    fmt = {k: clips[0][k] for k in ("width", "height", "fps")}
    folder, base = out_dir(), out_base()
    title = project["name"]
    tl = exporters.timeline(entries)
    files = []
    accurate = s["render_mode"] == "accurate"
    angles = [(c, c["angle_of"], c["sync_offset"]) for c in project["clips"] if c.get("angle_of")]

    if s["out_video"]:
        enc = video_engine.hardware_encoder() if accurate and s["encoder"] != "software" else None
        log(f"Step 4: stitching {len(entries)} cuts "
            f"({'frame-accurate, ' + (enc or 'libx264') if accurate else 'fast stream copy'}) "
            f"at {fmt['width']}x{fmt['height']} @ {fmt['fps']:g} fps")
        tmp = folder / f".{base}.joined.mp4"
        video_engine.render([(c["path"], a, b, sp) for c, a, b, sp in entries], tmp, accurate, fmt,
                            lambda f: step("Stitching video…", f * 0.7), s["encoder"], cache_dir=CACHE / "parts")
        step("Finishing audio…", 0.72)
        music = s["music_path"] if s["music_path"] and Path(s["music_path"]).is_file() else None
        if s["loudness"] or s["denoise"] or music:
            log("Audio: " + ", ".join(x for x, on in [("wind/noise reduction", s["denoise"]),
                                                      ("music bed with ducking", music),
                                                      ("−14 LUFS loudness", s["loudness"])] if on))
        files.append(video_engine.finish_audio(tmp, folder / f"{base}.mp4", s["loudness"], s["denoise"], music,
                                               s["music_volume"]))

    chapters = []
    if s["out_youtube"]:
        step("Writing title, description and chapters…", 0.78)
        lines = exporters.chapter_lines(tl)
        try:
            meta = llm_agent.youtube_metadata(lines, s["provider"], s["base_url"], s["model"])
            chapters = exporters.clean_chapters(meta["chapters"], lines, tl[-1]["rec_end"])
            files.append(exporters.write_youtube_txt(meta, chapters, folder / f"{base}_youtube.txt"))
        except llm_agent.LLMError as e:
            log(f"Skipped YouTube metadata: {e}")
    if s["out_edl"]:
        files.append(exporters.write_edl(tl, folder / f"{base}.edl", fmt["fps"], title))
    if s["out_fcpxml"]:
        files.append(exporters.write_fcpxml(tl, folder / f"{base}.fcpxml", fmt, title, angles, chapters))
    if s["out_otio"]:
        files.append(exporters.write_otio(tl, folder / f"{base}.otio", fmt, title, angles))
    if s["out_srt"]:
        files.append(exporters.write_srt(tl, folder / f"{base}.srt"))
    if s["out_thumbs"]:
        step("Picking thumbnails…", 0.84)
        cands = sorted([(c["path"], (x["start"] + x["end"]) / 2, x.get("score", 4))
                        for c in clips for x in (c["segments"] or []) + c.get("broll", []) if x.get("keep")],
                       key=lambda r: -r[2])
        files += media_analysis.thumbnails(cands, folder, name=f"{base}_thumb")
    if s["out_shorts"]:
        wins = media_analysis.shorts_windows(clips, n=int(s["shorts_count"]))
        vf = f"scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1,fps={fmt['fps']}"
        for i, (c, a, b, value) in enumerate(wins, 1):
            step(f"Rendering Short {i}/{len(wins)}…", 0.86 + 0.13 * i / len(wins))
            tmp = folder / f".{base}_short_{i}.mp4"
            video_engine.render([(c["path"], a, b)], tmp, True, {**fmt, "width": 1080, "height": 1920},
                                encoder=s["encoder"], vf=vf, cache_dir=CACHE / "parts")
            files.append(video_engine.finish_audio(tmp, folder / f"{base}_short_{i}.mp4", s["loudness"]))
            log(f"Short {i}: {c['name']} {a:.0f}–{b:.0f}s ({b - a:.0f}s, score {value})")
    log("Saved to " + str(folder) + ": " + ", ".join(Path(f).name for f in files))
    return {"folder": str(folder), "files": [{"name": Path(f).name, "url": f"/api/output/{Path(f).name}"} for f in files]}


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
    return await call_next(request)


@api.get("/api/state")
def get_state():
    return {"version": VERSION, "project": project, "projects": list_projects(), "settings": settings,
            "sizes": transcriber.WHISPER_SIZES, "languages": transcriber.LANGUAGES,
            "platform": sys.platform, "data_dir": str(WORK), "default_output": str(project_dir() / "output")}


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
            added.append(add_clip(dst, name, owned=True))
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
        c = add_clip(p, p.name, owned=False)
    except ValueError as e:
        raise HTTPException(400, str(e))
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
    for f in (CACHE / f"{c['id']}.wav", CACHE / f"{c['id']}.keyframes8.json"):
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
def clip_file(clip_id: str):
    return FileResponse(clip(clip_id)["path"])


@api.patch("/api/clips/{clip_id}/{kind}/{index}")
def edit_item(clip_id: str, kind: str, index: int, body: dict):
    """Tick/untick a line or B-roll window, or drag its edges (start/end in seconds)."""
    c = clip(clip_id)
    items = {"segments": c.get("segments"), "broll": c.get("broll")}.get(kind) or []
    if not 0 <= index < len(items):
        raise HTTPException(404, "Not found")
    item = items[index]
    if "keep" in body:
        item["keep"] = bool(body["keep"])
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


api.mount("/", StaticFiles(directory=RESOURCES / "static", html=True), name="static")

if __name__ == "__main__":
    import uvicorn

    url = "http://127.0.0.1:8765"
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run(api, host="127.0.0.1", port=8765, log_level="warning")

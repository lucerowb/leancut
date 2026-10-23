"""LeanCut — local AI video editor. FastAPI backend + pipeline orchestration.

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

import llm_agent
import transcriber
import video_engine

APP_NAME = "LeanCut"
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
MEDIA, CACHE, OUTPUT = WORK / "media", WORK / "cache", WORK / "output"
STATE_FILE = WORK / "project.json"
OUT_NAME = "final_youtube_cut"
for d in (MEDIA, CACHE, OUTPUT):
    d.mkdir(parents=True, exist_ok=True)

DEFAULT_SETTINGS = {
    "whisper_size": "large-v3-turbo",  # best multilingual quality/speed; small garbles non-English speech
    "language": "auto",
    "provider": "ollama",
    "base_url": llm_agent.DEFAULT_URLS["ollama"],
    "model": "",
    "target_length": "",
    "render_mode": "accurate",
}

state = {"clips": [], "settings": dict(DEFAULT_SETTINGS)}
if STATE_FILE.exists():
    state.update(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    state["settings"] = {**DEFAULT_SETTINGS, **state["settings"]}  # projects saved before a setting existed
lock = threading.Lock()
job = {"running": False, "name": "", "step": "", "progress": 0.0, "logs": [], "error": None, "result": None}


def save():
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    tmp.replace(STATE_FILE)  # atomic: a crash mid-write never corrupts the project


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    job["logs"].append(line)


def step(name, progress):
    job["step"], job["progress"] = name, round(progress, 4)


def start_job(name, fn):
    with lock:
        if job["running"]:
            raise HTTPException(409, f"'{job['name']}' is still running.")
        job.update(running=True, name=name, step="Starting…", progress=0.0, logs=[], error=None, result=None)

    def run():
        try:
            job["result"] = fn()
            step("Done", 1.0)
            log(f"{name} finished.")
        except Exception as e:  # surface every failure in the UI console instead of a silent dead thread
            job["error"] = str(e)
            step("Failed", job["progress"])
            log(f"ERROR: {e}")
        finally:
            job["running"] = False

    threading.Thread(target=run, daemon=True).start()
    return {"ok": True}


def clip(clip_id):
    for c in state["clips"]:
        if c["id"] == clip_id:
            return c
    raise HTTPException(404, "Clip not found")


def parse_target(text):
    """'10-15 minutes' -> 750 s (midpoint); '12' -> 720 s; '' -> None."""
    nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", text or "")]
    return sum(nums) / len(nums) * 60 if nums else None


def add_clip(path: Path, name: str, owned: bool):
    info = video_engine.probe(path)
    c = {"id": uuid.uuid4().hex[:10], "name": name, "path": str(path), "owned": owned,
         "size": path.stat().st_size, **info, "segments": None, "whisper": None}
    state["clips"].append(c)
    return c


# ---------------------------------------------------------------- pipeline

def analyze():
    s = state["settings"]
    clips = state["clips"]
    if not clips:
        raise RuntimeError("Add at least one clip first.")
    if not s["model"]:
        raise RuntimeError("Choose an LLM model in the settings panel.")

    step("Checking LLM server…", 0.0)
    models = llm_agent.list_models(s["provider"], s["base_url"])  # fail fast, before an hour of transcription
    if s["model"] not in models:
        raise RuntimeError(f"Model '{s['model']}' is not available on the server. Available: {', '.join(models) or 'none'}")
    llm_agent.check_fits(s["provider"], s["base_url"], s["model"])

    whisper_key = f"{s['whisper_size']}/{s['language']}"  # transcripts are cached per model + language
    todo = [c for c in clips if c["segments"] is None or c["whisper"] != whisper_key]
    for i, c in enumerate(todo):
        if not c["has_audio"]:
            raise RuntimeError(f"{c['name']} has no audio track — nothing to transcribe.")
        step(f"Extracting audio — {c['name']}", 0.10 * i / len(todo))
        log(f"Step 1: isolating audio from {c['name']}")
        video_engine.extract_audio(c["path"], CACHE / f"{c['id']}.wav")

    if todo:
        log(f"Step 2: transcribing {len(todo)} clip(s) with Whisper '{s['whisper_size']}', language "
            f"{transcriber.LANGUAGES.get(s['language'], s['language'])} (VAD on) — first run downloads the model.")

        def prog(i, f):
            step(f"Transcribing clip {i + 1}/{len(todo)} — {todo[i]['name']}", 0.10 + 0.55 * (i + f) / len(todo))

        wavs = [CACHE / f"{c['id']}.wav" for c in todo]
        for i, segs in transcriber.transcribe_files(s["whisper_size"], wavs, s["language"], log, prog):
            c = todo[i]
            c["segments"], c["whisper"] = segs, whisper_key
            talk = sum(x["end"] - x["start"] for x in segs)
            log(f"{c['name']}: {len(segs)} speech segments, {talk / 60:.1f} of {c['duration'] / 60:.1f} min is talking.")
            save()
    else:
        log("Step 2: transcripts are cached — skipping Whisper.")

    for c in clips:
        c["segments"] = transcriber.merge_fragments(c["segments"])
    refs = [seg for c in clips for seg in c["segments"]]
    flat = [{"id": n, "clip": ci + 1, **seg}
            for n, (ci, seg) in enumerate((ci, seg) for ci, c in enumerate(clips) for seg in c["segments"])]
    if not flat:
        raise RuntimeError("No speech was detected in any clip, so there is nothing for the LLM to edit.")
    log("Step 3: LLM edit decisions")
    decisions = llm_agent.decide(flat, s["provider"], s["base_url"], s["model"], parse_target(s["target_length"]),
                                 log, lambda f: step("Querying local LLM…", 0.65 + 0.35 * f))
    for f, seg in zip(flat, refs):
        seg.update(decisions[f["id"]])
    save()
    kept = [x for x in refs if x["keep"]]
    log(f"LLM kept {len(kept)}/{len(flat)} segments ≈ {sum(x['end'] - x['start'] for x in kept) / 60:.1f} min. "
        "Review the timeline, then export.")


def export():
    s = state["settings"]
    ranges = []
    for c in state["clips"]:
        kept = [x for x in c["segments"] or [] if x.get("keep")]
        ranges += [(c["path"], a, b) for a, b in video_engine.merge_ranges(kept, c["duration"])]
    if not ranges:
        raise RuntimeError("No segments are marked to keep. Run the analysis or tick some segments first.")
    first = state["clips"][0]
    fmt = {k: first[k] for k in ("width", "height", "fps")}
    accurate = s["render_mode"] == "accurate"
    log(f"Step 4: stitching {len(ranges)} cuts ({'frame-accurate re-encode' if accurate else 'fast stream copy'}) "
        f"at {fmt['width']}x{fmt['height']} @ {fmt['fps']:g} fps")
    video = video_engine.render(ranges, OUTPUT / f"{OUT_NAME}.mp4", accurate, fmt,
                                lambda f: step("Stitching video…", f * 0.98))
    edl = video_engine.write_edl(ranges, OUTPUT / f"{OUT_NAME}.edl", fmt["fps"])
    log(f"Saved {video} and {edl.name}")
    return {"video": f"/api/output/{video.name}", "edl": f"/api/output/{edl.name}", "folder": str(OUTPUT)}


# ---------------------------------------------------------------- API

api = FastAPI(title="LeanCut AI Video Editor")


@api.get("/api/state")
def get_state():
    return {"clips": state["clips"], "settings": state["settings"], "sizes": transcriber.WHISPER_SIZES,
            "languages": transcriber.LANGUAGES}


@api.get("/api/job")
def get_job(since: int = 0):
    return {**job, "logs": job["logs"][since:], "log_count": len(job["logs"])}


@api.post("/api/settings")
def set_settings(body: dict):
    state["settings"].update({k: v for k, v in body.items() if k in DEFAULT_SETTINGS})
    save()
    return state["settings"]


@api.post("/api/clips/upload")
def upload(files: list[UploadFile]):
    added = []
    for f in files:
        name = Path(f.filename or "clip").name
        if Path(name).suffix.lower() not in video_engine.VIDEO_EXTS:
            raise HTTPException(400, f"{name}: only .mp4, .mov and .mkv are supported")
        dst = MEDIA / uuid.uuid4().hex[:8] / name  # keep the original filename so the EDL relinks by name
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
    state["clips"].remove(c)
    folder = Path(c["path"]).parent
    if c["owned"] and folder.parent == MEDIA:  # only delete copies we made; never the user's original footage
        shutil.rmtree(folder, ignore_errors=True)
    (CACHE / f"{c['id']}.wav").unlink(missing_ok=True)
    save()
    return {"ok": True}


@api.post("/api/clips/{clip_id}/move")
def move_clip(clip_id: str, body: dict):
    c = clip(clip_id)
    clips = state["clips"]
    i = clips.index(c)
    j = max(0, min(len(clips) - 1, i + int(body.get("delta", 0))))
    clips.insert(j, clips.pop(i))
    save()
    return {"ok": True}


@api.get("/api/clips/{clip_id}/file")
def clip_file(clip_id: str):
    return FileResponse(clip(clip_id)["path"])


@api.patch("/api/clips/{clip_id}/segments/{index}")
def set_segment(clip_id: str, index: int, body: dict):
    segs = clip(clip_id)["segments"] or []
    if not 0 <= index < len(segs):
        raise HTTPException(404, "Segment not found")
    segs[index]["keep"] = bool(body.get("keep"))
    save()
    return segs[index]


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


@api.post("/api/run")
def run_analysis():
    return start_job("AI edit", analyze)


@api.post("/api/export")
def run_export():
    return start_job("Export", export)


@api.get("/api/output/{name}")
def output_file(name: str):
    p = OUTPUT / Path(name).name
    if not p.is_file():
        raise HTTPException(404, "Not rendered yet")
    return FileResponse(p, filename=p.name)


@api.post("/api/reveal")
def reveal_output():
    """Open the output folder in Finder / Explorer (the native window has no download manager)."""
    opener = {"darwin": ["open"], "win32": ["explorer"]}.get(sys.platform, ["xdg-open"])
    subprocess.Popen([*opener, str(OUTPUT)])
    return {"ok": True}


api.mount("/", StaticFiles(directory=RESOURCES / "static", html=True), name="static")

if __name__ == "__main__":
    import uvicorn

    url = "http://127.0.0.1:8765"
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run(api, host="127.0.0.1", port=8765, log_level="warning")

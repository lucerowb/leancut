"""OpenAI-compatible text-to-speech server that speaks in a cloned voice (Chatterbox-Turbo), for LeanCut's "server" engine.

The packaged app can't bundle PyTorch, so run this from a source checkout that has the voice extras:
    pip install -r requirements.txt -r requirements-voice.txt
    python voice_server.py --voice my_voice.wav            # then in LeanCut: server http://127.0.0.1:8880/v1

Only clone your own voice, or a voice whose owner has given you permission.

POST /v1/audio/speech {"input": text, "voice": "default" | a registered name | a WAV path on this machine}
                      -> audio/wav (24 kHz mono). "model", "speed" and "response_format" are accepted and ignored.
POST /v1/voices?name=me  (multipart field "file")  registers a reference sample for this session.
GET  /v1/voices, /v1/models
"""
import argparse
import re
import tempfile
import threading
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse, Response

import voice

api = FastAPI(title="LeanCut voice server")
VOICES = {}  # name -> reference WAV path
UPLOADS = Path(tempfile.mkdtemp(prefix="leancut-voices-"))  # shortcut: uploads last one session; persist if wanted
_lock = threading.Lock()  # one model, one generation at a time


@api.middleware("http")
async def local_only(request, call_next):
    """Same rule as LeanCut's own API: no web page can reach this via DNS rebinding or a cross-site POST."""
    host = (request.headers.get("host") or "").rsplit(":", 1)[0]
    origin = request.headers.get("origin")
    own = {"127.0.0.1", "localhost", "[::1]"}
    if host not in own or (origin and origin.split("://")[-1].rsplit(":", 1)[0] not in own):
        return JSONResponse({"detail": "This voice server only accepts local requests."}, status_code=403)
    return await call_next(request)


def _resolve(name):
    """A registered voice name, or a path to an audio file on this machine (LeanCut sends its sample's path)."""
    if name in VOICES:
        return VOICES[name]
    if Path(name).suffix.lower() in (".wav", ".flac", ".mp3", ".m4a") and Path(name).is_file():
        return Path(name)
    raise HTTPException(400, f"Unknown voice {name!r}. Registered: {sorted(VOICES) or 'none'} "
                             "(start with --voice my_voice.wav or POST /v1/voices).")


@api.post("/v1/audio/speech")
def speech(body: dict):  # sync: FastAPI runs it in a worker thread, so a long generation doesn't block the server
    text = str(body.get("input") or "").strip()
    if not text:
        raise HTTPException(400, "'input' is empty.")
    sample = _resolve(str(body.get("voice") or "default"))
    with tempfile.TemporaryDirectory() as tmp, _lock:
        try:
            out = voice.chatterbox_wav(text, sample, Path(tmp) / "speech.wav")
        except voice.VoiceError as e:
            raise HTTPException(500, str(e)) from e
        return Response(out.read_bytes(), media_type="audio/wav")


@api.post("/v1/voices")
async def add_voice(name: str, file: UploadFile = File(...)):
    if not re.fullmatch(r"[\w-]{1,64}", name):
        raise HTTPException(400, "Voice names may use letters, digits, '-' and '_' only.")
    path = UPLOADS / f"{name}{Path(file.filename or '').suffix.lower() or '.wav'}"
    path.write_bytes(await file.read())
    VOICES[name] = path
    return {"voice": name}


@api.get("/v1/voices")
def list_voices():
    return {"voices": sorted(VOICES)}


@api.get("/v1/models")
def list_models():
    return {"object": "list", "data": [{"id": "chatterbox", "object": "model"}]}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--voice", required=True, help="reference WAV (~10 s of clean speech) used as the 'default' voice")
    ap.add_argument("--port", type=int, default=8880)
    args = ap.parse_args()
    if not Path(args.voice).is_file():
        ap.error(f"{args.voice} not found")
    VOICES["default"] = Path(args.voice).resolve()
    print("Loading Chatterbox (first run downloads ~3.8 GB)…", flush=True)
    voice._chatterbox_model()  # load now, so the first request isn't the slow one
    import uvicorn
    uvicorn.run(api, host="127.0.0.1", port=args.port)  # never 0.0.0.0: there is no login

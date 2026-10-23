"""Edit-decision engine backed by a local LLM (Ollama or LM Studio / any OpenAI-compatible server).

The LLM never invents timestamps: it picks segment IDs to keep, and the exact keep/cut timestamps
come from Whisper. That makes the EDL robust against the model hallucinating numbers.
"""
import json
import os
import re
import urllib.error
import urllib.request

DEFAULT_URLS = {"ollama": "http://localhost:11434", "lmstudio": "http://localhost:1234/v1"}
CHUNK_CHARS = 6000  # keeps each request well inside an 8k-token context

# The LLM only judges (scores every segment); code enforces the length budget. Local models ignore
# "keep N seconds" instructions, especially on rough transcripts, but they rank reasonably well.
SYSTEM_PROMPT = """You are a professional YouTube editor cutting a long-form vlog (moto vlog / daily vlog).
You receive the vlog's speech as numbered segments: [id] clip start-time (duration) text.
The text is automatic speech recognition and may be a rough machine translation: judge each segment by what is
happening, never score it down just because the wording is odd or ungrammatical.

Score EVERY segment for how much it belongs in the final video:
5 = must keep: opening hook, key story beat (where we go, what happens, why), funny moment, sign-off
4 = strong: genuine reaction ("Woo!", "Look at that!"), greeting/banter with friends or the camera, insight
3 = fine: ordinary narration or conversation that keeps the story flowing
2 = weak: logistics nobody cares about (prices, directions to bystanders), rambling
1 = cut: repeated take of a line already said, false start, mumbling, unintelligible noise
Give consecutive segments that form one thought the same score.

Reply with JSON only, no prose: {"scores": [{"id": <segment id>, "s": <1-5>, "r": "<2-5 word reason>"}]}"""

_MEMORY_HINTS = ("out of memory", "insufficient memory", "requires more system memory", "vram",
                 "failed to load model", "not enough memory", "cudamalloc")


class LLMError(RuntimeError):
    pass


class LLMParseError(LLMError):
    pass


STALL = ("The LLM produced no output for {s:.0f} s. This usually means the model is too big for this machine's "
         "memory and is swapping to disk. Pick a smaller model (7B–14B on a 16–24 GB machine) and retry.")


def _http(url, payload=None, timeout=10, on_line=None):
    """POST/GET JSON. With on_line, the response is streamed line by line and `timeout` becomes an
    inactivity timeout: a slow-but-working model never times out, a stalled one fails fast."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if on_line is None:
                return json.loads(r.read())
            for raw in r:
                if raw.strip():
                    on_line(raw.decode("utf-8", errors="replace").strip())
    except TimeoutError as e:
        raise LLMError(STALL.format(s=timeout)) from e
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        if any(k in body.lower() for k in _MEMORY_HINTS):
            raise LLMError("The local LLM ran out of (V)RAM. Pick a smaller model or quantization (e.g. a 7B/8B Q4), "
                           f"unload other models in LM Studio/Ollama, then retry. Server said: {body[:300]}") from e
        if e.code == 404:
            raise LLMError(f"{url} returned 404 — check the base URL and that the model name exists. {body[:300]}") from e
        raise LLMError(f"LLM server error {e.code}: {body[:300]}") from e
    except (urllib.error.URLError, ConnectionError) as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, TimeoutError) and on_line:  # server accepted the request but never answered
            raise LLMError(STALL.format(s=timeout)) from e
        raise LLMError(f"Cannot reach the LLM server at {url} ({reason}). "
                       "Start Ollama (`ollama serve`) or LM Studio's local server and check the base URL.") from e
    except json.JSONDecodeError as e:
        raise LLMError(f"{url} did not return JSON — is the base URL pointing at the right server?") from e


def list_models(provider, base_url):
    base = base_url.rstrip("/")
    if provider == "ollama":
        return [m["name"] for m in _http(f"{base}/api/tags").get("models", [])]
    return [m["id"] for m in _http(f"{base}/models").get("data", [])]


def check_fits(provider, base_url, model):
    """Refuse an Ollama model whose weights alone exceed ~75% of system RAM: it would swap and stall for hours."""
    if provider != "ollama":
        return
    try:
        ram = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, ValueError, OSError):
        return  # shortcut: no RAM check on Windows (no sysconf); the stall timeout still catches it
    size = next((m.get("size", 0) for m in _http(f"{base_url.rstrip('/')}/api/tags").get("models", [])
                 if m["name"] == model), 0)
    if size > 0.75 * ram:
        raise LLMError(f"'{model}' needs {size / 1e9:.1f} GB but this machine has {ram / 1e9:.0f} GB of memory, so it "
                       "would swap to disk and stall. Pick a smaller model, e.g. `ollama pull qwen2.5:14b` "
                       "(~9 GB) or `qwen2.5:7b` (~5 GB).")


def chat(provider, base_url, model, system, user, stall_timeout=300) -> str:
    base = base_url.rstrip("/")
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    parts = []

    def ollama_line(line):
        d = json.loads(line)
        if "error" in d:
            raise LLMError(f"Ollama error: {d['error']}")
        parts.append(d.get("message", {}).get("content", ""))

    def openai_line(line):  # Server-Sent Events: "data: {...}" ... "data: [DONE]"
        if line.startswith("data:") and line[5:].strip() != "[DONE]":
            for c in json.loads(line[5:]).get("choices", []):
                parts.append((c.get("delta") or {}).get("content") or "")

    if provider == "ollama":
        # think=False: thinking models (gemma4, qwen3…) would otherwise burn minutes reasoning about a pick-list
        _http(f"{base}/api/chat", {"model": model, "messages": messages, "stream": True, "think": False,
                                   "format": "json", "options": {"temperature": 0.2, "num_ctx": 8192}},
              stall_timeout, ollama_line)
    else:
        _http(f"{base}/chat/completions", {"model": model, "messages": messages, "temperature": 0.2, "stream": True},
              stall_timeout, openai_line)
    return "".join(parts)


def parse_scores(text, valid_ids) -> dict:
    """Extract {id: (score 1-5, reason)} from an LLM reply. Tolerates <think> blocks, code fences and junk items."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    start, end = text.find("{"), text.rfind("}")
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise LLMParseError(f"LLM reply was not valid JSON: {text[:200]!r}") from e
    items = data.get("scores", []) if isinstance(data, dict) else []
    out = {}
    for it in items if isinstance(items, list) else []:
        try:
            sid, score = int(it["id"]), min(5, max(1, int(it["s"])))
        except (TypeError, ValueError, KeyError):
            continue
        if sid in valid_ids:
            out[sid] = (score, str(it.get("r", "")))
    if not out:
        raise LLMParseError(f"LLM reply had no usable scores: {text[:200]!r}")
    return out


def select(durations, scores, ratio):
    """Pick segment ids to keep. durations: {id: seconds}; scores: {id: 1-5}; ratio: target/speech or None.

    No target: keep score >= 3 (at least 30%). Target above the speech length: drop only 1s, keep >= 80%.
    Otherwise: best scores first until the target is reached. Ties keep the earlier segment.
    """
    if ratio is None:
        thr, lo, hi = 3, 0.3, 1.0
    elif ratio >= 1:
        thr, lo, hi = 2, 0.8, 1.0
    else:
        thr, lo, hi = 6, ratio, ratio
    total = sum(durations.values()) or 1.0
    rank = sorted(durations, key=lambda i: (-scores.get(i, 3), i))
    keep = {i for i in durations if scores.get(i, 3) >= thr}
    kept = sum(durations[i] for i in keep)
    for i in rank:  # top up, best first
        if kept >= lo * total:
            break
        if i not in keep:
            keep.add(i)
            kept += durations[i]
    for i in reversed(rank):  # trim, worst first
        if kept <= hi * total:
            break
        if i in keep:
            keep.discard(i)
            kept -= durations[i]
    return keep


def _chunks(segments):
    chunk, size = [], 0
    for s in segments:
        line = f"[{s['id']}] clip{s['clip']} {_hms(s['start'])} ({s['end'] - s['start']:.0f}s) {s['text']}\n"
        if chunk and size + len(line) > CHUNK_CHARS:
            yield chunk
            chunk, size = [], 0
        chunk.append((s, line))
        size += len(line)
    if chunk:
        yield chunk


def _hms(t):
    t = int(t)
    return f"{t // 3600:02d}:{t // 60 % 60:02d}:{t % 60:02d}"


def decide(segments, provider, base_url, model, target_seconds=None, log=print, on_progress=lambda f: None) -> dict:
    """segments: [{id, clip, start, end, text}] -> {id: {"keep": bool, "reason": str}} for every segment."""
    total = sum(s["end"] - s["start"] for s in segments)
    ratio = target_seconds / total if target_seconds and total else None
    if ratio and ratio >= 1:
        log(f"Speech totals {total / 60:.1f} min, under the {target_seconds / 60:.1f} min target — "
            "the LLM will only cut clear fluff.")
    elif ratio:
        log(f"Speech totals {total / 60:.1f} min; target {target_seconds / 60:.1f} min → keeping ~{ratio:.0%}.")
    chunks = list(_chunks(segments))
    scores, reasons = {}, {}
    for n, chunk in enumerate(chunks, 1):
        prompt = (f"Part {n} of {len(chunks)} of the vlog. Score all {len(chunk)} segments.\n\nSegments:\n"
                  + "".join(l for _, l in chunk))
        ids = {s["id"] for s, _ in chunk}
        log(f"Querying local LLM ({model}) — part {n}/{len(chunks)}, {len(ids)} segments…")
        got = None
        for attempt in (1, 2):
            try:
                got = parse_scores(chat(provider, base_url, model, SYSTEM_PROMPT, prompt), ids)
                break
            except LLMParseError as e:
                log(f"Attempt {attempt}: {e}")
        if got is None:
            log(f"Part {n}: LLM gave no usable answer twice — scoring it neutral so nothing is lost.")
            got = {i: (3, "LLM fallback") for i in ids}
        elif len(got) < len(ids):
            log(f"Part {n}: LLM skipped {len(ids) - len(got)} segment(s); treating them as neutral (3).")
        for i, (sc, r) in got.items():
            scores[i], reasons[i] = sc, r
        on_progress(n / len(chunks))
    keep = select({s["id"]: s["end"] - s["start"] for s in segments}, scores, ratio)
    return {s["id"]: {"keep": s["id"] in keep, "score": scores.get(s["id"], 3), "reason": reasons.get(s["id"], "")}
            for s in segments}

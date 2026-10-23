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


def chat(provider, base_url, model, system, user, stall_timeout=300, schema=None, num_ctx=8192, history=(),
         images=(), temperature=0.2) -> str:
    """One chat turn, streamed. schema: a JSON schema the reply is *constrained* to (grammar-constrained decoding in
    Ollama, json_schema in LM Studio / OpenAI-compatible servers), so structured answers always parse."""
    base = base_url.rstrip("/")
    msg = {"role": "user", "content": user}
    if images:
        msg["images"] = list(images)
    messages = [{"role": "system", "content": system}, *history, msg]
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
                                   "format": schema or "json",
                                   "options": {"temperature": temperature, "num_ctx": num_ctx}},
              stall_timeout, ollama_line)
    else:
        body = {"model": model, "messages": messages, "temperature": temperature, "stream": True}
        if schema:
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema}}
        _http(f"{base}/chat/completions", body, stall_timeout, openai_line)
    return "".join(parts)


def option_probs(provider, base_url, model, system, user, num_ctx=8192, images=()) -> dict:
    """Probabilities of the *first* answer token (Jev / OpenJev-style decisions): one forward step, no generation.
    Identical system prompts hit the server's prompt cache, so asking about many lines of one transcript costs
    ~0.2 s each after the first. Raises LLMError('no logprobs') when the server can't return them."""
    import math
    base = base_url.rstrip("/")
    msg = {"role": "user", "content": user}
    if images:
        msg["images"] = list(images)
    messages = [{"role": "system", "content": system}, msg]
    if provider == "ollama":
        r = _http(f"{base}/api/chat", {"model": model, "messages": messages, "stream": False, "think": False,
                                       "logprobs": True, "top_logprobs": 12, "keep_alive": "15m",
                                       "options": {"num_predict": 1, "temperature": 0, "num_ctx": num_ctx}}, 300)
        lp = (r.get("logprobs") or [{}])[0].get("top_logprobs")
    else:
        r = _http(f"{base}/chat/completions", {"model": model, "messages": messages, "max_tokens": 1,
                                               "temperature": 0, "logprobs": True, "top_logprobs": 12}, 300)
        lp = ((((r.get("choices") or [{}])[0].get("logprobs") or {}).get("content") or [{}])[0]).get("top_logprobs")
    if not lp:
        raise LLMError("no logprobs: this server/model doesn't return token probabilities")
    out = {}
    for t in lp:
        key = t["token"].strip().upper()
        out[key] = out.get(key, 0.0) + math.exp(t["logprob"])
    return out


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


def decide(segments, provider, base_url, model, target_seconds=None, log=print, on_progress=lambda f: None,
           known=None, engine="auto", taste=()) -> dict:
    """segments: [{id, clip, start, end, text}] -> {id: {"keep", "score", "reason"}} for every segment.

    known: {id: (score, reason)} already decided (cached scores, retakes, spoken markers); only the rest is sent
    to the LLM, so changing the target length or resuming after a cancel costs no LLM time."""
    total = sum(s["end"] - s["start"] for s in segments)
    ratio = target_seconds / total if target_seconds and total else None
    if ratio and ratio >= 1:
        log(f"Speech totals {total / 60:.1f} min, under the {target_seconds / 60:.1f} min target — "
            "the LLM will only cut clear fluff.")
    elif ratio:
        log(f"Speech totals {total / 60:.1f} min; target {target_seconds / 60:.1f} min → keeping ~{ratio:.0%}.")
    known = known or {}
    scores = {i: sc for i, (sc, _) in known.items()}
    reasons = {i: r for i, (_, r) in known.items()}
    confs = {}
    todo = [s for s in segments if s["id"] not in known]
    if len(todo) < len(segments):
        log(f"{len(segments) - len(todo)} segment(s) already scored (cache, retakes, markers); "
            f"asking the LLM about {len(todo)}.")
    chunks = list(_chunks(todo))
    if engine in ("auto", "decision") and todo:
        try:
            got = decide_probs(todo, provider, base_url, model, log, on_progress, taste)
            for i, (sc, r, conf) in got.items():
                scores[i], reasons[i], confs[i] = sc, r, conf
            chunks = []  # done: the decision engine answered every line
        except LLMError as e:
            if engine == "decision" or "no logprobs" not in str(e):
                raise
            log("This server can't return token probabilities; scoring in batches instead.")
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
    return {s["id"]: {"keep": s["id"] in keep, "score": scores.get(s["id"], 3), "reason": reasons.get(s["id"], ""),
                      **({"conf": confs[s["id"]]} if s["id"] in confs else {})} for s in segments}


DECISION_PROMPT = """You are a professional YouTube editor cutting a long-form vlog (moto vlog / daily vlog).
The transcript below is automatic speech recognition and may be a rough machine translation: judge what is
happening, not the grammar. You will be asked about one line at a time.
Scores: 5 = must keep (opening hook, key story beat, funny moment, sign-off); 4 = strong (genuine reaction,
greeting/banter, insight); 3 = fine (ordinary narration that keeps the story flowing); 2 = weak (logistics nobody
needs, rambling); 1 = cut (repeated take, false start, mumbling, unintelligible)."""
LABELS = {"A": "opening hook", "B": "story beat", "C": "funny moment", "D": "genuine reaction",
          "E": "greeting or banter", "F": "insight", "G": "logistics", "H": "rambling", "I": "false start or repeat",
          "J": "unintelligible", "K": "sign-off"}


def taste_block(taste, n=8):
    """Few-shot examples from the creator's own overrides, so the cut drifts towards their taste."""
    kept = [t["text"] for t in reversed(taste) if t.get("kept")][:n]
    cut = [t["text"] for t in reversed(taste) if not t.get("kept")][:n]
    if not kept and not cut:
        return ""
    q = lambda xs: "\n".join(f'- "{x[:90]}"' for x in xs)
    return ("\n\nThis creator's own past choices (follow their taste):\nKept:\n" + (q(kept) or "-")
            + "\nCut:\n" + (q(cut) or "-"))


def decide_probs(segments, provider, base_url, model, log=print, on_progress=lambda f: None, taste=()):
    """Decision engine: for every line, read the model's probabilities over ★1–★5 and over a fixed list of reason
    labels instead of generating JSON. Returns {id: (expected score, reason, confidence 0..1)}."""
    out = {}
    chunks = list(_chunks(segments))
    done, total = 0, len(segments)
    letters = "".join(f"{k}) {v}  " for k, v in LABELS.items())
    for n, chunk in enumerate(chunks, 1):
        system = DECISION_PROMPT + taste_block(taste) + "\n\nTranscript:\n" + "".join(l for _, l in chunk)
        log(f"Decision engine ({model}) — part {n}/{len(chunks)}, {len(chunk)} lines…")
        for seg, _ in chunk:
            p = option_probs(provider, base_url, model, system,
                             f"Line [{seg['id']}]: score it 1-5. Answer with one digit.")
            digits = {d: p.get(str(d), 0.0) for d in range(1, 6)}
            z = sum(digits.values())
            exp = sum(d * v for d, v in digits.items()) / z if z > 0.05 else 3.0
            conf = max(digits.values()) / z if z > 0.05 else 0.0
            q = option_probs(provider, base_url, model, system,
                             f"Line [{seg['id']}]: which fits best? {letters}Answer with one letter.")
            label = max(LABELS, key=lambda k: q.get(k, 0.0))
            out[seg["id"]] = (round(exp, 1), LABELS[label], round(conf, 2))
            done += 1
            on_progress(done / total)
    return out


ASSISTANT_PROMPT = """You are LeanCut's video-editing assistant. The user edits a vlog by talking to you; you change the
edit with actions. Each part of the video has an id like c0.s3 (a spoken line) or c0.b1 (silent B-roll).
Action types (use only these, with only the listed fields):
- keep / cut {ids}            - trim {ids:[one id], start, end} (source seconds)
- effect {ids, value: none|zoom|push_in|slowmo|speedup|bw|vignette|dreamy|stabilise}
- transition {ids, value: cut|jcut|crossfade|dissolve|dip_black|dip_white|wipe_left|wipe_right|slide_left|slide_right|circle|radial|zoom|pixelize|smooth, duration} (transition INTO each part)
- cut_style {value: (a transition name), duration}  (default for every cut)
- grade {ids (optional; empty = whole video), value: none|punchy|warm|cool|cinematic|teal_orange|moody|vintage|bright|bw, params: {exposure -1..1, contrast 0.5..1.5, saturation 0..2, temperature -1..1, vignette 0..1}}
- auto_grade {value: on|off}
- text {ids:[one id], text, value: title|lower_third|caption|label|plain, offset (s into the part), duration, x, y (0..1 centre on the frame, optional), size (0.02..0.3 of the height, optional)}
- edit_text {ids:[one id], index (0 = first text on that part), text (empty removes), offset, duration, x, y, size}
- split {ids:[one id], start (source seconds inside it)}   - cut_words {ids:[one id], start, end} (cut just those words, source seconds)
- broll_mode {ids: B-roll ids, value: cutaway (picture over the nearest kept line, talking continues; optional over: line id, offset) | insert (its own shot)}
- voiceover {ids:[one id], text, offset}   (spoken in the creator's cloned voice)
- remove_text / remove_voiceover {ids}
- intro / outro {text: title (empty removes), value: subtitle, duration, over: true to show it over the first/last shot instead of a card}
- target {value: e.g. "8 minutes"}   - broll_share {amount 0..60}   - music_volume {amount 0..0.6}
- captions {value: on|off}   - privacy_blur {value: on|off}   - stabilise {ids, value: on|off}
- add_broll {value: clip id, start, end, text: why}   (add silent footage as B-roll)
- lut {value: path to a .cube/.3dl file the user named; empty removes it}
Rules: reference only ids that exist in the parts list. Make the smallest set of actions that does what was asked.
Do only what the user asked: no extra changes they didn't mention.
If the user asks a question or something you can't do, answer in "reply" and return no actions.
"reply" is one or two short sentences saying what you changed. Reply with JSON only.
Example: user "Open with a title 'Day 3' subtitled 'Into the hills' and fade into c1.s0" ->
{"reply": "Added the Day 3 intro and a crossfade into c1.s0.", "actions": [{"type": "intro", "text": "Day 3",
"value": "Into the hills", "duration": 4}, {"type": "transition", "ids": ["c1.s0"], "value": "crossfade", "duration": 0.6}]}"""
ASSISTANT_SCHEMA = {"type": "object", "properties": {"reply": {"type": "string"},
                                                     "actions": {"type": "array", "items": None}},
                    "required": ["reply", "actions"]}

SUGGEST_PROMPT = """You are a senior YouTube editor reviewing a vlog edit. Propose up to 10 specific improvements that
would make the video more watchable. Each suggestion targets one or more part ids and carries exactly one action
(same action vocabulary as below). Good suggestions: a lower-third title where the location or day changes, a
transition at a scene change, slow motion or a punch-in on a big reaction, cutting a rambling or repeated line,
keeping a strong line that was cut, a voice-over line to explain a jump, a grade for a dull or dark shot, an intro
title. Never suggest what is already applied (check the KEEP/cut column and the effects column). "title" is a
specific imperative under 8 words that names the moment, e.g. "Slow down the summit arrival", "Title the fuel stop",
"Cut the second take of the intro"; "why" is one short sentence. Reply with JSON only."""
SUGGEST_SCHEMA = {"type": "object", "properties": {"suggestions": {"type": "array", "items": {
    "type": "object", "properties": {"ids": {"type": "array", "items": {"type": "string"}}, "title": {"type": "string"},
                                     "why": {"type": "string"}, "action": None},
    "required": ["ids", "title", "why", "action"]}}}, "required": ["suggestions"]}


def _with_action_schema(schema, action_schema):
    import copy
    s = copy.deepcopy(schema)
    if "actions" in s["properties"]:
        s["properties"]["actions"]["items"] = action_schema
    else:
        s["properties"]["suggestions"]["items"]["properties"]["action"] = action_schema
    return s


def assistant(context, history, message, provider, base_url, model, action_schema):
    """One chat turn: {"reply", "actions"}; output constrained to the action schema."""
    reply = chat(provider, base_url, model, ASSISTANT_PROMPT + "\n\n" + context, message,
                 schema=_with_action_schema(ASSISTANT_SCHEMA, action_schema), num_ctx=16384,
                 history=[{"role": h["role"], "content": h["text"]} for h in history[-6:]])
    try:
        data = json.loads(reply[reply.find("{"):reply.rfind("}") + 1])
    except json.JSONDecodeError as e:
        raise LLMParseError(f"Assistant reply was not valid JSON: {reply[:200]!r}") from e
    return {"reply": str(data.get("reply", "")).strip(), "actions": [a for a in data.get("actions", []) if isinstance(a, dict)]}


def suggest(context, provider, base_url, model, action_schema):
    reply = chat(provider, base_url, model, SUGGEST_PROMPT + "\n\n" + ASSISTANT_PROMPT.split("Rules:")[0]
                 + "\n\n" + context, "Suggest improvements for this edit.",
                 schema=_with_action_schema(SUGGEST_SCHEMA, action_schema), num_ctx=16384, temperature=0.4)
    try:
        data = json.loads(reply[reply.find("{"):reply.rfind("}") + 1])
    except json.JSONDecodeError as e:
        raise LLMParseError(f"Suggestions were not valid JSON: {reply[:200]!r}") from e
    return [x for x in data.get("suggestions", []) if isinstance(x, dict) and isinstance(x.get("action"), dict)][:10]


def look_at(provider, base_url, model, jpeg_b64):
    """Visual judge for one frame with a local vision model: (short description, scenic score 1-5, confidence)."""
    desc = chat(provider, base_url, model, "You describe video frames for a video editor.",
                'Describe this frame in at most 12 words. Reply as JSON: {"desc": "..."}',
                schema={"type": "object", "properties": {"desc": {"type": "string"}}, "required": ["desc"]},
                images=[jpeg_b64], num_ctx=4096, temperature=0)
    try:
        desc = json.loads(desc[desc.find("{"):desc.rfind("}") + 1]).get("desc", "")
    except json.JSONDecodeError:
        desc = ""
    p = option_probs(provider, base_url, model, "You rate video frames for a motorcycle travel vlog.",
                     "How visually striking is this frame as B-roll (scenery, light, composition)? 1 = dull or "
                     "blurry, 5 = stunning. Answer with one digit.", num_ctx=4096, images=[jpeg_b64])
    digits = {d: p.get(str(d), 0.0) for d in range(1, 6)}
    z = sum(digits.values()) or 1.0
    return desc.strip()[:120], round(sum(d * v for d, v in digits.items()) / z, 2), round(max(digits.values()) / z, 2)


METADATA_PROMPT = """You write YouTube metadata for a vlog (moto vlog / daily vlog) from its transcript.
The transcript may be a rough machine translation; describe what happens, never quote broken sentences.
Lines are "[m:ss] text" on the final video's timeline.
Reply with JSON only:
{"title": "<under 70 characters, specific, no clickbait caps>",
 "description": "<2-4 short paragraphs: what happens, where, highlights>",
 "tags": ["<10-15 search tags>"],
 "chapters": [{"time": "<m:ss copied from a line>", "title": "<2-5 words>"}]}
Chapters: 4-10 of them, in order, at real topic changes, the first at 0:00."""


def _seconds(text):
    parts = [int(p) for p in re.findall(r"\d+", str(text))][-3:]
    return sum(p * 60 ** i for i, p in enumerate(reversed(parts))) if parts else None


def youtube_metadata(lines, provider, base_url, model, max_chars=CHUNK_CHARS * 2):
    """lines: [(record_seconds, text)] -> {title, description, tags, chapters: [(seconds, title)]}."""
    rows = [f"[{int(t) // 60}:{int(t) % 60:02d}] {text}" for t, text in lines]
    step = max(1, sum(map(len, rows)) // max_chars + 1)  # long videos: sample evenly to fit the context
    reply = chat(provider, base_url, model, METADATA_PROMPT, "\n".join(rows[::step]))
    reply = re.sub(r"<think>.*?</think>", "", reply, flags=re.S)
    try:
        data = json.loads(reply[reply.find("{"):reply.rfind("}") + 1])
    except json.JSONDecodeError as e:
        raise LLMParseError(f"Metadata reply was not valid JSON: {reply[:200]!r}") from e
    chapters = [(_seconds(c.get("time")), str(c.get("title", "")).strip()) for c in data.get("chapters", [])
                if isinstance(c, dict)]
    return {"title": str(data.get("title", "")).strip(), "description": str(data.get("description", "")).strip(),
            "tags": [str(t).strip() for t in data.get("tags", []) if str(t).strip()][:15],
            "chapters": [(s, n) for s, n in chapters if s is not None and n]}


# ---------------------------------------------------------------- agent with tools (local "coding agent" for the edit)

def capabilities(provider, base_url, model):
    """What a local model can do: {"tools": bool, "vision": bool}. Ollama says so in /api/show; for other servers
    assume tools (most OpenAI-compatible servers accept them) and no pictures."""
    if provider != "ollama":
        return {"tools": True, "vision": False}
    try:
        caps = _http(f"{base_url.rstrip('/')}/api/show", {"model": model}).get("capabilities") or []
    except LLMError:
        caps = []
    return {"tools": "tools" in caps, "vision": "vision" in caps}


def agent_loop(provider, base_url, model, system, messages, tools, call_tool, on_event=lambda kind, text: None,
               max_steps=16, check_cancel=lambda: None, num_ctx=16384, vision=False):
    """Run a tool-using conversation until the model answers without calling a tool.

    tools: [{"name", "description", "input_schema"}]; call_tool(name, args) -> {"content": [...], "isError": bool}
    (MCP-shaped, the same tools coding agents get over MCP). Pictures from tools go to the model only when it can
    see them (vision=True). Returns (final text, messages)."""
    base = base_url.rstrip("/")
    spec = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                              "parameters": t["input_schema"]}} for t in tools]
    msgs = [{"role": "system", "content": system}, *messages]
    for step in range(max_steps):
        check_cancel()
        if provider == "ollama":
            content, calls = [], []

            def line(raw):
                d = json.loads(raw)
                if "error" in d:
                    raise LLMError(f"Ollama error: {d['error']}")
                m = d.get("message") or {}
                content.append(m.get("content") or "")
                calls.extend(m.get("tool_calls") or [])

            _http(f"{base}/api/chat", {"model": model, "messages": msgs, "tools": spec, "stream": True, "think": False,
                                       "options": {"temperature": 0.2, "num_ctx": num_ctx}}, 300, line)
            text = "".join(content).strip()
            calls = [{"id": f"call{step}_{k}", "name": c["function"]["name"],
                      "args": c["function"].get("arguments") or {}} for k, c in enumerate(calls)]
            msgs.append({"role": "assistant", "content": text,
                         "tool_calls": [{"function": {"name": c["name"], "arguments": c["args"]}} for c in calls]})
        else:  # OpenAI-compatible (LM Studio…): not streamed, so the tool-call pieces needn't be reassembled
            r = _http(f"{base}/chat/completions", {"model": model, "messages": msgs, "tools": spec,
                                                   "temperature": 0.2}, 600)
            m = (r.get("choices") or [{}])[0].get("message") or {}
            text = (m.get("content") or "").strip()
            calls = []
            for c in m.get("tool_calls") or []:
                try:
                    args = json.loads(c["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                calls.append({"id": c.get("id") or f"call{step}", "name": c["function"]["name"], "args": args})
            msgs.append({"role": "assistant", "content": text or None, "tool_calls": m.get("tool_calls")} if calls
                        else {"role": "assistant", "content": text})
        if text:
            on_event("say", text)
        if not calls:
            return text, msgs[1:]
        for c in calls:
            check_cancel()
            on_event("tool", f"{c['name']} {json.dumps(c['args'], ensure_ascii=False)[:200]}")
            try:
                res = call_tool(c["name"], c["args"] if isinstance(c["args"], dict) else {})
            except Exception as e:  # a failing tool is the model's problem to solve, not a crash
                res = {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True}
            out = "\n".join(x["text"] for x in res["content"] if x["type"] == "text")[:8000] or "(no text)"
            pics = [x["data"] for x in res["content"] if x["type"] == "image"]
            on_event("result", ("ERROR " if res.get("isError") else "") + out[:300])
            if provider == "ollama":
                msg = {"role": "tool", "content": out, "tool_name": c["name"]}
                if pics and vision:
                    msg["images"] = pics
                msgs.append(msg)
            else:
                msgs.append({"role": "tool", "tool_call_id": c["id"], "content": out})
    return "I stopped after the maximum number of steps; ask me to continue.", msgs[1:]

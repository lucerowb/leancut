"""The tools an agent gets to edit a LeanCut project: the same set for the built-in local agent and for coding agents
connected over MCP (mcp_server.py proxies /api/agent/tools and /api/agent/call to here).

Every change goes through the edit model's validated actions (editing.apply_action), so an agent can't corrupt a
project and everything it does is one undo away. Results are MCP-shaped: {"content": [...], "isError": bool}.
`A` is the running app module (its project, settings and helpers), passed in so this file holds no state.
"""
import base64
import json
import re
from pathlib import Path

import editing
import llm_agent

MANUAL = """# LeanCut for agents

You edit a vlog in LeanCut, a local video editor. The footage is already transcribed and cut by its AI; you refine
the edit and add motion graphics. You never touch video files: you change the edit with tools, and LeanCut renders.

## The edit
- `get_edit` shows the project: every transcript line (`c0.s3`) and B-roll window (`c0.b1`), KEEP or cut, its
  score, effects, titles and graphics. The video plays the kept items in clip order, then source order.
- `apply_actions` changes it with validated actions (keep/cut, trim, split, cut_words, effect, transition, grade,
  text, intro/outro, B-roll placement, graphic...). Invalid actions are skipped and reported. Everything is undoable
  (`undo`).
- Times: action times are *source* seconds of a clip; `look`, `add_graphic` and the timeline use *programme*
  seconds (the finished video's time).

## Motion graphics
A graphic is a web page (`graphics/<name>.html` in the project folder) laid over the video for a while. It draws
any moment with one function, the same contract as OpenFilm's pages:

    <script type="module">
    window.film = {
      frame(t) { /* draw the whole picture at t seconds (may return a Promise) */ },
      duration: 4,          // optional: its own length
      ready: loadFonts(),   // optional: a Promise to wait for
    };
    </script>

The one rule: the same t always draws the same picture, whatever was drawn before (no animation loops, no
Math.random() without a seed, no Date.now()). The stage is 1920x1080 CSS px; what you don't paint is transparent, so
the footage shows through. No network: system fonts only, everything inline or in files next to the page.
- `list_templates` + `add_graphic` (template + fields) is the fast way: titles, lower thirds, chapter cards,
  location pins, end screens, quotes, timers, a speedometer and a route map from the ride's GPS (`get_telemetry`).
- For anything else, `write_page` your own page, `check_page` it (page errors, fonts, text off the frame, and
  whether the same t draws the same picture), then `add_graphic` with `page`.

## Check your work
`look` shows the finished video at a moment, rendered exactly as the export will be (colour, effects, titles,
captions, B-roll, graphics). Look after every visual change. `look_sheet` shows a strip of moments.
Ask before exporting: `export` renders the final files.
"""


def _text(s, err=False):
    return {"content": [{"type": "text", "text": str(s)}], "isError": err}


def _image(jpeg, caption=""):
    return {"content": [{"type": "image", "data": base64.b64encode(jpeg).decode(), "mimeType": "image/jpeg"},
                        *([{"type": "text", "text": caption}] if caption else [])], "isError": False}


TOOLS = [
    ("get_edit", "The project as a list of every line and B-roll window with its id, source time, KEEP/cut, score, "
                 "effects, titles and graphics, plus the finished video's length. Start here.",
     {"full_text": {"type": "boolean", "description": "full transcript lines instead of the first 70 characters"}}, []),
    ("apply_actions", "Change the edit. actions: a list of action objects, e.g. {\"type\": \"cut\", \"ids\": [\"c0.s4\"]}. "
                      "Vocabulary:\n" + llm_agent.ASSISTANT_PROMPT.split("Action types (use only these, with only the listed fields):\n")[1].split("Rules:")[0],
     {"actions": {"type": "array", "items": {"type": "object"}}}, ["actions"]),
    ("undo", "Undo the last change.", {}, []),
    ("redo", "Redo the last undone change.", {}, []),
    ("look", "A frame of the finished video at programme time t (seconds), rendered exactly like the export: colour, "
             "effects, titles, captions, B-roll and graphics.", {"t": {"type": "number"}}, ["t"]),
    ("look_sheet", "A contact sheet: n frames from programme time a to b (quick source frames, no graphics).",
     {"a": {"type": "number"}, "b": {"type": "number"}, "n": {"type": "integer", "description": "2-12, default 8"}}, []),
    ("list_templates", "Motion-graphics templates with their fields and default length.", {}, []),
    ("add_graphic", "Lay a graphic over the video from programme time `at` for `duration` s. Give either template + "
                    "fields (see list_templates; for speedometer/route_map pass clip to use its GPS) or page (a page "
                    "you wrote with write_page).",
     {"at": {"type": "number"}, "duration": {"type": "number"}, "template": {"type": "string"},
      "fields": {"type": "object"}, "page": {"type": "string", "description": "e.g. graphics/my_title.html"},
      "clip": {"type": "integer", "description": "clip index for telemetry templates"}}, ["at"]),
    ("update_graphic", "Move, retime or remove a graphic by its id (g…, see get_edit). remove=true deletes it.",
     {"id": {"type": "string"}, "at": {"type": "number"}, "duration": {"type": "number"},
      "remove": {"type": "boolean"}}, ["id"]),
    ("write_page", "Write a motion-graphics page (the whole HTML) into the project's graphics folder. Returns its path.",
     {"name": {"type": "string", "description": "file name, e.g. intro_title"}, "html": {"type": "string"}},
     ["name", "html"]),
    ("read_page", "Read a page from the graphics folder.", {"page": {"type": "string"}}, ["page"]),
    ("check_page", "Check a page: errors, fonts, text cut off by the frame, and whether the same t draws the same "
                   "picture. Fix every problem it names.", {"page": {"type": "string"}}, ["page"]),
    ("get_telemetry", "GoPro GPS of a clip (speed, altitude, position over time) and its highlights, for data "
                      "graphics.", {"clip": {"type": "integer"}}, ["clip"]),
    ("search_footage", "Find shots by what's on screen (on-device CLIP): returns clip id + source time of the best "
                       "matches.", {"query": {"type": "string"}}, ["query"]),
    ("job_status", "What LeanCut is doing (analysis, preview or export) and its last log lines.", {}, []),
    ("render_preview", "Start an exact 540p preview render of the whole edit (runs in the background).", {}, []),
    ("export", "Render the final video and edit lists into the export folder. Only when the person asks.", {}, []),
]


def tool_list():
    return [{"name": n, "description": d, "input_schema": {"type": "object", "properties": props, "required": req}}
            for n, d, props, req in TOOLS]


def call(A, name, args):
    """Run a tool; never raises (errors come back as isError results the agent can act on)."""
    fn = globals().get(f"_t_{name}")
    if not fn:
        return _text(f"Unknown tool '{name}'.", True)
    try:
        return fn(A, args or {})
    except Exception as e:  # noqa: BLE001 - the agent sees the reason and can retry differently
        return _text(f"{name} failed: {e}", True)


# ---------------------------------------------------------------- edit

def _t_get_edit(A, args):
    plan = A.get_plan()
    gr = [f"{g['id']}: {g['page']} on {iid} at +{g['offset']}s for {g['duration']}s"
          for iid, _, _, g in editing.all_graphics(A.project)]
    return _text(editing.context(A.project, 400 if args.get("full_text") else 70)
                 + f"\n\nFinished video: {plan['duration']:.1f} s in {len(plan['parts'])} parts."
                 + ("\nGraphics: " + "; ".join(gr) if gr else "\nNo graphics yet."))


def _t_apply_actions(A, args):
    acts = args.get("actions")
    if isinstance(acts, str):
        acts = json.loads(acts)
    if not isinstance(acts, list) or not acts:
        return _text("actions must be a non-empty list of action objects.", True)
    done, errors = A.apply_edit(acts, "agent")
    return _text("\n".join([*(f"done: {d}" for d in done), *(f"skipped: {e}" for e in errors)]) or "Nothing changed.",
                 bool(errors) and not done)


def _t_undo(A, args):
    A.post_undo()
    return _text("Undone.")


def _t_redo(A, args):
    A.post_redo()
    return _text("Redone.")


# ---------------------------------------------------------------- look

def _t_look(A, args):
    t = float(args.get("t", 0))
    jpeg, what = A.programme_still(t)
    return _image(jpeg, f"t = {t:.2f} s · {what}")


def _t_look_sheet(A, args):
    plan = A.get_plan()
    a = max(0.0, float(args.get("a", 0)))
    b = min(plan["duration"], float(args.get("b", plan["duration"])))
    n = max(2, min(12, int(args.get("n", 8))))
    return _image(A.contact_sheet([a + (b - a) * (k + 0.5) / n for k in range(n)]),
                  f"{n} frames from {a:.1f} s to {b:.1f} s, left to right, top to bottom")


# ---------------------------------------------------------------- graphics

def _gm(A):
    gm = A._optional("graphics")
    if not gm:
        raise RuntimeError("motion graphics need graphics.py")
    return gm


def _t_list_templates(A, args):
    return _text(json.dumps(_gm(A).list_templates(), indent=1, ensure_ascii=False))


def _page_path(A, page):
    page = str(page).replace("\\", "/")
    if not page.startswith("graphics/"):
        page = "graphics/" + page
    if not page.endswith(".html"):
        page += ".html"
    if not editing.PAGE_RE.fullmatch(page):
        raise ValueError("page names may use letters, digits, - _ . and spaces")
    return page, A.project_dir() / page


def _t_write_page(A, args):
    page, path = _page_path(A, re.sub(r"[^\w\- .]", "_", str(args["name"]).strip())[:70] or "page")
    html = str(args["html"])
    if "window.film" not in html:
        return _text("A page must set window.film = { frame(t) {...} } (see the manual).", True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    return _text(f"Wrote {page}. Next: check_page, then add_graphic with page={page}.")


def _t_read_page(A, args):
    page, path = _page_path(A, args["page"])
    return _text(path.read_text(encoding="utf-8") if path.exists() else f"No page {page}.", not path.exists())


def _t_check_page(A, args):
    page, path = _page_path(A, args["page"])
    if not path.exists():
        return _text(f"No page {page}.", True)
    problems = A.check_graphic(path)
    return _text("No problems found." if not problems else "Problems:\n- " + "\n- ".join(problems), bool(problems))


def _anchor(A, at):
    """Programme time -> (item id, offset in source seconds into that item)."""
    plan = A.get_plan()
    p = next((p for p in plan["parts"] if p["rec_start"] <= at < p["rec_end"] and p["ids"]), None)
    if not p:
        p = min((p for p in plan["parts"] if p["ids"]), key=lambda p: abs(p["rec_start"] - at), default=None)
        if not p:
            raise ValueError("the edit is empty: keep some lines first")
        at = max(p["rec_start"], min(at, p["rec_end"] - 0.1))
    src = p["start"] + (at - p["rec_start"]) * p["speed"]
    best = None
    for iid in p["ids"]:
        it = editing.find(A.project, iid)[3]
        d = 0 if it["start"] <= src < it["end"] else min(abs(it["start"] - src), abs(it["end"] - src))
        if best is None or d < best[0]:
            best = (d, iid, it)
    return best[1], max(0.0, src - best[2]["start"])


def _t_add_graphic(A, args):
    at = float(args["at"])
    dur = args.get("duration")
    if args.get("page"):
        page, path = _page_path(A, args["page"])
        if not path.exists():
            return _text(f"No page {page}: write_page it first.", True)
        template = None
    elif args.get("template"):
        gm = _gm(A)
        template = str(args["template"])
        meta = next((t for t in gm.list_templates() if t["name"] == template), None)
        if not meta:
            return _text(f"No template '{template}'. Templates: " + ", ".join(t["name"] for t in gm.list_templates()), True)
        fields = dict(args.get("fields") or {})
        data = A.telemetry_series(int(args["clip"])) if args.get("clip") is not None else None
        n = len(list(editing.all_graphics(A.project))) + 1
        page, path = _page_path(A, f"{template}_{n}")
        while path.exists():
            n += 1
            page, path = _page_path(A, f"{template}_{n}")
        path.parent.mkdir(parents=True, exist_ok=True)
        if data:
            fields["data"] = data
        gm.instantiate(template, fields, path)
        dur = dur or meta.get("duration", 4)
    else:
        return _text("Give a template (with fields) or a page.", True)
    iid, off = _anchor(A, at)
    done, errors = A.apply_edit([{"type": "graphic", "ids": [iid], "value": page, "offset": round(off, 2),
                                  "duration": float(dur or 4), **({"text": template} if template else {})}], "agent")
    return _text("; ".join(done + errors) + f". Check it with look at t={at + 0.5:.1f}.", bool(errors))


def _t_update_graphic(A, args):
    gid = str(args["id"])
    if args.get("remove"):
        acts = [{"type": "remove_graphic", "value": gid}]
    else:
        a = {"type": "edit_graphic", "value": gid}
        if args.get("at") is not None:
            iid, off = _anchor(A, float(args["at"]))
            a.update(ids=[iid], offset=round(off, 2))
        if args.get("duration") is not None:
            a["duration"] = float(args["duration"])
        acts = [a]
    done, errors = A.apply_edit(acts, "agent")
    return _text("; ".join(done + errors), bool(errors))


# ---------------------------------------------------------------- footage, data, jobs

def _t_get_telemetry(A, args):
    data = A.telemetry_series(int(args["clip"]))
    if not data:
        return _text("No GoPro GPS telemetry in that clip.", True)
    k = max(1, len(data["t"]) // 60)  # a readable summary; templates get the full series
    rows = [f"{data['t'][i]:.0f}s {data['speed_kmh'][i]:.0f} km/h alt {data['alt'][i]:.0f} m"
            for i in range(0, len(data["t"]), k)]
    return _text(f"{len(data['t'])} samples. Highlights: {json.dumps(data.get('highlights', []))}\n" + "\n".join(rows))


def _t_search_footage(A, args):
    hits = A.search_hits(str(args["query"]))
    return _text("\n".join(f"clip {h['clip']} ({h['name']}) at {h['t']:.1f} s · score {h['score']:.2f}" for h in hits)
                 or "Nothing matched.")


def _t_job_status(A, args):
    j = A.job
    return _text(f"{j['name'] or 'Nothing'}: {'running' if j['running'] else (j['error'] or 'done')} · {j['step']} "
                 f"{j['progress']:.0%}\n" + "\n".join(j["logs"][-8:]))


def _t_render_preview(A, args):
    A.render_preview()
    return _text("Preview render started; poll job_status.")


def _t_export(A, args):
    A.run_export()
    return _text("Export started; poll job_status. Files land in the export folder.")

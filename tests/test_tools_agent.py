"""The tools agents use (built-in local agent and coding agents over MCP). Run: python -m pytest -q tests/test_tools_agent.py (named to run after test_core, which needs a fresh data folder)"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("LEANCUT_HOME", tempfile.mkdtemp())

import editing  # noqa: E402
from test_editing import project  # noqa: E402


def client():
    import app
    from fastapi.testclient import TestClient
    app.new_project("agents")
    app.project["clips"] = project()["clips"]
    return app, TestClient(app.api, headers={"host": "127.0.0.1:8765"})


def call(c, name, args=None):
    return c.post("/api/agent/call", json={"name": name, "arguments": args or {}}).json()


def test_tools_are_listed_with_schemas_and_manual():
    app, c = client()
    tools = c.get("/api/agent/tools").json()["tools"]
    names = {t["name"] for t in tools}
    assert {"get_edit", "apply_actions", "look", "add_graphic", "write_page", "check_page", "undo"} <= names
    assert all(t["input_schema"]["type"] == "object" for t in tools)
    assert "window.film" in c.get("/api/agent/manual").json()["text"]


def test_edit_through_tools_is_validated_and_undoable():
    app, c = client()
    assert "c0.s0" in call(c, "get_edit")["content"][0]["text"]
    r = call(c, "apply_actions", {"actions": [{"type": "cut", "ids": ["c0.s0"]}, {"type": "explode"}]})
    assert not r["isError"] and "done: Cut 1" in r["content"][0]["text"] and "skipped" in r["content"][0]["text"]
    assert not app.project["clips"][0]["segments"][0]["keep"]
    call(c, "undo")
    assert app.project["clips"][0]["segments"][0]["keep"]
    assert call(c, "apply_actions", {"actions": []})["isError"]
    assert call(c, "no_such_tool")["isError"]


def test_pages_stay_in_the_graphics_folder():
    app, c = client()
    html = "<script>window.film = {duration: 2, frame(t) {}}</script>"
    r = call(c, "write_page", {"name": "../../evil", "html": html})
    assert not r["isError"]
    written = list((app.project_dir() / "graphics").glob("*.html"))
    assert [p.name for p in written] == [".._.._evil.html"] or all(p.parent.name == "graphics" for p in written)
    assert call(c, "write_page", {"name": "x", "html": "<p>no film</p>"})["isError"]
    assert c.get(f"/api/graphics/page/{app.project['id']}/../project.json").status_code == 404


def test_graphics_run_on_across_cuts():
    app, c = client()
    page = app.project_dir() / "graphics" / "t.html"
    page.parent.mkdir(exist_ok=True)
    page.write_text("<script>window.film = {duration: 6, frame(t) {}}</script>", encoding="utf-8")
    r = call(c, "add_graphic", {"page": "graphics/t.html", "at": 2.0, "duration": 6})
    assert not r["isError"], r
    gid = next(editing.all_graphics(app.project))[3]["id"]
    plan = c.get("/api/plan").json()
    slices = [(g["in"], round(g["t1"] - g["t0"], 2)) for p in plan["parts"] for g in p["graphics"]]
    assert len(slices) >= 2 and slices[0][0] == 0 and abs(sum(d for _, d in slices) - 6) < 0.05
    assert not call(c, "update_graphic", {"id": gid, "duration": 3})["isError"]
    assert next(editing.all_graphics(app.project))[3]["duration"] == 3
    assert not call(c, "update_graphic", {"id": gid, "remove": True})["isError"]
    assert not list(editing.all_graphics(app.project))
    assert editing.apply_actions(app.project, [{"type": "graphic", "ids": ["c0.s0"], "value": "../x.html"}])[1]

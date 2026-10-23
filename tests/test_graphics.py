"""Tests for motion graphics. Run: python -m pytest -q tests/test_graphics.py

The first group needs no browser. The second draws real pages in headless Chrome and is skipped when no Chrome,
Chromium, Edge or Brave is installed (set LEANCUT_CHROME to point at one).
"""
import json
import os
import re
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("LEANCUT_HOME", tempfile.mkdtemp())

import graphics as g  # noqa: E402
import video_engine as ve  # noqa: E402

REQUIRED = {"title", "lower_third", "chapter", "location", "end_screen", "quote", "countdown_timer", "speedometer",
            "route_map", "stat_card"}


# ---------------------------------------------------------------- no browser needed

def test_templates_list_and_declare_fields():
    ts = {t["name"]: t for t in g.list_templates()}
    assert REQUIRED <= set(ts)
    for name, t in ts.items():
        assert t["description"] and t["duration"] > 0 and t["fields"], name
        assert all("label" in f and "default" in f for f in t["fields"].values()), name
        src = (g.TEMPLATES / f"{name}.html").read_text(encoding="utf-8")
        assert "window.film" in src and "{{duration}}" in src, name
        assert not re.search(r"(src|href)=[\"']?(https?:)?//", src), f"{name} loads something from the network"
        body = src.replace(g._META.search(src).group(0), "")
        assert set(re.findall(r"\{\{(\w+)\}\}", body)) <= set(t["fields"]) | {"duration"}, name


def test_instantiate_escapes_text_and_embeds_data(tmp_path):
    evil = '<script>alert(1)</script> & "quoted" {{detail}}'
    out = g.instantiate("lower_third", {"name": evil, "duration": "7.5"}, tmp_path / "lt.html")
    html = out.read_text(encoding="utf-8")
    assert "<script>alert(1)" not in html and "&lt;script&gt;alert(1)&lt;/script&gt; &amp; &quot;quoted&quot;" in html
    assert "{{detail}}" in html  # a value is never substituted again
    assert "Riding the Kali Gandaki valley" in html and 'data-duration="7.5"' in html  # defaults fill the rest
    data = {"t": [0, 1], "lat": [28.2, 28.3], "lon": [83.9, 84.0], "note": "</script><b>x</b> "}
    html = g.instantiate("route_map", {"data": data}, tmp_path / "map.html").read_text(encoding="utf-8")
    embedded = re.search(r'<script type="application/json" id="data">(.*?)</script>', html, re.S).group(1)
    assert "</" not in embedded and json.loads(embedded) == data
    html = g.instantiate("speedometer", {"data": json.dumps({"t": [0], "speed_kmh": [5]})}, tmp_path / "s.html").read_text()
    assert '{"t": [0], "speed_kmh": [5]}' in html
    with pytest.raises(g.GraphicsError):
        g.instantiate("../app", {}, tmp_path / "x.html")
    with pytest.raises(g.GraphicsError):
        g.instantiate("nope", {}, tmp_path / "x.html")


def test_inject_host_goes_first_in_head():
    page = '<!doctype html><html lang="en"><head data-x="1"><script>window.film = {}</script></head><body></body></html>'
    out = g.inject_host(page, '/static/graphics-host.js?v="1"')
    assert out.index("graphics-host.js") < out.index("window.film")
    assert '<head data-x="1"><script src="/static/graphics-host.js?v=&quot;1&quot;"></script>' in out
    assert g.inject_host("<html><body>x</body></html>", "h.js").startswith('<html><script src="h.js"></script>')
    assert g.inject_host("<p>bare</p>", "h.js") == '<script src="h.js"></script><p>bare</p>'


def test_problem_wording():
    assert g.fmt_times([1.5, 0.25]) == "0.25 s, 1.5 s"
    assert g.fmt_times([0.1, 0.2, 0.3, 0.4, 0.5]) == "0.1 s, 0.2 s, 0.3 s … (5 frames)"


def test_cache_key_follows_page_assets_and_settings(tmp_path):
    page = tmp_path / "a.html"
    page.write_text("<p>one</p>")
    key = g.cache_key(page, 30, 4, 1920, 1080)
    assert g.cache_key(page.as_uri(), 30, 4, 1920, 1080) == key  # a path and its file URL are the same page
    assert g.cache_key(page, 25, 4, 1920, 1080) != key and g.cache_key(page, 30, 4, 1280, 720) != key
    (tmp_path / "b.html").write_text("<p>a neighbour</p>")
    assert g.cache_key(page, 30, 4, 1920, 1080) == key  # other pages in the folder don't count
    (tmp_path / "track.json").write_text("[1, 2]")
    k2 = g.cache_key(page, 30, 4, 1920, 1080)
    assert k2 != key  # files a page may load do
    page.write_text("<p>two</p>")
    assert g.cache_key(page, 30, 4, 1920, 1080) != k2


def test_browser_discovery_and_shell_install(tmp_path, monkeypatch):
    exe = tmp_path / "my-chrome"
    exe.write_text("")
    monkeypatch.setenv("LEANCUT_CHROME", str(exe))
    assert g.find_browser() == str(exe) and g.ensure_browser(tmp_path / "models", log=lambda m: None) == str(exe)
    monkeypatch.setenv("LEANCUT_CHROME", str(tmp_path / "missing"))
    assert g.find_browser() is None
    plat = g._cft_platform()
    assert plat in g.CFT_SIZES
    # the unpacking of a Chrome for Testing archive (a fake one: no network in tests)
    z = tmp_path / "shell.zip"
    name = "chrome-headless-shell.exe" if plat.startswith("win") else "chrome-headless-shell"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr(f"chrome-headless-shell-{plat}/{name}", "#!/bin/sh\n")
        f.writestr(f"chrome-headless-shell-{plat}/libGLESv2.so", "")
    dest = tmp_path / "models" / f"chrome-headless-shell-{g.CFT_VERSION}"
    got = g._install_zip(z, dest, plat)
    assert got == g._shell_path(tmp_path / "models", plat) and got.is_file()
    if os.name != "nt":
        assert os.access(got, os.X_OK)


# ---------------------------------------------------------------- real drawing in headless Chrome

needs_browser = pytest.mark.skipif(not g.find_browser(), reason="no Chrome/Chromium/Edge/Brave installed")


@pytest.fixture(scope="module")
def browser():
    with g.Browser() as b:
        yield b


def _page(tmp_path, name, script, extra=""):
    p = tmp_path / name
    p.write_text(f"<!doctype html><html><head><meta charset='utf-8'></head><body>{extra}"
                 f"<div id='b' style='position:absolute;top:100px;width:200px;height:200px;background:#f80'></div>"
                 f"<script>{script}</script></body></html>", encoding="utf-8")
    return p


@needs_browser
def test_template_frame_is_transparent_and_drawn(browser, tmp_path):
    page = g.instantiate("title", {"title": "Thorong La", "subtitle": "5,416 m"}, tmp_path / "title.html")
    with browser.page(page, 960, 540) as p:
        assert p.meta()["duration"] == 5
        px = g.rgba(p.frame_png(2.0))
        assert p.errors == []
    assert px.shape == (540, 960, 4)
    assert all(px[y, x, 3] == 0 for y, x in ((0, 0), (0, -1), (-1, 0), (-1, -1)))  # unpainted = transparent
    assert px[..., 3].max() == 255 and (px[..., 3] > 0).mean() < 0.3  # the title is drawn, the rest is clear


@needs_browser
def test_every_template_passes_the_look_check(browser, tmp_path):
    for t in g.list_templates():
        page = g.instantiate(t["name"], {}, tmp_path / f"{t['name']}.html")
        assert g.check(page, width=960, height=540, samples=6, browser=browser) == [], t["name"]


@needs_browser
def test_check_catches_nondeterminism_and_errors(browser, tmp_path, monkeypatch):
    rnd = _page(tmp_path, "rnd.html", "window.film = {duration: 2, frame(t) {"
                "document.getElementById('b').style.left = (t * 300 + Math.random() * 400) + 'px'; }};",
                extra="<p style='position:absolute;left:600px;top:20px;font-size:40px'>This runs off the edge</p>")
    problems = g.check(rnd, width=640, height=360, browser=browser)
    assert any(p.startswith("not deterministic") for p in problems), problems
    assert any(p.startswith("text cut off by the edge") and "This runs off" in p for p in problems), problems
    bad = _page(tmp_path, "bad.html", "window.film = {frame(t) { if (t > 1) throw new Error('boom at ' + t); "
                "document.getElementById('b').style.left = t * 100 + 'px'; }};", extra="<img src='missing.png'>")
    text = "\n".join(g.check(bad, width=640, height=360, samples=4, browser=browser))
    assert "duration is missing" in text and "boom at" in text and "missing.png" in text
    monkeypatch.setattr(g, "FILM_WAIT_MS", 300)
    none = _page(tmp_path, "none.html", "document.title = 'not a film';")
    assert "window.film is missing" in g.check(none, 2, 640, 360, browser=browser)[0]


@needs_browser
def test_render_clip_has_alpha_duration_cache_and_overlays(browser, tmp_path):
    page = g.instantiate("stat_card", {"duration": 1.2}, tmp_path / "pages" / "stats.html")
    steps = []
    mov = g.render_clip(page, tmp_path / "clips", fps=10, width=640, height=360, on_progress=steps.append,
                        browser=browser)
    assert mov.suffix == ".mov" and mov.parent == tmp_path / "clips" and steps[-1] == 1.0
    info = ve._ffmpeg(["-i", str(mov)], check=False).stderr
    assert "qtrle" in info and "argb" in info and abs(ve.probe(mov)["duration"] - 1.2) < 0.05
    mtime = mov.stat().st_mtime_ns
    assert g.render_clip(page, tmp_path / "clips", fps=10, width=640, height=360, browser=browser) == mov
    assert mov.stat().st_mtime_ns == mtime  # cached: not drawn again
    # laid over a part by video_engine's overlay path: alpha kept, placed at `at`, no fades, the part's length kept
    base = tmp_path / "base.mp4"
    ve._ffmpeg(["-y", "-f", "lavfi", "-i", "color=c=0x204060:s=640x360:d=3:r=10", "-f", "lavfi", "-i", "sine=d=3",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(base)])
    fmt = {"width": 640, "height": 360, "fps": 10}
    spec = {"src": str(base), "start": 0.0, "end": 3.0, "vf": "null", "top": "fps=10,format=yuv420p", "af": "",
            "overlays": [{"src": str(mov), "start": 0.0, "end": 1.2, "at": 1.0, "vf": "scale=640:360", "fade": False}]}
    out, _ = ve.render_parts([spec], tmp_path / "over.mp4", fmt, "software", tmp_path / "parts", str(tmp_path))
    assert abs(ve.probe(out)["duration"] - 3.0) < 0.15
    before, during = ve.frame_gray(out, 0.5, 64, 36), ve.frame_gray(out, 1.9, 64, 36)
    assert before.std() < 1 and during.std() > 5  # plain before the graphic, the card over it after
    assert abs(int(during[0, 0]) - int(before[0, 0])) < 3  # transparent corners let the base through


@needs_browser
def test_preview_host_drives_a_sandboxed_page(browser, tmp_path):
    """static/graphics-host.js in a sandboxed iframe, driven the way the editor's monitor drives it. Served over
    HTTP like the app serves pages (an opaque-origin frame can't load file:// scripts)."""
    import functools
    import http.server
    import shutil
    import threading
    shutil.copy(Path(g.__file__).parent / "static" / "graphics-host.js", tmp_path / "graphics-host.js")
    page = g.instantiate("lower_third", {"name": "Srijan"}, tmp_path / "lt.html")
    page.write_text(g.inject_host(page.read_text(encoding="utf-8"), "graphics-host.js"), encoding="utf-8")
    broken = _page(tmp_path, "broken.html", "window.film = {duration: 1, frame(t) { throw new Error('nope'); }};")
    broken.write_text(g.inject_host(broken.read_text(encoding="utf-8"), "graphics-host.js"), encoding="utf-8")
    parent = tmp_path / "monitor.html"
    parent.write_text("""<!doctype html><body>
<iframe id="a" sandbox="allow-scripts" src="lt.html" width="640" height="360"></iframe>
<iframe id="b" sandbox="allow-scripts" src="broken.html" width="640" height="360"></iframe>
<script>
const replies = [];
addEventListener('message', (e) => replies.push(e.data));
window.ask = (id, msg) => new Promise((done) => {
  const seen = () => { const r = replies.find((x) => x.id === msg.id); if (r) done(r); else setTimeout(seen, 20); };
  document.getElementById(id).contentWindow.postMessage(msg, '*');
  seen();
});
window.film = {duration: 1, frame() {}};  // so the test browser opens this page like any other
</script></body>""", encoding="utf-8")
    quiet = type("Quiet", (http.server.SimpleHTTPRequestHandler,), {"log_message": lambda *a: None})
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(quiet, directory=str(tmp_path)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        _drive_monitor(browser, f"http://127.0.0.1:{srv.server_address[1]}/monitor.html")
    finally:
        srv.shutdown()


def _drive_monitor(browser, url):
    with browser.page(url, 1300, 400) as p:
        meta = p.evaluate("ask('a', {leancut: 'meta', id: 1})")
        assert meta == {"leancut": "meta", "id": 1, "ok": True, "duration": 5, "width": 640, "height": 360}
        assert p.evaluate("ask('a', {leancut: 'frame', id: 2, t: 1.5})") == {"leancut": "drawn", "id": 2, "ok": True}
        bad = p.evaluate("ask('b', {leancut: 'frame', id: 3, t: 0.5})")
        assert bad["ok"] is False and "nope" in bad["error"]

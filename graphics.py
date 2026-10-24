"""Motion graphics: animated HTML pages drawn frame by frame in headless Chrome, plus a template library.

A page follows the OpenFilm page contract, so pages are interchangeable with OpenFilm's:

    window.film = { frame(t) {...draw the whole picture at t; may return a Promise},
                    duration?: seconds, ready?: Promise | () => Promise, width?, height? }

The one rule: the same t always draws the same picture. Pages never animate by themselves (the host sets
`window.filmHost` before any page script runs). What a page does not paint is transparent.

Chrome is driven over the DevTools Protocol (one websocket, flattened sessions) with the small `websockets` package.
The browser is the user's Chrome / Chromium / Edge / Brave, or Chrome for Testing's chrome-headless-shell downloaded
once into the models folder (≈100–125 MB zipped, depending on the platform).
"""
import base64
import hashlib
import html
import json
import os
import platform
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import video_engine

TEMPLATES = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "graphics_templates"
ENGINE = 1  # bump when rendering changes, so cached clips are redrawn
HOST_PRELUDE = "window.filmHost = { version: '0.1', name: 'leancut' };"
FILM_WAIT_MS = 10000  # how long a page may take to set window.film after load (the contract says at least 10 s)

# Chrome for Testing, pinned. Sizes from storage.googleapis.com; the download must match them exactly.
CFT_VERSION = "156.0.8078.47"
CFT_SIZES = {"mac-arm64": 103927850, "mac-x64": 109256749, "win64": 126051683, "win32": 118998176,
             "linux64": 125799226, "linux-arm64": 125696256}
CFT_URL = "https://storage.googleapis.com/chrome-for-testing-public/{v}/{p}/chrome-headless-shell-{p}.zip"


class GraphicsError(RuntimeError):
    pass


# ---------------------------------------------------------------- finding (or fetching) a browser

def _cft_platform():
    arm = platform.machine().lower() in ("arm64", "aarch64")
    if sys.platform == "darwin":
        return "mac-arm64" if arm else "mac-x64"
    if os.name == "nt":
        return "win64" if sys.maxsize > 2 ** 32 else "win32"
    return "linux-arm64" if arm else "linux64"


def _candidates():
    if sys.platform == "darwin":
        apps = ["Google Chrome.app/Contents/MacOS/Google Chrome", "Chromium.app/Contents/MacOS/Chromium",
                "Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                "Brave Browser.app/Contents/MacOS/Brave Browser",
                "Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary"]
        return [str(Path(root) / a) for root in ("/Applications", Path.home() / "Applications") for a in apps]
    if os.name == "nt":
        rel = [r"Google\Chrome\Application\chrome.exe", r"Chromium\Application\chrome.exe",
               r"Microsoft\Edge\Application\msedge.exe", r"BraveSoftware\Brave-Browser\Application\brave.exe"]
        roots = [os.environ.get(k) for k in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
        return [str(Path(r) / x) for r in roots if r for x in rel]
    names = ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge",
             "microsoft-edge-stable", "brave-browser", "chrome"]
    return [p for n in names if (p := shutil.which(n))]


def find_browser():
    """LEANCUT_CHROME if set, else an installed Chrome, Chromium, Edge or Brave; None if there is none."""
    env = os.environ.get("LEANCUT_CHROME")
    if env:
        return env if Path(env).is_file() else None
    return next((c for c in _candidates() if Path(c).is_file()), None)


def _shell_path(models_dir, plat=None):
    plat = plat or _cft_platform()
    exe = "chrome-headless-shell.exe" if plat.startswith("win") else "chrome-headless-shell"
    return Path(models_dir) / f"chrome-headless-shell-{CFT_VERSION}" / f"chrome-headless-shell-{plat}" / exe


def _install_zip(zip_path, dest, plat):
    """Unpack the downloaded archive into dest and make the binaries executable; returns the shell's path."""
    with zipfile.ZipFile(zip_path) as z:
        bad = z.testzip()
        if bad:
            raise GraphicsError(f"The browser download is corrupt ({bad}). Try again.")
        z.extractall(dest)  # extractall drops absolute paths and '..' parts
    exe = _shell_path(dest.parent, plat)
    if not exe.is_file():
        raise GraphicsError("The browser download did not contain chrome-headless-shell.")
    if os.name != "nt":
        for f in exe.parent.rglob("*"):  # zipfile drops the executable bits
            if f.is_file():
                f.chmod(0o755)
    return exe


def ensure_browser(models_dir, log=print):
    """A browser to draw with: an installed one, else chrome-headless-shell (Chrome for Testing, pinned), downloaded
    once into models_dir (≈100–125 MB zipped, ≈200–300 MB unpacked). Later calls work offline."""
    found = find_browser()
    if found:
        return found
    plat = _cft_platform()
    exe = _shell_path(models_dir, plat)
    if exe.is_file():
        return str(exe)
    url, size = CFT_URL.format(v=CFT_VERSION, p=plat), CFT_SIZES[plat]
    dest = exe.parent.parent
    dest.mkdir(parents=True, exist_ok=True)
    part = dest / "download.zip.part"
    log(f"Downloading Chrome for Testing {CFT_VERSION} ({size / 1e6:.0f} MB) for motion graphics…")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "LeanCut"})
        with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as f:
            if not r.geturl().startswith("https://storage.googleapis.com/"):
                raise GraphicsError("The browser download was redirected somewhere unexpected.")
            shutil.copyfileobj(r, f, 1 << 20)
        if part.stat().st_size != size:
            raise GraphicsError(f"The browser download is incomplete ({part.stat().st_size} of {size} bytes). Try again.")
        exe = _install_zip(part, dest, plat)
    except urllib.error.URLError as e:
        raise GraphicsError(f"Couldn't download the browser for motion graphics ({e.reason}). "
                            "Install Google Chrome, or check the internet connection; it is only needed once.") from e
    finally:
        part.unlink(missing_ok=True)
    log("Browser ready.")
    return str(exe)


# ---------------------------------------------------------------- the browser and its pages (DevTools Protocol)

def _read_port_file(path):
    """The DevToolsActivePort lines, or [] while Chrome is still writing it (on Windows it's locked meanwhile)."""
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except OSError:  # not there yet, or PermissionError while Chrome holds it open
        return []


class Browser:
    """One headless browser process, reused for many pages. Use as a context manager."""

    def __init__(self, exe=None, timeout=90):  # a first start on a fresh Windows machine can be slow
        from websockets.sync.client import connect
        exe = exe or find_browser()
        if not exe:
            raise GraphicsError("No Chrome, Chromium, Edge or Brave found. Install one, or set LEANCUT_CHROME.")
        self.profile = Path(tempfile.mkdtemp(prefix="leancut-chrome-"))
        args = [str(exe), "--remote-debugging-port=0", f"--user-data-dir={self.profile}",
                "--allow-file-access-from-files", "--hide-scrollbars", "--force-device-scale-factor=1",
                "--mute-audio", "--disable-gpu-vsync", "--disable-frame-rate-limit", "--no-first-run",
                "--no-default-browser-check", "--disable-extensions", "--disable-sync",
                "--disable-background-networking", "--disable-component-update", "--disable-default-apps",
                "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
                "--disable-backgrounding-occluded-windows", "--disable-features=Translate,MediaRouter",
                "--password-store=basic", "--use-mock-keychain", "--font-render-hinting=none", "about:blank"]
        if "headless-shell" not in Path(exe).name:
            args.insert(1, "--headless=new")
        if getattr(os, "geteuid", lambda: 1)() == 0:  # Chrome refuses to sandbox as root (containers)
            args.insert(1, "--no-sandbox")
        self._log = open(self.profile / "stderr.log", "wb")
        self.proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=self._log,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            port_file, end = self.profile / "DevToolsActivePort", time.monotonic() + timeout
            while len(lines := _read_port_file(port_file)) < 2:
                if self.proc.poll() is not None:
                    raise GraphicsError(f"The browser exited at start: {self._stderr()}")
                if time.monotonic() > end:
                    raise GraphicsError("The browser did not start in time.")
                time.sleep(0.02)
            port, path = lines[:2]
            # entered by hand: kept open across calls; works with websockets 15–17+ without a legacy warning
            self.ws = connect(f"ws://127.0.0.1:{port}{path}", max_size=None, compression=None,
                              ping_interval=None, open_timeout=timeout).__enter__()
        except BaseException:
            self.close()
            raise
        self._id, self._pages = 0, {}

    def _stderr(self):
        try:
            return (self.profile / "stderr.log").read_text(errors="replace").strip()[-400:]
        except OSError:
            return ""

    def send(self, method, session=None, timeout=60, **params):
        self._id += 1
        mid = self._id
        msg = {"id": mid, "method": method, "params": params}
        if session:
            msg["sessionId"] = session
        self.ws.send(json.dumps(msg))
        end = time.monotonic() + timeout
        while True:
            m = self._recv(end, method)
            if m.get("id") == mid:
                if "error" in m:
                    raise GraphicsError(f"{method}: {m['error'].get('message')}")
                return m.get("result", {})
            self._dispatch(m)

    def pump(self, until, timeout, what):
        """Read events until until() is true."""
        end = time.monotonic() + timeout
        while not until():
            self._dispatch(self._recv(end, what))

    def _recv(self, end, what):
        try:
            return json.loads(self.ws.recv(timeout=max(0.01, end - time.monotonic())))
        except TimeoutError:
            raise GraphicsError(f"The browser did not finish {what} in time.") from None
        except Exception as e:  # the connection closed: the browser crashed or was killed
            video_engine.check_cancel()
            raise GraphicsError(f"The browser stopped ({type(e).__name__}). {self._stderr()}") from e

    def _dispatch(self, m):
        page = self._pages.get(m.get("sessionId"))
        if page:
            page._event(m.get("method"), m.get("params") or {})

    def page(self, url, width=1920, height=1080):
        return Page(self, url, width, height)

    def close(self):
        try:
            self.ws.send(json.dumps({"id": 0, "method": "Browser.close"}))  # closes its helper processes too
            self.proc.wait(timeout=5)
        except Exception:
            pass
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=10)
        try:
            self.ws.close()
        except Exception:
            pass
        self._log.close()
        for _ in range(20):  # Windows keeps files locked for a moment after the process ends
            shutil.rmtree(self.profile, ignore_errors=True)
            if not self.profile.exists():
                break
            time.sleep(0.1)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


_PREPARE = """(async () => {
  const end = Date.now() + %d;
  while (!(window.film && typeof window.film.frame === 'function') && Date.now() < end)
    await new Promise((r) => setTimeout(r, 25));
  if (!window.film) throw new Error('window.film is missing: this page does not follow the page contract');
  if (typeof window.film.frame !== 'function') throw new Error('window.film.frame is not a function');
  const r = window.film.ready;
  await (typeof r === 'function' ? r() : r);
  await document.fonts.ready;
  const f = window.film;
  return {duration: f.duration ?? null, width: f.width ?? null, height: f.height ?? null};
})()"""

_DRAW = """(async (t) => { await window.film.frame(t); await document.fonts.ready; })"""

_CUT_TEXT = """(() => {
  const W = innerWidth, H = innerHeight, out = [];
  const name = (el) => el.id ? '#' + el.id : el.tagName.toLowerCase() + [...el.classList].slice(0, 2).map((c) => '.' + c).join('');
  const shows = (el) => { for (let e = el; e; e = e.parentElement) { const cs = getComputedStyle(e);
    if (Number(cs.opacity) < 0.05 || cs.visibility === 'hidden' || cs.display === 'none') return false; } return true; };
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n = walker.nextNode(); n && out.length < 50; n = walker.nextNode()) {
    const el = n.parentElement;
    if (!el || !n.textContent.trim() || /^(SCRIPT|STYLE|NOSCRIPT|TITLE)$/.test(el.tagName)) continue;
    const range = document.createRange(); range.selectNodeContents(n);
    const r = range.getBoundingClientRect();
    if (!(r.width > 1 && r.height > 1) || !shows(el)) continue;
    const inside = Math.max(0, Math.min(r.right, W) - Math.max(r.left, 0)) * Math.max(0, Math.min(r.bottom, H) - Math.max(r.top, 0));
    if (r.left < -2 || r.top < -2 || r.right > W + 2 || r.bottom > H + 2)
      out.push({sel: name(el), text: n.textContent.trim().slice(0, 40), gone: inside === 0});
  }
  return out;
})()"""


class Page:
    """A tab showing one page. meta() reads window.film; frame_png(t) draws t and returns a transparent PNG."""

    def __init__(self, browser, url, width=1920, height=1080):
        self.b, self.url, self.errors, self.loaded = browser, _url(url), [], False
        self._requests = {}
        self.target = browser.send("Target.createTarget", url="about:blank")["targetId"]
        self.session = browser.send("Target.attachToTarget", targetId=self.target, flatten=True)["sessionId"]
        browser._pages[self.session] = self
        for domain in ("Page", "Runtime", "Log", "Network"):
            self._send(f"{domain}.enable")
        self.resize(width, height)
        self._send("Emulation.setDefaultBackgroundColorOverride", color={"r": 0, "g": 0, "b": 0, "a": 0})
        self._send("Page.addScriptToEvaluateOnNewDocument", source=HOST_PRELUDE)
        try:
            nav = self._send("Page.navigate", url=self.url)
            if nav.get("errorText"):
                raise GraphicsError(f"Couldn't open {self.url}: {nav['errorText']}")
            browser.pump(lambda: self.loaded, 30, "loading the page")
            self._meta = self._eval(_PREPARE % FILM_WAIT_MS, "getting the page ready")
        except GraphicsError as e:
            self.close()
            raise GraphicsError("; ".join([str(e), *self.errors[:3]])) from None

    def _send(self, method, timeout=60, **params):
        return self.b.send(method, session=self.session, timeout=timeout, **params)

    def _event(self, method, p):
        if method == "Page.loadEventFired":
            self.loaded = True
        elif method == "Runtime.exceptionThrown":
            d = p.get("exceptionDetails", {})
            self._error((d.get("exception") or {}).get("description") or d.get("text") or "uncaught exception")
        elif method == "Runtime.consoleAPICalled" and p.get("type") == "error":
            self._error("console.error: " + " ".join(str(a.get("value", a.get("description", ""))) for a in p.get("args", [])))
        elif method == "Log.entryAdded" and p.get("entry", {}).get("level") == "error":
            e = p["entry"]
            self._error(e.get("text", "") + (f" ({e['url']})" if e.get("url") else ""))
        elif method == "Network.requestWillBeSent":
            self._requests[p.get("requestId")] = p.get("request", {}).get("url")
        elif method == "Network.loadingFailed" and not p.get("canceled"):
            self._error(f"request failed: {self._requests.get(p.get('requestId'), '?')} ({p.get('errorText')})")

    def _error(self, text):
        text = text.strip().splitlines()[0][:300] if text.strip() else "error"
        if text not in self.errors:
            self.errors.append(text)

    def _eval(self, expr, what, timeout=60):
        r = self._send("Runtime.evaluate", timeout=timeout, expression=expr, awaitPromise=True, returnByValue=True)
        if r.get("exceptionDetails"):
            d = r["exceptionDetails"]
            msg = (d.get("exception") or {}).get("description") or d.get("text") or "error"
            raise GraphicsError(f"{what}: {msg.splitlines()[0]}")
        return (r.get("result") or {}).get("value")

    def resize(self, width, height):
        self.width, self.height = int(width), int(height)
        self._send("Emulation.setDeviceMetricsOverride", width=self.width, height=self.height, deviceScaleFactor=1,
                   mobile=False)

    def meta(self):
        return dict(self._meta)

    def draw(self, t):
        self._eval(f"{_DRAW}({float(t)!r})", f"drawing t={t:.3f}")

    def frame_png(self, t):
        self.draw(t)
        shot = self._send("Page.captureScreenshot", format="png", fromSurface=True, captureBeyondViewport=False,
                          optimizeForSpeed=True)
        return base64.b64decode(shot["data"])

    def evaluate(self, expr):
        return self._eval(expr, "evaluating")

    def close(self):
        self.b._pages.pop(self.session, None)
        try:
            self.b.send("Target.closeTarget", targetId=self.target, timeout=10)
        except GraphicsError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _url(x):
    """A URL from a URL or a local path."""
    x = str(x)
    return x if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]+://", x) or x.startswith("about:") else Path(x).resolve().as_uri()


def _local(url):
    u = urllib.parse.urlsplit(url)
    return Path(urllib.request.url2pathname(u.path)) if u.scheme == "file" else None


# ---------------------------------------------------------------- rendering

def cache_key(url, fps, duration, width, height):
    """sha1 of what decides the pictures: the page file's bytes, every other file in its folder (by size and time;
    other .html pages excluded, so editing one page does not invalidate its neighbours), and the render settings."""
    url = _url(url)
    sig = [ENGINE, url, float(fps), duration and float(duration), int(width), int(height)]
    page = _local(url)
    if page and page.is_file():
        sig.append(hashlib.sha1(page.read_bytes()).hexdigest())
        # shortcut: other pages in the folder are ignored; a page that iframes a sibling page needs that one touched
        for f in sorted(page.parent.rglob("*"))[:2000]:
            if f.is_file() and f.suffix.lower() not in (".html", ".htm", ".mov") and not f.name.startswith("."):
                st = f.stat()
                sig.append([f.relative_to(page.parent).as_posix(), st.st_size, st.st_mtime_ns])
    return hashlib.sha1(json.dumps(sig).encode()).hexdigest()[:20]


def frame_times(fps, duration):
    return [i / fps for i in range(max(1, round(duration * fps)))]


def render_clip(url, out_path, fps=30, duration=None, width=1920, height=1080, on_progress=None, cache=True,
                browser=None):
    """Render a page to a QuickTime Animation (qtrle, ARGB) .mov with alpha that video_engine can overlay.

    out_path: a folder (the clip is `<folder>/<cache key>.mov`, so unchanged pages are never drawn twice) or a .mov
    file (cached through a `<file>.key` beside it). duration: the page's own when None. Returns the .mov's path."""
    url = _url(url)
    out = Path(out_path)
    own = browser is None
    b = browser or Browser()
    try:
        with b.page(url, width, height) as page:
            m = page.meta()
            dur = float(duration or m["duration"] or 0)
            if not dur > 0:
                raise GraphicsError("The page has no duration: pass one.")
            key = cache_key(url, fps, dur, width, height)
            if out.suffix.lower() == ".mov":
                stamp = out.with_name(out.name + ".key")
                if cache and out.exists() and stamp.exists() and stamp.read_text() == key:
                    return out
            else:
                out.mkdir(parents=True, exist_ok=True)
                out, stamp = out / f"{key}.mov", None
                if cache and out.exists():
                    return out
            out.parent.mkdir(parents=True, exist_ok=True)
            times = frame_times(fps, dur)
            with tempfile.TemporaryDirectory(dir=out.parent) as tmp:
                for i, t in enumerate(times):
                    video_engine.check_cancel()
                    (Path(tmp) / f"{i:06d}.png").write_bytes(page.frame_png(t))
                    if on_progress:
                        on_progress((i + 1) / (len(times) + 1))
                if page.errors:
                    raise GraphicsError("The page reported errors: " + "; ".join(page.errors[:3]))
                partial = Path(tmp) / "clip.mov"
                video_engine._ffmpeg(["-y", "-framerate", str(fps), "-i", str(Path(tmp) / "%06d.png"),
                                      "-c:v", "qtrle", "-pix_fmt", "argb", "-r", str(fps), str(partial)])
                partial.replace(out)
            if stamp:
                stamp.write_text(key)
            if on_progress:
                on_progress(1.0)
            return out
    finally:
        if own:
            b.close()


def still(url, t, width=1920, height=1080, browser=None):
    """One frame at t as PNG bytes (transparent where the page paints nothing)."""
    own = browser is None
    b = browser or Browser()
    try:
        with b.page(url, width, height) as page:
            return page.frame_png(t)
    finally:
        if own:
            b.close()


def rgba(png):
    """PNG bytes -> h×w×4 uint8 numpy array, decoded by FFmpeg (no image library needed)."""
    import numpy as np
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "f.png"
        f.write_bytes(png)
        info = video_engine._ffmpeg(["-i", str(f)], check=False).stderr
        w, h = map(int, re.search(r"Video:.*?\b(\d{1,5})x(\d{1,5})\b", info).groups())
        raw = video_engine._ffmpeg(["-i", str(f), "-f", "rawvideo", "-pix_fmt", "rgba", "-"], binary=True).stdout
    return np.frombuffer(raw, dtype=np.uint8).reshape(h, w, 4)


def differs(a, b, limit=3.0, block=16):
    """Whether two PNGs show different pictures: the largest mean difference over 16×16 blocks is above `limit`
    (0–255), so anti-aliasing noise on a re-rasterised edge does not count."""
    if a == b:
        return False
    import numpy as np
    x, y = rgba(a).astype(np.int16), rgba(b).astype(np.int16)
    if x.shape != y.shape:
        return True
    d = np.abs(x - y).mean(axis=2)
    h, w = (d.shape[0] // block) * block, (d.shape[1] // block) * block
    return float(d[:h, :w].reshape(h // block, block, w // block, block).mean(axis=(1, 3)).max()) > limit


def fmt_times(times):
    """[0.5, 1.25, …] -> '0.5 s, 1.25 s' (the first three and a count when there are many)."""
    shown = [f"{round(t, 3):g} s" for t in sorted(times)]
    return ", ".join(shown) if len(shown) <= 4 else ", ".join(shown[:3]) + f" … ({len(shown)} frames)"


def check(url, duration=None, width=1920, height=1080, samples=8, browser=None):
    """The "look" check: what is wrong with a page, as plain sentences ([] when nothing is).

    Page errors (exceptions, console errors, failed requests), window.film missing or invalid, fonts that failed,
    frames that change when drawn in another order (sampled t's drawn twice in two shuffled orders), text cut off by
    the frame in two sampled frames or more, and a missing duration."""
    url = _url(url)
    own = browser is None
    b = browser or Browser()
    problems = []
    try:
        try:
            page = b.page(url, width, height)
        except GraphicsError as e:
            return [str(e)]
        with page:
            m = page.meta()
            d = m["duration"]
            film_dur = d if isinstance(d, (int, float)) and not isinstance(d, bool) and 0 < d < 1e6 else None
            if d is not None and film_dur is None:
                problems.append(f"window.film.duration must be a number of seconds > 0, not {m['duration']!r}")
            elif m["duration"] is None and not duration:
                problems.append("window.film.duration is missing: set it, or give the clip a length")
            for k in ("width", "height"):
                if m[k] is not None and not (isinstance(m[k], int) and m[k] > 0):
                    problems.append(f"window.film.{k}, when set, must be a positive whole number of pixels")
            dur = float(duration or film_dur or 5)
            times = [round(dur * (i + 0.5) / samples, 3) for i in range(samples)]
            first, second, cut = {}, {}, {}
            try:
                for order, shots in ((random.Random(1).sample(times, len(times)), first),
                                     (random.Random(2).sample(times, len(times)), second)):
                    for t in order:
                        shots[t] = page.frame_png(t)
                        if shots is first:
                            for c in page.evaluate(_CUT_TEXT) or []:
                                cut.setdefault((c["sel"], c["text"]), []).append(t)
            except GraphicsError as e:
                problems.append(str(e))
            changed = [t for t in times if t in first and t in second and differs(first[t], second[t])]
            if changed:
                problems.append(f"not deterministic: the same t drew different pictures at {fmt_times(changed)} "
                                "(frame(t) must depend only on t: no Math.random, Date, timers or state kept "
                                "between calls)")
            if len(first) > 1 and len({hashlib.sha1(p).digest() for p in first.values()}) == 1:
                problems.append("every sampled frame looks the same: frame(t) may not draw anything that changes with t")
            for (sel, text), ts in cut.items():
                if len(ts) >= 2:  # once can be text on its way in
                    problems.append(f'text cut off by the edge of the frame at {fmt_times(ts)}: "{text}" ({sel})')
            failed = page.evaluate("[...document.fonts].filter((f) => f.status === 'error').map((f) => f.family)")
            problems += [f"font failed to load: {f}" for f in dict.fromkeys(failed or [])]
            problems += [f"page error: {e}" for e in page.errors]
    finally:
        if own:
            b.close()
    return problems


# ---------------------------------------------------------------- templates

_META = re.compile(r'<script type="application/json" id="leancut-template">(.*?)</script>', re.S)
_FIELD = re.compile(r"\{\{(\w+)\}\}")


def _meta(path):
    m = _META.search(Path(path).read_text(encoding="utf-8"))
    if not m:
        raise GraphicsError(f"{Path(path).name} has no leancut-template block")
    return json.loads(m.group(1))


def list_templates():
    """[{name, description, duration, fields}] for every template, sorted by name."""
    out = []
    for f in sorted(TEMPLATES.glob("*.html")):
        meta = _meta(f)
        out.append({"name": f.stem, "description": meta.get("description", ""), "duration": meta.get("duration", 5),
                    "fields": meta.get("fields", {}), "title": meta.get("name", f.stem)})
    return out


def _json_for_script(value):
    """JSON safe inside <script type="application/json">: no '</script>', '<!--' or ']]>' can form."""
    if isinstance(value, str):
        value = json.loads(value)
    return (json.dumps(value, ensure_ascii=False).replace("&", "\\u0026").replace("<", "\\u003c")
            .replace(">", "\\u003e").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def instantiate(template_name, fields, dest_path):
    """Write a page from a template: {{field}} placeholders become the HTML-escaped values (defaults for missing
    ones), {{data}} becomes JSON for its <script type="application/json">, {{duration}} the clip length."""
    if not re.fullmatch(r"[a-z0-9_]+", str(template_name)) or not (TEMPLATES / f"{template_name}.html").is_file():
        raise GraphicsError(f"Unknown graphics template: {template_name}")
    src = (TEMPLATES / f"{template_name}.html").read_text(encoding="utf-8")
    meta = _meta(TEMPLATES / f"{template_name}.html")
    values = {k: v.get("default", "") for k, v in meta.get("fields", {}).items()}
    values["duration"] = meta.get("duration", 5)
    values.update({k: v for k, v in (fields or {}).items() if v is not None})
    try:
        values["duration"] = max(0.5, min(600.0, float(values["duration"])))
    except (TypeError, ValueError):
        values["duration"] = float(meta.get("duration", 5))

    def fill(m):
        k = m.group(1)
        if k not in values:
            return m.group(0)
        if k == "data":
            return _json_for_script(values[k])
        return html.escape(str(values[k]), quote=True)

    # the template's own description block keeps its literal text
    head, block, tail = src.partition(_META.search(src).group(0))
    out = _FIELD.sub(fill, head) + block + _FIELD.sub(fill, tail)
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_text(out, encoding="utf-8")
    tmp.replace(dest)
    return dest


def inject_host(page_html, host_src):
    """The page with graphics-host.js loaded first thing in <head>, for the editor's live preview (a sandboxed
    iframe the editor drives with postMessage)."""
    tag = f'<script src="{html.escape(str(host_src), quote=True)}"></script>'
    for pat in (r"<head\b[^>]*>", r"<html\b[^>]*>", r"<!doctype[^>]*>"):
        m = re.search(pat, page_html, re.I)
        if m:
            return page_html[:m.end()] + tag + page_html[m.end():]
    return tag + page_html

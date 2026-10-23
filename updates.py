"""Updates: one anonymous request to GitHub's latest-release API (nothing about the machine is sent), and an optional
in-app install of the release asset. Turn the check off in Settings › Updates.

Install flow: download() the asset → install() stages the new app next to the running one → restart() starts a small
detached helper and the app exits; the helper waits for this process to end, swaps the folders by rename, relaunches,
and only then deletes the old version. The user's data folder is never inside the app, so it is never touched.
"""
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

REPO = "lucerowb/leancut"
LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
ASSETS = {"darwin": "LeanCut-macOS.dmg", "win32": "LeanCut-Windows.zip", "linux": "LeanCut-Linux-x64.tar.gz"}
# github.com/<repo>/releases/download/… answers with a redirect to one of GitHub's asset hosts
ASSET_HOSTS = ("objects.githubusercontent.com", "release-assets.githubusercontent.com")
STAGE = ".leancut-update"  # folder next to the installed app holding the new version until restart
_helper = []  # argv of the staged helper, set by install(), run by restart()


def is_newer(candidate: str, current: str) -> bool:
    """Numeric dotted compare: '0.3.10' > '0.3.9'."""
    parts = lambda v: [int(p) if p.isdigit() else 0 for p in re.split(r"[.+-]", v.lstrip("v"))[:3]] + [0] * 3
    return parts(candidate)[:3] > parts(current)[:3]


def check(current: str) -> dict:
    req = urllib.request.Request(LATEST, headers={"Accept": "application/vnd.github+json", "User-Agent": "LeanCut"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            rel = json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise RuntimeError("no published release found") from e
        raise
    latest = rel["tag_name"].lstrip("v")
    assets = {a["name"]: a["browser_download_url"] for a in rel.get("assets", [])}
    asset = assets.get(ASSETS.get(sys.platform, ""))
    url = asset or rel["html_url"]
    return {"current": current, "latest": latest, "newer": is_newer(latest, current),
            "url": url if url.startswith("https://") else rel["html_url"], "notes_url": rel["html_url"],
            "notes": (rel.get("body") or "")[:4000],
            "can_install": bool(asset and trusted(asset)) and current_bundle() is not None}


def trusted(url: str, redirect: bool = False) -> bool:
    """Downloads start at our own GitHub repo; only its redirect may move to GitHub's asset hosts."""
    try:
        u = urllib.parse.urlsplit(url)
        if u.scheme != "https" or u.port not in (None, 443) or u.username or "\\" in url:
            return False
    except ValueError:
        return False
    if redirect and u.hostname in ASSET_HOSTS:
        return True
    return u.hostname == "github.com" and u.path.startswith(f"/{REPO}/") and ".." not in u.path.split("/")


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not trusted(newurl, redirect=True):
            raise ValueError(f"refusing a redirect to {urllib.parse.urlsplit(newurl).hostname}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url: str, dest, on_progress=lambda f: None) -> Path:
    """Stream the release asset to dest (via a .part file), reporting 0..1, and check the size GitHub announced."""
    if not trusted(url):
        raise ValueError("Not a LeanCut release download.")
    dest = Path(dest)
    part = dest.with_name(dest.name + ".part")
    opener = urllib.request.build_opener(_CheckedRedirects)
    req = urllib.request.Request(url, headers={"User-Agent": "LeanCut"})
    try:
        with opener.open(req, timeout=30) as r, open(part, "wb") as f:
            total, done = int(r.headers.get("Content-Length") or 0), 0
            if not total:
                raise RuntimeError("the download has no size, so it can't be verified")
            while chunk := r.read(1 << 20):
                f.write(chunk)
                done += len(chunk)
                on_progress(min(done / total, 1.0))
        if done != total:
            raise RuntimeError(f"download incomplete ({done} of {total} bytes)")
        return part.replace(dest)
    finally:
        part.unlink(missing_ok=True)


def current_bundle() -> Path | None:
    """The running LeanCut.app (macOS) or install folder (Windows/Linux); None when running from source."""
    if not getattr(sys, "frozen", False):
        return None
    exe = Path(sys.executable).resolve()
    if sys.platform == "darwin":  # …/LeanCut.app/Contents/MacOS/LeanCut
        return exe.parents[2] if exe.parents[2].suffix == ".app" else None
    return exe.parent


def install(downloaded, on_log=print) -> dict:
    """Stage the downloaded release next to the running app. Nothing is replaced until restart()."""
    bundle = current_bundle()
    if bundle is None:
        return {"restart": False, "message": "You're running LeanCut from source: update with `git pull`."}
    if (bundle / "settings.json").exists():  # never swap away a folder that holds user data
        return {"restart": False, "message": f"{bundle} also holds your LeanCut data; reinstall manually."}
    stage = bundle.parent / STAGE
    try:
        shutil.rmtree(stage, ignore_errors=True)
        stage.mkdir()
    except OSError:
        return {"restart": False, "message": f"LeanCut can't write to {bundle.parent}. Move it to a folder you own "
                                             "(on a Mac: Applications) or install the update manually."}
    try:
        new = _unpack(Path(downloaded), stage, on_log)
        if not (new / _exe_path()).is_file():
            raise RuntimeError(f"the download doesn't contain {new.name}/{_exe_path()}")
        script = stage / ("update.ps1" if sys.platform == "win32" else "update.sh")
        old = bundle.with_name(f".{bundle.name}.old")
        script.write_text(helper_script(sys.platform, bundle, new, old, os.getpid(), stage), encoding="utf-8")
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    _helper[:] = (["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File",
                   str(script)] if sys.platform == "win32" else ["/bin/sh", str(script)])
    on_log(f"Update staged in {stage}.")
    return {"restart": True, "message": "The update is ready. Restart LeanCut to finish."}


def restart() -> None:
    """Start the staged helper; the caller must then exit the app so the helper can swap it."""
    if not _helper:
        raise RuntimeError("No update is staged.")
    # The new copy must not inherit this PyInstaller process's private env (bundled lib paths, _PYI_* state).
    env = {**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"}
    if "LD_LIBRARY_PATH_ORIG" in env:
        env["LD_LIBRARY_PATH"] = env.pop("LD_LIBRARY_PATH_ORIG")
    else:
        env.pop("LD_LIBRARY_PATH", None)
    detach = ({"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
              if sys.platform == "win32" else {"start_new_session": True})
    subprocess.Popen(_helper, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True, **detach)


def _exe_path() -> str:
    return {"darwin": "Contents/MacOS/LeanCut", "win32": "LeanCut.exe"}.get(sys.platform, "LeanCut")


def _unpack(archive: Path, stage: Path, on_log) -> Path:
    """Put the new LeanCut.app / LeanCut folder into stage and return its path."""
    if sys.platform == "darwin":
        mount = Path(tempfile.mkdtemp(prefix="leancut-dmg-"))
        on_log("Opening the disk image…")
        subprocess.run(["hdiutil", "attach", "-nobrowse", "-readonly", "-noautoopen", "-mountpoint", str(mount),
                        str(archive)], check=True, capture_output=True)
        try:
            on_log("Copying the new version…")
            # ditto keeps the bundle's symlinks, permissions and code signature intact
            subprocess.run(["ditto", str(mount / "LeanCut.app"), str(stage / "LeanCut.app")], check=True,
                           capture_output=True)
        finally:
            subprocess.run(["hdiutil", "detach", str(mount), "-force"], capture_output=True)
            mount.rmdir()
        return stage / "LeanCut.app"
    on_log("Extracting the new version…")
    if sys.platform == "win32":
        with zipfile.ZipFile(archive) as z:  # extractall drops absolute and ../ paths
            z.extractall(stage)
    else:
        with tarfile.open(archive) as t:
            t.extractall(stage, filter="data")  # blocks paths and links that point outside stage
    return stage / "LeanCut"


def helper_script(platform: str, bundle: Path, new: Path, old: Path, pid: int, stage: Path) -> str:
    """The detached updater: wait for pid to exit, rename bundle→old and new→bundle, relaunch, then drop old.
    If the swap or the launch fails, the old version is put back and started instead."""
    if platform == "win32":
        q = lambda p: "'" + str(p).replace("'", "''") + "'"
        return f"""# LeanCut updater (written by updates.py; deletes itself)
$app = {q(bundle)}; $new = {q(new)}; $old = {q(old)}; $stage = {q(stage)}
Wait-Process -Id {int(pid)} -ErrorAction SilentlyContinue
if (Test-Path $old) {{ Remove-Item $old -Recurse -Force }}
# Windows (and antivirus) can hold files for a moment after the app exits: retry the rename.
for ($i = 0; $i -lt 30 -and (Test-Path $app); $i++) {{ try {{ Move-Item $app $old -ErrorAction Stop }} catch {{ Start-Sleep 1 }} }}
if (Test-Path $app) {{ Start-Process (Join-Path $app 'LeanCut.exe'); exit 1 }}
try {{ Move-Item $new $app -ErrorAction Stop; Start-Process (Join-Path $app 'LeanCut.exe') -ErrorAction Stop }}
catch {{ if (Test-Path $app) {{ Remove-Item $app -Recurse -Force }}; Move-Item $old $app; Start-Process (Join-Path $app 'LeanCut.exe'); exit 1 }}
Remove-Item $old -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
"""
    q = shlex.quote
    # shortcut: "launched" means open/exec started it, not that it stayed up; add a health check if bad builds ship
    launch = 'open "$app"' if platform == "darwin" else '( "$app/LeanCut" >/dev/null 2>&1 & )'
    return f"""#!/bin/sh
# LeanCut updater (written by updates.py; deletes itself)
app={q(str(bundle))}; new={q(str(new))}; old={q(str(old))}; stage={q(str(stage))}
while kill -0 {int(pid)} 2>/dev/null; do sleep 0.5; done
rm -rf "$old"
mv "$app" "$old" || {{ {launch}; exit 1; }}
if mv "$new" "$app" && {launch}; then
  rm -rf "$old"
else
  rm -rf "$app"; mv "$old" "$app"; {launch}
fi
rm -rf "$stage"
"""

"""End-to-end test of the in-app updater against a real installed LeanCut, without the GUI.

    python tools/update_e2e.py --bundle <installed LeanCut.app or LeanCut folder> [--tag v0.3.0] [--with-old-app]

What it does, using this repo's updates.py exactly as the app does:
  1. downloads the release asset for this OS with updates.download() (same https/allow-list/size checks);
  2. starts a child Python process that plays the running app: updates.install(…, bundle=<installed>) then
     updates.restart(), then exits, so the detached helper's "wait for the app's PID" sees a real PID end;
  3. waits for the helper, then checks: the app executable now matches the one in the downloaded asset, a marker
     planted in the old copy is gone, the backup and staging folders are cleaned up, and the new app was relaunched
     and stays up (it is then killed).
--with-old-app also starts the installed (old) app first and kills it once the update is staged, so the swap
happens right after a real LeanCut process held the folder (what file locking on Windows cares about).
Exit code 0 = PASS. Logs go to --logs (default ./update-e2e-logs).
"""
import argparse
import hashlib
import os
import plistlib
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import updates  # noqa: E402

LOG = []


def say(msg):
    print(msg, flush=True)
    LOG.append(msg)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def archive_exe(archive: Path) -> bytes:
    """The app executable as stored in the downloaded asset, read without updates.py's unpacking."""
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as z:
            return z.read("LeanCut/LeanCut.exe")
    if archive.name.endswith(".tar.gz"):
        with tarfile.open(archive) as t:
            return t.extractfile("LeanCut/LeanCut").read()
    mount = Path(tempfile.mkdtemp(prefix="e2e-dmg-"))
    subprocess.run(["hdiutil", "attach", "-nobrowse", "-readonly", "-noautoopen", "-mountpoint", str(mount),
                    str(archive)], check=True, capture_output=True)
    try:
        return (mount / "LeanCut.app/Contents/MacOS/LeanCut").read_bytes()
    finally:
        subprocess.run(["hdiutil", "detach", str(mount), "-force"], capture_output=True)
        mount.rmdir()


def version(bundle: Path) -> str:
    plist = bundle / "Contents" / "Info.plist"  # only the macOS bundle records its version outside the code
    return plistlib.loads(plist.read_bytes()).get("CFBundleShortVersionString", "?") if plist.is_file() else "-"


def pids_in(folder: Path) -> list[int]:
    """Processes whose executable lives inside folder (never this driver: its command line starts with python)."""
    if os.name == "nt":
        ps = ("[Console]::OutputEncoding = [Text.Encoding]::UTF8; Get-CimInstance Win32_Process | "
              "Where-Object ExecutablePath | ForEach-Object { \"$($_.ProcessId)`t$($_.ExecutablePath)\" }")
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, encoding="utf-8",
                             errors="replace").stdout
    else:
        out = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True).stdout
    prefix = os.path.normcase(str(folder)) + os.sep
    found = []
    for line in out.splitlines():
        pid, _, cmd = line.strip().partition("\t" if os.name == "nt" else " ")
        if pid.isdigit() and int(pid) != os.getpid() and os.path.normcase(cmd.strip()).startswith(prefix):
            found.append(int(pid))
    return found


def kill(pids):
    for pid in pids:
        if os.name == "nt":  # /T also ends the app's WebView2 helpers
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def wait_for(cond, timeout, step=0.5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(step)
    return cond()


def child(args):
    """Plays the running app: stage the update, start the detached helper, exit."""
    r = updates.install(Path(args.archive), on_log=lambda m: print("install:", m, flush=True),
                        bundle=Path(args.bundle))
    print("install() ->", r, flush=True)
    if not r["restart"]:
        sys.exit(2)
    stage = Path(args.bundle).parent / ".leancut-update"  # Windows passes the helper as -EncodedCommand, not a path
    script = stage / ("update.ps1" if sys.platform == "win32" else "update.sh")
    shutil.copy2(script, Path(args.logs) / script.name)  # the helper deletes itself; keep a copy for the logs
    updates.restart()
    print("restart(): helper started, app process exiting", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundle", required=True, help="installed LeanCut.app (macOS) or LeanCut folder")
    ap.add_argument("--tag", default="v0.3.0", help="release to update to")
    ap.add_argument("--work", help="download folder (default: a temp folder)")
    ap.add_argument("--logs", default="update-e2e-logs")
    ap.add_argument("--with-old-app", action="store_true", help="run the old app until the update is staged")
    ap.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--archive", help=argparse.SUPPRESS)
    args = ap.parse_args()
    logs = Path(args.logs).resolve()
    logs.mkdir(parents=True, exist_ok=True)
    if args.child:
        return child(args)

    results = []

    def check(name, ok, detail=""):
        results.append((name, bool(ok)))
        say(f"[{'PASS' if ok else 'FAIL'}] {name}{': ' + detail if detail else ''}")

    bundle = Path(args.bundle).resolve()  # the helper and `open` work on real paths (/var -> /private/var on macOS)
    exe = bundle / updates._exe_path()
    stage, old = bundle.parent / updates.STAGE, bundle.with_name(f".{bundle.name}.old")
    say(f"platform={sys.platform} bundle={bundle} tag={args.tag}")
    if not exe.is_file():
        sys.exit(f"FAIL: no installed app at {exe}")
    old_exe, old_version = exe.read_bytes(), version(bundle)
    marker = bundle / "e2e-old-copy-marker.txt"
    marker.write_text("this file only exists in the old copy")

    work = Path(args.work or tempfile.mkdtemp(prefix="leancut-e2e-"))
    work.mkdir(parents=True, exist_ok=True)
    asset = updates.ASSETS[sys.platform]
    url = f"https://github.com/{updates.REPO}/releases/download/{args.tag}/{asset}"
    archive = work / asset
    say(f"download() {url}")
    t = time.monotonic()
    updates.download(url, archive)
    say(f"  {archive.stat().st_size} bytes in {time.monotonic() - t:.0f}s")
    new_exe = archive_exe(archive)
    say(f"exe sha256: installed {digest(old_exe)}  in {args.tag} asset {digest(new_exe)}")

    old_app, old_pids = None, []
    if args.with_old_app:
        old_app = subprocess.Popen([str(exe)], stdout=open(logs / "old-app.out", "w"), stderr=subprocess.STDOUT)
        check("old app starts and stays up 5 s", wait_for(lambda: old_app.poll() is not None, 5) is False,
              f"pid {old_app.pid}, exit code {old_app.poll()}")
        # list them now: once the helper has swapped folders, they no longer run from inside the bundle
        old_pids = [old_app.pid, *pids_in(bundle)]

    say("child: install() + restart(), then exit")
    with open(logs / "child.log", "w") as out:
        rc = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--child", "--bundle", str(bundle),
                             "--archive", str(archive), "--logs", str(logs)],
                            stdout=out, stderr=subprocess.STDOUT, timeout=900).returncode
    say((logs / "child.log").read_text())
    check("install() staged the update and restart() started the helper", rc == 0, f"child exit code {rc}")
    if old_app:
        kill(old_pids)  # the app quits, as after "Restart now"
        old_app.wait(timeout=30)
        say(f"old app stopped (exit code {old_app.returncode})")

    if rc == 0:
        t = time.monotonic()
        done = wait_for(lambda: not stage.exists(), 180)
        check("helper finished (staging folder removed)", done, f"after {time.monotonic() - t:.0f}s")
        installed = exe.read_bytes() if exe.is_file() else b""
        check("installed exe is the new release's", installed == new_exe, digest(installed))
        if old_exe == new_exe:
            say("[INFO] old and new exe are identical (same release on both sides), so only the marker shows the swap")
        check("old copy replaced (its marker is gone)", not marker.exists())
        check("backup of the old copy deleted", not old.exists(), str(old))
        if sys.platform == "darwin":
            check("bundle version changed", version(bundle) != old_version or old_exe == new_exe,
                  f"{old_version} -> {version(bundle)}")
        new_pids = lambda: [p for p in pids_in(bundle) if p not in old_pids]
        up = wait_for(new_pids, 60)
        check("new app relaunched from the installed location", up, f"pids {new_pids()}")
        if up:
            time.sleep(10)
            check("relaunched app still running after 10 s", new_pids())
            kill(new_pids())
    elif marker.exists():
        check("failed install left the old copy in place", exe.read_bytes() == old_exe)

    say(f"install folder now holds: {sorted(p.name for p in bundle.parent.iterdir())}")
    log = bundle.parent / ".leancut-update.log"
    if log.exists():
        say("updater log:\n" + log.read_text(encoding="utf-8", errors="replace"))
    say("\n==== update e2e summary ====")
    for name, ok in results:
        say(f"{'PASS' if ok else 'FAIL'}  {name}")
    passed = bool(results) and all(ok for _, ok in results)
    say(f"RESULT: {'PASS' if passed else 'FAIL'} ({sum(ok for _, ok in results)}/{len(results)} checks)")
    (logs / "summary.txt").write_text("\n".join(LOG) + "\n")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()

"""Offline tests for the update check and in-app install (no network: urlopen/opener are faked)."""
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import updates  # noqa: E402

DL = "https://github.com/lucerowb/leancut/releases/download/v9.0.0/"


@pytest.fixture(autouse=True)
def no_staged_update(monkeypatch):
    monkeypatch.setattr(updates, "_helper", [])


class FakeResponse(io.BytesIO):
    def __init__(self, body: bytes, length=None):
        super().__init__(body)
        self.headers = {"Content-Length": str(len(body) if length is None else length)}

    def __exit__(self, *a):
        self.close()


def test_is_newer():
    assert updates.is_newer("0.10.0", "0.9.9") and updates.is_newer("v0.3.0", "0.2.9")
    assert not updates.is_newer("0.2.0", "v0.2.0") and not updates.is_newer("0.1.9", "0.2.0")


@pytest.mark.parametrize("url,redirect,ok", [
    (DL + "LeanCut-macOS.dmg", False, True),
    ("https://github.com/lucerowb/leancut/releases/latest", False, True),
    ("http://github.com/lucerowb/leancut/releases/download/x", False, False),        # not https
    ("https://github.com/lucerowb/leancut-evil/x", False, False),                     # other repo
    ("https://github.com/lucerowb/leancut/../../evil/repo/x", False, False),          # path escape
    ("https://github.com.evil.com/lucerowb/leancut/x", False, False),
    ("https://github.com@evil.com/lucerowb/leancut/x", False, False),
    ("https://user@github.com/lucerowb/leancut/x", False, False),
    ("https://github.com:8443/lucerowb/leancut/x", False, False),
    ("https://objects.githubusercontent.com/github-production-release-asset/1", False, False),  # only via redirect
    ("https://objects.githubusercontent.com/github-production-release-asset/1", True, True),
    ("https://release-assets.githubusercontent.com/github-production-release-asset/1", True, True),
    ("http://objects.githubusercontent.com/x", True, False),
    ("https://evil.githubusercontent.com/x", True, False),
    ("https://evil.com/x", True, False),
])
def test_trusted_urls(url, redirect, ok):
    assert updates.trusted(url, redirect=redirect) is ok


def test_redirect_to_other_host_is_refused():
    req = urllib.request.Request(DL + "a.zip")
    with pytest.raises(ValueError):
        updates._CheckedRedirects().redirect_request(req, None, 302, "Found", {}, "https://evil.com/a.zip")
    ok = updates._CheckedRedirects().redirect_request(req, None, 302, "Found", {},
                                                      "https://release-assets.githubusercontent.com/a")
    assert ok.full_url == "https://release-assets.githubusercontent.com/a"


@pytest.mark.parametrize("platform", ["darwin", "win32", "linux"])
def test_check_picks_platform_asset(monkeypatch, platform):
    rel = {"tag_name": "v9.0.0", "html_url": "https://github.com/lucerowb/leancut/releases/tag/v9.0.0", "body": "x",
           "assets": [{"name": n, "browser_download_url": DL + n} for n in updates.ASSETS.values()]}
    monkeypatch.setattr(updates.urllib.request, "urlopen", lambda *a, **k: FakeResponse(json.dumps(rel).encode()))
    monkeypatch.setattr(updates.sys, "platform", platform)
    info = updates.check("0.2.0")
    assert info["newer"] and info["latest"] == "9.0.0" and info["url"] == DL + updates.ASSETS[platform]
    assert info["can_install"] is False  # tests run from source
    monkeypatch.setattr(updates.sys, "platform", "sunos5")
    assert updates.check("0.2.0")["url"] == rel["html_url"]  # no asset for this OS: the release page


def fake_opener(monkeypatch, body, length=None):
    class Opener:
        def open(self, req, timeout):
            return FakeResponse(body, length)
    monkeypatch.setattr(updates.urllib.request, "build_opener", lambda *h: Opener())


def test_download_streams_and_reports_progress(monkeypatch, tmp_path):
    body = os.urandom(3 * (1 << 20) + 5)
    fake_opener(monkeypatch, body)
    seen = []
    out = updates.download(DL + "LeanCut-Linux-x64.tar.gz", tmp_path / "u.tar.gz", seen.append)
    assert out.read_bytes() == body and seen[-1] == 1.0 and len(seen) == 4
    assert list(tmp_path.iterdir()) == [out]


@pytest.mark.parametrize("length", [10 ** 9, 0])
def test_download_rejects_wrong_or_missing_size(monkeypatch, tmp_path, length):
    fake_opener(monkeypatch, b"short", length)
    with pytest.raises(RuntimeError):
        updates.download(DL + "a.zip", tmp_path / "a.zip")
    assert list(tmp_path.iterdir()) == []  # no partial file left behind


def test_download_rejects_foreign_url(tmp_path):
    with pytest.raises(ValueError):
        updates.download("https://evil.com/LeanCut-macOS.dmg", tmp_path / "x")


@pytest.mark.parametrize("platform,exe,expected", [
    ("darwin", "Apps/LeanCut.app/Contents/MacOS/LeanCut", "Apps/LeanCut.app"),
    ("win32", "Apps/LeanCut/LeanCut.exe", "Apps/LeanCut"),
    ("linux", "share/LeanCut/app/LeanCut", "share/LeanCut/app"),
])
def test_current_bundle_when_frozen(monkeypatch, tmp_path, platform, exe, expected):
    assert updates.current_bundle() is None  # from source
    monkeypatch.setattr(updates.sys, "frozen", True, raising=False)
    monkeypatch.setattr(updates.sys, "platform", platform)
    monkeypatch.setattr(updates.sys, "executable", str(tmp_path / exe))
    assert updates.current_bundle() == (tmp_path / expected).resolve()


def test_install_from_source_changes_nothing(tmp_path):
    archive = tmp_path / "LeanCut-Linux-x64.tar.gz"
    archive.write_bytes(b"x")
    r = updates.install(archive, on_log=lambda m: None)
    assert r["restart"] is False and "git pull" in r["message"] and list(tmp_path.iterdir()) == [archive]
    with pytest.raises(RuntimeError):
        updates.restart()


def fake_install(tmp_path, monkeypatch, platform):
    """An 'installed' LeanCut folder plus a downloaded release archive for that platform."""
    monkeypatch.setattr(updates.sys, "platform", platform)
    exe = "LeanCut.exe" if platform == "win32" else "LeanCut"
    bundle = tmp_path / "apps" / "LeanCut"
    bundle.mkdir(parents=True)
    (bundle / exe).write_text("old")
    monkeypatch.setattr(updates, "current_bundle", lambda: bundle)
    src = tmp_path / "src" / "LeanCut"
    src.mkdir(parents=True)
    (src / exe).write_text("new")
    if platform == "win32":
        archive = Path(shutil.make_archive(str(tmp_path / "LeanCut-Windows"), "zip", src.parent, "LeanCut"))
    else:
        archive = Path(shutil.make_archive(str(tmp_path / "LeanCut-Linux-x64"), "gztar", src.parent, "LeanCut"))
    return bundle, archive


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_install_stages_next_to_the_app(tmp_path, monkeypatch, platform):
    bundle, archive = fake_install(tmp_path, monkeypatch, platform)
    r = updates.install(archive, on_log=lambda m: None)
    stage = bundle.parent / updates.STAGE
    assert r["restart"] is True
    assert (stage / "LeanCut" / ("LeanCut.exe" if platform == "win32" else "LeanCut")).read_text() == "new"
    assert (bundle / ("LeanCut.exe" if platform == "win32" else "LeanCut")).read_text() == "old"  # untouched
    script = stage / ("update.ps1" if platform == "win32" else "update.sh")
    assert str(os.getpid()) in script.read_text(encoding="utf-8")
    if platform == "win32":  # the script's text, not its path (PowerShell 5.1 reads [ ] in -File paths as wildcards)
        import base64
        assert "-File" not in updates._helper
        code = updates._helper[updates._helper.index("-EncodedCommand") + 1]
        assert base64.b64decode(code).decode("utf-16-le") == script.read_text(encoding="utf-8-sig")
    else:
        assert str(script) in updates._helper
    assert script.read_bytes().startswith(b"\xef\xbb\xbf") == (platform == "win32")  # PowerShell 5.1 needs the BOM


def test_install_rejects_archive_without_the_app(tmp_path, monkeypatch):
    bundle, _ = fake_install(tmp_path, monkeypatch, "linux")
    bad = tmp_path / "bad.tar.gz"
    with tarfile.open(bad, "w:gz") as t:
        t.add(tmp_path / "src" / "LeanCut" / "LeanCut", arcname="Other/LeanCut")
    with pytest.raises(RuntimeError):
        updates.install(bad, on_log=lambda m: None)
    assert not (bundle.parent / updates.STAGE).exists()


def test_install_refuses_a_folder_holding_user_data(tmp_path, monkeypatch):
    bundle, archive = fake_install(tmp_path, monkeypatch, "linux")
    (bundle / "settings.json").write_text("{}")
    r = updates.install(archive, on_log=lambda m: None)
    assert r["restart"] is False and not (bundle.parent / updates.STAGE).exists()


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs POSIX permissions as a normal user")
def test_install_refuses_read_only_location(tmp_path, monkeypatch):
    bundle, archive = fake_install(tmp_path, monkeypatch, "linux")
    bundle.parent.chmod(0o555)
    try:
        r = updates.install(archive, on_log=lambda m: None)
    finally:
        bundle.parent.chmod(0o755)
    assert r["restart"] is False and "can't write" in r["message"]


def test_zip_slip_is_blocked(tmp_path, monkeypatch):
    bundle, _ = fake_install(tmp_path, monkeypatch, "win32")
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as z:
        z.writestr("../../escaped.txt", "x")
        z.writestr("LeanCut/LeanCut.exe", "new")
    updates.install(evil, on_log=lambda m: None)
    assert not (tmp_path / "escaped.txt").exists() and not (tmp_path / "apps" / "escaped.txt").exists()


def test_helper_scripts_quote_paths():
    odd = Path("/tmp/it's a \"test\"/LeanCut")
    sh = updates.helper_script("linux", odd, odd.with_name("new"), odd.with_name(".old"), 42, odd.parent)
    assert "kill -0 42" in sh and "'\"'\"'" in sh  # shlex-quoted single quote
    mac = updates.helper_script("darwin", odd, odd, odd, 42, odd.parent)
    assert 'open "$app"' in mac
    ps = updates.helper_script("win32", Path("C:/Program Files/it's/LeanCut"), Path("C:/n"), Path("C:/o"), 42,
                               Path("C:/s"))
    assert "Wait-Process -Id 42" in ps and "it''s" in ps


@pytest.mark.skipif(not shutil.which("powershell") and not shutil.which("pwsh"), reason="no PowerShell")
def test_windows_helper_parses(tmp_path):
    script = tmp_path / "update.ps1"
    script.write_text(updates.helper_script("win32", tmp_path / "a", tmp_path / "b", tmp_path / "c", 1, tmp_path))
    check = (f"$e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile('{script}', [ref]$null, "
             "[ref]$e); if ($e) { $e; exit 1 }")
    subprocess.run([shutil.which("powershell") or shutil.which("pwsh"), "-NoProfile", "-Command", check], check=True)


def dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def make_folder_app(root: Path, name: str, marker: str) -> Path:
    """A stand-in LeanCut folder whose 'LeanCut' just records that it was launched."""
    app = root / name
    app.mkdir(parents=True)
    exe = app / "LeanCut"
    exe.write_text(f"#!/bin/sh\necho {marker} > \"$(dirname \"$0\")/../launched\"\n")
    exe.chmod(0o755)
    return app


@pytest.mark.skipif(os.name == "nt", reason="POSIX helper")
def test_sh_helper_swaps_relaunches_and_drops_backup(tmp_path):
    app = make_folder_app(tmp_path, "LeanCut", "old")
    (app / "keep-me-not").write_text("x")
    stage = tmp_path / updates.STAGE
    new = make_folder_app(stage, "LeanCut", "new")
    old = tmp_path / ".LeanCut.old"
    script = stage / "update.sh"
    script.write_text(updates.helper_script("linux", app, new, old, dead_pid(), stage))
    subprocess.run(["/bin/sh", "-n", str(script)], check=True)
    subprocess.run(["/bin/sh", str(script)], check=True, timeout=20)
    for _ in range(50):  # the relaunch runs in the background
        if (tmp_path / "launched").exists():
            break
        time.sleep(0.1)
    assert (tmp_path / "launched").read_text().strip() == "new"
    assert not (app / "keep-me-not").exists() and not old.exists() and not stage.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX helper")
def test_sh_helper_rolls_back_when_the_new_copy_is_missing(tmp_path):
    app = make_folder_app(tmp_path, "LeanCut", "old")
    stage = tmp_path / updates.STAGE
    stage.mkdir()
    script = stage / "update.sh"
    script.write_text(updates.helper_script("linux", app, stage / "missing", tmp_path / ".LeanCut.old", dead_pid(),
                                            stage))
    subprocess.run(["/bin/sh", str(script)], check=True, timeout=20)
    for _ in range(50):
        if (tmp_path / "launched").exists():
            break
        time.sleep(0.1)
    assert (tmp_path / "launched").read_text().strip() == "old" and (app / "LeanCut").exists()


def test_install_into_an_explicit_bundle(tmp_path, monkeypatch):
    """tools/update_e2e.py points install() at a real installed copy instead of the running (unfrozen) app."""
    bundle, archive = fake_install(tmp_path, monkeypatch, "linux")
    monkeypatch.setattr(updates, "current_bundle", lambda: None)
    assert updates.install(archive, on_log=lambda m: None, bundle=bundle)["restart"] is True
    assert (bundle.parent / updates.STAGE / "LeanCut" / "LeanCut").read_text() == "new"


def test_release_workflow_publishes_every_update_asset():
    """The updater looks for these names on the release: the release workflow must upload all of them."""
    wf = (Path(__file__).resolve().parent.parent / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    for name in updates.ASSETS.values():
        assert f"dist/{name}" in wf, name


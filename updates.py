"""Update check: one anonymous request to GitHub's latest-release API. Nothing about the machine is sent.
Turn it off in Settings › Updates."""
import json
import re
import sys
import urllib.error
import urllib.request

REPO = "lucerowb/leancut"
LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
ASSETS = {"darwin": "LeanCut-macOS.dmg", "win32": "LeanCut-Windows.zip"}


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
    url = assets.get(ASSETS.get(sys.platform, ""), rel["html_url"])
    return {"current": current, "latest": latest, "newer": is_newer(latest, current),
            "url": url if url.startswith("https://") else rel["html_url"], "notes_url": rel["html_url"],
            "notes": (rel.get("body") or "")[:4000]}

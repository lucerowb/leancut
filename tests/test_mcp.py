"""Offline tests for the MCP server and the leancut CLI, against a fake LeanCut app (http.server in a thread)."""
import base64
import json
import os
import shutil
import subprocess
import sys
import threading
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import leancut_cli  # noqa: E402
import mcp_server  # noqa: E402

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()
TOOLS = [{"name": "look", "description": "Frames of the edit at t.",
          "input_schema": {"type": "object", "properties": {"t": {"type": "number"}}, "required": ["t"]}},
         {"name": "apply", "description": "Apply edit actions.", "input_schema": {"type": "object", "properties": {}}}]


class FakeApp:
    """Speaks the /api/agent/* contract; records requests."""

    def __init__(self, tag="A"):
        self.tag, self.requests = tag, []
        app = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, obj, code=200):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                app.requests.append(("GET", self.path, dict(self.headers), None))
                if self.path == "/api/agent/manual":
                    return self._reply({"text": f"# LeanCut manual {app.tag}"})
                if self.path == "/api/agent/tools":
                    return self._reply({"tools": TOOLS})
                self._reply({"detail": "Not Found"}, 404)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                app.requests.append(("POST", self.path, dict(self.headers), body))
                if self.path != "/api/agent/call":
                    return self._reply({"detail": "Not Found"}, 404)
                if body["name"] == "look":
                    return self._reply({"content": [{"type": "text", "text": f"{app.tag} t={body['arguments'].get('t')}"},
                                                    {"type": "image", "data": PNG, "mimeType": "image/png"}],
                                        "isError": False})
                if body["name"] == "boom":
                    return self._reply({"detail": "bad action"}, 400)
                self._reply({"content": [{"type": "text", "text": f"unknown tool {body['name']}"}], "isError": True})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated install: copies of the scripts (so ./workspace is empty), a fake home, no real LeanCut in sight."""
    for f in ("mcp_server.py", "leancut_cli.py"):
        shutil.copy2(ROOT / f, tmp_path / f)
    home = tmp_path / "home"
    home.mkdir()
    e = {k: v for k, v in os.environ.items() if not k.startswith(("LEANCUT_", "CODEX_HOME"))}
    e.update(HOME=str(home), USERPROFILE=str(home), APPDATA=str(home / "AppData"), XDG_DATA_HOME=str(home / "data"),
             LEANCUT_HOME=str(tmp_path / "lc"), PYTHONIOENCODING="utf-8")
    for k in ("LEANCUT_URL", "CODEX_HOME"):
        monkeypatch.delenv(k, raising=False)
    for k in ("HOME", "USERPROFILE", "APPDATA", "XDG_DATA_HOME", "LEANCUT_HOME"):
        monkeypatch.setenv(k, e[k])
    monkeypatch.setattr(mcp_server, "HERE", tmp_path)
    return tmp_path, e


def announce(folder: Path, app: FakeApp):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "server.json").write_text(json.dumps({"url": app.url, "pid": os.getpid(), "version": "9.9"}))


class Client:
    def __init__(self, cwd, env):
        self.p = subprocess.Popen([sys.executable, str(cwd / "mcp_server.py")], cwd=cwd, env=env, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.n = 0

    def send(self, obj):
        self.p.stdin.write(json.dumps(obj).encode() + b"\n")
        self.p.stdin.flush()

    def read(self):
        line = self.p.stdout.readline()
        assert line, self.p.stderr.read().decode()
        return json.loads(line)  # every stdout line is a JSON-RPC message

    def rpc(self, method, params=None):
        self.n += 1
        self.send({"jsonrpc": "2.0", "id": self.n, "method": method, **({"params": params} if params else {})})
        msg = self.read()
        while "id" not in msg:  # notifications come first
            msg = self.read()
        assert msg["id"] == self.n
        return msg

    def close(self):
        self.p.stdin.close()
        assert self.p.wait(10) == 0
        self.p.stdout.close()
        self.p.stderr.close()


def test_mcp_session(env):
    tmp, e = env
    app = FakeApp()
    announce(tmp / "lc", app)
    c = Client(tmp, e)
    init = c.rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "t"}})
    assert init["result"]["protocolVersion"] == "2024-11-05"
    assert init["result"]["serverInfo"]["name"] == "leancut"
    assert {"tools", "prompts", "resources"} <= set(init["result"]["capabilities"])
    c.send({"jsonrpc": "2.0", "method": "notifications/initialized"})  # no reply
    assert c.rpc("ping")["result"] == {}

    tools = c.rpc("tools/list")["result"]["tools"]
    assert [t["name"] for t in tools] == ["leancut_manual", "look", "apply"]
    assert tools[1]["inputSchema"]["required"] == ["t"]

    res = c.rpc("tools/call", {"name": "look", "arguments": {"t": 4.5}})["result"]
    assert res["isError"] is False
    assert res["content"][0] == {"type": "text", "text": "A t=4.5"}
    assert res["content"][1] == {"type": "image", "data": PNG, "mimeType": "image/png"}
    assert c.rpc("tools/call", {"name": "nope", "arguments": {}})["result"]["isError"] is True
    bad = c.rpc("tools/call", {"name": "boom", "arguments": {}})["result"]
    assert bad["isError"] is True and "bad action" in bad["content"][0]["text"]
    assert c.rpc("tools/call", {"name": "leancut_manual"})["result"]["content"][0]["text"] == "# LeanCut manual A"

    assert c.rpc("prompts/list")["result"]["prompts"][0]["name"] == "leancut"
    msg = c.rpc("prompts/get", {"name": "leancut"})["result"]["messages"][0]
    assert msg["content"]["text"] == "# LeanCut manual A"
    assert c.rpc("prompts/get", {"name": "x"})["error"]["code"] == -32602
    uri = c.rpc("resources/list")["result"]["resources"][0]["uri"]
    assert c.rpc("resources/read", {"uri": uri})["result"]["contents"][0]["text"] == "# LeanCut manual A"
    assert c.rpc("nope/nope")["error"]["code"] == -32601

    c.send("garbage")  # not JSON
    c.p.stdin.write(b"{not json\n")
    c.p.stdin.flush()
    assert c.read()["error"]["code"] == -32600
    assert c.read()["error"]["code"] == -32700

    # the app restarts on another port between calls: rediscovered, no restart of the MCP server needed
    app.stop()
    app2 = FakeApp("B")
    announce(tmp / "lc", app2)
    res = c.rpc("tools/call", {"name": "look", "arguments": {"t": 1}})["result"]
    assert res["content"][0]["text"] == "B t=1"
    c.close()
    posts = [r for r in app2.requests if r[0] == "POST"]
    assert posts[0][3] == {"name": "look", "arguments": {"t": 1}}
    assert all("Origin" not in r[2] for r in app.requests + app2.requests)
    app2.stop()


def test_mcp_app_down_then_up(env):
    tmp, e = env
    c = Client(tmp, e)
    init = c.rpc("initialize", {"protocolVersion": "1999-01-01"})["result"]
    assert init["protocolVersion"] == "2025-06-18"  # unknown version: we answer with our latest
    tools = c.rpc("tools/list")["result"]["tools"]
    assert [t["name"] for t in tools] == ["leancut_status"]
    res = c.rpc("tools/call", {"name": "leancut_status"})["result"]
    assert res["isError"] and "LeanCut isn't running" in res["content"][0]["text"]
    res = c.rpc("tools/call", {"name": "look", "arguments": {"t": 1}})["result"]
    assert res["isError"] and "open it first" in res["content"][0]["text"]
    assert "isn't running" in c.rpc("prompts/get", {"name": "leancut"})["error"]["message"]

    app = FakeApp()
    announce(tmp / "lc", app)
    c.n += 1
    c.send({"jsonrpc": "2.0", "id": c.n, "method": "tools/call", "params": {"name": "leancut_status"}})
    assert c.read() == {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}
    res = c.read()["result"]
    assert not res["isError"] and "9.9" in res["content"][0]["text"] and app.url in res["content"][0]["text"]
    assert [t["name"] for t in c.rpc("tools/list")["result"]["tools"]][1:] == ["look", "apply"]
    c.close()
    app.stop()


def test_discovery_precedence(env, monkeypatch):
    tmp, _ = env
    a, b, w, u = FakeApp("home"), FakeApp("os"), FakeApp("workspace"), FakeApp("url")
    osdir = tmp / "osdata"
    monkeypatch.setattr(mcp_server, "user_data_dir", lambda: osdir)
    announce(tmp / "workspace", w)
    assert mcp_server.discover()[0] == w.url
    announce(osdir, b)
    assert mcp_server.discover()[0] == b.url
    announce(tmp / "lc", a)
    assert mcp_server.discover() == (a.url, {"url": a.url, "pid": os.getpid(), "version": "9.9"})
    monkeypatch.setenv("LEANCUT_URL", u.url)
    assert mcp_server.discover()[0] == u.url
    u.stop()  # a dead higher-priority entry falls through to the next live one
    assert mcp_server.discover()[0] == a.url
    for x in (a, b, w):
        x.stop()
    with pytest.raises(mcp_server.NotRunning, match="open it first"):
        mcp_server.discover()


def test_mcp_command():
    assert mcp_server.mcp_command() == [sys.executable, str(ROOT / "mcp_server.py")]


def test_connect_snippets_shape():
    cmd = ["/Apps/LeanCut.app/Contents/MacOS/LeanCut", "--mcp"]
    s = leancut_cli.connect_snippets(cmd)
    assert set(s) == set(leancut_cli.AGENTS)
    for agent, v in s.items():
        assert set(v) == {"how", "snippet", "path", "cli"}, agent
        assert v["how"] and v["snippet"]
    assert s["claude"]["path"] is None and s["claude"]["cli"].startswith("claude mcp add --scope user leancut -- ")
    toml = tomllib.loads(s["codex"]["snippet"])["mcp_servers"]["leancut"]
    assert [toml["command"], *toml["args"]] == cmd and toml["tool_timeout_sec"] >= 3600
    for agent in ("claude", "cursor"):
        e = json.loads(s[agent]["snippet"])["mcpServers"]["leancut"]
        assert [e["command"], *e["args"]] == cmd
    assert json.loads(s["opencode"]["snippet"])["mcp"]["leancut"]["command"] == cmd
    for agent in ("gemini", "qwen"):
        assert json.loads(s[agent]["snippet"])["mcpServers"]["leancut"]["timeout"] >= 600_000


def test_connect_write_merges(env, capsys):
    tmp, e = env
    home = Path(e["HOME"])
    cursor = home / ".cursor" / "mcp.json"
    cursor.parent.mkdir()
    cursor.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "keep": 1}))
    codex = home / ".codex" / "config.toml"
    codex.parent.mkdir()
    codex.write_text('model = "o3"\n\n[mcp_servers.leancut]\ncommand = "old"\n\n[mcp_servers.leancut.env]\nA = "1"\n\n'
                     '[mcp_servers.other]\ncommand = "y"\n')
    bad = home / ".gemini" / "settings.json"
    bad.parent.mkdir()
    bad.write_text("{ // comments\n}")
    assert leancut_cli.main(["connect", "cursor", "--write"]) == 0
    assert leancut_cli.main(["connect", "codex", "--write"]) == 0
    data = json.loads(cursor.read_text())
    assert data["keep"] == 1 and data["mcpServers"]["other"] == {"command": "x"}
    assert data["mcpServers"]["leancut"]["args"] == [str(tmp / "mcp_server.py")]
    assert json.loads((cursor.parent / "mcp.json.bak").read_text())["keep"] == 1
    cfg = tomllib.loads(codex.read_text())
    assert cfg["model"] == "o3" and cfg["mcp_servers"]["other"] == {"command": "y"}
    assert cfg["mcp_servers"]["leancut"]["command"] == sys.executable and "env" not in cfg["mcp_servers"]["leancut"]
    with pytest.raises(SystemExit, match="Can't parse"):
        leancut_cli.main(["connect", "gemini", "--write"])
    assert bad.read_text() == "{ // comments\n}"  # never overwrites what it can't read
    capsys.readouterr()
    assert leancut_cli.main(["connect"]) == 0
    out = capsys.readouterr().out
    assert all(f"## {a}" in out for a in leancut_cli.AGENTS)


def run_cli(tmp, e, *args):
    return subprocess.run([sys.executable, str(tmp / "leancut_cli.py"), *args], cwd=tmp, env=e, capture_output=True,
                          text=True, encoding="utf-8", timeout=30)


def test_cli(env):
    tmp, e = env
    r = run_cli(tmp, e, "status")
    assert r.returncode == 1 and "open it first" in r.stderr
    app = FakeApp()
    announce(tmp / "lc", app)
    r = run_cli(tmp, e, "status")
    assert r.returncode == 0 and app.url in r.stdout and "9.9" in r.stdout
    assert run_cli(tmp, e, "manual").stdout.strip() == "# LeanCut manual A"
    out = run_cli(tmp, e, "tools").stdout
    assert "look(t)" in out and "Apply edit actions." in out
    assert json.loads(run_cli(tmp, e, "tools", "--json").stdout) == TOOLS
    r = run_cli(tmp, e, "look", "4.5")
    assert r.returncode == 0
    text, image = r.stdout.strip().splitlines()
    assert text == "A t=4.5"
    path = Path(image.removeprefix("[image] "))
    assert path.read_bytes() == base64.b64decode(PNG)
    path.unlink()
    assert run_cli(tmp, e, "look", "c1.s4").stdout.startswith("A t=c1.s4")
    r = run_cli(tmp, e, "call", "nope", "{}")
    assert r.returncode == 1 and "unknown tool nope" in r.stdout
    assert run_cli(tmp, e, "call", "look", "{bad").returncode == 2
    looks = [r[3]["arguments"] for r in app.requests if r[0] == "POST" and r[3]["name"] == "look"]
    assert looks == [{"t": 4.5}, {"t": "c1.s4"}]
    app.stop()

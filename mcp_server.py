"""LeanCut MCP server: lets any coding agent (Claude Code, Codex, Cursor, OpenCode, Gemini CLI, Qwen Code...) drive the
running LeanCut app. Stdio transport (newline-delimited JSON-RPC 2.0), standard library only.

The tools themselves live in the app (GET /api/agent/tools, POST /api/agent/call); this process only finds the app
and forwards calls, so the tool set has one source. Nothing but protocol messages is ever written to stdout.

    python mcp_server.py          (source checkout)
    LeanCut --mcp                 (packaged app; see mcp_command)
"""
import json
import logging
import os
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

APP_NAME = "LeanCut"
BRIDGE_VERSION = "1"
PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")  # newest first
HERE = Path(__file__).resolve().parent
log = logging.getLogger("leancut-mcp")

NOT_RUNNING = ("LeanCut isn't running — open it first. Start the LeanCut app (from source: `python desktop.py` or "
               "`python app.py`), then try again. LeanCut announces itself in server.json in its data folder; "
               "set LEANCUT_URL=http://127.0.0.1:<port> to point at it by hand.")
INSTRUCTIONS = ("LeanCut is a local AI vlog editor. Before editing, call leancut_manual (or read the `leancut` prompt): "
                "it explains the project, the edit vocabulary and how to check your work by looking at frames.")
STATUS_TOOL = {"name": "leancut_status", "description": "Says whether the LeanCut app is running and how to start it. "
               "LeanCut's editing tools appear once the app is open.", "inputSchema": {"type": "object", "properties": {}}}
MANUAL_TOOL = {"name": "leancut_manual", "description": "LeanCut's manual for agents. Read it first: the project, the "
               "edit vocabulary, and how to check your work.", "inputSchema": {"type": "object", "properties": {}}}


class NotRunning(Exception):
    pass


class AppError(Exception):
    pass


def mcp_command() -> list[str]:
    """The command that starts this MCP server for the current install (what agent configs should run)."""
    if getattr(sys, "frozen", False):  # packaged app: the LeanCut binary itself (macOS: inside LeanCut.app/Contents/MacOS)
        return [sys.executable, "--mcp"]
    return [sys.executable, str(HERE / "mcp_server.py")]


def user_data_dir() -> Path:  # same as app._user_data_dir; app.py isn't imported here (heavy, creates folders)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / APP_NAME


def candidates():
    """(url, info) to try, in order: LEANCUT_URL, then server.json in LEANCUT_HOME, the OS data dir, ./workspace."""
    if os.environ.get("LEANCUT_URL"):
        yield os.environ["LEANCUT_URL"], {"url": os.environ["LEANCUT_URL"]}
    for d in (os.environ.get("LEANCUT_HOME"), user_data_dir(), HERE / "workspace"):
        try:
            info = json.loads((Path(d) / "server.json").read_text(encoding="utf-8"))
            yield info["url"], info
        except (TypeError, OSError, ValueError, KeyError):
            continue


_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never send 127.0.0.1 through a proxy


def http(url: str, path: str, body=None, timeout=30):
    """JSON request to the app. No Origin header: the app rejects browser origins. timeout=None waits forever."""
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url.rstrip("/") + path, data=data,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with _opener.open(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            detail = json.load(e).get("detail")
        except Exception:
            detail = None
        raise AppError(f"LeanCut answered {e.code}: {detail or e.reason}") from None
    except urllib.error.URLError as e:  # never connected: the request did not reach the app
        raise NotRunning(NOT_RUNNING) from e
    except (OSError, ValueError) as e:  # connected, then lost (crash, quit) or garbage: don't retry, it may have acted
        raise AppError(f"LeanCut stopped answering during the request ({e}). Check the app before retrying.") from None


def discover():
    """First candidate that answers /api/agent/manual -> (url, info). Raises NotRunning."""
    for url, info in candidates():
        try:
            http(url, "/api/agent/manual", timeout=3)
            return url, info
        except (NotRunning, AppError):
            continue
    raise NotRunning(NOT_RUNNING)


class App:
    """The running LeanCut app. Rediscovers it when it restarted (new port) between calls."""

    def __init__(self):
        self.url, self.info = None, {}

    def request(self, path, body=None, timeout=30):
        if not self.url:
            self.url, self.info = discover()
        try:
            return http(self.url, path, body, timeout)
        except NotRunning:  # safe to retry: the request never reached the app
            self.url, self.info = discover()
            return http(self.url, path, body, timeout)

    def manual(self) -> str:
        return self.request("/api/agent/manual")["text"]


def _text(t, error=False):
    return {"content": [{"type": "text", "text": t}], "isError": error}


class RpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class Server:
    def __init__(self, send):
        self.send, self.app, self.fallback = send, App(), False

    def _connected(self):
        if self.fallback:  # we told the client only leancut_status exists; now the real tools are there
            self.fallback = False
            self.send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})

    def handle(self, msg):
        if not isinstance(msg, dict):
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
        if "id" not in msg or "method" not in msg:
            return None  # notification (initialized, cancelled...) or a response to us
        mid, method = msg["id"], msg["method"]
        fn = getattr(self, "m_" + method.replace("/", "_"), None) if isinstance(method, str) else None
        if fn is None:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}}
        try:
            return {"jsonrpc": "2.0", "id": mid, "result": fn(msg.get("params") or {})}
        except RpcError as e:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": e.code, "message": str(e)}}
        except Exception as e:
            log.exception("%s failed", method)
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)}}

    def m_initialize(self, p):
        asked = p.get("protocolVersion")
        return {"protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[0],
                "capabilities": {"tools": {"listChanged": True}, "prompts": {}, "resources": {}},
                "serverInfo": {"name": "leancut", "title": APP_NAME, "version": BRIDGE_VERSION},
                "instructions": INSTRUCTIONS}

    def m_ping(self, p):
        return {}

    def m_tools_list(self, p):
        try:
            tools = self.app.request("/api/agent/tools")["tools"]
        except NotRunning:
            self.fallback = True
            return {"tools": [STATUS_TOOL]}
        self._connected()
        out = [{"name": t["name"], "description": t.get("description", ""),
                "inputSchema": t.get("input_schema") or t.get("inputSchema") or {"type": "object"}} for t in tools]
        if not any(t["name"] == MANUAL_TOOL["name"] for t in out):
            out.insert(0, MANUAL_TOOL)
        return {"tools": out}

    def m_tools_call(self, p):
        name, args = p.get("name"), p.get("arguments") or {}
        try:
            if name == "leancut_status":
                self.app.request("/api/agent/manual")
                self._connected()
                v = self.app.info.get("version")
                return _text(f"LeanCut{' ' + v if v else ''} is running at {self.app.url}. Its tools are available; "
                             "call leancut_manual first.")
            if name == "leancut_manual":
                res = _text(self.app.manual())
            else:
                res = self.app.request("/api/agent/call", {"name": name, "arguments": args}, timeout=None)  # renders
                if not isinstance(res, dict) or not isinstance(res.get("content"), list):
                    res = _text(json.dumps(res))
            self._connected()
            return res
        except (NotRunning, AppError) as e:
            return _text(str(e), error=True)

    def _manual_or_error(self):
        try:
            return self.app.manual()
        except (NotRunning, AppError) as e:
            raise RpcError(-32603, str(e))

    def m_prompts_list(self, p):
        return {"prompts": [{"name": "leancut", "title": "LeanCut manual",
                             "description": "How to edit a vlog with LeanCut: read this before using its tools."}]}

    def m_prompts_get(self, p):
        if p.get("name") != "leancut":
            raise RpcError(-32602, f"Unknown prompt: {p.get('name')}")
        return {"description": "LeanCut manual",
                "messages": [{"role": "user", "content": {"type": "text", "text": self._manual_or_error()}}]}

    def m_resources_list(self, p):
        return {"resources": [{"uri": "leancut://manual", "name": "manual", "title": "LeanCut manual",
                               "mimeType": "text/markdown"}]}

    def m_resources_read(self, p):
        if p.get("uri") != "leancut://manual":
            raise RpcError(-32602, f"Unknown resource: {p.get('uri')}")
        return {"contents": [{"uri": "leancut://manual", "mimeType": "text/markdown", "text": self._manual_or_error()}]}


def main() -> int:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="leancut-mcp: %(message)s")
    stdin = sys.stdin.buffer if sys.stdin else os.fdopen(0, "rb")
    stdout = sys.stdout.buffer if sys.stdout else os.fdopen(1, "wb")
    sys.stdout = sys.stderr  # a stray print anywhere must not corrupt the protocol stream
    lock = threading.Lock()

    def send(msg):
        if msg is not None:
            with lock:
                stdout.write(json.dumps(msg, ensure_ascii=False).encode("utf-8") + b"\n")
                stdout.flush()

    server = Server(send)
    for line in stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
            continue
        if isinstance(msg, dict) and msg.get("method") == "tools/call":  # may take minutes: keep answering pings
            threading.Thread(target=lambda m=msg: send(server.handle(m)), daemon=True).start()
        else:
            send(server.handle(msg))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""LeanCut from a shell: lets a coding agent (or you) drive the running LeanCut app, and connects agents to it.

    leancut manual                    the manual for agents: read it first
    leancut tools [--json]            the tools and their arguments
    leancut call <tool> '<json>'      run one tool; prints its text, saves images and prints their paths
    leancut look <t>                  shortcut for: leancut call look '{"t": <t>}'
    leancut status                    is LeanCut running, and where
    leancut connect [agent] [--write] the MCP config for claude, codex, cursor, opencode, gemini, qwen (or all)

`leancut` is `python leancut_cli.py` in a source checkout, or `LeanCut --cli` with the packaged app.
"""
import argparse
import base64
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import mcp_server
from mcp_server import AppError, NotRunning

AGENTS = ("claude", "codex", "cursor", "opencode", "gemini", "qwen")
TOOL_TIMEOUT_S = 3 * 3600  # renders of a long vlog take minutes; agents default to 60 s (Codex) or 10 min


def _cmdline(args: list[str]) -> str:
    return subprocess.list2cmdline(args) if os.name == "nt" else shlex.join(args)


def _json_snippet(key, entry):
    return json.dumps({key: {"leancut": entry}}, indent=2)


def connect_snippets(command: list[str]) -> dict:
    """How to connect each agent to the LeanCut MCP server started by `command` (see mcp_server.mcp_command).
    {agent: {"how", "snippet", "path" (config file --write merges into, or None), "cli" (command line, or None)}}"""
    exe, args = command[0], command[1:]
    home = Path.home()
    codex_home = Path(os.environ.get("CODEX_HOME") or home / ".codex")
    stdio = {"type": "stdio", "command": exe, "args": args}
    warn = ""
    if "/AppTranslocation/" in exe or exe.startswith("/Volumes/"):
        warn = "Move LeanCut to Applications and open it from there first: this path is temporary. "
    toml = (f"[mcp_servers.leancut]\ncommand = {json.dumps(exe, ensure_ascii=False)}\n"
            f"args = {json.dumps(args, ensure_ascii=False)}\nstartup_timeout_sec = 30\n"
            f"tool_timeout_sec = {TOOL_TIMEOUT_S}\n")
    return {
        "claude": {"how": warn + "Claude Code: run this once (all projects), or put the snippet in a project's .mcp.json.",
                   "snippet": _json_snippet("mcpServers", stdio), "path": None,
                   "cli": _cmdline(["claude", "mcp", "add", "--scope", "user", "leancut", "--", *command])},
        "codex": {"how": warn + "Codex: add this to config.toml (tool_timeout_sec lets renders finish; "
                                "`codex mcp add` can't set it).",
                  "snippet": toml, "path": str(codex_home / "config.toml"),
                  "cli": _cmdline(["codex", "mcp", "add", "leancut", "--", *command])},
        "cursor": {"how": warn + "Cursor: add this to ~/.cursor/mcp.json (all projects) or .cursor/mcp.json.",
                   "snippet": _json_snippet("mcpServers", stdio), "path": str(home / ".cursor" / "mcp.json"),
                   "cli": None},
        "opencode": {"how": warn + "OpenCode: add this to ~/.config/opencode/opencode.json or a project's opencode.json.",
                     "snippet": _json_snippet("mcp", {"type": "local", "command": command, "enabled": True}),
                     "path": str(home / ".config" / "opencode" / "opencode.json"), "cli": None},
        "gemini": {"how": warn + "Gemini CLI: add this to ~/.gemini/settings.json (timeout in ms lets renders finish).",
                   "snippet": _json_snippet("mcpServers", {"command": exe, "args": args, "timeout": TOOL_TIMEOUT_S * 1000}),
                   "path": str(home / ".gemini" / "settings.json"),
                   "cli": _cmdline(["gemini", "mcp", "add", "--scope", "user", "--timeout", str(TOOL_TIMEOUT_S * 1000),
                                    "leancut", *command])},
        "qwen": {"how": warn + "Qwen Code: add this to ~/.qwen/settings.json (timeout in ms lets renders finish).",
                 "snippet": _json_snippet("mcpServers", {"command": exe, "args": args, "timeout": TOOL_TIMEOUT_S * 1000}),
                 "path": str(home / ".qwen" / "settings.json"),
                 "cli": _cmdline(["qwen", "mcp", "add", "--scope", "user", "--timeout", str(TOOL_TIMEOUT_S * 1000),
                                  "leancut", *command])},
    }


def install(agent: str, info: dict, command: list[str]) -> str:
    """Write the config for one agent. Merges into existing files (backup in <file>.bak); refuses what it can't parse."""
    if info["path"] is None:  # Claude Code keeps user servers in ~/.claude.json, which it rewrites itself: use its CLI
        if not shutil.which("claude"):
            raise SystemExit(f"`claude` isn't on PATH. Run this yourself:\n  {info['cli']}")
        subprocess.run(["claude", "mcp", "remove", "--scope", "user", "leancut"], capture_output=True)
        subprocess.run(["claude", "mcp", "add", "--scope", "user", "leancut", "--", *command], check=True)
        return "Added to Claude Code (user scope)."
    path = Path(info["path"])
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    if agent == "codex":
        import tomllib
        kept, skip = [], False
        for line in old.splitlines():
            if line.strip().startswith("["):  # a table header: drop our old [mcp_servers.leancut(.*)] tables
                skip = re.match(r'\[\s*mcp_servers\s*\.\s*"?leancut"?\s*[.\]]', line.strip()) is not None
            if not skip:
                kept.append(line)
        new = "\n".join(kept).rstrip() + ("\n\n" if kept else "") + info["snippet"]
        try:
            tomllib.loads(new)
        except tomllib.TOMLDecodeError as e:
            raise SystemExit(f"Can't merge into {path} ({e}). Add this yourself:\n{info['snippet']}")
    else:
        try:
            data = json.loads(old) if old.strip() else {}
        except ValueError as e:
            raise SystemExit(f"Can't parse {path} ({e}). Add this yourself:\n{info['snippet']}")
        for key, servers in json.loads(info["snippet"]).items():
            data.setdefault(key, {}).update(servers)
        new = json.dumps(data, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if old:
        path.with_name(path.name + ".bak").write_text(old, encoding="utf-8")
    path.write_text(new, encoding="utf-8")
    return f"Wrote {path}" + (f" (previous version in {path.name}.bak)" if old else "")


def _print_result(tool: str, res: dict) -> int:
    folder = Path(tempfile.gettempdir()) / "leancut"
    for i, item in enumerate(res.get("content", [])):
        if item.get("type") == "text":
            print(item["text"])
        elif item.get("type") == "image":
            folder.mkdir(exist_ok=True)
            ext = (item.get("mimeType") or "image/png").split("/")[-1].replace("jpeg", "jpg")
            safe = re.sub(r"[^\w-]", "_", tool)
            path = folder / f"{safe}-{time.strftime('%Y%m%d-%H%M%S')}-{i}.{ext}"
            path.write_bytes(base64.b64decode(item["data"]))
            print(f"[image] {path}")
        else:
            print(json.dumps(item))
    return 1 if res.get("isError") else 0


def _number(s: str):
    try:
        return float(s)
    except ValueError:
        return s  # a range ("3-6") or an item id ("c1.s4")


def _signature(tool: dict) -> str:
    schema = tool.get("input_schema") or tool.get("inputSchema") or {}
    req = set(schema.get("required", []))
    return ", ".join(k if k in req else k + "?" for k in schema.get("properties", {}))


def main(argv=None) -> int:
    if sys.stdout is None and os.name == "nt":  # windowed LeanCut.exe started from a console: borrow that console
        import ctypes
        if ctypes.windll.kernel32.AttachConsole(-1):
            sys.stdout = sys.stderr = open("CONOUT$", "w", encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows pipes default to cp1252
    p = argparse.ArgumentParser(prog="leancut", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("manual", help="the manual for agents")
    sub.add_parser("tools", help="list tools").add_argument("--json", action="store_true", help="raw tool list")
    c = sub.add_parser("call", help="run a tool")
    c.add_argument("tool")
    c.add_argument("arguments", nargs="?", default="{}", help="JSON object")
    sub.add_parser("look", help="look at the edit at a time").add_argument("t", nargs="?", help="seconds, a range or an id")
    sub.add_parser("status", help="is LeanCut running")
    k = sub.add_parser("connect", help="connect a coding agent")
    k.add_argument("agent", nargs="?", default="all", choices=(*AGENTS, "all"))
    k.add_argument("--write", action="store_true", help="install the config, not just print it")
    a = p.parse_args(argv)

    if a.cmd == "connect":
        command = mcp_server.mcp_command()
        snippets = connect_snippets(command)
        for name in AGENTS if a.agent == "all" else (a.agent,):
            s = snippets[name]
            if a.write and a.agent == "all" and not (shutil.which(name) or s["path"] and Path(s["path"]).parent.exists()):
                print(f"{name}: not installed, skipped")
                continue
            if a.write:
                print(f"{name}: {install(name, s, command)}")
                continue
            print(f"## {name}\n{s['how']}")
            if s["cli"]:
                print(f"\n  {s['cli']}\n")
            print((f"{s['path']}:\n" if s["path"] else "") + s["snippet"] + "\n")
        return 0

    app = mcp_server.App()
    try:
        if a.cmd == "status":
            app.request("/api/agent/manual")
            v = app.info.get("version")
            print(f"LeanCut{' ' + v if v else ''} is running at {app.url}")
        elif a.cmd == "manual":
            print(app.manual())
        elif a.cmd == "tools":
            tools = app.request("/api/agent/tools")["tools"]
            if a.json:
                print(json.dumps(tools, indent=2))
            for t in [] if a.json else tools:
                print(f"{t['name']}({_signature(t)})\n    {t.get('description', '').strip()}\n")
        else:
            if a.cmd == "look":
                tool, args = "look", {} if a.t is None else {"t": _number(a.t)}
            else:
                tool = a.tool
                try:
                    args = json.loads(a.arguments)
                except ValueError as e:
                    print(f"arguments must be a JSON object: {e}", file=sys.stderr)
                    return 2
            return _print_result(tool, app.request("/api/agent/call", {"name": tool, "arguments": args}, timeout=None))
    except (NotRunning, AppError) as e:
        print(e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

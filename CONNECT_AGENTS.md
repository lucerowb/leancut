# Driving LeanCut with a coding agent

LeanCut can be edited by any coding agent that speaks [MCP](https://modelcontextprotocol.io) or can run a shell
command: Claude Code, Codex, Cursor, OpenCode, Gemini CLI, Qwen Code and others. The agent works on the project open in
the LeanCut window, and you watch every change land there.

> Working on LeanCut's own code? See [CONTRIBUTING.md](CONTRIBUTING.md) and [ARCHITECTURE.md](ARCHITECTURE.md).

## What LeanCut exposes

- **A manual** written for agents: the project, the edit model (lines `c<clip>.s<n>` and B-roll `c<clip>.b<n>`, kept or
  cut, with looks, transitions, titles), the action vocabulary, and how to check work. The agent reads it once, first.
  It is the `leancut_manual` tool, the `leancut` prompt and the `leancut://manual` resource.
- **A few tools**, served by the running app, so every agent sees the same ones (`leancut tools` lists them). They
  read the project and transcript, apply edit actions, undo, look at frames of the edit, and render.
- **Looking.** An agent checks its work the way you would: it looks at frames of the edit (images it can see) before
  and after a change, rather than trusting its own description.

## Connect an agent

LeanCut must be open: the agent talks to the app on your machine. Then print the setup for your agent:

```sh
leancut connect            # all agents
leancut connect codex      # one: claude, codex, cursor, opencode, gemini, qwen
leancut connect codex --write   # install it (merges into the config file, keeps a .bak)
```

`leancut` is `LeanCut --cli` with the packaged app (macOS: `/Applications/LeanCut.app/Contents/MacOS/LeanCut --cli`)
and `python leancut_cli.py` in a source checkout. The MCP server it configures is `LeanCut --mcp`, or
`python mcp_server.py` from source. On macOS, move LeanCut to Applications first, so the path stays valid.

| Agent | How | Config |
|---|---|---|
| Claude Code | `claude mcp add --scope user leancut -- <LeanCut> --mcp` | or a project's `.mcp.json` (`mcpServers`) |
| Codex | `[mcp_servers.leancut]` with `command`, `args`, `tool_timeout_sec` | `~/.codex/config.toml` |
| Cursor | `mcpServers.leancut` with `type: "stdio"`, `command`, `args` | `~/.cursor/mcp.json` or `.cursor/mcp.json` |
| OpenCode | `mcp.leancut` with `type: "local"`, `command: [...]` | `~/.config/opencode/opencode.json` or `opencode.json` |
| Gemini CLI | `mcpServers.leancut` with `command`, `args`, `timeout` | `~/.gemini/settings.json` |
| Qwen Code | `mcpServers.leancut` with `command`, `args`, `timeout` | `~/.qwen/settings.json` |

Renders take minutes, longer than some agents wait by default (Codex: 60 s). The snippets raise the tool timeout where
the agent allows it (`tool_timeout_sec` for Codex, `timeout` in ms for Gemini and Qwen).

An agent without MCP can use the CLI directly: `leancut manual`, `leancut tools`, `leancut call <tool> '<json>'`
(text is printed; images are saved to files and their paths printed, for the agent to open), and `leancut look <t>`.

If LeanCut isn't open, the MCP server offers a single `leancut_status` tool that says so; open the app and the real
tools appear (the server tells the agent its tool list changed). If LeanCut restarts, nothing needs reconnecting.
LeanCut writes its address to `server.json` in its data folder; `LEANCUT_URL=http://127.0.0.1:<port>` overrides it.

## A good prompt

Say what the video is for, who watches it, and how long it should be; let the agent look before and after it cuts.

> Read the LeanCut manual first. This is a 25-minute vlog of a day in Pokhara, Nepali and English. Make an 8-minute
> cut for YouTube: open on the paragliding take-off, keep the conversation at the tea shop, drop retakes and the
> parking-lot stretch. Use B-roll from the lake over the silent gaps. Keep my voice level. Look at the frames around
> each change before moving on, and tell me what you cut and why. Don't render until I say so.

Good habits: one goal per request; name moments by what happens in them (the agent finds them in the transcript);
ask for a summary of changes; review in the LeanCut window and undo anything you don't like.

## Safety

- **Only validated actions.** An agent edits through the same action vocabulary as LeanCut's own assistant and panels:
  every action is checked (known ids, value ranges, existing files) and an invalid one is refused with a reason. An
  agent can't write arbitrary files or run commands through LeanCut.
- **Undoable.** Each change is an undo step, in the app and through the agent (the history lasts while LeanCut is
  open). Your original footage is never modified.
- **Local only.** The server listens on 127.0.0.1 and refuses requests from web pages. Your footage and transcript go
  only to the agent you connected; whether that agent sends them to a cloud model is up to the agent you chose.

# Agent clients

How `tomaccoctl setup` connects each coding agent to Tomacco's MCP server, what it
writes where, how each agent then names Tomacco's tools, where it finds the agent
skill, and what of that was checked. [Agents](agents.md) is the guide to using Tomacco
once a client is connected.

```sh
tomaccoctl setup agents            # connect every agent found here, asking first for each
tomaccoctl setup agents status     # what each agent's file says, without changing it
tomaccoctl setup agents remove     # take Tomacco's own entries and skill links out again
```

`tomaccoctl setup` (the whole check, Android included) does the same as `setup agents`
before the Android part. `--yes` answers every question with yes; `--json` prints the
report as JSON with the questions on standard error; `--client ID` limits a run to one
client; `--tools full|compact|grouped` picks the [tool list](agents.md#a-shorter-tool-list)
a new entry asks for; `--opencode-config v1|v2` names OpenCode's layout when nothing else
does. `status` exits 1 when a client that is here needs a hand.

Setup detects a client by its configuration directory or its program on PATH. It reads
files, asks `opencode --version` at most, and starts no agent, no model and no device.
Every file it changes is copied first to `~/.local/share/tomacco/agent-backups/<client>/`
(the last ten are kept), written to a temporary file beside the target and moved over
it, then read back and checked; a file that does not read back as written is restored
from the copy. The entry is always the absolute path of `tomaccoctl` with the argument
`mcp`: a local stdio server, no port, no token.

## What each line means

| Verdict | Meaning | What to do |
| --- | --- | --- |
| `ok` | The `tomacco` entry is there and points at this `tomaccoctl`. | Nothing. |
| `missing` | The client is here and has no entry. | `tomaccoctl setup agents`. |
| `outdated` | Tomacco's entry points at another installation, or asks for another tool list than `--tools`. | `tomaccoctl setup agents` updates it, keeping the entry's other keys. |
| `off` | Tomacco's entry is there and the person switched it off in the client (`enabled: false`, `disabled: true`, omp's `disabledServers`). | Left as it is; switch it on in the client. |
| `conflict` | An entry named `tomacco` runs something else. | Left as it is; rename or remove it yourself. |
| `manual` | The file is one setup does not rewrite: comments or trailing commas (JSONC), a link, not an object, or YAML with anchors, aliases, merge keys, tags, tabs, document markers or duplicate names. | Add the printed snippet yourself, or run the client's own command where one is printed. |
| `unknown` | OpenCode's version could not be told. | `--opencode-config v1` or `v2`, or add the snippet yourself. |
| `skipped` | You said no. | |
| `failed` | The client's command or the write failed; the reason is printed and the file restored. | Run the printed command yourself. |

## The clients

Each adapter was written against the upstream named here, read on 2026-11-06. *Fixture*
means the file handling was run against files like the client's own in a scratch home
(`tests/python/test_agent_clients.py`, `native/agent_clients.rs`) and the written entry
was then started as that client would start it and spoken to over MCP. *Real client*
means the client itself was run against Tomacco; none was in this checkout, so each
client's own start of the server is still to be seen.

| Client | Upstream read | Where setup writes (user scope) | Entry | How the client names `device.list` | Skill directory | Checked |
| --- | --- | --- | --- | --- | --- | --- |
| Claude Code | [code.claude.com/docs/en/mcp](https://code.claude.com/docs/en/mcp) | its own `claude mcp add --transport stdio --scope user tomacco -- PROGRAM mcp`; `claude mcp get tomacco` reports it | | as Claude Code does; not verified here | `~/.claude/skills/tomacco` | fixture: a stand-in `claude` records the exact command |
| Codex | [developers.openai.com/codex/mcp](https://developers.openai.com/codex/mcp) | its own `codex mcp add tomacco -- PROGRAM mcp` | | not verified here | `~/.codex/skills/tomacco` | fixture, as above |
| Gemini CLI | [google-gemini/gemini-cli](https://github.com/google-gemini/gemini-cli) | `~/.gemini/settings.json`, `mcpServers` | `{"command": PROGRAM, "args": ["mcp"]}` | not verified here | `~/.agents/skills` (when that directory exists) | fixture |
| Cursor | [cursor.com/docs/context/mcp](https://cursor.com/docs/context/mcp) | `~/.cursor/mcp.json`, `mcpServers` | as Gemini CLI | not verified here | | fixture |
| VS Code | [MCP servers in VS Code](https://code.visualstudio.com/docs/copilot/customization/mcp-servers) | `$XDG_CONFIG_HOME/Code/User/mcp.json`, `servers` | `{"type": "stdio", "command": PROGRAM, "args": ["mcp"]}` | not verified here | | fixture |
| Zed | [zed.dev/docs/ai/mcp](https://zed.dev/docs/ai/mcp) | `$XDG_CONFIG_HOME/zed/settings.json`, `context_servers` | `{"source": "custom", "command": PROGRAM, "args": ["mcp"], "env": {}}` | not verified here | | fixture; Zed's usual file has comments, so it is mostly `manual` |
| OpenCode 2 | [anomalyco/opencode](https://github.com/anomalyco/opencode) `@opencode/cli` 2.2.5, commit `3ec9b3b` (2026-11-06): `packages/schema/src/mcp.ts`, `packages/cli/src/commands/handlers/mcp/add.ts` | its own `opencode mcp add --global tomacco -- PROGRAM mcp` when `opencode` is on PATH (it keeps JSONC comments), else `~/.config/opencode/opencode.json`, `mcp.servers` | `{"type": "local", "command": [PROGRAM, "mcp"]}`; `disabled: true` switches it off | `tomacco_device_list` (`packages/core/src/tool/runtime.ts`: every character outside `[A-Za-z0-9_-]` becomes `_`) | `~/.config/opencode/skills/tomacco`; it also reads `~/.agents/skills` and `~/.claude/skills` | fixture: a stand-in `opencode` answers `--version` and records the add |
| OpenCode 1 | the same repository, `packages/core/src/v1/config/mcp.ts` | `~/.config/opencode/opencode.json`, `mcp` | `{"type": "local", "command": [PROGRAM, "mcp"], "enabled": true}` | as OpenCode 2 | as OpenCode 2 | fixture |
| Grok CLI | [superagent-ai/grok-cli](https://github.com/superagent-ai/grok-cli), npm `grok-dev` 1.1.7, commit `fb97af8` (2026-05-15): `src/utils/settings.ts`, `src/mcp/runtime.ts`, `src/utils/skills.ts`. The community program, not xAI's Grok app or API. | `~/.grok/user-settings.json`, the `mcp.servers` array | `{"id": "tomacco", "label": "Tomacco", "enabled": true, "transport": "stdio", "command": PROGRAM, "args": ["mcp"], "env": {"TOMACCO_MCP_TOOLS": "grouped"}}` | `mcp_tomacco__device.list` (`mcp_<id>__<name>`, the name unchanged) | `~/.agents/skills/tomacco`; setup makes `~/.agents/skills` for it | fixture |
| Hermes Agent | [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent), commit `90d6b98` (2026-11-06): `hermes_cli/mcp_config.py`, `tools/mcp_tool_schema.py`, `tools/skills_tool.py`, `hermes_constants.py`; [docs: MCP](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp) | `$HERMES_HOME/config.yaml` (`~/.hermes/config.yaml`), `mcp_servers`, edited as text | `tomacco:` with `command: "PROGRAM"` and `args: ["mcp"]`; `enabled: false` switches it off | `mcp__tomacco__device_list` (`[^A-Za-z0-9_]` becomes `_`, 64 characters at most with a hash suffix) | `~/.hermes/skills/tomacco` (its catalog walks with `os.scandir`, which follows links) | fixture |
| omp | [can1357/oh-my-pi](https://github.com/can1357/oh-my-pi), `@oh-my-pi/pi-coding-agent` 18.17.0, commit `b47e53b` (2026-11-06): `docs/mcp-config.md`, `docs/mcp-runtime-lifecycle.md`, `docs/skills.md`, `packages/coding-agent/src/mcp/config-writer.ts`, `src/discovery/helpers.ts` | `~/.omp/agent/mcp.json` (`$PI_CONFIG_DIR`, `$PI_CODING_AGENT_DIR` and `$OMP_PROFILE` move it), `mcpServers`; a new file gets its `$schema` | `{"type": "stdio", "command": PROGRAM, "args": ["mcp"]}`; `enabled: false` or `disabledServers` switches it off | `mcp__tomacco_device_list` (lowercased, `[^a-z0-9_]` becomes `_`, runs collapsed, 64 at most) | `~/.omp/agent/skills/tomacco` (links are accepted) | fixture |

PROGRAM is the absolute path of the `tomaccoctl` that ran setup. Every tool name a
client makes from Tomacco's 288 operations stays distinct and within 64 characters
under each rule (`agent_clients::tests::tool_names_follow_each_clients_rule_and_stay_distinct`).

### Grok CLI and the grouped list

Grok CLI passes tool names to the xAI API as they are, dots included, and the API takes
at most 350 tools a request. Setup therefore asks Grok CLI for the
[grouped list](agents.md#a-shorter-tool-list): one tool per area with plain names, every
operation reachable as `operation` inside it, and a `schema` tool for any operation's
arguments. `--tools full` or `compact` overrides that for a new entry; an existing entry's
choice stands until `--tools` says otherwise. Whether the xAI API accepts dotted names
was not checked.

### OpenCode 1 and 2

OpenCode 2 lists servers under `mcp.servers` and switches one off with `disabled`;
version 1 lists them directly under `mcp` with `enabled`. Setup takes the layout from
`opencode --version` when the program is on PATH, else from the file (an entry with a
`type` under `mcp` is version 1, a `mcp.servers` object version 2), else it reports
`unknown` and `--opencode-config v1|v2` decides. With version 2 on PATH, OpenCode's own
`mcp add --global` writes the file, which keeps the comments of an `opencode.jsonc`; the
file is read back afterwards. The global file is the first of `opencode.json`,
`opencode.jsonc`, `.opencode/opencode.json`, `.opencode/opencode.jsonc` in
`~/.config/opencode` that exists, as OpenCode's command chooses. A version 1 entry in a
version 2 installation is reported as a conflict and left for you to move. OpenCode's
documentation says a project `opencode.json` replaces a same-named server whole, so a
project file of yours wins over the global entry.

### Hermes Agent and YAML

Setup does not parse YAML in general. It adds Tomacco's entry at the end of the
`mcp_servers` block, with the block's own indentation, when the file is a plain block
mapping without anchors, aliases, merge keys, tags, tabs, document markers or duplicate
names, and it changes only the `command` and `args` lines of its own entry on an update.
Anything else makes the file yours to edit, with the block to paste printed. Hermes'
own `hermes mcp add tomacco --command PROGRAM --args mcp` is printed too: it starts
`tomaccoctl mcp` once to list the tools and asks which to enable, so setup does not run
it for you. A named Hermes profile is reached through `HERMES_HOME`; setup says so when
`~/.hermes/active_profile` names one. Hermes also reads `~/.agents/skills` when
`skills.external_dirs` lists it.

## Check it without an agent

The written entry can be tried without any client or model. Take the command and
arguments from the file, start them with the entry's `env`, and speak MCP on standard
input:

```sh
TOMACCO_MCP_TOOLS=grouped "$(command -v tomaccoctl)" mcp <<'EOF'
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"check","version":"0"}}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
{"jsonrpc":"2.0","id":2,"method":"tools/list"}
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"agent","arguments":{"operation":"agent.policy"}}}
EOF
```

The first answer names the server `tomacco`; the second lists the tools in the mode the
variable asks for; the third answers `agent.policy` with `client` set to the name the
client sent in `initialize`, which is what the audit log and the agent policy go by. A
wrong argument comes back as a result with `isError` true and the error under
`structuredContent.error`, not as a protocol error. `tests/python/test_agent_clients.py`
does this for every written entry with each client's own name.

## Cleanup

`tomaccoctl setup agents remove` takes out the `tomacco` entry where it is Tomacco's
(a `tomaccoctl` serving `mcp`), through `claude mcp remove` and `codex mcp remove` for
those two, and removes the skill links that point at the installed skill. Other servers,
other keys, a conflicting entry and a skill of yours stay. `tomaccoctl uninstall` removes
the skill links the installer made and nothing from the agents' server lists; run
`setup agents remove` before it when the entries should go too.

"""`tomaccoctl setup agents` against stand-in coding agents in a scratch home: Claude
Code and Codex as scripts that record what they were told, OpenCode 2 as a script that
writes its own file, and Gemini CLI, Cursor, VS Code, Zed, OpenCode 1, Grok CLI, Hermes
Agent and omp as the files they read, from tests/fixtures/agent-clients. Then the
server each written entry names is started the way that client would start it, with
that entry's environment, and asked over MCP for its tools, a read-only call, a resource,
the prompts and a refused call. No real agent, model, network or device is involved:
PATH holds only the stand-ins, and the home is private.

Run: TOMACCOCTL=target/debug/tomaccoctl PYTHONPATH=tools:tests/python:tests:. \\
     python3 -m unittest tests.python.test_agent_clients"""
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from native_client import binary

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/agent-clients"
OPERATIONS = [entry["name"] for entry in json.loads((ROOT / "assets/operations.json").read_text(encoding="utf-8"))]
PLAIN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

# Stand-ins. Each uses only shell builtins and absolute paths, as PATH holds nothing else.
CLI_STAND_IN = """#!/bin/sh
# A stand-in for `{name}`: records every call and keeps a mark for `mcp get`.
printf '%s\\n' "$*" >> "$HOME/{name}.log"
case "$1 $2" in
  "mcp get") [ -f "$HOME/.{name}/.tomacco-mark" ] && exit 0 || exit 1 ;;
  "mcp add") : > "$HOME/.{name}/.tomacco-mark"; exit 0 ;;
  "mcp remove") /bin/rm -f "$HOME/.{name}/.tomacco-mark"; exit 0 ;;
esac
exit 2
"""
OPENCODE_STAND_IN = """#!/bin/sh
# A stand-in for OpenCode 2: --version, and `mcp add --global` written as its
# jsonc-parser would, into the first global file that exists.
if [ "$1" = --version ]; then echo "2.2.5"; exit 0; fi
printf '%s\\n' "$*" >> "$HOME/opencode.log"
exec /usr/bin/python3 -I - "$@" <<'PY'
import json, os, re, sys
args = sys.argv[1:]
assert args[:3] == ["mcp", "add", "--global"], args
rest = args[3:]
environment = {}
while rest and rest[0] == "--env":
    key, value = rest[1].split("=", 1)
    environment[key] = value
    rest = rest[2:]
name = rest[0]
assert rest[1] == "--", rest
command = rest[2:]
base = os.path.join(os.environ["XDG_CONFIG_HOME"], "opencode")
candidates = [os.path.join(base, n) for n in ("opencode.json", "opencode.jsonc", ".opencode/opencode.json", ".opencode/opencode.jsonc")]
path = next((c for c in candidates if os.path.isfile(c)), candidates[0])
server = {"type": "local", "command": command}
if environment:
    server["environment"] = environment
os.makedirs(os.path.dirname(path), exist_ok=True)
if path.endswith(".jsonc"):
    # The comment stays, the entry is added; what jsonc-parser's modify does.
    text = open(path).read()
    entry = json.dumps(server, indent=2).replace("\\n", "\\n      ")
    text = text.replace('"servers": {', '"servers": {\\n      "%s": %s,' % (name, entry), 1)
    open(path, "w").write(text)
else:
    data = json.load(open(path)) if os.path.isfile(path) else {}
    data.setdefault("mcp", {}).setdefault("servers", {})[name] = server
    json.dump(data, open(path, "w"), indent=2)
print('MCP server "%s" added to %s' % (name, path))
PY
"""


def tool_name(client, operation):
    """How each client names Tomacco's tool to its model, from its source."""
    if client == "opencode":
        return "tomacco_" + re.sub(r"[^a-zA-Z0-9_-]", "_", operation)
    if client == "grok-cli":
        return "mcp_tomacco__" + operation
    if client == "hermes":
        name = "mcp__tomacco__" + re.sub(r"[^A-Za-z0-9_]", "_", operation)
        if len(name) > 64:
            name = name[:55] + "_" + hashlib.sha256(name.encode()).hexdigest()[:8]
        return name
    if client == "omp":
        name = re.sub(r"_+", "_", re.sub(r"[^a-z0-9_]", "_", operation.lower())).strip("_")
        return "mcp__tomacco_" + name
    return operation


class Session:
    """The server started as a client's entry says: that command, those arguments,
    that environment on top of the scratch one."""

    def __init__(self, command, args, env):
        self.process = subprocess.Popen([command, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, env=env)
        self.replies = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self.process.stderr.read, daemon=True).start()
        self.next = 1

    def _read(self):
        for line in self.process.stdout:
            self.replies.put(json.loads(line))

    def request(self, method, params=None, timeout=60):
        ident = self.next
        self.next += 1
        message = {"jsonrpc": "2.0", "id": ident, "method": method}
        if params is not None:
            message["params"] = params
        self.process.stdin.write(json.dumps(message).encode() + b"\n")
        self.process.stdin.flush()
        while True:
            reply = self.replies.get(timeout=timeout)
            if reply.get("id") == ident:
                return reply

    def notify(self, method, params):
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method, "params": params}).encode() + b"\n")
        self.process.stdin.flush()

    def close(self):
        try:
            self.process.stdin.close()
            return self.process.wait(30)
        finally:
            if self.process.poll() is None:
                self.process.kill()
                self.process.wait()
            for pipe in (self.process.stdout, self.process.stderr):
                pipe.close()


class Home:
    """A scratch home with a PATH of stand-ins only."""

    def __init__(self, name="home"):
        self.directory = tempfile.TemporaryDirectory(prefix="agents-")
        self.home = Path(self.directory.name) / name
        self.home.mkdir()
        self.bin = self.home / "bin"
        self.bin.mkdir()
        self.config = self.home / ".config"
        self.env = {"HOME": str(self.home), "TOMACCO_HOME": str(self.home / ".tomacco"),
                    "XDG_CONFIG_HOME": str(self.config), "PATH": str(self.bin), "TOMACCO_UPDATES": "off",
                    "LANG": "C.UTF-8"}

    def cleanup(self):
        self.directory.cleanup()

    def stand_in(self, name, text):
        path = self.bin / name
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755)

    def file(self, relative, text=None, fixture=None):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text if text is not None else (FIXTURES / fixture).read_text(encoding="utf-8"), encoding="utf-8")
        return path

    def run(self, *args, expect=0):
        run = subprocess.run([binary(), "setup", "agents", *args], env=self.env, capture_output=True,
                             text=True, timeout=300)
        if run.returncode != expect:
            raise AssertionError((run.returncode, run.stdout, run.stderr))
        return run

    def report(self, *args, expect=0):
        run = self.run(*args, "--json", expect=expect)
        report = json.loads(run.stdout)
        return {client["id"]: client for client in report["clients"]}, report

    def read_json(self, relative):
        return json.loads((self.home / relative).read_text(encoding="utf-8"))


class SetupAgents(unittest.TestCase):
    def setUp(self):
        self.homes = []

    def tearDown(self):
        for home in self.homes:
            home.cleanup()

    def home(self, name="home"):
        home = Home(name)
        self.homes.append(home)
        return home

    def populated(self, name="home"):
        """Every client present: two by stand-in command, OpenCode 2 by stand-in and
        file, the rest by their files."""
        home = self.home(name)
        for client in ("claude", "codex"):
            home.stand_in(client, CLI_STAND_IN.format(name=client))
            (home.home / f".{client}").mkdir()
        home.stand_in("opencode", OPENCODE_STAND_IN)
        home.file(".config/opencode/opencode.jsonc", fixture="opencode-v2.jsonc")
        home.file(".gemini/settings.json", '{"theme": "Dracula", "mcpServers": {"other": {"command": "x"}}}\n')
        home.file(".cursor/mcp.json", '{"mcpServers": {}}\n')
        (home.config / "Code/User").mkdir(parents=True)
        home.file(".config/zed/settings.json", '// Zed\n{"vim_mode": true}\n')
        home.file(".grok/user-settings.json", fixture="grok-user-settings.json")
        home.file(".hermes/config.yaml", fixture="hermes-config.yaml")
        home.file(".omp/agent/mcp.json", fixture="omp-mcp.json")
        return home

    def test_an_empty_home_has_no_client_and_nothing_is_made(self):
        home = self.home()
        clients, report = home.report("status")
        self.assertEqual({client["state"] for client in clients.values()}, {"absent"})
        self.assertEqual(report["attention"], 0)
        self.assertEqual(sorted(p.name for p in home.home.iterdir()), ["bin"])
        self.assertEqual(home.run("--yes").stdout, "")
        self.assertEqual(sorted(p.name for p in home.home.iterdir()), ["bin"])

    def test_every_client_is_connected_once_in_its_own_layout_and_then_removed(self):
        home = self.populated()
        program = str(Path(binary()).resolve())
        clients, report = home.report("status", expect=1)
        self.assertEqual({c: clients[c]["state"] for c in clients},
                         {"claude-code": "not_connected", "codex": "not_connected", "gemini-cli": "not_connected",
                          "cursor": "not_connected", "vscode": "not_connected", "zed": "manual",
                          "opencode": "not_connected", "grok-cli": "not_connected", "hermes": "not_connected",
                          "omp": "not_connected"})
        self.assertIn("comments", clients["zed"]["detail"])
        self.assertEqual(clients["opencode"]["layout"], "v2")
        self.assertEqual(report["attention"], 10)
        hermes_before = (home.home / ".hermes/config.yaml").read_text(encoding="utf-8")

        clients, report = home.report("--yes")
        self.assertEqual(report["program"], program)
        expected = {c: "added" for c in clients}
        expected["zed"] = "manual"
        self.assertEqual({c: clients[c]["state"] for c in clients}, expected)
        # Claude Code and Codex were told the absolute program, as their docs want.
        # One `mcp get` from the status run, one before the add.
        self.assertEqual((home.home / "claude.log").read_text().splitlines(),
                         ["mcp get tomacco", "mcp get tomacco", f"mcp add --transport stdio --scope user tomacco -- {program} mcp"])
        self.assertEqual((home.home / "codex.log").read_text().splitlines(),
                         ["mcp get tomacco", "mcp get tomacco", f"mcp add tomacco -- {program} mcp"])
        # OpenCode 2's own command wrote the JSONC file; the comment is still there.
        self.assertEqual((home.home / "opencode.log").read_text().splitlines(),
                         [f"mcp add --global tomacco -- {program} mcp"])
        jsonc = (home.config / "opencode/opencode.jsonc").read_text()
        self.assertIn("// OpenCode 2 global configuration", jsonc)
        self.assertIn(f'"{program}",', jsonc)
        self.assertLess(jsonc.index("tomacco"), jsonc.index("context7"))
        self.assertEqual(clients["opencode"]["command"].split("   #")[0], f"opencode mcp add --global tomacco -- {program} mcp")
        # The file clients, each in its layout, everything else kept.
        gemini = home.read_json(".gemini/settings.json")
        self.assertEqual(gemini, {"theme": "Dracula", "mcpServers": {"other": {"command": "x"},
                                                                   "tomacco": {"command": program, "args": ["mcp"]}}})
        self.assertEqual(home.read_json(".cursor/mcp.json"), {"mcpServers": {"tomacco": {"command": program, "args": ["mcp"]}}})
        self.assertEqual(home.read_json(".config/Code/User/mcp.json"),
                         {"servers": {"tomacco": {"type": "stdio", "command": program, "args": ["mcp"]}}})
        self.assertEqual((home.config / "zed/settings.json").read_text(), '// Zed\n{"vim_mode": true}\n')
        self.assertIn('"context_servers": {', clients["zed"]["snippet"])
        grok = home.read_json(".grok/user-settings.json")
        self.assertEqual(grok["apiKey"], "xai-REDACTED-FIXTURE")
        self.assertEqual([s["id"] for s in grok["mcp"]["servers"]], ["github", "tomacco"])
        self.assertEqual(grok["mcp"]["servers"][1], {"id": "tomacco", "label": "Tomacco", "enabled": True, "transport": "stdio",
                                                    "command": program, "args": ["mcp"], "env": {"TOMACCO_MCP_TOOLS": "grouped"}})
        hermes = (home.home / ".hermes/config.yaml").read_text(encoding="utf-8")
        self.assertEqual(hermes, hermes_before.replace(
            "      include: [find_issues, get_issue]\n",
            f'      include: [find_issues, get_issue]\n  tomacco:\n    command: "{program}"\n    args: ["mcp"]\n'))
        omp = home.read_json(".omp/agent/mcp.json")
        self.assertEqual(omp["mcpServers"]["tomacco"], {"type": "stdio", "command": program, "args": ["mcp"]})
        self.assertEqual(omp["disabledServers"], ["filesystem"])
        self.assertTrue(omp["$schema"].endswith("mcp-schema.json"))
        # Backups of the files that were changed, outside those files' directories.
        backups = home.home / ".local/share/tomacco/agent-backups"
        self.assertEqual(sorted(p.name for p in backups.iterdir()), ["cursor", "gemini-cli", "grok-cli", "hermes", "omp"])
        self.assertEqual(json.loads(next((backups / "grok-cli").iterdir()).read_text())["mcp"]["servers"][0]["id"], "github")

        # A second run asks nothing, changes nothing.
        snapshot = {p: p.read_bytes() for p in home.home.rglob("*") if p.is_file() and "agent-backups" not in str(p) and p.suffix != ".log"}
        clients, report = home.report("--yes")
        expected = {c: "connected" for c in clients}
        expected["zed"] = "manual"
        self.assertEqual({c: clients[c]["state"] for c in clients}, expected)
        self.assertEqual({p: p.read_bytes() for p in home.home.rglob("*") if p.is_file() and "agent-backups" not in str(p) and p.suffix != ".log"}, snapshot)
        clients, report = home.report("status", expect=1)
        self.assertEqual(report["attention"], 1)  # Zed's file is the person's to edit.
        self.assertEqual(home.run("status", "--client", "hermes", "--client", "omp").returncode, 0)

        # Removal takes exactly the entries and leaves the other servers.
        clients, report = home.report("remove", "--yes")
        expected = {c: "removed" for c in clients}
        expected["zed"] = "manual"
        expected["opencode"] = "manual"  # OpenCode has no remove command and its file has comments
        self.assertEqual({c: clients[c]["state"] for c in clients}, expected)
        self.assertEqual((home.home / "claude.log").read_text().splitlines()[-1], "mcp remove --scope user tomacco")
        self.assertEqual((home.home / "codex.log").read_text().splitlines()[-1], "mcp remove tomacco")
        self.assertEqual(home.read_json(".gemini/settings.json"), {"theme": "Dracula", "mcpServers": {"other": {"command": "x"}}})
        self.assertEqual([s["id"] for s in home.read_json(".grok/user-settings.json")["mcp"]["servers"]], ["github"])
        self.assertEqual((home.home / ".hermes/config.yaml").read_text(encoding="utf-8"), hermes_before)
        self.assertEqual(sorted(home.read_json(".omp/agent/mcp.json")["mcpServers"]), ["filesystem"])
        # OpenCode has no remove command; its JSONC file is the person's.
        self.assertIn("remove the tomacco entry yourself", clients["opencode"]["detail"])
        self.assertIn("tomacco", (home.config / "opencode/opencode.jsonc").read_text())

    def test_opencode_1_and_an_unknown_version_are_handled_as_documented(self):
        home = self.home()
        program = str(Path(binary()).resolve())
        # A version 1 file without opencode on PATH: its layout says v1.
        home.file(".config/opencode/opencode.json", fixture="opencode-v1.json")
        clients, _ = home.report("--yes", "--client", "opencode")
        self.assertEqual((clients["opencode"]["state"], clients["opencode"]["layout"]), ("added", "v1"))
        written = home.read_json(".config/opencode/opencode.json")
        self.assertEqual(written["mcp"]["tomacco"], {"type": "local", "command": [program, "mcp"], "enabled": True})
        self.assertEqual(written["mcp"]["context7"]["enabled"], False)
        self.assertEqual(written["model"], "anthropic/claude-sonnet-4")
        # Nothing in the file and no program: unknown, with the way out named.
        home.file(".config/opencode/opencode.json", '{"model": "x"}\n')
        clients, report = home.report("--yes", "--client", "opencode")
        self.assertEqual(clients["opencode"]["state"], "unknown")
        self.assertIn("--opencode-config", clients["opencode"]["detail"])
        self.assertEqual(home.read_json(".config/opencode/opencode.json"), {"model": "x"})
        clients, _ = home.report("--yes", "--client", "opencode", "--opencode-config", "v2")
        self.assertEqual(clients["opencode"]["state"], "added")
        self.assertEqual(home.read_json(".config/opencode/opencode.json")["mcp"]["servers"]["tomacco"]["command"], [program, "mcp"])

    def test_a_disabled_entry_a_conflict_and_an_unreadable_file_are_left_alone(self):
        home = self.home()
        program = str(Path(binary()).resolve())
        home.file(".gemini/settings.json", json.dumps({"mcpServers": {"tomacco": {"command": program, "args": ["mcp"], "disabled": True}}}))
        home.file(".cursor/mcp.json", '{"mcpServers": {"tomacco": {"command": "/usr/bin/other", "args": []}}}')
        home.file(".hermes/config.yaml", fixture="hermes-anchors.yaml")
        home.file(".omp/agent/mcp.json", json.dumps({"mcpServers": {"tomacco": {"type": "stdio", "command": program, "args": ["mcp"]}}, "disabledServers": ["tomacco"]}))
        before = {p: p.read_bytes() for p in home.home.rglob("*") if p.is_file()}
        clients, report = home.report("--yes")
        self.assertEqual(clients["gemini-cli"]["state"], "disabled")
        self.assertEqual(clients["cursor"]["state"], "conflict")
        self.assertIn("/usr/bin/other", clients["cursor"]["detail"])
        self.assertEqual(clients["hermes"]["state"], "manual")
        self.assertIn("anchor", clients["hermes"]["detail"])
        self.assertEqual(clients["hermes"]["snippet"], f'mcp_servers:\n  tomacco:\n    command: "{program}"\n    args: ["mcp"]')
        self.assertEqual(clients["omp"]["state"], "disabled")
        self.assertEqual({p: p.read_bytes() for p in home.home.rglob("*") if p.is_file()}, before)
        text = home.run("status", expect=1).stdout
        self.assertIn("conflict Cursor", text)
        self.assertIn("manual   Hermes Agent", text)
        self.assertIn("off      Gemini CLI", text)
        self.assertIn("mcp_servers:", text)
        # Removal leaves the conflict and takes nothing from a disabled entry it did not add.
        clients, _ = home.report("remove", "--yes")
        self.assertEqual(clients["cursor"]["state"], "conflict")
        self.assertEqual(clients["gemini-cli"]["state"], "removed")
        self.assertEqual(home.read_json(".gemini/settings.json"), {"mcpServers": {}})

    def test_a_home_with_spaces_and_unicode_is_written_and_read_back(self):
        home = self.home("Mein Ordner üñí")
        home.file(".hermes/config.yaml", "model: x\n")
        home.file(".grok/user-settings.json", "{}\n")
        (home.home / ".omp").mkdir()
        clients, _ = home.report("--yes")
        self.assertEqual({clients[c]["state"] for c in ("hermes", "grok-cli", "omp")}, {"added"})
        clients, _ = home.report("status")
        self.assertEqual({clients[c]["state"] for c in ("hermes", "grok-cli", "omp")}, {"connected"})
        self.assertIn(str(home.home / ".omp/agent/mcp.json"), clients["omp"]["file"])

    def test_the_skill_is_linked_where_each_found_client_reads_it(self):
        home = self.populated()
        skill = home.home / ".local/lib/tomacco/skills/tomacco"
        skill.mkdir(parents=True)
        shutil.copy(ROOT / "integrations/agent-skill/tomacco/SKILL.md", skill / "SKILL.md")
        # Something of the person's under ~/.agents/skills/tomacco stays.
        home.file(".agents/skills/tomacco/SKILL.md", "theirs\n")
        _, report = home.report("--yes")
        skills = {entry["label"]: entry for entry in report["skills"]}
        self.assertEqual({label: entry["state"] for label, entry in skills.items()},
                         {"Claude Code": "added", "Codex": "added", "Grok CLI and other agents that read ~/.agents": "taken",
                          "OpenCode": "added", "Hermes Agent": "added", "omp": "added"})
        for relative in (".claude", ".codex", ".config/opencode", ".hermes", ".omp/agent"):
            link = home.home / relative / "skills/tomacco"
            self.assertEqual(os.readlink(link), str(skill), relative)
            self.assertTrue((link / "SKILL.md").is_file())
        self.assertEqual((home.home / ".agents/skills/tomacco/SKILL.md").read_text(), "theirs\n")
        _, report = home.report("remove", "--yes")
        self.assertEqual({entry["label"]: entry["state"] for entry in report["skills"]},
                         {"Claude Code": "removed", "Codex": "removed", "Grok CLI and other agents that read ~/.agents": "taken",
                          "OpenCode": "removed", "Hermes Agent": "removed", "omp": "removed"})
        self.assertFalse(os.path.lexists(home.home / ".hermes/skills/tomacco"))
        self.assertEqual((home.home / ".agents/skills/tomacco/SKILL.md").read_text(), "theirs\n")

    def entries(self, home):
        """(client, command, args, env) as each written file says."""
        program = str(Path(binary()).resolve())
        found = []
        gemini = home.read_json(".gemini/settings.json")["mcpServers"]["tomacco"]
        found.append(("gemini-cli", gemini["command"], gemini["args"], gemini.get("env", {})))
        grok = next(s for s in home.read_json(".grok/user-settings.json")["mcp"]["servers"] if s["id"] == "tomacco")
        self.assertTrue(grok["enabled"])
        found.append(("grok-cli", grok["command"], grok["args"], grok.get("env", {})))
        omp = home.read_json(".omp/agent/mcp.json")["mcpServers"]["tomacco"]
        found.append(("omp", omp["command"], omp["args"], omp.get("env", {})))
        jsonc = (home.config / "opencode/opencode.jsonc").read_text()
        opencode = json.loads(re.sub(r",(\s*[}\]])", r"\1", re.sub(r"^\s*//.*$", "", jsonc, flags=re.M)))
        server = opencode["mcp"]["servers"]["tomacco"]
        found.append(("opencode", server["command"][0], server["command"][1:], server.get("environment", {})))
        hermes = (home.home / ".hermes/config.yaml").read_text(encoding="utf-8")
        block = hermes.split("  tomacco:\n", 1)[1]
        command = re.search(r'^    command: "(.*)"$', block, re.M).group(1)
        args = json.loads(re.search(r"^    args: (\[.*\])$", block, re.M).group(1))
        found.append(("hermes", command, args, {}))
        for client, command, args, env in found:
            self.assertEqual(command, program, client)
            self.assertEqual(args, ["mcp"], client)
        return found

    def test_the_server_each_entry_names_speaks_mcp_as_that_client_starts_it(self):
        home = self.populated()
        home.report("--yes")
        names = {"gemini-cli": "gemini-cli-mcp-client", "grok-cli": "grok-cli-tomacco", "omp": "omp",
                 "opencode": "opencode", "hermes": "hermes-agent"}
        for client, command, args, env in self.entries(home):
            with self.subTest(client=client):
                session = Session(command, args, {**home.env, **env})
                try:
                    reply = session.request("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                                           "clientInfo": {"name": names[client], "version": "0"}})
                    self.assertEqual(reply["result"]["serverInfo"]["name"], "tomacco")
                    self.assertEqual(reply["result"]["protocolVersion"], "2025-11-25")
                    session.notify("notifications/initialized", {})
                    tools = session.request("tools/list", {})["result"]["tools"]
                    listed = [tool["name"] for tool in tools]
                    mode = env.get("TOMACCO_MCP_TOOLS", "full")
                    if mode == "grouped":
                        # One tool per area (the name before the first dot, else
                        # "general", as native/mcp_tools.rs area()) plus schema, and
                        # within the xAI API's 350 tools a request that Grok CLI
                        # forwards them to (docs/agent-clients.md).
                        areas = {name.split(".", 1)[0] if "." in name else "general" for name in OPERATIONS}
                        self.assertEqual(sorted(listed), sorted(areas | {"schema"}))
                        self.assertLessEqual(len(tools), 350)
                        self.assertLess(len(tools), len(OPERATIONS))
                        offered = sorted(name for tool in tools if tool["name"] != "schema"
                                         for name in tool["inputSchema"]["properties"]["operation"]["enum"])
                        self.assertEqual(offered, sorted(OPERATIONS))
                        call = ("agent", {"operation": "agent.policy", "arguments": {}})
                    else:
                        self.assertEqual(sorted(n for n in listed if n != "schema"), sorted(OPERATIONS))
                        self.assertEqual(len(tools), len(OPERATIONS) + (1 if mode == "compact" else 0))
                        call = ("agent.policy", {})
                    # The names the client's model sees are distinct and plain.
                    seen = [tool_name(client, name) for name in listed]
                    self.assertEqual(len(set(seen)), len(seen))
                    if client != "gemini-cli":  # its rule was not read; the others' were
                        for name in seen:
                            self.assertRegex(name, PLAIN)
                    for tool in tools:
                        self.assertEqual(sorted(tool["annotations"]), ["destructiveHint", "idempotentHint", "openWorldHint", "readOnlyHint"]) if mode != "compact" else None
                    # A read-only call, with the client's name recorded for the policy.
                    reply = session.request("tools/call", {"name": call[0], "arguments": call[1]})["result"]
                    self.assertFalse(reply["isError"], reply)
                    self.assertEqual(reply["structuredContent"]["channel"], "mcp")
                    self.assertEqual(reply["structuredContent"]["client"], names[client])
                    self.assertEqual(json.loads(reply["content"][0]["text"]), reply["structuredContent"])
                    # A refused call answers as an error result, not a protocol error.
                    wrong = session.request("tools/call", {"name": call[0], "arguments": {**call[1], "nothing": 1}})["result"]
                    self.assertTrue(wrong["isError"])
                    self.assertEqual(wrong["structuredContent"]["error"]["code"], "invalid_argument")
                    self.assertTrue(wrong["structuredContent"]["error"]["remedy"])
                    unknown = session.request("tools/call", {"name": "no.such.tool", "arguments": {}})
                    self.assertTrue(unknown["result"]["isError"])
                    # The schema tool, where the list is short, names an operation's arguments.
                    if mode != "full":
                        schema = session.request("tools/call", {"name": "schema", "arguments": {"operation": "device.list"}})["result"]
                        self.assertEqual(schema["structuredContent"]["name"], "device.list")
                    # Resources and prompts.
                    guide = session.request("resources/read", {"uri": "tomacco://docs/agents"})["result"]["contents"][0]
                    self.assertIn("# Agents", guide["text"])
                    prompts = session.request("prompts/list", {})["result"]["prompts"]
                    self.assertEqual(sorted(p["name"] for p in prompts), ["run-android-app", "run-virtual-iphone", "setup"])
                    # A cancelled wait on a job that does not exist ends at once, without a crash.
                    session.notify("notifications/cancelled", {"requestId": 999})
                    self.assertEqual(session.request("ping", {})["result"], {})
                finally:
                    self.assertEqual(session.close(), 0)

    def test_tools_option_sets_the_list_for_new_entries_and_updates_old_ones(self):
        home = self.home()
        home.file(".gemini/settings.json", "{}\n")
        home.file(".hermes/config.yaml", "")
        clients, _ = home.report("--yes", "--tools", "compact")
        self.assertEqual(home.read_json(".gemini/settings.json")["mcpServers"]["tomacco"]["env"], {"TOMACCO_MCP_TOOLS": "compact"})
        self.assertIn('    env:\n      TOMACCO_MCP_TOOLS: "compact"\n', (home.home / ".hermes/config.yaml").read_text())
        clients, _ = home.report("status", "--tools", "grouped", expect=1)
        self.assertEqual(clients["gemini-cli"]["state"], "outdated")
        clients, _ = home.report("--yes", "--tools", "grouped", "--client", "gemini-cli")
        self.assertEqual(clients["gemini-cli"]["state"], "updated")
        self.assertEqual(home.read_json(".gemini/settings.json")["mcpServers"]["tomacco"]["env"], {"TOMACCO_MCP_TOOLS": "grouped"})
        # Without --tools the person's choice stands.
        clients, _ = home.report("status", "--client", "gemini-cli")
        self.assertEqual(clients["gemini-cli"]["state"], "connected")
        home.run("--tools", "tiny", expect=2)


if __name__ == "__main__":
    unittest.main()

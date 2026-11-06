//! The coding agents Tomacco connects itself to, and how: where each one
//! lists its MCP servers, which layout and version it reads, how its tool
//! names come out and where it looks for skills. `tomaccoctl setup`
//! (setup.rs) asks the questions and prints; this module detects, plans,
//! writes and checks, and changes nothing in a person's file beyond the
//! `tomacco` entry it owns. Every write is backed up first, written to a
//! temporary file beside the target, read back and checked after it lands,
//! and rolled back when the check fails. Detection reads files and asks a
//! client's own command for its version at most; it starts no agent, no
//! model and no device.
//!
//! What each client reads was verified against its own source or current
//! documentation on 2026-11-06; docs/agent-clients.md names the commits.
use crate::core::*;
use crate::mcp_tools::Mode;
use crate::product;
use serde_json::{Map, Value, json};
use std::collections::BTreeMap;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

/// The name of the server entry in every client.
pub const SERVER: &str = "tomacco";
/// The environment variable that picks the tool list, in a client's `env`.
pub const TOOLS_VARIABLE: &str = crate::mcp_tools::VARIABLE;
/// Where a client's file is copied before it is changed, under HOME.
const BACKUPS: &str = ".local/share/tomacco/agent-backups";
const KEPT_BACKUPS: usize = 10;
/// The environment variables that move a client's files.
const VARIABLES: &[&str] = &[
    "XDG_CONFIG_HOME",
    "HERMES_HOME",
    "HERMES_DATA_DIR_SUFFIX",
    "PI_CONFIG_DIR",
    "PI_CODING_AGENT_DIR",
    "OMP_PROFILE",
    "PI_PROFILE",
];

#[derive(Clone, Copy, PartialEq, Eq, Debug, Hash, PartialOrd, Ord)]
pub enum ClientId {
    ClaudeCode,
    Codex,
    GeminiCli,
    Cursor,
    VsCode,
    Zed,
    OpenCode,
    GrokCli,
    Hermes,
    Omp,
}
pub const CLIENTS: [ClientId; 10] = [
    ClientId::ClaudeCode,
    ClientId::Codex,
    ClientId::GeminiCli,
    ClientId::Cursor,
    ClientId::VsCode,
    ClientId::Zed,
    ClientId::OpenCode,
    ClientId::GrokCli,
    ClientId::Hermes,
    ClientId::Omp,
];

impl ClientId {
    pub fn id(self) -> &'static str {
        match self {
            Self::ClaudeCode => "claude-code",
            Self::Codex => "codex",
            Self::GeminiCli => "gemini-cli",
            Self::Cursor => "cursor",
            Self::VsCode => "vscode",
            Self::Zed => "zed",
            Self::OpenCode => "opencode",
            Self::GrokCli => "grok-cli",
            Self::Hermes => "hermes",
            Self::Omp => "omp",
        }
    }
    pub fn parse(id: &str) -> Option<Self> {
        CLIENTS.into_iter().find(|client| client.id() == id)
    }
    pub fn name(self) -> &'static str {
        match self {
            Self::ClaudeCode => "Claude Code",
            Self::Codex => "Codex",
            Self::GeminiCli => "Gemini CLI",
            Self::Cursor => "Cursor",
            Self::VsCode => "VS Code",
            Self::Zed => "Zed",
            Self::OpenCode => "OpenCode",
            Self::GrokCli => "Grok CLI",
            Self::Hermes => "Hermes Agent",
            Self::Omp => "omp",
        }
    }
    /// The project this adapter was written against. Grok CLI is the
    /// community program by superagent-ai, not xAI's Grok app or API.
    pub fn upstream(self) -> &'static str {
        match self {
            Self::ClaudeCode => "https://code.claude.com/docs/en/mcp",
            Self::Codex => "https://developers.openai.com/codex/mcp",
            Self::GeminiCli => "https://github.com/google-gemini/gemini-cli",
            Self::Cursor => "https://cursor.com/docs/context/mcp",
            Self::VsCode => "https://code.visualstudio.com/docs/copilot/customization/mcp-servers",
            Self::Zed => "https://zed.dev/docs/ai/mcp",
            Self::OpenCode => "https://github.com/anomalyco/opencode",
            Self::GrokCli => "https://github.com/superagent-ai/grok-cli",
            Self::Hermes => "https://github.com/NousResearch/hermes-agent",
            Self::Omp => "https://github.com/can1357/oh-my-pi",
        }
    }
    /// The programs whose presence on PATH means the client is installed.
    pub fn programs(self) -> &'static [&'static str] {
        match self {
            Self::ClaudeCode => &["claude"],
            Self::Codex => &["codex"],
            Self::GeminiCli => &["gemini"],
            Self::Cursor => &["cursor"],
            Self::VsCode => &["code"],
            Self::Zed => &["zed", "zeditor"],
            Self::OpenCode => &["opencode"],
            Self::GrokCli => &["grok"],
            Self::Hermes => &["hermes"],
            Self::Omp => &["omp"],
        }
    }
    /// The tool list a new entry asks for. Grok CLI sends tool names to the
    /// xAI API unchanged, dots included, and takes at most 350 tools a
    /// request; the grouped list keeps every operation reachable through
    /// plain names.
    pub fn default_tools(self) -> Option<Mode> {
        match self {
            Self::GrokCli => Some(Mode::Grouped),
            _ => None,
        }
    }
    /// How the client names Tomacco's tool `operation` to its model, where
    /// the client's source says so: None where it was not verified.
    pub fn tool_name(self, operation: &str) -> Option<String> {
        let plain = |text: &str, keep_dash: bool| -> String {
            text.chars()
                .map(|c| {
                    if c.is_ascii_alphanumeric() || c == '_' || (keep_dash && c == '-') {
                        c
                    } else {
                        '_'
                    }
                })
                .collect()
        };
        match self {
            // packages/core/src/tool/runtime.ts: `${namespace}_${name}` with
            // every character outside [A-Za-z0-9_-] replaced.
            Self::OpenCode => Some(format!("{SERVER}_{}", plain(operation, true))),
            // src/mcp/runtime.ts: `mcp_${id}__${name}`, the name as it is.
            Self::GrokCli => Some(format!("mcp_{SERVER}__{operation}")),
            // tools/mcp_tool_schema.py: `mcp__<server>__<tool>`, [^A-Za-z0-9_]
            // replaced, clamped to 64 characters with a hash suffix.
            Self::Hermes => {
                let name = format!("mcp__{SERVER}__{}", plain(operation, false));
                Some(if name.len() <= 64 {
                    name
                } else {
                    let digest = sha256_hex(name.as_bytes());
                    format!("{}_{}", &name[..64 - 9], &digest[..8])
                })
            }
            // docs/mcp-runtime-lifecycle.md: `mcp__<server>_<tool>`, lowercased,
            // letters, digits and underscores, runs collapsed, 64 at most.
            Self::Omp => {
                let lower = plain(&operation.to_ascii_lowercase(), false);
                let mut collapsed = String::new();
                for c in lower.chars() {
                    if c == '_' && collapsed.ends_with('_') {
                        continue;
                    }
                    collapsed.push(c);
                }
                let name = format!("mcp__{SERVER}_{}", collapsed.trim_matches('_'));
                (name.len() <= 64).then_some(name)
            }
            _ => None,
        }
    }
}

fn sha256_hex(bytes: &[u8]) -> String {
    use sha2::Digest;
    let digest = sha2::Sha256::digest(bytes);
    digest.iter().map(|b| format!("{b:02x}")).collect()
}

/// The home, the XDG configuration directory, PATH and the variables that
/// move a client's files: read once from the process, or given by a test.
#[derive(Debug, Clone)]
pub struct Environment {
    pub home: PathBuf,
    pub config: PathBuf,
    vars: BTreeMap<String, String>,
    path: Vec<PathBuf>,
}
impl Environment {
    pub fn current() -> Result<Self> {
        let home = std::env::var_os("HOME")
            .filter(|home| !home.is_empty())
            .map(PathBuf::from)
            .ok_or_else(|| Error::new("setup_failed", "HOME is not set"))?;
        let vars = std::env::vars()
            .filter(|(key, _)| VARIABLES.contains(&key.as_str()))
            .collect();
        let path = std::env::var_os("PATH")
            .map(|path| std::env::split_paths(&path).collect())
            .unwrap_or_default();
        Ok(Self::new(home, vars, path))
    }
    pub fn new(home: PathBuf, vars: BTreeMap<String, String>, path: Vec<PathBuf>) -> Self {
        let config = vars
            .get("XDG_CONFIG_HOME")
            .map(PathBuf::from)
            .filter(|path| path.is_absolute())
            .unwrap_or_else(|| home.join(".config"));
        Self {
            home,
            config,
            vars,
            path,
        }
    }
    fn var(&self, name: &str) -> Option<&str> {
        self.vars
            .get(name)
            .map(|value| value.trim())
            .filter(|value| !value.is_empty())
    }
    /// The first file of that name on PATH.
    pub fn program(&self, name: &str) -> Option<PathBuf> {
        self.path
            .iter()
            .map(|directory| directory.join(name))
            .find(|path| path.is_file())
    }
    fn any_program(&self, names: &[&str]) -> Option<PathBuf> {
        names.iter().find_map(|name| self.program(name))
    }
    fn expand(&self, raw: &str) -> PathBuf {
        if raw == "~" {
            self.home.clone()
        } else if let Some(rest) = raw.strip_prefix("~/") {
            self.home.join(rest)
        } else {
            PathBuf::from(raw)
        }
    }
    /// Hermes' home: `HERMES_HOME` when it is set and absolute, else
    /// `~/.hermes` with `HERMES_DATA_DIR_SUFFIX` (hermes_constants.py).
    pub fn hermes_home(&self) -> PathBuf {
        if let Some(raw) = self.var("HERMES_HOME") {
            let path = self.expand(raw);
            if path.is_absolute() {
                return path;
            }
        }
        self.home.join(format!(
            ".hermes{}",
            self.var("HERMES_DATA_DIR_SUFFIX").unwrap_or("")
        ))
    }
    /// omp's configuration root: `~/<PI_CONFIG_DIR>`, `.omp` by default
    /// (docs/environment-variables.md).
    pub fn omp_root(&self) -> PathBuf {
        self.home.join(self.var("PI_CONFIG_DIR").unwrap_or(".omp"))
    }
    /// omp's agent directory, where its user-level mcp.json and skills are:
    /// the active profile's, `PI_CODING_AGENT_DIR` for the default profile,
    /// else `<root>/agent` (docs/mcp-config.md, Profiles).
    pub fn omp_agent(&self) -> PathBuf {
        let profile = match self.vars.get("OMP_PROFILE") {
            // OMP_PROFILE wins over PI_PROFILE even when it is empty.
            Some(value) => Some(value.trim()).filter(|value| !value.is_empty()),
            None => self.var("PI_PROFILE"),
        };
        if let Some(name) = profile {
            return self.omp_root().join("profiles").join(name).join("agent");
        }
        self.var("PI_CODING_AGENT_DIR")
            .map(PathBuf::from)
            .filter(|path| path.is_absolute())
            .unwrap_or_else(|| self.omp_root().join("agent"))
    }
}

/// The OpenCode configuration layout: 1 lists servers directly under `mcp`
/// with `enabled`; 2 lists them under `mcp.servers` with `disabled`.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Layout {
    OpenCodeV1,
    OpenCodeV2,
}
impl Layout {
    pub fn parse(text: &str) -> Option<Self> {
        match text {
            "v1" | "1" => Some(Self::OpenCodeV1),
            "v2" | "2" => Some(Self::OpenCodeV2),
            _ => None,
        }
    }
    pub fn name(self) -> &'static str {
        match self {
            Self::OpenCodeV1 => "v1",
            Self::OpenCodeV2 => "v2",
        }
    }
}

/// What setup registers: the absolute program, the tool list the person
/// chose (None keeps a client's default and an existing entry's choice) and
/// a forced OpenCode layout.
#[derive(Debug, Clone)]
pub struct Options {
    pub program: PathBuf,
    pub tools: Option<Mode>,
    pub opencode: Option<Layout>,
}
impl Options {
    fn tools_for(&self, id: ClientId) -> Option<Mode> {
        self.tools.or_else(|| id.default_tools())
    }
}

/// The key path to the server map and the entry's style.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Shape {
    pub path: &'static [&'static str],
    pub style: Style,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Style {
    /// `{"command": ..., "args": ["mcp"]}`: Gemini CLI and Cursor.
    Plain,
    VsCode,
    Zed,
    OpenCodeV1,
    OpenCodeV2,
    /// An array of `{"id", "label", "enabled", "transport", ...}`.
    GrokCli,
    Omp,
}

#[derive(Debug, Clone)]
pub enum Kind {
    /// The client's own command adds, reports and removes the server; its
    /// files are not read. `get`, `add` and `remove` are the arguments after
    /// the program; `add` gets `-- PROGRAM mcp` appended.
    Cli {
        get: Vec<String>,
        add: Vec<String>,
        remove: Vec<String>,
    },
    Json(Shape),
    /// Hermes' config.yaml, edited as text within the `mcp_servers` block.
    Yaml,
}

/// A client as found in one environment.
#[derive(Debug, Clone)]
pub struct Resolved {
    pub id: ClientId,
    /// Its configuration directory exists or one of its programs is on PATH.
    pub found: bool,
    pub program: Option<PathBuf>,
    pub file: PathBuf,
    pub kind: Kind,
    pub layout: Option<Layout>,
    /// The client's own command that adds the server while keeping its file's
    /// comments, preferred over rewriting the file when it is there.
    pub writer: Option<Vec<String>>,
    pub notes: Vec<String>,
}

#[derive(Debug, Clone, PartialEq)]
pub enum State {
    /// Neither its directory nor its program is here.
    Absent,
    NotConnected,
    Connected,
    /// Connected, and the person switched it off in the client; left alone.
    Disabled,
    /// Tomacco's entry points elsewhere or asks for another tool list.
    Outdated(String),
    /// Something that is not Tomacco has the name; left alone.
    Conflict(String),
    /// The file is not one to rewrite; the person adds the snippet.
    Manual(String),
    /// Which layout the client reads could not be told.
    Unknown(String),
    Added,
    Updated,
    Removed,
    Declined,
    Failed(String),
}
impl State {
    pub fn kind(&self) -> &'static str {
        match self {
            Self::Absent => "absent",
            Self::NotConnected => "not_connected",
            Self::Connected => "connected",
            Self::Disabled => "disabled",
            Self::Outdated(_) => "outdated",
            Self::Conflict(_) => "conflict",
            Self::Manual(_) => "manual",
            Self::Unknown(_) => "unknown",
            Self::Added => "added",
            Self::Updated => "updated",
            Self::Removed => "removed",
            Self::Declined => "declined",
            Self::Failed(_) => "failed",
        }
    }
    pub fn detail(&self) -> Option<&str> {
        match self {
            Self::Outdated(text)
            | Self::Conflict(text)
            | Self::Manual(text)
            | Self::Unknown(text)
            | Self::Failed(text) => Some(text),
            _ => None,
        }
    }
    /// Whether the client uses Tomacco as things stand.
    pub fn connected(&self) -> bool {
        matches!(self, Self::Connected | Self::Added | Self::Updated)
    }
}

#[derive(Debug, Clone, PartialEq)]
pub enum SkillState {
    /// The agent's directory or the installed skill is not here.
    Absent,
    Linked,
    NotLinked,
    Added,
    Removed,
    Declined,
    /// Something else has the skill's name there; it stays.
    Taken(PathBuf),
    Failed(String),
}
impl SkillState {
    pub fn kind(&self) -> &'static str {
        match self {
            Self::Absent => "absent",
            Self::Linked => "linked",
            Self::NotLinked => "not_linked",
            Self::Added => "added",
            Self::Removed => "removed",
            Self::Declined => "declined",
            Self::Taken(_) => "taken",
            Self::Failed(_) => "failed",
        }
    }
}

/// Where a client reads skills, and whether setup may make the directory.
#[derive(Debug, Clone)]
pub struct SkillTarget {
    pub label: String,
    pub directory: PathBuf,
    pub create: bool,
}

/// One client's status, before or after a change.
#[derive(Debug, Clone)]
pub struct Report {
    pub id: ClientId,
    pub found: bool,
    pub state: State,
    pub file: Option<PathBuf>,
    pub layout: Option<Layout>,
    pub tools: Option<Mode>,
    /// The entry to add by hand, as the client's file wants it.
    pub snippet: String,
    /// The client's own command that adds the server, when it has one.
    pub command: Option<String>,
    pub backup: Option<PathBuf>,
    pub notes: Vec<String>,
}
impl Report {
    pub fn value(&self) -> Value {
        json!({
            "id": self.id.id(),
            "name": self.id.name(),
            "upstream": self.id.upstream(),
            "found": self.found,
            "state": self.state.kind(),
            "detail": self.state.detail(),
            "connected": self.state.connected(),
            "file": self.file.as_ref().map(|path| path.to_string_lossy().into_owned()),
            "layout": self.layout.map(Layout::name),
            "tools": self.tools.map(Mode::name),
            "tool_name_example": self.id.tool_name("device.list"),
            "snippet": self.snippet,
            "command": self.command,
            "backup": self.backup.as_ref().map(|path| path.to_string_lossy().into_owned()),
            "notes": self.notes,
        })
    }
}

/// Finds the client in the environment: its directory, file and the way
/// its file is written.
pub fn resolve(id: ClientId, env: &Environment, options: &Options) -> Resolved {
    let program = env.any_program(id.programs());
    let simple = |directory: PathBuf, file: &str, kind: Kind| Resolved {
        id,
        found: directory.is_dir() || program.is_some(),
        program: program.clone(),
        file: directory.join(file),
        kind,
        layout: None,
        writer: None,
        notes: Vec::new(),
    };
    let shape = |path, style| Kind::Json(Shape { path, style });
    match id {
        ClientId::ClaudeCode => simple(
            env.home.join(".claude"),
            "",
            Kind::Cli {
                get: argv(&["mcp", "get", SERVER]),
                add: argv(&[
                    "mcp",
                    "add",
                    "--transport",
                    "stdio",
                    "--scope",
                    "user",
                    SERVER,
                ]),
                remove: argv(&["mcp", "remove", "--scope", "user", SERVER]),
            },
        ),
        ClientId::Codex => simple(
            env.home.join(".codex"),
            "",
            Kind::Cli {
                get: argv(&["mcp", "get", SERVER]),
                add: argv(&["mcp", "add", SERVER]),
                remove: argv(&["mcp", "remove", SERVER]),
            },
        ),
        ClientId::GeminiCli => simple(
            env.home.join(".gemini"),
            "settings.json",
            shape(&["mcpServers"], Style::Plain),
        ),
        ClientId::Cursor => simple(
            env.home.join(".cursor"),
            "mcp.json",
            shape(&["mcpServers"], Style::Plain),
        ),
        ClientId::VsCode => simple(
            env.config.join("Code/User"),
            "mcp.json",
            shape(&["servers"], Style::VsCode),
        ),
        ClientId::Zed => simple(
            env.config.join("zed"),
            "settings.json",
            shape(&["context_servers"], Style::Zed),
        ),
        ClientId::OpenCode => resolve_opencode(env, options, program),
        ClientId::GrokCli => {
            let mut resolved = simple(
                env.home.join(".grok"),
                "user-settings.json",
                shape(&["mcp", "servers"], Style::GrokCli),
            );
            resolved.notes.push(
                "Grok CLI reads MCP servers from ~/.grok/user-settings.json only; its project .grok/settings.json has no server list (src/utils/settings.ts).".to_string(),
            );
            resolved
        }
        ClientId::Hermes => {
            let home = env.hermes_home();
            let mut resolved = simple(home.clone(), "config.yaml", Kind::Yaml);
            if let Ok(active) = std::fs::read_to_string(home.join("active_profile")) {
                let active = active.trim();
                if !active.is_empty() && active != "default" && env.var("HERMES_HOME").is_none() {
                    resolved.notes.push(format!(
                        "Hermes' active profile is {active:?}; this registers in the default profile. Set HERMES_HOME to {} to register there.",
                        home.join("profiles").join(active).display()
                    ));
                }
            }
            resolved
        }
        ClientId::Omp => {
            let agent = env.omp_agent();
            let mut resolved = simple(agent, "mcp.json", shape(&["mcpServers"], Style::Omp));
            // The agent directory is made with the profile; the root says omp is here.
            resolved.found = resolved.found || env.omp_root().is_dir();
            resolved
        }
    }
}

fn resolve_opencode(env: &Environment, options: &Options, program: Option<PathBuf>) -> Resolved {
    let directory = env.config.join("opencode");
    // packages/cli/src/commands/handlers/mcp/add.ts: the first of these that
    // exists is the global file, else the first is made.
    let candidates = [
        "opencode.json",
        "opencode.jsonc",
        ".opencode/opencode.json",
        ".opencode/opencode.jsonc",
    ];
    let file = candidates
        .iter()
        .map(|name| directory.join(name))
        .find(|path| path.is_file())
        .unwrap_or_else(|| directory.join(candidates[0]));
    let mut notes = Vec::new();
    let mut layout = options.opencode;
    if layout.is_none() {
        if let Some(program) = &program {
            match opencode_version(program) {
                Some(version) => {
                    layout = Some(if version.0 >= 2 {
                        Layout::OpenCodeV2
                    } else {
                        Layout::OpenCodeV1
                    });
                    notes.push(format!(
                        "opencode --version says {}.{}.{}",
                        version.0, version.1, version.2
                    ));
                }
                None => notes.push("opencode --version gave no version".to_string()),
            }
        }
    }
    if layout.is_none() {
        layout = layout_of_file(&file);
    }
    let style = match layout {
        Some(Layout::OpenCodeV1) => Style::OpenCodeV1,
        // Unknown is written as the current layout only when the person says so.
        _ => Style::OpenCodeV2,
    };
    let shape = Shape {
        path: match style {
            Style::OpenCodeV1 => &["mcp"],
            _ => &["mcp", "servers"],
        },
        style,
    };
    // OpenCode 2's own command edits JSON and JSONC alike (jsonc-parser), so it
    // is used when it is there; it writes mcp.servers.
    let writer = match (&program, layout) {
        (Some(program), Some(Layout::OpenCodeV2)) => {
            let mut args = vec![
                program.to_string_lossy().into_owned(),
                "mcp".to_string(),
                "add".to_string(),
                "--global".to_string(),
            ];
            if let Some(tools) = options.tools_for(ClientId::OpenCode) {
                args.push("--env".to_string());
                args.push(format!("{TOOLS_VARIABLE}={}", tools.name()));
            }
            args.push(SERVER.to_string());
            Some(args)
        }
        _ => None,
    };
    Resolved {
        id: ClientId::OpenCode,
        found: directory.is_dir() || program.is_some(),
        program,
        file,
        kind: Kind::Json(shape),
        layout,
        writer,
        notes,
    }
}

/// The layout a file shows: servers with a `type` directly under `mcp` are
/// version 1, a `mcp.servers` object version 2; a file without either says
/// nothing.
fn layout_of_file(file: &Path) -> Option<Layout> {
    let text = std::fs::read_to_string(file).ok()?;
    let value: Value = serde_json::from_str(&text)
        .or_else(|_| serde_json::from_str(&without_comments(&text)))
        .ok()?;
    let mcp = value.get("mcp")?.as_object()?;
    if mcp
        .values()
        .any(|server| server.get("type").is_some_and(Value::is_string))
    {
        return Some(Layout::OpenCodeV1);
    }
    if mcp.get("servers").is_some_and(Value::is_object) {
        return Some(Layout::OpenCodeV2);
    }
    None
}

/// `opencode --version` as (major, minor, patch): the first version-like
/// word it prints. It prints and exits; no agent starts.
fn opencode_version(program: &Path) -> Option<(u64, u64, u64)> {
    let output = run_quietly(program, &["--version"], std::time::Duration::from_secs(20))?;
    output
        .split(|c: char| c.is_whitespace() || c == 'v')
        .find_map(|word| {
            let mut parts = word.trim().split('.');
            let major = parts.next()?.parse().ok()?;
            let minor = parts.next().unwrap_or("0").parse().ok()?;
            let patch = parts
                .next()
                .unwrap_or("0")
                .chars()
                .take_while(char::is_ascii_digit)
                .collect::<String>()
                .parse()
                .unwrap_or(0);
            Some((major, minor, patch))
        })
}

/// Runs a program with closed stdin and returns its standard output when it
/// exits in time with success; a program that hangs is killed.
fn run_quietly(program: &Path, args: &[&str], timeout: std::time::Duration) -> Option<String> {
    let mut child = Command::new(program)
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .ok()?;
    let mut stdout = child.stdout.take()?;
    let reader = std::thread::spawn(move || {
        let mut text = String::new();
        let _ = std::io::Read::read_to_string(&mut stdout, &mut text);
        text
    });
    let started = std::time::Instant::now();
    loop {
        match child.try_wait() {
            Ok(Some(status)) => {
                let text = reader.join().unwrap_or_default();
                return status.success().then_some(text);
            }
            Ok(None) if started.elapsed() < timeout => {
                std::thread::sleep(std::time::Duration::from_millis(50));
            }
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                return None;
            }
        }
    }
}

/// Where the client looks for skills, given the clients that were found:
/// each one's own directory, made only where the client is known to be here.
pub fn skill_targets(env: &Environment, found: &dyn Fn(ClientId) -> bool) -> Vec<SkillTarget> {
    let mut targets = vec![
        SkillTarget {
            label: "Claude Code".to_string(),
            directory: env.home.join(".claude"),
            create: false,
        },
        SkillTarget {
            label: "Codex".to_string(),
            directory: env.home.join(".codex"),
            create: false,
        },
        // Grok CLI reads ~/.agents/skills (src/utils/skills.ts), as do other
        // agents; the directory is made for Grok CLI, which has no other.
        SkillTarget {
            label: if found(ClientId::GrokCli) {
                "Grok CLI and other agents that read ~/.agents".to_string()
            } else {
                "Agents that read ~/.agents".to_string()
            },
            directory: env.home.join(".agents"),
            create: found(ClientId::GrokCli),
        },
    ];
    if found(ClientId::OpenCode) {
        // ~/.config/opencode/skills, its global directory (docs/skills).
        targets.push(SkillTarget {
            label: "OpenCode".to_string(),
            directory: env.config.join("opencode"),
            create: false,
        });
    }
    if found(ClientId::Hermes) {
        // ~/.hermes/skills, walked with os.scandir, which follows links.
        targets.push(SkillTarget {
            label: "Hermes Agent".to_string(),
            directory: env.hermes_home(),
            create: false,
        });
    }
    if found(ClientId::Omp) {
        // <agent>/skills, its native provider; links are accepted (helpers.ts).
        targets.push(SkillTarget {
            label: "omp".to_string(),
            directory: env.omp_agent(),
            create: true,
        });
    }
    targets
}

/// The installed skill, linked as `<directory>/skills/tomacco`.
fn installed_skill(home: &Path) -> PathBuf {
    home.join(product::INSTALLED_LIBRARY)
        .join("skills")
        .join(product::REPO_NAME)
}

fn skill_link_is_ours(link: &Path, skill: &Path) -> Option<bool> {
    match std::fs::symlink_metadata(link) {
        Ok(meta) => Some(
            meta.is_symlink()
                && (std::fs::read_link(link).is_ok_and(|target| target == skill)
                    || std::fs::canonicalize(link).ok() == std::fs::canonicalize(skill).ok()),
        ),
        Err(_) => None,
    }
}

/// The skill's state in one directory, without changing anything.
pub fn skill_state(home: &Path, target: &SkillTarget) -> SkillState {
    let skill = installed_skill(home);
    if (!target.directory.is_dir() && !target.create) || !skill.join("SKILL.md").is_file() {
        return SkillState::Absent;
    }
    let link = target.directory.join("skills").join(product::REPO_NAME);
    match skill_link_is_ours(&link, &skill) {
        Some(true) => SkillState::Linked,
        Some(false) => SkillState::Taken(link),
        None => SkillState::NotLinked,
    }
}

/// Links the installed agent skill into an agent's skills directory after
/// the person's yes, as install.py does. An entry of that name that is not
/// a link to the installed skill is left alone.
pub fn link_skill(
    home: &Path,
    target: &SkillTarget,
    ask: &mut dyn FnMut(&str) -> bool,
) -> Result<SkillState> {
    let state = skill_state(home, target);
    if state != SkillState::NotLinked {
        return Ok(state);
    }
    let skill = installed_skill(home);
    let skills = target.directory.join("skills");
    let link = skills.join(product::REPO_NAME);
    if !ask(&format!(
        "Add the {} skill to {}? This links {} to {}.",
        product::PRODUCT_NAME,
        target.label,
        link.display(),
        skill.display()
    )) {
        return Ok(SkillState::Declined);
    }
    std::fs::create_dir_all(&skills)?;
    std::os::unix::fs::symlink(&skill, &link)?;
    Ok(SkillState::Added)
}

/// Removes the link to the installed skill from an agent's skills directory;
/// anything else of that name stays.
pub fn unlink_skill(home: &Path, target: &SkillTarget) -> Result<SkillState> {
    let skill = installed_skill(home);
    let link = target.directory.join("skills").join(product::REPO_NAME);
    match skill_link_is_ours(&link, &skill) {
        Some(true) => {
            std::fs::remove_file(&link)?;
            Ok(SkillState::Removed)
        }
        Some(false) => Ok(SkillState::Taken(link)),
        None => Ok(SkillState::Absent),
    }
}

/// What a client's file says about its entry.
enum Found {
    Missing,
    /// The entry, as Tomacco's or another's, enabled or not.
    Entry(Owner),
    Malformed(String),
}
#[derive(Debug, Clone, PartialEq)]
pub enum Owner {
    Theirs(String),
    Ours {
        program: PathBuf,
        enabled: bool,
        tools: Option<String>,
    },
}

/// The command and arguments an entry runs, in the client's style.
fn command_of(style: Style, entry: &Value) -> Option<(String, Vec<String>)> {
    match style {
        Style::OpenCodeV1 | Style::OpenCodeV2 => {
            let command = strings(&entry["command"]);
            let (first, rest) = command.split_first()?;
            Some((first.clone(), rest.to_vec()))
        }
        _ => Some((
            entry["command"].as_str()?.to_string(),
            strings(&entry["args"]),
        )),
    }
}

fn env_key(style: Style) -> &'static str {
    match style {
        Style::OpenCodeV1 | Style::OpenCodeV2 => "environment",
        _ => "env",
    }
}

/// Whether the entry is Tomacco's: a `tomaccoctl` that serves `mcp`.
fn owner_of(style: Style, entry: &Value) -> Owner {
    let Some((command, args)) = command_of(style, entry) else {
        return Owner::Theirs("has no command".to_string());
    };
    let program = PathBuf::from(&command);
    let ours = program
        .file_name()
        .is_some_and(|name| name == product::CLI_NAME)
        && args.first().is_some_and(|first| first == "mcp");
    if !ours {
        return Owner::Theirs(format!("runs {command}"));
    }
    let enabled = match style {
        Style::OpenCodeV2 => entry["disabled"] != true,
        _ => entry["enabled"] != false && entry["disabled"] != true,
    };
    let tools = entry[env_key(style)][TOOLS_VARIABLE]
        .as_str()
        .map(str::to_string);
    Owner::Ours {
        program,
        enabled,
        tools,
    }
}

/// The object that holds the entries, or where it would be.
fn container(root: &Value, shape: Shape) -> std::result::Result<Option<&Value>, String> {
    let mut value = root;
    for (index, key) in shape.path.iter().enumerate() {
        let Some(object) = value.as_object() else {
            return Err(format!(
                "{} is not an object",
                shape.path[..index].join(".")
            ));
        };
        match object.get(*key) {
            Some(next) => value = next,
            None => return Ok(None),
        }
    }
    Ok(Some(value))
}

fn find(root: &Value, shape: Shape) -> Found {
    let holder = match container(root, shape) {
        Ok(Some(holder)) => holder,
        Ok(None) => return Found::Missing,
        Err(reason) => return Found::Malformed(reason),
    };
    let entry = match (shape.style, holder) {
        (Style::GrokCli, Value::Array(servers)) => servers.iter().find(|server| {
            server["id"] == SERVER || server["label"].as_str().is_some_and(|l| slug(l) == SERVER)
        }),
        (Style::GrokCli, _) => {
            return Found::Malformed(format!("{} is not an array", shape.path.join(".")));
        }
        (_, Value::Object(servers)) => servers.get(SERVER),
        _ => return Found::Malformed(format!("{} is not an object", shape.path.join("."))),
    };
    match entry {
        None => Found::Missing,
        Some(entry) if !entry.is_object() => {
            Found::Malformed(format!("the {SERVER} entry is not an object"))
        }
        Some(entry) => Found::Entry(owner_of(shape.style, entry)),
    }
}

/// Grok CLI's server id for a label (src/mcp/validate.ts).
fn slug(label: &str) -> String {
    let mut out = String::new();
    for c in label.trim().to_lowercase().chars() {
        if c.is_ascii_alphanumeric() {
            out.push(c);
        } else if !out.ends_with('-') {
            out.push('-');
        }
    }
    out.trim_matches('-').to_string()
}

/// Tomacco's entry in the client's style, built on an existing entry of
/// Tomacco's when there is one so its other keys stay.
fn entry(style: Style, options: &Options, tools: Option<Mode>, existing: Option<&Value>) -> Value {
    let program = options.program.to_string_lossy().into_owned();
    let mut object = existing
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let fresh = existing.is_none();
    let set = |object: &mut Map<String, Value>, key: &str, value: Value| {
        object.insert(key.to_string(), value);
    };
    match style {
        Style::Plain => {
            set(&mut object, "command", json!(program));
            set(&mut object, "args", json!(["mcp"]));
        }
        Style::VsCode => {
            set(&mut object, "type", json!("stdio"));
            set(&mut object, "command", json!(program));
            set(&mut object, "args", json!(["mcp"]));
        }
        Style::Zed => {
            set(&mut object, "source", json!("custom"));
            set(&mut object, "command", json!(program));
            set(&mut object, "args", json!(["mcp"]));
            if fresh {
                set(&mut object, "env", json!({}));
            }
        }
        Style::OpenCodeV1 => {
            set(&mut object, "type", json!("local"));
            set(&mut object, "command", json!([program, "mcp"]));
            if fresh {
                set(&mut object, "enabled", json!(true));
            }
        }
        Style::OpenCodeV2 => {
            set(&mut object, "type", json!("local"));
            set(&mut object, "command", json!([program, "mcp"]));
        }
        Style::GrokCli => {
            if fresh {
                set(&mut object, "id", json!(SERVER));
                set(&mut object, "label", json!(product::PRODUCT_NAME));
                set(&mut object, "enabled", json!(true));
            }
            set(&mut object, "transport", json!("stdio"));
            set(&mut object, "command", json!(program));
            set(&mut object, "args", json!(["mcp"]));
        }
        Style::Omp => {
            set(&mut object, "type", json!("stdio"));
            set(&mut object, "command", json!(program));
            set(&mut object, "args", json!(["mcp"]));
        }
    }
    if let Some(tools) = tools {
        let key = env_key(style);
        if !object.get(key).is_some_and(Value::is_object) {
            object.insert(key.to_string(), json!({}));
        }
        object[key][TOOLS_VARIABLE] = json!(tools.name());
    }
    Value::Object(object)
}

/// Puts the entry where the client reads it, making the objects on the way.
fn put(root: &mut Value, shape: Shape, entry: Value) -> std::result::Result<(), String> {
    let mut value = root;
    for key in shape.path {
        let object = value
            .as_object_mut()
            .ok_or_else(|| format!("{} is not an object", shape.path.join(".")))?;
        let holder = object.entry(key.to_string()).or_insert_with(|| {
            if shape.style == Style::GrokCli && *key == "servers" {
                json!([])
            } else {
                json!({})
            }
        });
        value = holder;
    }
    match (shape.style, value) {
        (Style::GrokCli, Value::Array(servers)) => {
            match servers.iter_mut().find(|server| server["id"] == SERVER) {
                Some(slot) => *slot = entry,
                None => servers.push(entry),
            }
            Ok(())
        }
        (Style::GrokCli, _) => Err("mcp.servers is not an array".to_string()),
        (_, Value::Object(servers)) => {
            servers.insert(SERVER.to_string(), entry);
            Ok(())
        }
        _ => Err(format!("{} is not an object", shape.path.join("."))),
    }
}

fn take(root: &mut Value, shape: Shape) -> bool {
    let mut value = root;
    for key in shape.path {
        match value.get_mut(*key) {
            Some(next) => value = next,
            None => return false,
        }
    }
    match (shape.style, value) {
        (Style::GrokCli, Value::Array(servers)) => {
            let before = servers.len();
            servers.retain(|server| server["id"] != SERVER);
            servers.len() != before
        }
        (_, Value::Object(servers)) => servers.remove(SERVER).is_some(),
        _ => false,
    }
}

/// The current entry's object, for building on it.
fn existing_entry(root: &Value, shape: Shape) -> Option<&Value> {
    let holder = container(root, shape).ok().flatten()?;
    match (shape.style, holder) {
        (Style::GrokCli, Value::Array(servers)) => servers.iter().find(|s| s["id"] == SERVER),
        (_, Value::Object(servers)) => servers.get(SERVER),
        _ => None,
    }
}

/// JSON with `//` and `/* */` comments and trailing commas removed, enough
/// to read a JSONC file. Writing one is left to the client's own command.
pub fn without_comments(text: &str) -> String {
    let chars: Vec<char> = text.chars().collect();
    let mut out = String::with_capacity(text.len());
    let mut i = 0;
    while i < chars.len() {
        let c = chars[i];
        if c == '"' {
            out.push(c);
            i += 1;
            while i < chars.len() {
                out.push(chars[i]);
                if chars[i] == '\\' && i + 1 < chars.len() {
                    out.push(chars[i + 1]);
                    i += 2;
                    continue;
                }
                if chars[i] == '"' {
                    i += 1;
                    break;
                }
                i += 1;
            }
            continue;
        }
        if c == '/' && chars.get(i + 1) == Some(&'/') {
            while i < chars.len() && chars[i] != '\n' {
                i += 1;
            }
            continue;
        }
        if c == '/' && chars.get(i + 1) == Some(&'*') {
            i += 2;
            while i + 1 < chars.len() && !(chars[i] == '*' && chars[i + 1] == '/') {
                i += 1;
            }
            i += 2;
            continue;
        }
        if c == ',' {
            let mut j = i + 1;
            while j < chars.len() && chars[j].is_whitespace() {
                j += 1;
            }
            if matches!(chars.get(j), Some('}') | Some(']')) {
                i += 1;
                continue;
            }
        }
        out.push(c);
        i += 1;
    }
    out
}

/// A client's JSON file as read: missing, an object, or something setup
/// does not rewrite (with the object when it could still be read).
enum JsonFile {
    Missing,
    Object {
        value: Value,
        mode: std::fs::Permissions,
    },
    /// Readable as JSONC, so its status is known, but not rewritten.
    ReadOnly {
        value: Value,
        reason: String,
    },
    Problem(String),
}

fn read_json(file: &Path) -> JsonFile {
    let shown = file.display();
    let meta = match std::fs::symlink_metadata(file) {
        Ok(meta) => meta,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return JsonFile::Missing,
        Err(error) => return JsonFile::Problem(format!("{shown}: {error}")),
    };
    if !meta.is_file() {
        return JsonFile::Problem(format!("{shown} is a link or not a regular file"));
    }
    let text = match std::fs::read_to_string(file) {
        Ok(text) => text,
        Err(error) => return JsonFile::Problem(format!("{shown}: {error}")),
    };
    match serde_json::from_str::<Value>(&text) {
        Ok(value) if value.is_object() => JsonFile::Object {
            value,
            mode: meta.permissions(),
        },
        Ok(_) => JsonFile::Problem(format!("{shown} is not a JSON object")),
        Err(_) => match serde_json::from_str::<Value>(&without_comments(&text)) {
            Ok(value) if value.is_object() => JsonFile::ReadOnly {
                value,
                reason: format!("{shown} has comments or trailing commas; not rewritten"),
            },
            _ => JsonFile::Problem(format!("{shown} is not JSON")),
        },
    }
}

/// The state an owner means against what setup would write.
fn state_of(owner: &Owner, options: &Options, tools: Option<Mode>) -> State {
    match owner {
        Owner::Theirs(reason) => State::Conflict(format!("the {SERVER} entry {reason}")),
        Owner::Ours {
            program,
            enabled,
            tools: current,
        } => {
            if !enabled {
                return State::Disabled;
            }
            if program != &options.program {
                return State::Outdated(format!("points at {}", program.display()));
            }
            // The person's own choice of tool list stays unless --tools says otherwise.
            if let (Some(wanted), true) = (options.tools, options.tools.is_some()) {
                if current.as_deref() != Some(wanted.name()) {
                    return State::Outdated(format!(
                        "uses the {} tool list",
                        current.as_deref().unwrap_or("full")
                    ));
                }
            }
            let _ = tools;
            State::Connected
        }
    }
}

/// Where the file's copy goes before a change: one directory per client.
fn backup(env: &Environment, id: ClientId, file: &Path) -> Result<Option<PathBuf>> {
    if !file.is_file() {
        return Ok(None);
    }
    let directory = env.home.join(BACKUPS).join(id.id());
    std::fs::create_dir_all(&directory)?;
    let stamp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0);
    let name = file
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_else(|| "file".to_string());
    let mut copy = directory.join(format!("{stamp}-{name}"));
    let mut serial = 1;
    while copy.exists() {
        copy = directory.join(format!("{stamp}-{serial}-{name}"));
        serial += 1;
    }
    std::fs::copy(file, &copy)?;
    // The oldest copies go once there are more than kept.
    let mut copies: Vec<PathBuf> = std::fs::read_dir(&directory)?
        .filter_map(|entry| entry.ok().map(|e| e.path()))
        .filter(|path| path.is_file())
        .collect();
    copies.sort();
    while copies.len() > KEPT_BACKUPS {
        let _ = std::fs::remove_file(copies.remove(0));
    }
    Ok(Some(copy))
}

/// Writes the text to a temporary file beside the target, flushes it and
/// moves it over the target, keeping the target's permissions (0600 for a
/// new file).
fn write_atomically(file: &Path, text: &[u8], mode: Option<std::fs::Permissions>) -> Result<()> {
    let directory = file
        .parent()
        .ok_or_else(|| Error::new("setup_failed", "the file has no directory"))?;
    std::fs::create_dir_all(directory)?;
    let mut temporary = tempfile::NamedTempFile::new_in(directory)?;
    temporary.write_all(text)?;
    temporary.as_file().sync_all()?;
    let permissions = mode.unwrap_or_else(|| {
        use std::os::unix::fs::PermissionsExt;
        std::fs::Permissions::from_mode(0o600)
    });
    std::fs::set_permissions(temporary.path(), permissions)?;
    temporary
        .persist(file)
        .map_err(|error| Error::from(error.error))?;
    Ok(())
}

/// Backs the file up, writes the new text, reads it back and checks it; a
/// failed check puts the backup back (or removes a file that was new) and
/// is an error.
fn commit(
    env: &Environment,
    id: ClientId,
    file: &Path,
    text: &[u8],
    mode: Option<std::fs::Permissions>,
    check: &dyn Fn(&str) -> std::result::Result<(), String>,
) -> Result<Option<PathBuf>> {
    let copy = backup(env, id, file)?;
    write_atomically(file, text, mode.clone())?;
    let written = std::fs::read_to_string(file).map_err(Error::from);
    let verdict = match &written {
        Ok(written) => check(written),
        Err(error) => Err(error.message.clone()),
    };
    if let Err(reason) = verdict {
        match &copy {
            Some(copy) => {
                let previous = std::fs::read(copy)?;
                write_atomically(file, &previous, mode)?;
            }
            None => {
                let _ = std::fs::remove_file(file);
            }
        }
        return Err(Error::new(
            "setup_failed",
            format!(
                "{} did not read back as written ({reason}); restored",
                file.display()
            ),
        ));
    }
    Ok(copy)
}

fn pretty(value: &Value) -> Vec<u8> {
    let mut bytes = serde_json::to_vec_pretty(value).unwrap_or_default();
    bytes.push(b'\n');
    bytes
}

/// The entry to add by hand: the client's own command where it has one, else
/// the JSON or YAML as its file wants it.
pub fn snippet(resolved: &Resolved, options: &Options) -> String {
    let tools = options.tools_for(resolved.id);
    let program = options.program.to_string_lossy();
    match &resolved.kind {
        Kind::Cli { add, .. } => {
            let mut words: Vec<String> = vec![resolved.id.programs()[0].to_string()];
            words.extend(add.iter().cloned());
            words.push("--".to_string());
            words.push(shell_quote(&program));
            words.push("mcp".to_string());
            words.join(" ")
        }
        Kind::Json(shape) => {
            let value = entry(shape.style, options, tools, None);
            let mut wrapped = match shape.style {
                Style::GrokCli => json!([value]),
                _ => json!({ SERVER: value }),
            };
            for key in shape.path.iter().rev() {
                wrapped = json!({ *key: wrapped });
            }
            let text = serde_json::to_string_pretty(&wrapped).unwrap_or_default();
            // Without the outer braces: what goes inside the file's object.
            let inner: Vec<&str> = text.lines().skip(1).collect();
            let inner = &inner[..inner.len().saturating_sub(1)];
            inner
                .iter()
                .map(|line| line.strip_prefix("  ").unwrap_or(line))
                .collect::<Vec<_>>()
                .join("\n")
        }
        Kind::Yaml => yaml::block(&program, tools, 2, 4),
    }
}

/// The client's own command that adds the server, for the person to run.
pub fn upstream_command(resolved: &Resolved, options: &Options) -> Option<String> {
    let program = shell_quote(&options.program.to_string_lossy());
    match resolved.id {
        ClientId::ClaudeCode | ClientId::Codex => Some(snippet(resolved, options)),
        ClientId::OpenCode => Some(format!(
            "opencode mcp add --global {SERVER} -- {program} mcp   # OpenCode 2; version 1 reads the JSON below"
        )),
        ClientId::Hermes => Some(format!(
            "hermes mcp add {SERVER} --command {program} --args mcp   # asks about tools; starts tomaccoctl once"
        )),
        _ => None,
    }
}

fn report(resolved: &Resolved, options: &Options, state: State) -> Report {
    Report {
        id: resolved.id,
        found: resolved.found,
        state,
        file: match resolved.kind {
            Kind::Cli { .. } => None,
            _ => Some(resolved.file.clone()),
        },
        layout: resolved.layout,
        tools: options.tools_for(resolved.id),
        snippet: snippet(resolved, options),
        command: upstream_command(resolved, options),
        backup: None,
        notes: resolved.notes.clone(),
    }
}

fn quiet(program: &Path, args: &[String]) -> bool {
    Command::new(program)
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .is_ok_and(|status| status.success())
}

/// The client's state, changing nothing.
pub fn inspect(resolved: &Resolved, options: &Options) -> Report {
    let state = if !resolved.found {
        State::Absent
    } else {
        match &resolved.kind {
            Kind::Cli { get, .. } => match &resolved.program {
                Some(program) if quiet(program, get) => State::Connected,
                Some(_) => State::NotConnected,
                None => State::NotConnected,
            },
            Kind::Json(shape) => json_state(resolved, *shape, options),
            Kind::Yaml => yaml_state(resolved, options),
        }
    };
    let mut report = report(resolved, options, state);
    if let Kind::Cli { .. } = resolved.kind {
        if resolved.program.is_none() {
            report.notes.push(format!(
                "{} is not on PATH; its directory is here",
                resolved.id.programs()[0]
            ));
        }
    }
    report
}

fn json_state(resolved: &Resolved, shape: Shape, options: &Options) -> State {
    let tools = options.tools_for(resolved.id);
    let (value, read_only) = match read_json(&resolved.file) {
        JsonFile::Missing => {
            return match (resolved.id, resolved.layout) {
                (ClientId::OpenCode, None) => State::Unknown(unknown_layout(resolved)),
                _ => State::NotConnected,
            };
        }
        JsonFile::Object { value, .. } => (value, None),
        JsonFile::ReadOnly { value, reason } => (value, Some(reason)),
        JsonFile::Problem(reason) => return State::Manual(reason),
    };
    let found = if resolved.id == ClientId::OpenCode {
        opencode_find(&value, shape)
    } else {
        find(&value, shape)
    };
    match found {
        Found::Malformed(reason) => {
            State::Manual(format!("{} in {}", reason, resolved.file.display()))
        }
        Found::Entry(owner) => match state_of(&owner, options, tools) {
            State::Connected if shape.style == Style::Omp && omp_disabled(&value) => {
                State::Disabled
            }
            state => state,
        },
        Found::Missing => match (resolved.id, resolved.layout, read_only, &resolved.writer) {
            (ClientId::OpenCode, None, _, _) => State::Unknown(unknown_layout(resolved)),
            (_, _, Some(reason), None) => State::Manual(reason),
            _ => State::NotConnected,
        },
    }
}

/// OpenCode's entry in either layout: the one the client reads first, then
/// the other with a note that it is the older layout.
fn opencode_find(value: &Value, shape: Shape) -> Found {
    match find(value, shape) {
        Found::Missing => {
            let other = Shape {
                path: match shape.style {
                    Style::OpenCodeV1 => &["mcp", "servers"],
                    _ => &["mcp"],
                },
                style: match shape.style {
                    Style::OpenCodeV1 => Style::OpenCodeV2,
                    _ => Style::OpenCodeV1,
                },
            };
            match find(value, other) {
                Found::Entry(Owner::Ours { program, .. }) => Found::Entry(Owner::Theirs(format!(
                    "is in the {} layout ({}); OpenCode {} reads {}",
                    other_layout_name(other.style),
                    program.display(),
                    if shape.style == Style::OpenCodeV1 {
                        "1"
                    } else {
                        "2"
                    },
                    shape.path.join(".")
                ))),
                _ => Found::Missing,
            }
        }
        found => found,
    }
}
fn other_layout_name(style: Style) -> &'static str {
    match style {
        Style::OpenCodeV1 => "v1",
        _ => "v2",
    }
}

fn unknown_layout(resolved: &Resolved) -> String {
    format!(
        "OpenCode's version is not known: opencode is not on PATH and {} names no layout. Run setup with --opencode-config v1 or v2, or add the entry yourself",
        resolved.file.display()
    )
}

fn omp_disabled(root: &Value) -> bool {
    strings(&root["disabledServers"])
        .iter()
        .any(|name| name == SERVER)
}

fn yaml_state(resolved: &Resolved, options: &Options) -> State {
    let text = match std::fs::symlink_metadata(&resolved.file) {
        Ok(meta) if !meta.is_file() => {
            return State::Manual(format!(
                "{} is a link or not a regular file",
                resolved.file.display()
            ));
        }
        Ok(_) => match std::fs::read_to_string(&resolved.file) {
            Ok(text) => text,
            Err(error) => return State::Manual(format!("{}: {error}", resolved.file.display())),
        },
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return State::NotConnected,
        Err(error) => return State::Manual(format!("{}: {error}", resolved.file.display())),
    };
    match yaml::inspect(&text) {
        Err(reason) => State::Manual(format!("{}: {reason}", resolved.file.display())),
        Ok(view) => match view.tomacco() {
            None => State::NotConnected,
            Some(entry) => state_of(&entry.owner(), options, options.tools_for(resolved.id)),
        },
    }
}

/// Connects the client after the person's yes: adds the entry, or brings
/// Tomacco's own entry up to date. A disabled or conflicting entry and a
/// file setup does not rewrite are reported and left as they are.
pub fn connect(
    env: &Environment,
    resolved: &Resolved,
    options: &Options,
    ask: &mut dyn FnMut(&str) -> bool,
) -> Report {
    let mut report = inspect(resolved, options);
    let question = match &report.state {
        State::NotConnected => format!(
            "Connect {} to {}? This adds {SERVER} to {}.",
            resolved.id.name(),
            product::PRODUCT_NAME,
            match &resolved.kind {
                Kind::Cli { .. } => "its settings".to_string(),
                _ => resolved.file.display().to_string(),
            }
        ),
        State::Outdated(what) => format!(
            "Update {}'s {SERVER} entry in {}? It {what}.",
            resolved.id.name(),
            resolved.file.display()
        ),
        _ => return report,
    };
    if !ask(&question) {
        report.state = State::Declined;
        return report;
    }
    let updating = matches!(report.state, State::Outdated(_));
    let outcome = match &resolved.kind {
        Kind::Cli { add, .. } => connect_cli(resolved, options, add),
        Kind::Json(shape) => match &resolved.writer {
            Some(writer) if !updating => connect_with_writer(resolved, options, writer),
            _ => connect_json(env, resolved, *shape, options, updating),
        },
        Kind::Yaml => connect_yaml(env, resolved, options, updating),
    };
    match outcome {
        Ok(copy) => {
            report.backup = copy;
            report.state = if updating {
                State::Updated
            } else {
                State::Added
            };
        }
        Err(error) => report.state = State::Failed(error.message),
    }
    report
}

fn connect_cli(resolved: &Resolved, options: &Options, add: &[String]) -> Result<Option<PathBuf>> {
    let program = resolved.program.as_ref().ok_or_else(|| {
        Error::new(
            "setup_failed",
            format!("{} is not on PATH", resolved.id.programs()[0]),
        )
    })?;
    let mut args = add.to_vec();
    args.push("--".to_string());
    args.push(options.program.to_string_lossy().into_owned());
    args.push("mcp".to_string());
    if quiet(program, &args) {
        Ok(None)
    } else {
        Err(Error::new(
            "setup_failed",
            format!(
                "run `{} {}` yourself",
                resolved.id.programs()[0],
                args.iter()
                    .map(|a| shell_quote(a))
                    .collect::<Vec<_>>()
                    .join(" ")
            ),
        ))
    }
}

/// OpenCode 2's `mcp add --global`: it writes mcp.servers itself and keeps
/// the file's comments; the file is read back to be sure.
fn connect_with_writer(
    resolved: &Resolved,
    options: &Options,
    writer: &[String],
) -> Result<Option<PathBuf>> {
    let (program, args) = writer
        .split_first()
        .ok_or_else(|| Error::new("setup_failed", "no writer command"))?;
    let mut args = args.to_vec();
    args.push("--".to_string());
    args.push(options.program.to_string_lossy().into_owned());
    args.push("mcp".to_string());
    if !quiet(Path::new(program), &args) {
        return Err(Error::new(
            "setup_failed",
            format!(
                "run `{}` yourself",
                std::iter::once(program)
                    .chain(args.iter())
                    .map(|a| shell_quote(a))
                    .collect::<Vec<_>>()
                    .join(" ")
            ),
        ));
    }
    match inspect(resolved, options).state {
        State::Connected => Ok(None),
        other => Err(Error::new(
            "setup_failed",
            format!(
                "opencode mcp add ran, but {} then shows {}{}",
                resolved.file.display(),
                other.kind(),
                other.detail().map(|d| format!(": {d}")).unwrap_or_default()
            ),
        )),
    }
}

fn connect_json(
    env: &Environment,
    resolved: &Resolved,
    shape: Shape,
    options: &Options,
    updating: bool,
) -> Result<Option<PathBuf>> {
    let (mut value, mode) = match read_json(&resolved.file) {
        JsonFile::Missing => (json!({}), None),
        JsonFile::Object { value, mode } => (value, Some(mode)),
        JsonFile::ReadOnly { reason, .. } | JsonFile::Problem(reason) => {
            return Err(Error::new("setup_failed", reason));
        }
    };
    let tools = options.tools_for(resolved.id);
    let existing = if updating {
        existing_entry(&value, shape).cloned()
    } else {
        None
    };
    let new_entry = entry(shape.style, options, tools, existing.as_ref());
    if shape.style == Style::Omp && mode.is_none() {
        value["$schema"] = json!(
            "https://raw.githubusercontent.com/can1357/oh-my-pi/main/packages/coding-agent/src/config/mcp-schema.json"
        );
    }
    put(&mut value, shape, new_entry).map_err(|reason| Error::new("setup_failed", reason))?;
    let wanted = options.clone();
    let check = move |text: &str| -> std::result::Result<(), String> {
        let written: Value = serde_json::from_str(text).map_err(|e| e.to_string())?;
        match find(&written, shape) {
            Found::Entry(owner) => match state_of(&owner, &wanted, tools) {
                State::Connected | State::Disabled => Ok(()),
                other => Err(other.kind().to_string()),
            },
            Found::Missing => Err("entry missing".to_string()),
            Found::Malformed(reason) => Err(reason),
        }
    };
    commit(
        env,
        resolved.id,
        &resolved.file,
        &pretty(&value),
        mode,
        &check,
    )
}

fn connect_yaml(
    env: &Environment,
    resolved: &Resolved,
    options: &Options,
    updating: bool,
) -> Result<Option<PathBuf>> {
    let (text, mode) = match std::fs::metadata(&resolved.file) {
        Ok(meta) => (
            std::fs::read_to_string(&resolved.file)?,
            Some(meta.permissions()),
        ),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => (String::new(), None),
        Err(error) => return Err(error.into()),
    };
    let program = options.program.to_string_lossy().into_owned();
    let tools = options.tools_for(resolved.id);
    let edited = if updating {
        yaml::update(&text, &program, tools)
    } else {
        yaml::insert(&text, &program, tools)
    }
    .map_err(|reason| {
        Error::new(
            "setup_failed",
            format!("{}: {reason}", resolved.file.display()),
        )
    })?;
    let wanted = options.clone();
    let check = move |written: &str| -> std::result::Result<(), String> {
        let view = yaml::inspect(written)?;
        match view.tomacco() {
            Some(entry) => match state_of(&entry.owner(), &wanted, tools) {
                State::Connected | State::Disabled => Ok(()),
                other => Err(other.kind().to_string()),
            },
            None => Err("entry missing".to_string()),
        }
    };
    commit(
        env,
        resolved.id,
        &resolved.file,
        edited.as_bytes(),
        mode,
        &check,
    )
}

/// Removes Tomacco's own entry after the person's yes. An entry that is not
/// Tomacco's, and a file setup does not rewrite, stay.
pub fn remove(
    env: &Environment,
    resolved: &Resolved,
    options: &Options,
    ask: &mut dyn FnMut(&str) -> bool,
) -> Report {
    let mut report = inspect(resolved, options);
    match &report.state {
        State::Connected | State::Outdated(_) | State::Disabled => {}
        _ => return report,
    }
    // A file with comments is read for its state and left to the person.
    if let Kind::Json(_) = resolved.kind {
        if let JsonFile::ReadOnly { reason, .. } = read_json(&resolved.file) {
            report.state = State::Manual(format!("{reason}; remove the {SERVER} entry yourself"));
            return report;
        }
    }
    if !ask(&format!(
        "Remove {SERVER} from {}? Everything else there stays.",
        match &resolved.kind {
            Kind::Cli { .. } => resolved.id.name().to_string(),
            _ => resolved.file.display().to_string(),
        }
    )) {
        report.state = State::Declined;
        return report;
    }
    let outcome: Result<Option<PathBuf>> = match &resolved.kind {
        Kind::Cli { remove, .. } => match &resolved.program {
            Some(program) if quiet(program, remove) => Ok(None),
            _ => Err(Error::new(
                "setup_failed",
                format!(
                    "run `{} {}` yourself",
                    resolved.id.programs()[0],
                    remove.join(" ")
                ),
            )),
        },
        Kind::Json(shape) => remove_json(env, resolved, *shape),
        Kind::Yaml => remove_yaml(env, resolved),
    };
    match outcome {
        Ok(copy) => {
            report.backup = copy;
            report.state = State::Removed;
        }
        Err(error) => report.state = State::Failed(error.message),
    }
    report
}

fn remove_json(env: &Environment, resolved: &Resolved, shape: Shape) -> Result<Option<PathBuf>> {
    let (mut value, mode) = match read_json(&resolved.file) {
        JsonFile::Object { value, mode } => (value, Some(mode)),
        JsonFile::Missing => return Ok(None),
        JsonFile::ReadOnly { reason, .. } | JsonFile::Problem(reason) => {
            return Err(Error::new("setup_failed", reason));
        }
    };
    if !take(&mut value, shape) {
        return Ok(None);
    }
    let check = |text: &str| -> std::result::Result<(), String> {
        let written: Value = serde_json::from_str(text).map_err(|e| e.to_string())?;
        match find(&written, shape) {
            Found::Missing => Ok(()),
            Found::Entry(_) => Err("entry still there".to_string()),
            Found::Malformed(reason) => Err(reason),
        }
    };
    commit(
        env,
        resolved.id,
        &resolved.file,
        &pretty(&value),
        mode,
        &check,
    )
}

fn remove_yaml(env: &Environment, resolved: &Resolved) -> Result<Option<PathBuf>> {
    let meta = match std::fs::metadata(&resolved.file) {
        Ok(meta) => meta,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error.into()),
    };
    let text = std::fs::read_to_string(&resolved.file)?;
    let edited = yaml::remove(&text).map_err(|reason| {
        Error::new(
            "setup_failed",
            format!("{}: {reason}", resolved.file.display()),
        )
    })?;
    let check = |written: &str| -> std::result::Result<(), String> {
        match yaml::inspect(written)?.tomacco() {
            None => Ok(()),
            Some(_) => Err("entry still there".to_string()),
        }
    };
    commit(
        env,
        resolved.id,
        &resolved.file,
        edited.as_bytes(),
        Some(meta.permissions()),
        &check,
    )
}

/// Hermes' config.yaml as text. Nothing here parses YAML in general: the
/// file is left alone unless its `mcp_servers` block is a plain block mapping
/// setup can add one entry to or take its own entry from, and every other
/// line stays byte for byte. Anchors, aliases, merge keys, tags, tabs,
/// document markers, flow mappings and duplicate names make it the person's
/// to edit, with the block to paste.
pub mod yaml {
    use super::{Owner, SERVER, TOOLS_VARIABLE};
    use crate::mcp_tools::Mode;
    use std::path::PathBuf;

    #[derive(Debug, Clone, PartialEq)]
    pub struct Entry {
        pub name: String,
        /// Line indexes: the key line, and one past its last line.
        pub start: usize,
        pub end: usize,
        pub command: Option<String>,
        /// None when there is no args line or it is not a flow sequence.
        pub args: Option<Vec<String>>,
        pub enabled: Option<bool>,
        pub tools: Option<String>,
        /// Something inside the entry setup does not read.
        pub opaque: Option<String>,
    }
    impl Entry {
        pub fn owner(&self) -> Owner {
            let Some(command) = &self.command else {
                return Owner::Theirs("has no command".to_string());
            };
            let program = PathBuf::from(command);
            let ours = program
                .file_name()
                .is_some_and(|name| name == crate::product::CLI_NAME)
                && self
                    .args
                    .as_ref()
                    .and_then(|args| args.first())
                    .is_some_and(|first| first == "mcp");
            if !ours {
                return Owner::Theirs(format!("runs {command}"));
            }
            Owner::Ours {
                program,
                enabled: self.enabled != Some(false),
                tools: self.tools.clone(),
            }
        }
    }

    #[derive(Debug, Clone, PartialEq)]
    pub struct Block {
        /// The `mcp_servers:` line, and one past the last line of the block.
        pub start: usize,
        pub end: usize,
        pub indent: usize,
        /// The indentation of an entry's own keys, from the first entry that has one.
        pub field_indent: Option<usize>,
        pub entries: Vec<Entry>,
    }

    #[derive(Debug, Clone, PartialEq)]
    pub struct View {
        pub lines: Vec<String>,
        pub newline: &'static str,
        pub trailing_newline: bool,
        pub block: Option<Block>,
    }
    impl View {
        pub fn tomacco(&self) -> Option<&Entry> {
            self.block
                .as_ref()?
                .entries
                .iter()
                .find(|entry| entry.name == SERVER)
        }
        fn text(&self) -> String {
            let mut text = self.lines.join(self.newline);
            if self.trailing_newline && !self.lines.is_empty() {
                text.push_str(self.newline);
            }
            text
        }
    }

    fn indent_of(line: &str) -> usize {
        line.chars().take_while(|c| *c == ' ').count()
    }
    fn is_blank_or_comment(line: &str) -> bool {
        let trimmed = line.trim_start();
        trimmed.is_empty() || trimmed.starts_with('#')
    }
    /// A `key: value` line: the key (quotes stripped) and the raw value.
    fn key_value(line: &str) -> Option<(String, String)> {
        let trimmed = line.trim_start();
        if trimmed.starts_with('-') || trimmed.starts_with('#') {
            return None;
        }
        let (key, value) = match trimmed.chars().next() {
            Some(quote @ ('"' | '\'')) => {
                let rest = &trimmed[1..];
                let close = rest.find(quote)?;
                let key = &rest[..close];
                let after = rest[close + 1..].trim_start();
                (key.to_string(), after.strip_prefix(':')?)
            }
            _ => {
                let colon = trimmed.find(':')?;
                let key = &trimmed[..colon];
                if key.contains(' ') || key.is_empty() {
                    return None;
                }
                (key.to_string(), &trimmed[colon + 1..])
            }
        };
        if !value.is_empty() && !value.starts_with(' ') {
            return None;
        }
        Some((key, value.trim().to_string()))
    }
    /// A value with its trailing comment removed, unless it is quoted.
    fn scalar(raw: &str) -> Result<String, String> {
        let raw = raw.trim();
        if let Some(rest) = raw.strip_prefix('"') {
            let mut out = String::new();
            let mut chars = rest.chars();
            while let Some(c) = chars.next() {
                match c {
                    '\\' => match chars.next() {
                        Some('n') => out.push('\n'),
                        Some('t') => out.push('\t'),
                        Some('"') => out.push('"'),
                        Some('\\') => out.push('\\'),
                        Some('/') => out.push('/'),
                        Some(other) => return Err(format!("escape \\{other} is not read")),
                        None => return Err("unterminated escape".to_string()),
                    },
                    '"' => return Ok(out),
                    other => out.push(other),
                }
            }
            return Err("unterminated string".to_string());
        }
        if let Some(rest) = raw.strip_prefix('\'') {
            let mut out = String::new();
            let mut chars = rest.chars().peekable();
            while let Some(c) = chars.next() {
                if c == '\'' {
                    if chars.peek() == Some(&'\'') {
                        chars.next();
                        out.push('\'');
                        continue;
                    }
                    return Ok(out);
                }
                out.push(c);
            }
            return Err("unterminated string".to_string());
        }
        if raw.starts_with(['&', '*', '!', '{', '[', '|', '>']) {
            return Err(format!("value {raw:?} is not a plain scalar"));
        }
        let plain = match raw.find(" #") {
            Some(at) => &raw[..at],
            None => raw,
        };
        Ok(plain.trim().to_string())
    }
    /// A flow sequence of scalars: `["mcp"]`, `[mcp, x]`.
    fn sequence(raw: &str) -> Result<Vec<String>, String> {
        let raw = raw.trim();
        let inner = raw
            .strip_prefix('[')
            .and_then(|rest| {
                let close = rest.rfind(']')?;
                Some(&rest[..close])
            })
            .ok_or_else(|| format!("args {raw:?} is not a flow sequence"))?;
        let mut items = Vec::new();
        let mut current = String::new();
        let mut quote: Option<char> = None;
        for c in inner.chars() {
            match quote {
                Some(q) => {
                    current.push(c);
                    if c == q {
                        quote = None;
                    }
                }
                None if c == ',' => {
                    items.push(std::mem::take(&mut current));
                }
                None => {
                    if (c == '"' || c == '\'') && current.trim().is_empty() {
                        quote = Some(c);
                    }
                    current.push(c);
                }
            }
        }
        if !current.trim().is_empty() {
            items.push(current);
        }
        items.iter().map(|item| scalar(item)).collect()
    }

    pub fn inspect(text: &str) -> Result<View, String> {
        let newline = if text.contains("\r\n") { "\r\n" } else { "\n" };
        let trailing_newline = text.ends_with('\n') || text.is_empty();
        let mut lines: Vec<String> = text.split(newline).map(str::to_string).collect();
        if trailing_newline && lines.last().is_some_and(String::is_empty) {
            lines.pop();
        }
        for (number, line) in lines.iter().enumerate() {
            let number = number + 1;
            if line.starts_with("---") || line.starts_with("...") {
                return Err(format!("line {number} is a document marker"));
            }
            if line.starts_with("%") {
                return Err(format!("line {number} is a directive"));
            }
            if line.trim_start_matches(' ').starts_with('\t') {
                return Err(format!("line {number} is indented with a tab"));
            }
        }
        if let Some(first) = lines.iter().find(|line| !is_blank_or_comment(line)) {
            if first.starts_with(['{', '[', '-']) {
                return Err("the document is not a block mapping".to_string());
            }
        }
        let keys: Vec<usize> = lines
            .iter()
            .enumerate()
            .filter(|(_, line)| {
                indent_of(line) == 0 && key_value(line).is_some_and(|(key, _)| key == "mcp_servers")
            })
            .map(|(index, _)| index)
            .collect();
        if keys.len() > 1 {
            return Err("mcp_servers appears more than once".to_string());
        }
        let Some(&start) = keys.first() else {
            return Ok(View {
                lines,
                newline,
                trailing_newline,
                block: None,
            });
        };
        let (_, value) = key_value(&lines[start]).unwrap_or_default();
        if value.starts_with(['&', '*', '!']) {
            return Err("mcp_servers has an anchor, alias or tag".to_string());
        }
        if !value.is_empty() && !value.starts_with('#') {
            return Err("mcp_servers has an inline value".to_string());
        }
        // The block: every following line indented or blank or a comment, up
        // to the next line at column 0.
        let mut end = start + 1;
        while end < lines.len() && (is_blank_or_comment(&lines[end]) || indent_of(&lines[end]) > 0)
        {
            end += 1;
        }
        while end > start + 1 && lines[end - 1].trim().is_empty() {
            end -= 1;
        }
        let body: Vec<usize> = (start + 1..end)
            .filter(|index| !is_blank_or_comment(&lines[*index]))
            .collect();
        for index in &body {
            let line = &lines[*index];
            if line.contains("<<:") || line.trim_start().starts_with(['&', '*', '!']) {
                return Err(format!(
                    "line {} has an anchor, alias, merge key or tag",
                    index + 1
                ));
            }
            if let Some((_, value)) = key_value(line) {
                if value.starts_with(['&', '*', '!']) {
                    return Err(format!("line {} has an anchor, alias or tag", index + 1));
                }
            }
        }
        let indent = body
            .first()
            .map(|index| indent_of(&lines[*index]))
            .unwrap_or(2);
        let mut entries: Vec<Entry> = Vec::new();
        let mut field_indent = None;
        for index in &body {
            let line = &lines[*index];
            let this = indent_of(line);
            if this == indent {
                let (name, value) =
                    key_value(line).ok_or_else(|| format!("line {} is not an entry", index + 1))?;
                if entries.iter().any(|entry| entry.name == name) {
                    return Err(format!("{name} appears twice under mcp_servers"));
                }
                entries.push(Entry {
                    name,
                    start: *index,
                    end: *index + 1,
                    command: None,
                    args: None,
                    enabled: None,
                    tools: None,
                    opaque: (!value.is_empty() && !value.starts_with('#'))
                        .then(|| "an inline value".to_string()),
                });
            } else if this < indent {
                return Err(format!("line {} is indented irregularly", index + 1));
            } else {
                let Some(entry) = entries.last_mut() else {
                    return Err(format!("line {} is indented irregularly", index + 1));
                };
                entry.end = *index + 1;
                let own = *field_indent.get_or_insert(this);
                if this == own {
                    match key_value(line) {
                        Some((key, value)) => match key.as_str() {
                            "command" => entry.command = scalar(&value).ok(),
                            "args" => entry.args = sequence(&value).ok(),
                            "enabled" => {
                                entry.enabled = match scalar(&value).ok().as_deref() {
                                    Some("true") => Some(true),
                                    Some("false") => Some(false),
                                    _ => None,
                                }
                            }
                            _ => {}
                        },
                        None if entry.name == SERVER => {
                            entry.opaque = Some(format!("line {}", index + 1));
                        }
                        None => {}
                    }
                } else if this > own {
                    // A nested value, such as env or tools; only env's tool list is read.
                    if let Some((key, value)) = key_value(line) {
                        if key == TOOLS_VARIABLE {
                            entry.tools = scalar(&value).ok();
                        }
                    }
                } else {
                    return Err(format!("line {} is indented irregularly", index + 1));
                }
            }
        }
        Ok(View {
            lines,
            newline,
            trailing_newline,
            block: Some(Block {
                start,
                end,
                indent,
                field_indent,
                entries,
            }),
        })
    }

    /// A double-quoted YAML scalar.
    pub fn quote(text: &str) -> String {
        let mut out = String::from("\"");
        for c in text.chars() {
            match c {
                '"' => out.push_str("\\\""),
                '\\' => out.push_str("\\\\"),
                '\n' => out.push_str("\\n"),
                '\t' => out.push_str("\\t"),
                c if c.is_control() => out.push_str(&format!("\\u{:04x}", c as u32)),
                c => out.push(c),
            }
        }
        out.push('"');
        out
    }

    /// Tomacco's entry lines, without the `mcp_servers:` line.
    fn entry_lines(program: &str, tools: Option<Mode>, indent: usize, field: usize) -> Vec<String> {
        let pad = " ".repeat(indent);
        let inner = " ".repeat(field);
        let mut lines = vec![
            format!("{pad}{SERVER}:"),
            format!("{inner}command: {}", quote(program)),
            format!("{inner}args: [\"mcp\"]"),
        ];
        if let Some(tools) = tools {
            let deeper = " ".repeat(field + (field - indent).max(1));
            lines.push(format!("{inner}env:"));
            lines.push(format!("{deeper}{TOOLS_VARIABLE}: \"{}\"", tools.name()));
        }
        lines
    }

    /// The whole block to paste, `mcp_servers:` included.
    pub fn block(program: &str, tools: Option<Mode>, indent: usize, field: usize) -> String {
        let mut lines = vec!["mcp_servers:".to_string()];
        lines.extend(entry_lines(program, tools, indent, field));
        lines.join("\n")
    }

    /// The text with Tomacco's entry added to the block, or a block added at
    /// the end.
    pub fn insert(text: &str, program: &str, tools: Option<Mode>) -> Result<String, String> {
        let mut view = inspect(text)?;
        if view.tomacco().is_some() {
            return Err(format!("{SERVER} is already there"));
        }
        match view.block.clone() {
            Some(block) => {
                let field = block
                    .field_indent
                    .unwrap_or(block.indent * 2)
                    .max(block.indent + 1);
                let lines = entry_lines(program, tools, block.indent, field);
                let at = block.end;
                for (offset, line) in lines.into_iter().enumerate() {
                    view.lines.insert(at + offset, line);
                }
            }
            None => {
                if !view.lines.is_empty() && !view.lines.last().is_some_and(|l| l.trim().is_empty())
                {
                    view.lines.push(String::new());
                }
                view.lines.push("mcp_servers:".to_string());
                view.lines.extend(entry_lines(program, tools, 2, 4));
                view.trailing_newline = true;
            }
        }
        Ok(view.text())
    }

    /// The text with the `command:` and `args:` lines of Tomacco's entry set
    /// to the program; its other lines stay.
    pub fn update(text: &str, program: &str, tools: Option<Mode>) -> Result<String, String> {
        let mut view = inspect(text)?;
        let entry = view
            .tomacco()
            .cloned()
            .ok_or_else(|| format!("{SERVER} is not there"))?;
        let field = view
            .block
            .as_ref()
            .and_then(|block| block.field_indent)
            .ok_or_else(|| "the entry has no keys".to_string())?;
        if let Some(opaque) = &entry.opaque {
            return Err(format!(
                "the {SERVER} entry has {opaque} setup does not read"
            ));
        }
        if entry.command.is_none() || entry.args.is_none() {
            return Err(format!(
                "the {SERVER} entry's command or args is not a plain scalar and a flow sequence"
            ));
        }
        let inner = " ".repeat(field);
        let mut command_set = false;
        let mut args_set = false;
        for index in entry.start + 1..entry.end {
            if indent_of(&view.lines[index]) != field {
                continue;
            }
            match key_value(&view.lines[index]).map(|(key, _)| key) {
                Some(key) if key == "command" => {
                    view.lines[index] = format!("{inner}command: {}", quote(program));
                    command_set = true;
                }
                Some(key) if key == "args" => {
                    view.lines[index] = format!("{inner}args: [\"mcp\"]");
                    args_set = true;
                }
                _ => {}
            }
        }
        if !command_set || !args_set {
            return Err(format!(
                "the {SERVER} entry has no command and args lines to change"
            ));
        }
        if let Some(tools) = tools {
            if entry.tools.as_deref() != Some(tools.name()) {
                return Err(format!(
                    "set env {TOOLS_VARIABLE}: {} in the {SERVER} entry yourself",
                    tools.name()
                ));
            }
        }
        Ok(view.text())
    }

    /// The text without Tomacco's entry; the `mcp_servers:` line goes too
    /// when nothing else is in the block.
    pub fn remove(text: &str) -> Result<String, String> {
        let mut view = inspect(text)?;
        let Some(block) = view.block.clone() else {
            return Ok(view.text());
        };
        let Some(entry) = view.tomacco().cloned() else {
            return Ok(view.text());
        };
        view.lines.drain(entry.start..entry.end);
        let others = block.entries.iter().filter(|e| e.name != SERVER).count();
        if others == 0 {
            let remaining = (block.start + 1..block.end - (entry.end - entry.start))
                .filter(|index| !view.lines[*index].trim().is_empty())
                .count();
            if remaining == 0 {
                let end = block.end - (entry.end - entry.start);
                view.lines.drain(block.start..end);
            }
        }
        Ok(view.text())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    fn env(home: &Path) -> Environment {
        Environment::new(home.to_path_buf(), BTreeMap::new(), Vec::new())
    }
    fn options(program: &str) -> Options {
        Options {
            program: PathBuf::from(program),
            tools: None,
            opencode: None,
        }
    }
    fn yes() -> impl FnMut(&str) -> bool {
        |_| true
    }
    fn no() -> impl FnMut(&str) -> bool {
        |_| false
    }
    fn read(path: &Path) -> Value {
        serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap()
    }

    #[test]
    fn each_client_has_a_distinct_id_name_and_upstream() {
        let ids: std::collections::BTreeSet<_> = CLIENTS.iter().map(|c| c.id()).collect();
        assert_eq!(ids.len(), CLIENTS.len());
        for client in CLIENTS {
            assert_eq!(ClientId::parse(client.id()), Some(client));
            assert!(client.upstream().starts_with("https://"));
            assert!(!client.programs().is_empty());
        }
        assert_eq!(ClientId::parse("grok"), None);
    }

    #[test]
    fn tool_names_follow_each_clients_rule_and_stay_distinct() {
        assert_eq!(
            ClientId::OpenCode.tool_name("device.list").unwrap(),
            "tomacco_device_list"
        );
        assert_eq!(
            ClientId::GrokCli.tool_name("device.list").unwrap(),
            "mcp_tomacco__device.list"
        );
        assert_eq!(
            ClientId::Hermes.tool_name("device.list").unwrap(),
            "mcp__tomacco__device_list"
        );
        assert_eq!(
            ClientId::Omp.tool_name("android.avd_start").unwrap(),
            "mcp__tomacco_android_avd_start"
        );
        assert_eq!(ClientId::ClaudeCode.tool_name("device.list"), None);
        // Hermes clamps a long name with a hash; omp refuses to name it.
        let long = "a".repeat(80);
        let clamped = ClientId::Hermes.tool_name(&long).unwrap();
        assert_eq!(clamped.len(), 64);
        assert!(clamped.starts_with("mcp__tomacco__aaaa"));
        assert_eq!(ClientId::Omp.tool_name(&long), None);
        // Every operation keeps a distinct name under every rule.
        for client in [
            ClientId::OpenCode,
            ClientId::GrokCli,
            ClientId::Hermes,
            ClientId::Omp,
        ] {
            let names: Vec<String> = crate::api::names()
                .map(|name| client.tool_name(name).unwrap())
                .collect();
            let distinct: std::collections::BTreeSet<&String> = names.iter().collect();
            assert_eq!(distinct.len(), names.len(), "{}", client.name());
            for name in &names {
                assert!(name.len() <= 64, "{} {name}", client.name());
                if client != ClientId::GrokCli {
                    assert!(
                        name.chars()
                            .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-'),
                        "{} {name}",
                        client.name()
                    );
                }
            }
        }
        // The grouped list's names are plain under every rule, Grok CLI's too.
        for tool in crate::mcp_tools::list(Mode::Grouped).as_array().unwrap() {
            let name = ClientId::GrokCli.tool_name(s(tool, "name")).unwrap();
            assert!(
                name.chars().all(|c| c.is_ascii_alphanumeric() || c == '_'),
                "{name}"
            );
        }
    }

    #[test]
    fn the_environment_places_each_clients_files() {
        let home = Path::new("/home/ada");
        let plain = env(home);
        assert_eq!(plain.config, home.join(".config"));
        assert_eq!(plain.hermes_home(), home.join(".hermes"));
        assert_eq!(plain.omp_agent(), home.join(".omp/agent"));
        let vars: BTreeMap<String, String> = [
            ("XDG_CONFIG_HOME", "/cfg"),
            ("HERMES_HOME", "~/hermes home"),
            ("PI_CONFIG_DIR", ".pi"),
            ("OMP_PROFILE", "work"),
            ("PI_CODING_AGENT_DIR", "/elsewhere"),
        ]
        .iter()
        .map(|(k, v)| (k.to_string(), v.to_string()))
        .collect();
        let moved = Environment::new(home.to_path_buf(), vars.clone(), Vec::new());
        assert_eq!(moved.config, PathBuf::from("/cfg"));
        assert_eq!(moved.hermes_home(), home.join("hermes home"));
        // A named profile has its own agent directory, whatever PI_CODING_AGENT_DIR says.
        assert_eq!(moved.omp_agent(), home.join(".pi/profiles/work/agent"));
        let mut vars = vars;
        vars.insert("OMP_PROFILE".to_string(), "".to_string());
        vars.insert("PI_PROFILE".to_string(), "old".to_string());
        let empty = Environment::new(home.to_path_buf(), vars.clone(), Vec::new());
        // An empty OMP_PROFILE is the default profile, and PI_PROFILE is not read.
        assert_eq!(empty.omp_agent(), PathBuf::from("/elsewhere"));
        vars.remove("OMP_PROFILE");
        let legacy = Environment::new(home.to_path_buf(), vars.clone(), Vec::new());
        assert_eq!(legacy.omp_agent(), home.join(".pi/profiles/old/agent"));
        vars.insert("HERMES_HOME".to_string(), "relative".to_string());
        vars.insert("HERMES_DATA_DIR_SUFFIX".to_string(), "-dev".to_string());
        let relative = Environment::new(home.to_path_buf(), vars, Vec::new());
        assert_eq!(relative.hermes_home(), home.join(".hermes-dev"));
    }

    #[test]
    fn json_clients_are_connected_once_and_keep_everything_else() {
        let home = tempfile::tempdir().unwrap();
        let env = env(home.path());
        let options = options("/opt/tomacco/tomaccoctl");
        for id in [
            ClientId::GeminiCli,
            ClientId::Cursor,
            ClientId::VsCode,
            ClientId::Zed,
            ClientId::Omp,
            ClientId::GrokCli,
        ] {
            let resolved = resolve(id, &env, &options);
            assert!(!resolved.found, "{}", id.name());
            assert_eq!(inspect(&resolved, &options).state, State::Absent);
            assert_eq!(
                connect(&env, &resolved, &options, &mut yes()).state,
                State::Absent
            );
        }
        // Gemini CLI with its own settings and another server, mode kept.
        let gemini = env.home.join(".gemini");
        std::fs::create_dir_all(&gemini).unwrap();
        let file = gemini.join("settings.json");
        let before = "{\"theme\":\"Dracula\",\"mcpServers\":{\"other\":{\"command\":\"x\"}}}";
        std::fs::write(&file, before).unwrap();
        std::fs::set_permissions(&file, std::fs::Permissions::from_mode(0o640)).unwrap();
        let resolved = resolve(ClientId::GeminiCli, &env, &options);
        assert!(resolved.found);
        assert_eq!(inspect(&resolved, &options).state, State::NotConnected);
        assert_eq!(
            connect(&env, &resolved, &options, &mut no()).state,
            State::Declined
        );
        assert_eq!(std::fs::read_to_string(&file).unwrap(), before);
        let added = connect(&env, &resolved, &options, &mut yes());
        assert_eq!(added.state, State::Added);
        assert!(added.backup.as_ref().unwrap().is_file());
        let written = read(&file);
        assert_eq!(written["theme"], "Dracula");
        assert_eq!(written["mcpServers"]["other"]["command"], "x");
        assert_eq!(
            written["mcpServers"]["tomacco"],
            json!({"command":"/opt/tomacco/tomaccoctl","args":["mcp"]})
        );
        assert_eq!(
            std::fs::metadata(&file).unwrap().permissions().mode() & 0o777,
            0o640
        );
        // Connected already: nothing is asked and the file is byte for byte the same.
        let after = std::fs::read_to_string(&file).unwrap();
        let mut asked = false;
        let again = connect(&env, &resolved, &options, &mut |_| {
            asked = true;
            true
        });
        assert_eq!(again.state, State::Connected);
        assert!(!asked);
        assert_eq!(std::fs::read_to_string(&file).unwrap(), after);
        // Another installation path: offered as an update that keeps the other keys.
        std::fs::write(&file, "{\"mcpServers\":{\"tomacco\":{\"command\":\"/old/tomaccoctl\",\"args\":[\"mcp\"],\"timeout\":9}}}").unwrap();
        assert!(
            matches!(inspect(&resolved, &options).state, State::Outdated(ref what) if what.contains("/old/tomaccoctl"))
        );
        assert_eq!(
            connect(&env, &resolved, &options, &mut yes()).state,
            State::Updated
        );
        let written = read(&file);
        assert_eq!(
            written["mcpServers"]["tomacco"]["command"],
            "/opt/tomacco/tomaccoctl"
        );
        assert_eq!(written["mcpServers"]["tomacco"]["timeout"], 9);
        // The person's own server of that name: a conflict, left alone.
        let theirs = "{\"mcpServers\":{\"tomacco\":{\"command\":\"/usr/bin/other\",\"args\":[]}}}";
        std::fs::write(&file, theirs).unwrap();
        let conflict = connect(&env, &resolved, &options, &mut yes());
        assert!(
            matches!(conflict.state, State::Conflict(ref why) if why.contains("/usr/bin/other"))
        );
        assert_eq!(std::fs::read_to_string(&file).unwrap(), theirs);
        assert_eq!(
            remove(&env, &resolved, &options, &mut yes()).state,
            conflict.state
        );
        assert_eq!(std::fs::read_to_string(&file).unwrap(), theirs);
        // Disabled by the person: reported, not touched, not re-enabled.
        let off = "{\"mcpServers\":{\"tomacco\":{\"command\":\"/opt/tomacco/tomaccoctl\",\"args\":[\"mcp\"],\"disabled\":true}}}";
        std::fs::write(&file, off).unwrap();
        assert_eq!(
            connect(&env, &resolved, &options, &mut yes()).state,
            State::Disabled
        );
        assert_eq!(std::fs::read_to_string(&file).unwrap(), off);
        // Removal takes only the entry.
        std::fs::write(&file, "{\"theme\":\"Dracula\",\"mcpServers\":{\"other\":{\"command\":\"x\"},\"tomacco\":{\"command\":\"/opt/tomacco/tomaccoctl\",\"args\":[\"mcp\"]}}}").unwrap();
        assert_eq!(
            remove(&env, &resolved, &options, &mut yes()).state,
            State::Removed
        );
        assert_eq!(
            read(&file),
            json!({"theme":"Dracula","mcpServers":{"other":{"command":"x"}}})
        );
        assert_eq!(inspect(&resolved, &options).state, State::NotConnected);
        // Backups are kept, the oldest pruned.
        let backups = env.home.join(BACKUPS).join("gemini-cli");
        assert!(std::fs::read_dir(&backups).unwrap().count() >= 3);
    }

    #[test]
    fn a_path_with_spaces_and_unicode_is_written_as_it_is() {
        let home = tempfile::tempdir().unwrap();
        let env = env(home.path());
        let program = home.path().join("Mein Ordner/ünïcode/tomaccoctl");
        let options = options(&program.to_string_lossy());
        std::fs::create_dir_all(env.home.join(".cursor")).unwrap();
        let resolved = resolve(ClientId::Cursor, &env, &options);
        assert_eq!(
            connect(&env, &resolved, &options, &mut yes()).state,
            State::Added
        );
        let written = read(&resolved.file);
        assert_eq!(
            written["mcpServers"]["tomacco"]["command"],
            json!(program.to_string_lossy())
        );
        assert_eq!(inspect(&resolved, &options).state, State::Connected);
        // A new file is private.
        assert_eq!(
            std::fs::metadata(&resolved.file)
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o600
        );
        std::fs::create_dir_all(env.home.join(".hermes")).unwrap();
        let hermes = resolve(ClientId::Hermes, &env, &options);
        assert_eq!(
            connect(&env, &hermes, &options, &mut yes()).state,
            State::Added
        );
        assert_eq!(inspect(&hermes, &options).state, State::Connected);
        let text = std::fs::read_to_string(&hermes.file).unwrap();
        assert!(text.contains("Mein Ordner/ünïcode/tomaccoctl\""), "{text}");
    }

    #[test]
    fn files_setup_does_not_rewrite_are_reported_with_the_snippet() {
        let home = tempfile::tempdir().unwrap();
        let env = env(home.path());
        let options = options("/opt/tomacco/tomaccoctl");
        // Zed's settings usually have comments: read for the status, not rewritten.
        let zed = env.config.join("zed");
        std::fs::create_dir_all(&zed).unwrap();
        let commented = "// Zed settings\n{\"vim_mode\": true, \"context_servers\": {\"tomacco\": {\"command\": \"/opt/tomacco/tomaccoctl\", \"args\": [\"mcp\"],},},}\n";
        std::fs::write(zed.join("settings.json"), commented).unwrap();
        let resolved = resolve(ClientId::Zed, &env, &options);
        assert_eq!(inspect(&resolved, &options).state, State::Connected);
        std::fs::write(
            zed.join("settings.json"),
            "// Zed settings\n{\"vim_mode\": true}\n",
        )
        .unwrap();
        let report = connect(&env, &resolved, &options, &mut yes());
        assert!(matches!(report.state, State::Manual(ref why) if why.contains("comments")));
        assert!(
            report.snippet.contains("\"context_servers\"")
                && report.snippet.contains("\"source\": \"custom\""),
            "{}",
            report.snippet
        );
        assert_eq!(
            std::fs::read_to_string(zed.join("settings.json")).unwrap(),
            "// Zed settings\n{\"vim_mode\": true}\n"
        );
        // A link, as dotfile managers make, and a malformed server list.
        let cursor = env.home.join(".cursor");
        std::fs::create_dir_all(&cursor).unwrap();
        let target = env.home.join("dotfiles-mcp.json");
        std::fs::write(&target, "{}").unwrap();
        std::os::unix::fs::symlink(&target, cursor.join("mcp.json")).unwrap();
        let resolved = resolve(ClientId::Cursor, &env, &options);
        assert!(
            matches!(connect(&env, &resolved, &options, &mut yes()).state, State::Manual(ref why) if why.contains("link"))
        );
        assert_eq!(std::fs::read_to_string(&target).unwrap(), "{}");
        std::fs::remove_file(cursor.join("mcp.json")).unwrap();
        std::fs::write(cursor.join("mcp.json"), "{\"mcpServers\":[]}").unwrap();
        assert!(
            matches!(connect(&env, &resolved, &options, &mut yes()).state, State::Manual(ref why) if why.contains("not an object"))
        );
        std::fs::write(cursor.join("mcp.json"), "not json").unwrap();
        assert!(
            matches!(inspect(&resolved, &options).state, State::Manual(ref why) if why.contains("not JSON"))
        );
    }

    #[test]
    fn opencode_is_written_in_the_layout_its_version_reads() {
        let home = tempfile::tempdir().unwrap();
        let env = env(home.path());
        let directory = env.config.join("opencode");
        std::fs::create_dir_all(&directory).unwrap();
        let mut options = options("/opt/tomacco/tomaccoctl");
        // Without opencode on PATH and without a server in the file, the layout is unknown.
        let resolved = resolve(ClientId::OpenCode, &env, &options);
        assert_eq!(resolved.layout, None);
        assert!(matches!(
            connect(&env, &resolved, &options, &mut yes()).state,
            State::Unknown(_)
        ));
        assert!(!resolved.file.exists());
        // A version 1 file says so, and is extended in its own layout.
        let file = directory.join("opencode.json");
        std::fs::write(
            &file,
            "{\"model\":\"x\",\"mcp\":{\"other\":{\"type\":\"remote\",\"url\":\"https://e\"}}}",
        )
        .unwrap();
        let resolved = resolve(ClientId::OpenCode, &env, &options);
        assert_eq!(resolved.layout, Some(Layout::OpenCodeV1));
        assert_eq!(
            connect(&env, &resolved, &options, &mut yes()).state,
            State::Added
        );
        let written = read(&file);
        assert_eq!(
            written["mcp"]["tomacco"],
            json!({"type":"local","command":["/opt/tomacco/tomaccoctl","mcp"],"enabled":true})
        );
        assert_eq!(written["mcp"]["other"]["url"], "https://e");
        assert_eq!(inspect(&resolved, &options).state, State::Connected);
        // Forced to version 2 while the v1 entry exists: the v1 entry is reported, not touched.
        options.opencode = Some(Layout::OpenCodeV2);
        let forced = resolve(ClientId::OpenCode, &env, &options);
        assert_eq!(forced.layout, Some(Layout::OpenCodeV2));
        let state = inspect(&forced, &options).state;
        assert!(
            matches!(state, State::Conflict(ref why) if why.contains("v1 layout")),
            "{state:?}"
        );
        // A version 2 file: servers under mcp.servers, disabled instead of enabled.
        options.opencode = None;
        std::fs::write(&file, "{\"mcp\":{\"servers\":{\"other\":{\"type\":\"local\",\"command\":[\"x\"],\"disabled\":true}}}}").unwrap();
        let resolved = resolve(ClientId::OpenCode, &env, &options);
        assert_eq!(resolved.layout, Some(Layout::OpenCodeV2));
        assert_eq!(
            connect(&env, &resolved, &options, &mut yes()).state,
            State::Added
        );
        let written = read(&file);
        assert_eq!(
            written["mcp"]["servers"]["tomacco"],
            json!({"type":"local","command":["/opt/tomacco/tomaccoctl","mcp"]})
        );
        assert_eq!(written["mcp"]["servers"]["other"]["disabled"], true);
        // Disabled by the person in version 2.
        std::fs::write(&file, "{\"mcp\":{\"servers\":{\"tomacco\":{\"type\":\"local\",\"command\":[\"/opt/tomacco/tomaccoctl\",\"mcp\"],\"disabled\":true}}}}").unwrap();
        assert_eq!(inspect(&resolved, &options).state, State::Disabled);
        // A JSONC file is read for its state and left to opencode's own command.
        let jsonc = directory.join("opencode.jsonc");
        std::fs::remove_file(&file).unwrap();
        std::fs::write(&jsonc, "{\n  // my settings\n  \"mcp\": {\"servers\": {\"other\": {\"type\": \"local\", \"command\": [\"x\"],},},},\n}\n").unwrap();
        let resolved = resolve(ClientId::OpenCode, &env, &options);
        assert_eq!(resolved.file, jsonc);
        assert_eq!(resolved.layout, Some(Layout::OpenCodeV2));
        let report = connect(&env, &resolved, &options, &mut yes());
        assert!(
            matches!(report.state, State::Manual(ref why) if why.contains("comments")),
            "{:?}",
            report.state
        );
        assert!(
            report
                .command
                .as_deref()
                .unwrap()
                .starts_with("opencode mcp add --global tomacco")
        );
        // The tool list goes into environment.
        options.tools = Some(Mode::Compact);
        std::fs::remove_file(&jsonc).unwrap();
        std::fs::write(&file, "{\"mcp\":{\"servers\":{}}}").unwrap();
        let resolved = resolve(ClientId::OpenCode, &env, &options);
        assert_eq!(
            connect(&env, &resolved, &options, &mut yes()).state,
            State::Added
        );
        assert_eq!(
            read(&file)["mcp"]["servers"]["tomacco"]["environment"][TOOLS_VARIABLE],
            "compact"
        );
        assert_eq!(inspect(&resolved, &options).state, State::Connected);
        options.tools = Some(Mode::Grouped);
        assert!(
            matches!(inspect(&resolved, &options).state, State::Outdated(ref what) if what.contains("compact"))
        );
        assert_eq!(
            connect(&env, &resolved, &options, &mut yes()).state,
            State::Updated
        );
        assert_eq!(
            read(&file)["mcp"]["servers"]["tomacco"]["environment"][TOOLS_VARIABLE],
            "grouped"
        );
        options.tools = None;
        assert_eq!(inspect(&resolved, &options).state, State::Connected);
    }

    #[test]
    fn opencode_uses_its_own_command_when_it_is_version_2_on_path() {
        let home = tempfile::tempdir().unwrap();
        let bin = home.path().join("bin");
        std::fs::create_dir_all(&bin).unwrap();
        let log = home.path().join("calls.log");
        // A stand-in opencode: prints a version, records an add and writes the
        // entry the way OpenCode 2 does.
        let script = format!(
            "#!/bin/sh\nif [ \"$1\" = --version ]; then echo 2.2.5; exit 0; fi\nprintf '%s\\n' \"$*\" >> {log}\nmkdir -p \"$XDG_CONFIG_HOME/opencode\"\ncat > \"$XDG_CONFIG_HOME/opencode/opencode.json\" <<'EOF'\n{{\"mcp\":{{\"servers\":{{\"tomacco\":{{\"type\":\"local\",\"command\":[\"/opt/tomacco/tomaccoctl\",\"mcp\"]}}}}}}}}\nEOF\n",
            log = log.display()
        );
        std::fs::write(bin.join("opencode"), script).unwrap();
        std::fs::set_permissions(bin.join("opencode"), std::fs::Permissions::from_mode(0o755))
            .unwrap();
        let config = home.path().join("xdg");
        let vars = [(
            "XDG_CONFIG_HOME".to_string(),
            config.to_string_lossy().into_owned(),
        )]
        .into_iter()
        .collect();
        let env = Environment::new(home.path().to_path_buf(), vars, vec![bin]);
        // SAFETY of the test: the stand-in reads XDG_CONFIG_HOME from the process.
        unsafe { std::env::set_var("XDG_CONFIG_HOME", &config) };
        let options = options("/opt/tomacco/tomaccoctl");
        let resolved = resolve(ClientId::OpenCode, &env, &options);
        assert!(resolved.found);
        assert_eq!(resolved.layout, Some(Layout::OpenCodeV2));
        assert!(resolved.writer.is_some());
        assert_eq!(inspect(&resolved, &options).state, State::NotConnected);
        let report = connect(&env, &resolved, &options, &mut yes());
        assert_eq!(report.state, State::Added);
        assert_eq!(
            std::fs::read_to_string(&log).unwrap(),
            "mcp add --global tomacco -- /opt/tomacco/tomaccoctl mcp\n"
        );
        assert_eq!(inspect(&resolved, &options).state, State::Connected);
        unsafe { std::env::remove_var("XDG_CONFIG_HOME") };
    }

    #[test]
    fn grok_cli_gets_an_enabled_stdio_server_with_the_grouped_list_in_its_array() {
        let home = tempfile::tempdir().unwrap();
        let env = env(home.path());
        let options = options("/opt/tomacco/tomaccoctl");
        let grok = env.home.join(".grok");
        std::fs::create_dir_all(&grok).unwrap();
        let file = grok.join("user-settings.json");
        std::fs::write(&file, "{\"apiKey\":\"REDACTED\",\"mcp\":{\"servers\":[{\"id\":\"github\",\"label\":\"GitHub\",\"enabled\":false,\"transport\":\"stdio\",\"command\":\"npx\"}]}}").unwrap();
        let resolved = resolve(ClientId::GrokCli, &env, &options);
        assert!(resolved.found);
        let report = connect(&env, &resolved, &options, &mut yes());
        assert_eq!(report.state, State::Added);
        assert_eq!(report.tools, Some(Mode::Grouped));
        let written = read(&file);
        assert_eq!(written["apiKey"], "REDACTED");
        let servers = written["mcp"]["servers"].as_array().unwrap();
        assert_eq!(servers.len(), 2);
        assert_eq!(servers[0]["id"], "github");
        assert_eq!(
            servers[1],
            json!({"id":"tomacco","label":"Tomacco","enabled":true,"transport":"stdio","command":"/opt/tomacco/tomaccoctl","args":["mcp"],"env":{TOOLS_VARIABLE:"grouped"}})
        );
        assert_eq!(inspect(&resolved, &options).state, State::Connected);
        // Switched off in the TUI: stays off.
        let mut off = written.clone();
        off["mcp"]["servers"][1]["enabled"] = json!(false);
        std::fs::write(&file, off.to_string()).unwrap();
        assert_eq!(
            connect(&env, &resolved, &options, &mut yes()).state,
            State::Disabled
        );
        // Found by label when the id differs, as the TUI's slug does.
        std::fs::write(&file, "{\"mcp\":{\"servers\":[{\"id\":\"x\",\"label\":\" Tomacco \",\"enabled\":true,\"transport\":\"stdio\",\"command\":\"/elsewhere/tomaccoctl\",\"args\":[\"mcp\"]}]}}").unwrap();
        assert!(matches!(
            inspect(&resolved, &options).state,
            State::Outdated(_)
        ));
        // Removal keeps the other servers.
        std::fs::write(&file, written.to_string()).unwrap();
        assert_eq!(
            remove(&env, &resolved, &options, &mut yes()).state,
            State::Removed
        );
        assert_eq!(
            read(&file)["mcp"]["servers"],
            json!([{"id":"github","label":"GitHub","enabled":false,"transport":"stdio","command":"npx"}])
        );
        // A servers value that is not an array is the person's.
        std::fs::write(&file, "{\"mcp\":{\"servers\":{}}}").unwrap();
        assert!(
            matches!(inspect(&resolved, &options).state, State::Manual(ref why) if why.contains("not an array"))
        );
        assert_eq!(slug(" Tomacco "), "tomacco");
        assert_eq!(slug("My Server!"), "my-server");
    }

    #[test]
    fn omp_writes_its_agent_file_with_the_schema_and_honours_disabled_servers() {
        let home = tempfile::tempdir().unwrap();
        let env = env(home.path());
        let options = options("/opt/tomacco/tomaccoctl");
        std::fs::create_dir_all(env.home.join(".omp")).unwrap();
        let resolved = resolve(ClientId::Omp, &env, &options);
        assert!(resolved.found);
        assert_eq!(resolved.file, env.home.join(".omp/agent/mcp.json"));
        assert_eq!(
            connect(&env, &resolved, &options, &mut yes()).state,
            State::Added
        );
        let written = read(&resolved.file);
        assert!(s(&written, "$schema").contains("mcp-schema.json"));
        assert_eq!(
            written["mcpServers"]["tomacco"],
            json!({"type":"stdio","command":"/opt/tomacco/tomaccoctl","args":["mcp"]})
        );
        std::fs::write(&resolved.file, "{\"mcpServers\":{\"tomacco\":{\"type\":\"stdio\",\"command\":\"/opt/tomacco/tomaccoctl\",\"args\":[\"mcp\"]}},\"disabledServers\":[\"tomacco\"]}").unwrap();
        assert_eq!(inspect(&resolved, &options).state, State::Disabled);
        // An existing file without the schema line keeps its own keys.
        std::fs::write(
            &resolved.file,
            "{\"mcpServers\":{\"fs\":{\"command\":\"npx\"}},\"enabledServers\":[\"fs\"]}",
        )
        .unwrap();
        assert_eq!(
            connect(&env, &resolved, &options, &mut yes()).state,
            State::Added
        );
        let written = read(&resolved.file);
        assert!(written.get("$schema").is_none());
        assert_eq!(written["enabledServers"], json!(["fs"]));
        assert_eq!(written["mcpServers"]["fs"]["command"], "npx");
    }

    #[test]
    fn a_file_that_does_not_read_back_is_restored_from_its_backup() {
        let home = tempfile::tempdir().unwrap();
        let env = env(home.path());
        let file = env.home.join(".cursor/mcp.json");
        std::fs::create_dir_all(file.parent().unwrap()).unwrap();
        std::fs::write(&file, "{\"a\":1}").unwrap();
        let failing = |_: &str| -> std::result::Result<(), String> { Err("wrong".to_string()) };
        let error =
            commit(&env, ClientId::Cursor, &file, b"{\"a\":2}", None, &failing).unwrap_err();
        assert!(error.message.contains("restored"), "{}", error.message);
        assert_eq!(std::fs::read_to_string(&file).unwrap(), "{\"a\":1}");
        // A new file that fails its check is removed again.
        let fresh = env.home.join(".cursor/new.json");
        assert!(commit(&env, ClientId::Cursor, &fresh, b"{}", None, &failing).is_err());
        assert!(!fresh.exists());
        let passing = |_: &str| -> std::result::Result<(), String> { Ok(()) };
        let copy = commit(&env, ClientId::Cursor, &file, b"{\"a\":3}", None, &passing)
            .unwrap()
            .unwrap();
        assert_eq!(std::fs::read_to_string(&copy).unwrap(), "{\"a\":1}");
        assert_eq!(std::fs::read_to_string(&file).unwrap(), "{\"a\":3}");
    }

    #[test]
    fn hermes_yaml_gets_one_entry_in_its_block_and_everything_else_stays() {
        let home = tempfile::tempdir().unwrap();
        let env = env(home.path());
        let options = options("/opt/tomacco/tomaccoctl");
        let hermes = env.home.join(".hermes");
        std::fs::create_dir_all(&hermes).unwrap();
        let file = hermes.join("config.yaml");
        let resolved = resolve(ClientId::Hermes, &env, &options);
        assert!(resolved.found);
        assert_eq!(inspect(&resolved, &options).state, State::NotConnected);
        // No file yet: a small one is made.
        assert_eq!(
            connect(&env, &resolved, &options, &mut yes()).state,
            State::Added
        );
        assert_eq!(
            std::fs::read_to_string(&file).unwrap(),
            "mcp_servers:\n  tomacco:\n    command: \"/opt/tomacco/tomaccoctl\"\n    args: [\"mcp\"]\n"
        );
        assert_eq!(inspect(&resolved, &options).state, State::Connected);
        assert_eq!(
            std::fs::metadata(&file).unwrap().permissions().mode() & 0o777,
            0o600
        );
        // A file with other servers, comments and settings: one entry is added at the end of the block.
        let before = "# Hermes\nmodel: default\nmcp_servers:\n  # my servers\n  filesystem:\n    command: npx   # node\n    args: [\"-y\", \"@modelcontextprotocol/server-filesystem\"]\n    env:\n      X: '1'\n\n  linear:\n    url: https://mcp.linear.app/mcp\n    auth: oauth\n\nskills:\n  disabled: []\n";
        std::fs::write(&file, before).unwrap();
        assert_eq!(
            connect(&env, &resolved, &options, &mut yes()).state,
            State::Added
        );
        let after = std::fs::read_to_string(&file).unwrap();
        assert_eq!(
            after,
            "# Hermes\nmodel: default\nmcp_servers:\n  # my servers\n  filesystem:\n    command: npx   # node\n    args: [\"-y\", \"@modelcontextprotocol/server-filesystem\"]\n    env:\n      X: '1'\n\n  linear:\n    url: https://mcp.linear.app/mcp\n    auth: oauth\n  tomacco:\n    command: \"/opt/tomacco/tomaccoctl\"\n    args: [\"mcp\"]\n\nskills:\n  disabled: []\n"
        );
        assert_eq!(inspect(&resolved, &options).state, State::Connected);
        assert_eq!(
            connect(&env, &resolved, &options, &mut yes()).state,
            State::Connected
        );
        assert_eq!(std::fs::read_to_string(&file).unwrap(), after);
        // Removal takes the entry out and leaves the rest as it was.
        assert_eq!(
            remove(&env, &resolved, &options, &mut yes()).state,
            State::Removed
        );
        assert_eq!(std::fs::read_to_string(&file).unwrap(), before);
        // Switched off by the person, and another path: reported.
        std::fs::write(&file, "mcp_servers:\n  tomacco:\n    command: '/old/tomaccoctl'\n    args: [mcp]\n    enabled: false\n").unwrap();
        assert_eq!(inspect(&resolved, &options).state, State::Disabled);
        std::fs::write(&file, "mcp_servers:\n  tomacco:\n    command: '/old/tomaccoctl'\n    args: [mcp]\n    timeout: 90\n").unwrap();
        assert!(matches!(
            inspect(&resolved, &options).state,
            State::Outdated(_)
        ));
        assert_eq!(
            connect(&env, &resolved, &options, &mut yes()).state,
            State::Updated
        );
        assert_eq!(
            std::fs::read_to_string(&file).unwrap(),
            "mcp_servers:\n  tomacco:\n    command: \"/opt/tomacco/tomaccoctl\"\n    args: [\"mcp\"]\n    timeout: 90\n"
        );
        // Removing the only entry takes the block with it.
        assert_eq!(
            remove(&env, &resolved, &options, &mut yes()).state,
            State::Removed
        );
        assert_eq!(std::fs::read_to_string(&file).unwrap(), "");
        // Someone else's tomacco: a conflict.
        std::fs::write(
            &file,
            "mcp_servers:\n  tomacco:\n    url: https://example.com/mcp\n",
        )
        .unwrap();
        assert!(matches!(
            inspect(&resolved, &options).state,
            State::Conflict(_)
        ));
    }

    #[test]
    fn hermes_yaml_that_setup_cannot_read_is_left_to_the_person() {
        let program = "/opt/tomacco/tomaccoctl";
        for (text, why) in [
            ("mcp_servers: &servers\n  a:\n    command: x\n", "anchor"),
            (
                "base: &b\n  x: 1\nmcp_servers:\n  a:\n    <<: *b\n",
                "merge",
            ),
            ("mcp_servers: {a: {command: x}}\n", "inline"),
            (
                "---\nmcp_servers:\n  a:\n    command: x\n",
                "document marker",
            ),
            ("mcp_servers:\n\ta:\n\t\tcommand: x\n", "tab"),
            (
                "mcp_servers:\n  a:\n    command: x\n  a:\n    command: y\n",
                "twice",
            ),
            (
                "mcp_servers:\n    a:\n      command: x\n  b:\n    command: y\n",
                "irregular",
            ),
            ("- just\n- a list\n", "block mapping"),
            (
                "mcp_servers:\n  a:\n    command: x\nmcp_servers:\n  b:\n    command: y\n",
                "more than once",
            ),
        ] {
            let error = yaml::insert(text, program, None).unwrap_err();
            assert!(error.contains(why), "{text:?}: {error}");
        }
        // A plain file without the block, with and without a final newline.
        assert_eq!(
            yaml::insert("model: x", program, Some(Mode::Compact)).unwrap(),
            "model: x\n\nmcp_servers:\n  tomacco:\n    command: \"/opt/tomacco/tomaccoctl\"\n    args: [\"mcp\"]\n    env:\n      TOMACCO_MCP_TOOLS: \"compact\"\n"
        );
        assert_eq!(
            yaml::insert("", program, None).unwrap(),
            "mcp_servers:\n  tomacco:\n    command: \"/opt/tomacco/tomaccoctl\"\n    args: [\"mcp\"]\n"
        );
        // Four-space indentation is followed.
        assert_eq!(
            yaml::insert("mcp_servers:\n    a:\n        command: x\n", program, None).unwrap(),
            "mcp_servers:\n    a:\n        command: x\n    tomacco:\n        command: \"/opt/tomacco/tomaccoctl\"\n        args: [\"mcp\"]\n"
        );
        // Windows line endings stay.
        let crlf = yaml::insert("model: x\r\n", program, None).unwrap();
        assert!(crlf.starts_with("model: x\r\n\r\nmcp_servers:\r\n"));
        // An entry with an inline value or a block sequence is not updated.
        assert!(yaml::update("mcp_servers:\n  tomacco: {command: x}\n", program, None).is_err());
        assert!(
            yaml::update(
                "mcp_servers:\n  tomacco:\n    command: x\n    args:\n      - mcp\n",
                program,
                None
            )
            .is_err()
        );
        // Quoting.
        assert_eq!(yaml::quote("a \"b\" \\ ü"), "\"a \\\"b\\\" \\\\ ü\"");
        let view = yaml::inspect("mcp_servers:\n  tomacco:\n    command: \"/x y/tomaccoctl\"\n    args: ['mcp', \"--policy\"]\n    env:\n      TOMACCO_MCP_TOOLS: grouped\n").unwrap();
        let entry = view.tomacco().unwrap();
        assert_eq!(entry.command.as_deref(), Some("/x y/tomaccoctl"));
        assert_eq!(
            entry.args.as_deref(),
            Some(&["mcp".to_string(), "--policy".to_string()][..])
        );
        assert_eq!(entry.tools.as_deref(), Some("grouped"));
    }

    #[test]
    fn comments_and_trailing_commas_are_stripped_for_reading_only() {
        let text = "{\n  // c\n  \"a\": \"x // not a comment\", /* b */\n  \"b\": [1, 2,],\n}\n";
        let value: Value = serde_json::from_str(&without_comments(text)).unwrap();
        assert_eq!(value, json!({"a":"x // not a comment","b":[1,2]}));
        assert_eq!(opencode_version_words("opencode v2.2.5\n"), Some((2, 2, 5)));
        assert_eq!(opencode_version_words("1.0.200-beta"), Some((1, 0, 200)));
        assert_eq!(opencode_version_words("nothing"), None);
    }

    /// `opencode_version`'s parsing, on text instead of a program.
    fn opencode_version_words(text: &str) -> Option<(u64, u64, u64)> {
        text.split(|c: char| c.is_whitespace() || c == 'v')
            .find_map(|word| {
                let mut parts = word.trim().split('.');
                let major = parts.next()?.parse().ok()?;
                let minor = parts.next().unwrap_or("0").parse().ok()?;
                let patch = parts
                    .next()
                    .unwrap_or("0")
                    .chars()
                    .take_while(char::is_ascii_digit)
                    .collect::<String>()
                    .parse()
                    .unwrap_or(0);
                Some((major, minor, patch))
            })
    }

    #[test]
    fn the_skill_is_linked_where_each_client_reads_it_and_only_after_a_yes() {
        let home = tempfile::tempdir().unwrap();
        let home = home.path();
        let env = env(home);
        let skill = installed_skill(home);
        std::fs::create_dir_all(home.join(".claude")).unwrap();
        std::fs::create_dir_all(home.join(".grok")).unwrap();
        std::fs::create_dir_all(home.join(".omp")).unwrap();
        let found = |id: ClientId| matches!(id, ClientId::GrokCli | ClientId::Omp);
        let targets = skill_targets(&env, &found);
        let labels: Vec<&str> = targets.iter().map(|t| t.label.as_str()).collect();
        assert_eq!(
            labels,
            [
                "Claude Code",
                "Codex",
                "Grok CLI and other agents that read ~/.agents",
                "omp"
            ]
        );
        let claude = &targets[0];
        // Not installed: nothing to link.
        assert_eq!(skill_state(home, claude), SkillState::Absent);
        std::fs::create_dir_all(&skill).unwrap();
        std::fs::write(skill.join("SKILL.md"), "---\nname: tomacco\n---\n").unwrap();
        // No agent directory: nothing is asked or made.
        assert_eq!(
            link_skill(home, &targets[1], &mut yes()).unwrap(),
            SkillState::Absent
        );
        assert!(!home.join(".codex").exists());
        assert_eq!(
            link_skill(home, claude, &mut no()).unwrap(),
            SkillState::Declined
        );
        assert_eq!(
            link_skill(home, claude, &mut yes()).unwrap(),
            SkillState::Added
        );
        assert_eq!(
            std::fs::read_link(home.join(".claude/skills/tomacco")).unwrap(),
            skill
        );
        assert_eq!(
            link_skill(home, claude, &mut yes()).unwrap(),
            SkillState::Linked
        );
        // Grok CLI's ~/.agents is made for it; omp's agent directory too.
        assert_eq!(
            link_skill(home, &targets[2], &mut yes()).unwrap(),
            SkillState::Added
        );
        assert_eq!(
            std::fs::read_link(home.join(".agents/skills/tomacco")).unwrap(),
            skill
        );
        assert_eq!(
            link_skill(home, &targets[3], &mut yes()).unwrap(),
            SkillState::Added
        );
        assert!(home.join(".omp/agent/skills/tomacco/SKILL.md").is_file());
        // Another skill of that name stays, on linking and on removal.
        std::fs::create_dir_all(home.join(".hermes/skills/tomacco")).unwrap();
        std::fs::write(home.join(".hermes/skills/tomacco/SKILL.md"), "theirs").unwrap();
        let hermes = SkillTarget {
            label: "Hermes Agent".to_string(),
            directory: home.join(".hermes"),
            create: false,
        };
        assert_eq!(
            link_skill(home, &hermes, &mut yes()).unwrap(),
            SkillState::Taken(home.join(".hermes/skills/tomacco"))
        );
        assert_eq!(
            unlink_skill(home, &hermes).unwrap(),
            SkillState::Taken(home.join(".hermes/skills/tomacco"))
        );
        assert_eq!(
            std::fs::read_to_string(home.join(".hermes/skills/tomacco/SKILL.md")).unwrap(),
            "theirs"
        );
        assert_eq!(unlink_skill(home, claude).unwrap(), SkillState::Removed);
        assert!(std::fs::symlink_metadata(home.join(".claude/skills/tomacco")).is_err());
        assert_eq!(unlink_skill(home, claude).unwrap(), SkillState::Absent);
    }

    #[test]
    fn snippets_show_each_clients_entry_and_command() {
        let home = tempfile::tempdir().unwrap();
        let env = env(home.path());
        let options = options("/opt/to macco/tomaccoctl");
        let text = |id| {
            let resolved = resolve(id, &env, &options);
            (
                snippet(&resolved, &options),
                upstream_command(&resolved, &options),
            )
        };
        let (claude, command) = text(ClientId::ClaudeCode);
        assert_eq!(
            claude,
            "claude mcp add --transport stdio --scope user tomacco -- '/opt/to macco/tomaccoctl' mcp"
        );
        assert_eq!(command.as_deref(), Some(claude.as_str()));
        let (gemini, none) = text(ClientId::GeminiCli);
        assert_eq!(
            gemini,
            "\"mcpServers\": {\n  \"tomacco\": {\n    \"command\": \"/opt/to macco/tomaccoctl\",\n    \"args\": [\n      \"mcp\"\n    ]\n  }\n}"
        );
        assert!(none.is_none());
        let (grok, _) = text(ClientId::GrokCli);
        assert!(
            grok.starts_with("\"mcp\": {\n  \"servers\": [\n    {\n      \"id\": \"tomacco\""),
            "{grok}"
        );
        assert!(grok.contains("\"TOMACCO_MCP_TOOLS\": \"grouped\""));
        let (hermes, command) = text(ClientId::Hermes);
        assert_eq!(
            hermes,
            "mcp_servers:\n  tomacco:\n    command: \"/opt/to macco/tomaccoctl\"\n    args: [\"mcp\"]"
        );
        assert!(
            command.unwrap().starts_with(
                "hermes mcp add tomacco --command '/opt/to macco/tomaccoctl' --args mcp"
            )
        );
        let (omp, _) = text(ClientId::Omp);
        assert!(
            omp.starts_with("\"mcpServers\": {\n  \"tomacco\": {\n    \"type\": \"stdio\""),
            "{omp}"
        );
        let report = inspect(&resolve(ClientId::Omp, &env, &options), &options);
        let value = report.value();
        assert_eq!(value["id"], "omp");
        assert_eq!(value["state"], "absent");
        assert_eq!(value["connected"], false);
    }
}

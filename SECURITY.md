# Security policy

## Reporting a vulnerability

Please report security problems privately through
[GitHub's security advisories](https://github.com/marselnenaj/tomacco/security/advisories/new),
not in a public issue. Say which version or commit you used and how to reproduce
the problem. Tomacco is maintained by one person, so there is no guaranteed
response time, but a report is answered as soon as it has been looked at.

## Supported versions

Only the latest release gets fixes.

## What is in scope

- `tomaccoctl`, its stdio MCP server and the Qt desktop: everything an agent or a
  local process can make them do beyond what the operation states.
- The agent guards: a call that its [agent policy](docs/agents.md#guardrails), write roots or desktop approval should
  have refused but did not, or a refused call missing from the audit log.
- `install.py`, the release package and the handling of secrets and credentials.

## How Tomacco keeps local data private

- Operations take passwords, tokens and keys as paths of owner-only files (`password_file`, `key_password_file`,
  `updates.token_file`, `credentials`, `key_path`), never as values, and refuse files others can read. Only the path is stored in
  a job. When the path names no file, the error does not repeat what was given, in case it was the secret itself.
- Errors, job records (`job.get`, `job.list`, `job.wait`, `desktop.status`) and job logs (`job.logs`) are returned with
  recognised secrets masked as `[redacted]`: authorization headers and bearer tokens, credentials in URLs, signing passwords on a
  command line, `password=`, `token=` and similar fields, PEM private keys, and GitHub, GitLab, Google, AWS and Slack token
  formats. This masks known shapes and cannot recognise every secret. The log file on disk is not rewritten; it is owner-only.
- The data directory (`~/.local/share/tomacco`, or `TOMACCO_HOME`) is owner-only (0700). `tomaccoctl` tightens one an earlier
  version or another program left open, and refuses one that belongs to another user. The installer creates it, and its backups,
  owner-only as well.
- Directories it creates for its own state are made 0700 in one step. Job logs, worker logs and the job database are 0600, since
  they hold commands, environments and tool output; a log path that is a symbolic link is refused.
- A file an operation writes (a screenshot, recording, trace, report, profile export or signed release) is refused when the
  destination is itself a symbolic link, a directory or a special file, since some of the tools that write it follow links.
  Its folders may be links. `files.*` operations stay inside their project, also through links.
- Runtime and debugger sockets live in owner-only directories, in `TMPDIR` or, when that path is too long for a socket, in a private
  `XDG_RUNTIME_DIR` or `/tmp`.

## An agent with Tomacco's tools

A coding agent connected through `tomaccoctl mcp`, or one that runs `tomaccoctl call`, acts with your user's rights
through every operation it is allowed. Without an [agent policy](docs/agents.md#guardrails), that is all of them.
Concretely, it can:

- **Run code.** A build runs the project's own scripts (Gradle, npm, SwiftPM, CMake, a `tomacco.json` task), and
  `project.configure` can add such a task. `settings.configure` can point a tool such as `adb` at any program, which
  later operations run. `remote.run` runs a command on a Mac configured for SSH. None of these is marked destructive,
  so `no_destructive` allows them; only `read_only` or an allow list stops them.
- **Write and read files.** Without write roots, `files.write` saves into any directory it names as a project, and
  screenshots, reports, exports and keystores go to any path you can write. `files.read`, `files.list` and
  `files.search` read any directory it names as a project; no guard limits reading.
- **Change devices.** Install and remove apps and clear their data, change settings, wipe or delete emulators,
  restore or reset the virtual iPhone, and tap and type on a connected phone, including in apps signed in to your real
  accounts.
- **Reach the network and publish.** Download SDKs and tools, push to Google Play with the credentials you configured
  (`play.commit`), upload to App Store Connect (`ios.store_ship` with `upload`), and install a Tomacco update.

What the guards cover, when you set them:

- `agent_policy` refuses operations by annotation (`no_destructive`, `read_only`) or by name, per MCP client or for a
  server started with `--policy`, before they run. An agent cannot change the policy over MCP.
- Under a policy other than `full`, destinations an operation takes as an argument must be inside the write roots,
  after links are resolved, and never Tomacco's settings, audit log or saved results.
- With `approval.destructive`, a destructive MCP call waits for a yes in the desktop, which shows the arguments with
  recognised secrets masked; no answer is a no.
- An MCP session runs a limited number of jobs at once and gets results up to a size limit inline.
- An owner-only audit log records who called what that may change something, and every refused call.

What they do not cover:

- **An agent with a shell.** The policy, the roots and the audit log are files and variables of your user. An agent
  that can run commands as you can edit settings.json, start `tomaccoctl` with another `TOMACCO_POLICY`, set
  `TOMACCO_CALLER=desktop` (which the desktop uses for its own calls, and which skips the guards), run the same tools
  directly, or rewrite the audit log. The guards limit what an agent does through Tomacco's operations; they are not a
  sandbox. Run an untrusted agent in a container or a separate account.
- **What code does once it runs.** A build, a test run or a task can do anything your user can. Write roots apply to
  the paths in an operation's arguments, not to the processes it starts.
- **The annotations' judgement.** Approval and `no_destructive` follow `destructiveHint`, which marks operations that
  delete or overwrite data. Running code, installing an app or publishing a release that is not marked destructive is
  not asked about.
- **The desktop as a witness.** Approval goes to the desktop's agent socket, which is owner-only; another process of
  your user could answer it in the desktop's place. Without a desktop open, a destructive call runs unless
  `without_desktop` is `deny`.
- **Every secret.** Masking recognises known shapes. Tomacco never returns the contents of the secret files it takes
  as paths, but a file an agent reads with `files.read`, or a log line a tool prints in an unknown format, is not masked.

## Checking a release

Each release package comes with a `.sha256` and a detached Ed25519 signature, `.sig`. `install.sh` and
`tomaccoctl update` check both before they install anything. To check a package you downloaded yourself:

```sh
tomaccoctl verify tomacco-1.1.0-Linux-x86_64.tar.gz
```

It reads `FILE.sha256` and `FILE.sig` beside the file (`--sha256` and `--sig` name others), checks the signature
against the release keys built into `tomaccoctl`, and exits 0 only when the file is verified; `--json` prints the result
as JSON. How the release key is replaced is in [building](docs/building.md#rotating-the-release-key).

Each package also has a CycloneDX software bill of materials, `PACKAGE.cdx.json`, signed the same way, which lists the
third-party components the package contains with their versions, licenses and digests. `tomaccoctl verify PACKAGE.cdx.json`
checks its signature. The release's provenance, `tomacco-VERSION-provenance.intoto.json`, records the commit, the build
inputs and the builder, with the SHA-256 of every release file; `tomaccoctl verify` on it checks its signature and every listed
file in the same directory.

## What is not

The virtual iOS device is an experimental research runtime. It disables or
bypasses platform protections on purpose (pointer authentication, BTI, the
signature policy for apps you build yourself), and its guest is not a security
boundary. Do not put real accounts or personal data into it, and do not run apps
in it that you do not trust. See [docs/legal.md](docs/legal.md).

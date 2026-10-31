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

## What is not

The virtual iOS device is an experimental research runtime. It disables or
bypasses platform protections on purpose (pointer authentication, BTI, the
signature policy for apps you build yourself), and its guest is not a security
boundary. Do not put real accounts or personal data into it, and do not run apps
in it that you do not trust. See [docs/legal.md](docs/legal.md).

#!/usr/bin/env python3
"""Install the desktop, control plane, agent skill and optional Omarchy panel for one user."""
# Since 1.3 the installer is `tomaccoctl _install` (native/install.rs), which
# takes these flags and needs no Python. Run from a release package or an
# installation, this file hands over to the tomaccoctl there (see
# native_installer()), so older install.sh scripts and updaters keep working.
# The Python code below still installs from a source checkout, generates the
# package's documents for CMake (--package-docs), and stands in where no
# tomaccoctl that installs is found. Both write the same journal and backups.
import argparse
import contextlib
import datetime
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP_ID = "io.github.marselnenaj.tomacco"
MARKER = "# Managed by tomacco/install.py"
BACKUPS = Path(".local/share/tomacco/install-backups")
# Mutable user data: projects, device profiles, settings, runtime state and the
# installation backups. Neither an installation nor an uninstallation touches it.
USER_DATA = Path(".local/share/tomacco")


def identity_files(app_id):
    """The menu entry, icon and software-center listing installed under an app ID."""
    return (Path(".local/share/applications") / (app_id + ".desktop"),
            Path(".local/share/icons/hicolor/scalable/apps") / (app_id + ".svg"),
            Path(".local/share/metainfo") / (app_id + ".metainfo.xml"))


# man finds pages in ../share/man beside each directory on PATH, so
# ~/.local/bin/tomaccoctl gets its page from here.
MANUAL_PAGE = Path(".local/share/man/man1/tomaccoctl.1")
# What an installation owns outright. --uninstall removes these files when they
# are still exactly as installed; everything else it leaves alone.
OWNED = (Path(".local/bin/tomacco"), Path(".local/bin/tomaccoctl"), Path(".local/lib/tomacco"),
         Path(".local/share/doc/tomacco"), MANUAL_PAGE, *identity_files(APP_ID),
         Path(".config/omarchy/plugins") / APP_ID, Path(".config/hypr/tomacco.lua"),
         Path(".config/hypr/tomacco-bindings.lua"))
# Directories the installation creates for itself, removed once empty.
OWNED_DIRECTORIES = (Path(".local/lib/tomacco"), Path(".local/share/doc/tomacco"),
                     Path(".config/omarchy/plugins") / APP_ID)
# The agent skill: installed with the program, and linked into the skills
# directory of each coding agent whose configuration directory exists.
SKILL = Path(".local/lib/tomacco/skills/tomacco")
AGENT_SKILL_LINKS = tuple(Path(agent) / "skills/tomacco" for agent in (".claude", ".codex", ".agents"))
HYPR_REQUIRE = 'require("hypr.tomacco")'
HYPR_COMMENT = "-- Tomacco mobile workspace."
HYPR_BINDINGS_REQUIRE = 'require("hypr.tomacco-bindings")'
HYPR_BINDINGS_COMMENT = "-- Tomacco keys."
# Omarchy's menu reads the user's own entries from this file, beside its
# defaults. Tomacco's entries sit between two comment lines.
MENU_FILE = Path(".config/omarchy/extensions/omarchy-menu.jsonc")
MENU_BEGIN = "  // >>> tomacco (managed by Tomacco; tomaccoctl uninstall removes it)"
MENU_END = "  // <<< tomacco"
# Where an installation keeps the panel's sources, so `tomaccoctl omarchy
# repair` can install the panel again without the package.
PANEL_SOURCE = Path(".local/lib/tomacco/integrations/omarchy")


# The release tarball holds this file at its top, beside bin/, lib/ and share/
# as CMake installed them, and no source tree. These are the tree's paths there.
PACKAGED = (ROOT / "share/tomacco/VERSION").is_file()
# Every installation keeps a copy of this file beside the program in
# ~/.local/lib/tomacco, so `tomaccoctl uninstall` works after the package or
# checkout it came from is gone. That copy only uninstalls, rolls back and
# lists backups.
INSTALLED = not PACKAGED and (ROOT / "VERSION").is_file() and (ROOT / "tomaccoctl").is_file()
# A portable package (tools/build_portable.py) carries Qt, Qt WebEngine and
# FFmpeg here. The installation keeps them in ~/.local/lib/tomacco/runtime, and
# qt.conf beside the program points Qt at them.
RUNTIME = Path("lib/tomacco/runtime")
QT_CONF = "[Paths]\nPrefix=runtime\n"
PACKAGE_PATHS = {
    "tools": "share/tomacco/tools",
    "examples/ios-restore-native": "share/tomacco/examples/ios-restore-native",
    "integrations/ios-emulator": "share/tomacco/integrations/ios-emulator",
    "integrations/omarchy": "share/tomacco/omarchy",
    "integrations/agent-skill": "share/tomacco/agent-skill",
    "assets": "lib/tomacco/assets",
    "assets/icon.svg": f"share/icons/hicolor/scalable/apps/{APP_ID}.svg",
    f"assets/{APP_ID}.desktop": f"share/applications/{APP_ID}.desktop",
    f"assets/{APP_ID}.metainfo.xml": f"share/metainfo/{APP_ID}.metainfo.xml",
    "docs/tomaccoctl.1": "share/man/man1/tomaccoctl.1",
}


def source_path(relative):
    """A file or directory the installation copies, by its path in the source tree."""
    return ROOT / (PACKAGE_PATHS[relative] if PACKAGED else relative)


def project_version():
    """The version CMake builds (project(... VERSION x.y.z)), so both installers agree."""
    if PACKAGED:
        return (ROOT / "share/tomacco/VERSION").read_text().strip()
    if INSTALLED:
        return (ROOT / "VERSION").read_text().strip()
    found = re.search(r"project\(\s*Tomacco\s+VERSION\s+([0-9][0-9.]*)", (ROOT / "CMakeLists.txt").read_text())
    if not found:
        raise SystemExit("CMakeLists.txt names no project version")
    return found.group(1)


VERSION = project_version()
REPOSITORY = "https://github.com/marselnenaj/tomacco"
# The documents a package installs into share/doc/tomacco, a flat directory, by
# source path, and their installed names. The guides for using Tomacco and the
# legal texts; the architecture and building guides stay in the source tree, and
# links to them point at the release's copy on GitHub.
DOCUMENTS = {
    "README.md": "README.md", "LICENSE": "LICENSE", "COPYRIGHT": "COPYRIGHT",
    "THIRD_PARTY_NOTICES.md": "THIRD_PARTY_NOTICES.md", "native/protocol/THIRD_PARTY.md": "THIRD_PARTY.md",
    # The GPL version 3 text distributed binaries are conveyed under.
    "integrations/ios-emulator/COPYING_GPL-3.0": "COPYING.GPL-3.0",
    "docs/README.md": "guides.md", "docs/RELEASE_NOTES.md": "RELEASE_NOTES.md",
    **{f"docs/{name}.md": f"{name}.md" for name in
       ("getting-started", "agents", "recipes", "faq", "desktop", "android", "iphone", "troubleshooting", "legal", "privacy",
        "research-engines")},
    "docs/virtual-ios/README.md": "virtual-ios.md",
    **{f"docs/virtual-ios/{name}.md": f"virtual-ios-{name}.md" for name in ("setup", "usage", "troubleshooting")},
}
SOURCE_NOTICE = "docs/SOURCE.md.in"
INTEGRATION = "integrations/ios-emulator"
# Developer notes beside the engine recipes; only their README is installed.
INTEGRATION_NOTES = re.compile(r"(?!README[.]md$).*[.]md$")
MARKDOWN_LINK = re.compile(r"(\]\()([^)\s]+)(\))")
# The README's banner and navigation are HTML.
HTML_LINK = re.compile(r"""((?:href|src)=")([^"]+)(")""")


def source_commit():
    """The commit the binaries are built from, as the source notice states it."""
    given = os.environ.get("TOMACCO_SOURCE_COMMIT", "").strip()
    if given:
        return given
    def git(*arguments):
        result = subprocess.run(["git", "-C", str(ROOT), *arguments], capture_output=True, text=True, check=False)
        return result.stdout.strip() if result.returncode == 0 else None
    commit = git("rev-parse", "--verify", "HEAD") if shutil.which("git") else None
    if not commit:
        return "not recorded (built outside a Git checkout; the release's source tarball is the source)"
    if git("status", "--porcelain", "--untracked-files=no"):
        return f"`{commit}` with local changes that are not part of that commit"
    return f"`{commit}`"


def packaged_links(source, text, installed=None):
    """Links of a document as installed. `installed` maps the repository paths
    installed beside it to their relative installed names (by default the flat
    documentation directory); any other repository path points at that path in
    this version's tag on GitHub."""
    if installed is None:
        installed = {**DOCUMENTS, SOURCE_NOTICE: "SOURCE.md"}
    base = Path(source).parent
    def link(match):
        target = match.group(2)
        if re.match(r"[a-z][a-z0-9+.-]*:", target, re.I) or target.startswith("#"):
            return match.group(0)
        path, _, anchor = target.partition("#")
        resolved = os.path.normpath((base / path).as_posix()).replace(os.sep, "/")
        if resolved.startswith("../"):
            return match.group(0)
        kind = "raw" if match.group(1).startswith("src") else "blob"
        new = installed.get(resolved) or f"{REPOSITORY}/{kind}/v{VERSION}/{resolved}"
        return match.group(1) + new + ("#" + anchor if anchor else "") + match.group(3)
    return HTML_LINK.sub(link, MARKDOWN_LINK.sub(link, text))


def documents():
    """Installed name -> content of every document the package installs."""
    if PACKAGED:
        return {path.name: path.read_bytes() for path in sorted((ROOT / "share/doc/tomacco").iterdir()) if path.is_file()}
    result = {}
    for source, name in DOCUMENTS.items():
        data = (ROOT / source).read_bytes()
        result[name] = packaged_links(source, data.decode("utf-8")).encode("utf-8") if source.endswith(".md") else data
    notice = (ROOT / SOURCE_NOTICE).read_text(encoding="utf-8")
    notice = notice.replace("@TOMACCO_VERSION@", VERSION).replace("@TOMACCO_COMMIT@", source_commit())
    result["SOURCE.md"] = packaged_links(SOURCE_NOTICE, notice).encode("utf-8")
    return result


def integration_files():
    """Name -> content of the engine recipe files installed from
    integrations/ios-emulator: sources, patches and licenses, and the README
    with its links adjusted; the developer notes stay in the source tree."""
    files = {source.name: source.read_bytes() for source in sorted(source_path(INTEGRATION).iterdir())
             if source.is_file() and not INTEGRATION_NOTES.match(source.name)}
    if PACKAGED:
        return files
    beside = {f"{INTEGRATION}/{name}": name for name in files}
    files["README.md"] = packaged_links(f"{INTEGRATION}/README.md", files["README.md"].decode("utf-8"), beside).encode("utf-8")
    return files


def package_documents(root):
    """Writes the generated files of the CMake package below root: doc/ (documents())
    and integrations/ios-emulator/README.md. Unchanged files keep their time stamps."""
    written = []
    outputs = {Path("doc") / name: data for name, data in documents().items()}
    outputs[Path(INTEGRATION) / "README.md"] = integration_files()["README.md"]
    for relative, data in outputs.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.is_file() or path.read_bytes() != data:
            path.write_bytes(data)
            written.append(str(relative))
    for stale in sorted(p for p in (root / "doc").iterdir() if p.is_file() and Path("doc") / p.name not in outputs):
        stale.unlink()
    return written


def desktop_entry(executable):
    """The menu entry from assets/, its commands pointing at the installed launcher."""
    quoted = '"' + executable.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$").replace("%", "%%") + '"'
    lines = []
    for line in source_path(f"assets/{APP_ID}.desktop").read_text(encoding="utf-8").splitlines():
        if line.startswith("Exec=tomacco"):
            line = "Exec=" + quoted + line[len("Exec=tomacco"):]
        lines.append(line)
    return "\n".join(lines) + "\n"


HYPR_RULES = '''-- Tomacco: floating mobile workspace; F11 toggles fullscreen.
o.window({ class = "^io[.]github[.]marselnenaj[.]tomacco$", title = "negative:^Tomacco – .*$" }, {
  float = true,
  tile = false,
  center = true,
  opacity = "1.0 override 1.0 override",
  size = { 1380, 970 },
})

-- Detached device window: the app draws the phone itself, so the compositor
-- adds no border, shadow, rounding or blur and never resizes it.
o.window({ class = "^io[.]github[.]marselnenaj[.]tomacco$", title = "^Tomacco – .*$" }, {
  float = true,
  tile = false,
  border_size = 0,
  rounding = 0,
  no_shadow = true,
  no_blur = true,
  opacity = "1.0 override 1.0 override",
})
'''


# The keys --omarchy-bindings adds. Omarchy's own bindings leave
# SUPER + CTRL + SHIFT + a letter free, which is where these sit; the bindings
# file is the user's own, so the keys can be changed there.
HYPR_BINDINGS = '''-- Tomacco: keys for the mobile workspace. Omarchy's own bindings leave
-- SUPER + CTRL + SHIFT + a letter free. Change them here; an update keeps this file's name, not its edits.
o.bind("SUPER + CTRL + SHIFT + T", "Tomacco", [[$HOME/.local/bin/tomaccoctl call desktop.open '{"tab":"runtime"}']])
o.bind("SUPER + CTRL + SHIFT + P", "Tomacco device window", "omarchy-shell io.github.marselnenaj.tomacco window")
o.bind("SUPER + CTRL + SHIFT + S", "Tomacco device screenshot", "$HOME/.local/bin/tomaccoctl omarchy screenshot")
'''

# Tomacco's entries in Omarchy's menu (Trigger > Tomacco), without the marker
# lines. Each line ends in a comma, which the menu's reader allows.
MENU_ENTRIES = r'''  "trigger.tomacco": {"icon":"\uf10b","label":"Tomacco","aliases":["tomacco"]},
  "trigger.tomacco.open": {"icon":"\uf10b","label":"Open","action":"$HOME/.local/bin/tomaccoctl call desktop.open '{\"tab\":\"runtime\"}'"},
  "trigger.tomacco.devices": {"icon":"\uf10b","label":"Devices and emulators","action":"$HOME/.local/bin/tomaccoctl call desktop.open '{\"tab\":\"devices\"}'"},
  "trigger.tomacco.jobs": {"icon":"\uf10b","label":"Builds and jobs","action":"$HOME/.local/bin/tomaccoctl call desktop.open '{\"tab\":\"jobs\"}'"},
  "trigger.tomacco.iphone": {"icon":"\uf10b","label":"Start the virtual iPhone","when":"test -d \"$HOME/.config/omarchy/plugins/io.github.marselnenaj.tomacco\"","action":"omarchy-shell io.github.marselnenaj.tomacco iphone start"},
  "trigger.tomacco.window": {"icon":"\uf10b","label":"Device window","when":"test -d \"$HOME/.config/omarchy/plugins/io.github.marselnenaj.tomacco\"","action":"omarchy-shell io.github.marselnenaj.tomacco window"},
  "trigger.tomacco.screenshot": {"icon":"\uf10b","label":"Device screenshot","action":"$HOME/.local/bin/tomaccoctl omarchy screenshot"},
'''


def strip_jsonc(text):
    """Omarchy's reading of its menu files: whole-line comments and trailing
    commas go before the JSON is parsed."""
    text = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("//"))
    out, quoted, index = [], False, 0
    while index < len(text):
        char = text[index]
        if quoted:
            out.append(char)
            if char == "\\":
                index += 1
                out.append(text[index:index + 1])
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
            out.append(char)
        elif char == ",":
            following = text[index + 1:].lstrip()[:1]
            if following not in ("}", "]"):
                out.append(char)
        else:
            out.append(char)
        index += 1
    return "".join(out)


def menu_without_block(text):
    """The menu file's text without Tomacco's block, or None where it has none."""
    lines = text.split("\n")
    stripped = [line.strip() for line in lines]
    if MENU_BEGIN.strip() not in stripped:
        return None
    begin = stripped.index(MENU_BEGIN.strip())
    if MENU_END.strip() not in stripped[begin:]:
        return None
    end = begin + stripped[begin:].index(MENU_END.strip())
    return "\n".join(lines[:begin] + lines[end + 1:])


def menu_with_block(text, block):
    """The menu file's text with `block` in it: replacing Tomacco's earlier
    block, else right after the opening brace. None when the file is not an
    object of entries."""
    if not text.strip():
        return "{\n" + block + "}\n"
    without = menu_without_block(text)
    if without is not None:
        return menu_with_block(without, block)
    try:
        value = json.loads(strip_jsonc(text))
    except ValueError:
        return None
    if not isinstance(value, dict) or isinstance(value.get("items"), dict):
        return None
    offset = 0
    for line in text.splitlines(keepends=True):
        if not line.lstrip().startswith("//") and "{" in line:
            at = offset + line.index("{") + 1
            rest = text[at:]
            return text[:at] + "\n" + block + (rest[1:] if rest.startswith("\n") else rest)
        offset += len(line)
    return None


def launcher_script(name, target, portable=False):
    env = "export QT_QPA_PLATFORMTHEME=generic\nexport QT_QUICK_CONTROLS_STYLE=Basic\n" if name == "tomacco" else ""
    if portable and name == "tomacco":
        # The bundled Qt loads only its own plugins and QML modules.
        env += "unset QT_PLUGIN_PATH QML_IMPORT_PATH QML2_IMPORT_PATH\n"
    return "#!/bin/sh\n" + MARKER + "\n" + env + "exec " + shlex.quote(str(target / name)) + ' "$@"\n'


# Files are staged and published in batches of up to this many: the journal is
# written and the disk flushed once a batch, not once a file.
BATCH = 512
# The prefix of a file staged beside its destination and not yet published.
# One that a killed run left behind is deleted by the next run that stages there.
STAGED = ".tomacco-staged-"


def _syncfs():
    try:
        import ctypes
        function = ctypes.CDLL(None, use_errno=True).syncfs
        function.argtypes = [ctypes.c_int]
        return function
    except (OSError, AttributeError, ImportError):
        return None


SYNCFS = _syncfs()


def file_digest(path):
    # Not hashlib.file_digest: the installer also runs on Python 3.10
    # (Ubuntu 22.04), which lacks it.
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def make_durable(paths):
    """Gets the data and names of the files and directories in `paths` onto the
    disk: one syncfs(2) for each file system they are on or, where that is not
    available, one fsync for each path."""
    if SYNCFS is not None:
        devices = {}
        for path in paths:
            try:
                devices.setdefault(os.stat(path).st_dev, path)
            except FileNotFoundError:
                continue
        failed = False
        for path in devices.values():
            fd = os.open(path, os.O_RDONLY)
            try:
                failed = SYNCFS(fd) != 0
            finally:
                os.close(fd)
            if failed:
                break
        if not failed:
            return
    for path in paths:
        try:
            fd = os.open(path, os.O_RDONLY)
        except FileNotFoundError:
            continue
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


class Installer:
    """Changes files in a home through a write-ahead journal.

    write() and remove() stage a change: the new content goes into a temporary
    file beside the destination and the old content into this run's backup
    directory. flush(), which runs every BATCH changes and from finish(),
    records the batch in manifest.json, gets the staged files, the backups and
    the manifest onto the disk, and only then publishes the batch by renaming
    or unlinking. After an interruption each journalled file is either as it
    was before or as installed, and --rollback accepts both."""

    def __init__(self, user):
        self.user = user.resolve()
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        self.backup = self.user / BACKUPS / stamp
        self.changed = []
        self.records = []
        # The staged changes of the current batch, in order: (temporary file,
        # or None for a removal; destination; backup copy or None).
        self.pending = []
        self.pending_paths = set()
        self.swept = set()
        self.private = False
        # Whether changes wait for a full batch (see batch()) or are published one by one.
        self.batched = False
        self.journalled = 0

    def private_backup(self):
        """Creates this run's backup directory and the data directories above
        it owner-only: backups are copies of the user's configuration, and
        tomaccoctl keeps job records and credentials' paths beside them."""
        if self.private:
            return
        for directory in (self.user / USER_DATA, self.user / BACKUPS, self.backup):
            if not directory.is_dir():
                directory.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.mkdir(directory, 0o700)
                except FileExistsError:
                    pass
            if directory.stat().st_uid != os.geteuid():
                raise SystemExit(f"Refusing a backup directory this user does not own: {directory}")
            os.chmod(directory, 0o700)
        self.private = True

    def destination(self, relative, link=False):
        """The path in the home; with `link`, the path may be a symbolic link itself."""
        relative = Path(relative)
        if relative.is_absolute() or '..' in relative.parts or not relative.parts:
            raise SystemExit("Installation paths must stay inside the selected home")
        path = self.user / relative
        if not path.parent.resolve().is_relative_to(self.user) or (path.is_symlink() and not link):
            raise SystemExit(f"Refusing to follow an installation symlink: {path}")
        return path

    def sweep(self, directory):
        """Deletes the staged files an interrupted run left in `directory`,
        once a run: they were never published, so nothing refers to them."""
        if directory in self.swept:
            return
        self.swept.add(directory)
        for path in directory.glob(STAGED + "*"):
            if path.is_symlink() or path.is_file():
                path.unlink()

    def save(self, relative, path):
        """Keeps the current content of `path` in this run's backup. A file is
        only ever replaced by renaming over it or unlinked, never changed in
        place, so a hard link holds the old content; a copy where linking fails."""
        saved = self.backup / relative
        self.private_backup()
        saved.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(path, saved, follow_symlinks=False)
        except OSError:
            shutil.copy2(path, saved)
        return saved

    def stage(self, relative, path, temp, saved, after, before, extra=None):
        self.records.append({"path": str(relative), "before": before, "after": after, **(extra or {})})
        self.pending.append((temp, path, saved))
        self.pending_paths.add(path)
        if len(self.pending) >= (BATCH if self.batched else 1):
            self.flush()

    def write(self, relative, data, mode=0o644):
        path = self.destination(relative)
        if path in self.pending_paths:
            self.flush()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = data.encode() if isinstance(data, str) else data
        if path.is_symlink():
            raise SystemExit(f"Refusing to replace symlink: {path}")
        old = saved = None
        if path.exists():
            current = path.read_bytes()
            current_mode = path.stat().st_mode & 0o777
            if current == data and current_mode == mode:
                return
            saved = self.save(relative, path)
            old = {"sha256": hashlib.sha256(current).hexdigest(), "mode": current_mode}
        self.sweep(path.parent)
        fd, temp = tempfile.mkstemp(prefix=STAGED, dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(data)
            os.chmod(temp, mode)
        except BaseException:
            os.unlink(temp)
            raise
        self.stage(relative, path, temp, saved, {"sha256": hashlib.sha256(data).hexdigest(), "mode": mode}, old)

    def remove(self, relative):
        target = self.destination(relative)
        if target in self.pending_paths:
            self.flush()
        data = target.read_bytes()
        previous = {"sha256": hashlib.sha256(data).hexdigest(), "mode": target.stat().st_mode & 0o777}
        saved = self.save(relative, target)
        self.stage(relative, target, None, saved, None, previous)

    def link(self, relative, target):
        """Stages a symbolic link at `relative` to the absolute `target`. The
        journal entry's state is {"symlink": target}; directories the link
        needed are made now and listed under "created", so removing the link
        can remove them again."""
        path = self.destination(relative, link=True)
        if path in self.pending_paths:
            self.flush()
        if path.is_symlink() and os.readlink(path) == str(target):
            return
        if path.is_symlink() or path.exists():
            raise SystemExit(f"Refusing to replace {path}")
        missing = []
        parent = path.parent
        while not parent.is_dir():
            missing.append(parent)
            parent = parent.parent
        created = []
        for directory in reversed(missing):
            directory.mkdir()
            created.append(str(directory.relative_to(self.user)))
        self.sweep(path.parent)
        temp = path.parent / (STAGED + secrets.token_hex(8))
        os.symlink(target, temp)
        self.stage(relative, path, str(temp), None, {"symlink": str(target)}, None, {"created": created} if created else None)

    def unlink(self, relative):
        """Stages the removal of the symbolic link at `relative`."""
        path = self.destination(relative, link=True)
        if path in self.pending_paths:
            self.flush()
        self.stage(relative, path, None, None, None, {"symlink": os.readlink(path)})

    @staticmethod
    def state(target):
        """What a journal entry records for `target`: a symlink's target, a
        file's digest and mode, None when nothing is there, False for anything else."""
        if target.is_symlink():
            return {"symlink": os.readlink(target)}
        if target.is_file():
            return {"sha256": file_digest(target), "mode": target.stat().st_mode & 0o777}
        return False if target.exists() else None

    def remove_created(self, directories):
        """Removes the directories a link needed, deepest first, while they are empty."""
        self.flush()
        for relative in sorted(set(directories), key=lambda name: len(Path(name).parts), reverse=True):
            path = self.user / relative
            if path.is_dir() and not path.is_symlink() and not any(path.iterdir()):
                path.rmdir()

    def journal(self):
        """Writes manifest.json with every change recorded so far; the caller makes it durable."""
        self.private_backup()
        self.journalled = len(self.records)
        manifest = self.backup / 'manifest.json'
        fd, temporary = tempfile.mkstemp(prefix='.manifest-', dir=self.backup)
        try:
            with os.fdopen(fd, 'w') as output:
                json.dump({"format": 1, "created": self.backup.name,
                           "version": VERSION, "files": self.records}, output, indent=2)
                output.write('\n')
            os.replace(temporary, manifest)
        finally:
            if os.path.exists(temporary): os.unlink(temporary)
        return manifest

    def flush(self):
        """Journals the staged batch, makes it and its backups durable, then publishes it."""
        if not self.pending:
            return
        pending, self.pending, self.pending_paths = self.pending, [], set()
        try:
            manifest = self.journal()
            # Write-ahead: the new content, the old content and the journal
            # are on the disk before any destination changes.
            make_durable([manifest, self.backup, *(temp for temp, _, _ in pending if temp),
                          *(saved for _, _, saved in pending if saved)])
            directories = {}
            for index, (temp, path, _) in enumerate(pending):
                if temp:
                    os.replace(temp, path)
                else:
                    path.unlink()
                pending[index] = (None, path, None)
                directories[path.parent] = None
                self.changed.append(str(path))
            make_durable(list(directories))
        finally:
            for temp, _, _ in pending:
                if temp and os.path.lexists(temp):
                    os.unlink(temp)

    @contextlib.contextmanager
    def batch(self):
        """Stages changes in batches of BATCH until the block ends, which
        publishes the rest. Inside it a change is visible only after the next
        flush(); outside it each change is published at once."""
        self.batched = True
        try:
            yield self
            self.flush()
        finally:
            self.batched = False

    def finish(self):
        self.flush()
        if self.records and self.journalled != len(self.records):
            make_durable([self.journal(), self.backup])
        return self.backup if self.records else None

    def rollback(self, identifier):
        if not identifier or Path(identifier).name != identifier or identifier in ('.', '..'):
            raise SystemExit("Use a backup ID from --list-backups")
        source = self.user / BACKUPS / identifier
        manifest_path = source / 'manifest.json'
        if not manifest_path.is_file():
            raise SystemExit("This backup has no rollback manifest; its files remain available for manual recovery")
        manifest = json.loads(manifest_path.read_text())
        if manifest.get('format') != 1 or not isinstance(manifest.get('files'), list):
            raise SystemExit("Unsupported backup manifest")
        changes = []
        # Validate the complete restore before changing any current file.
        for entry in manifest['files']:
            relative = Path(entry['path'])
            symlink = any(isinstance(state, dict) and "symlink" in state for state in (entry['before'], entry['after']))
            target = self.destination(relative, link=symlink)
            current = self.state(target)
            if current is False:
                raise SystemExit(f"Preserved a file changed since installation: {target}")
            if current == entry['before']:
                # A journalled write may not have been published before interruption.
                continue
            if current != entry['after']:
                raise SystemExit(f"Preserved a file changed since installation: {target}")
            previous = entry['before']
            content = None
            if symlink:
                changes.append((relative, target, previous, entry))
                continue
            if previous:
                saved = source / relative
                if saved.is_symlink() or not saved.resolve().is_relative_to(source.resolve()) or not saved.is_file():
                    raise SystemExit(f"Missing backup file: {relative}")
                content = saved
                if file_digest(saved) != previous['sha256']:
                    raise SystemExit(f"Backup checksum mismatch: {relative}")
            changes.append((relative, target, previous, content))
        # The backups are read again one at a time, not held in memory together.
        created = []
        with self.batch():
            for relative, target, previous, saved in changes:
                if isinstance(saved, dict):
                    # A symbolic link: saved is its journal entry.
                    if previous:
                        self.link(relative, previous["symlink"])
                    else:
                        self.unlink(relative)
                        created += saved.get("created", [])
                elif previous:
                    content = saved.read_bytes()
                    if hashlib.sha256(content).hexdigest() != previous['sha256']:
                        raise SystemExit(f"Backup checksum mismatch: {relative}")
                    self.write(relative, content, previous['mode'])
                else:
                    # Only a file recorded as newly created by this installation is removed.
                    self.remove(relative)
        self.remove_created(created)
        self.finish()
        return {"restored": identifier, "changed": len(self.changed), "backups": str(self.backup)}

    def launchers(self, portable=False):
        target = self.user / ".local/lib/tomacco"
        for name in ("tomacco", "tomaccoctl"):
            launcher = self.user / ".local/bin" / name
            if launcher.exists() and MARKER not in launcher.read_text():
                raise SystemExit(f"Refusing to replace another command: {launcher}")
            self.write(Path(".local/bin") / name, launcher_script(name, target, portable), 0o755)

    def runtime(self, source):
        """Installs a portable package's bundled libraries, or none, and removes
        what an earlier installation's runtime left that this one does not have:
        a regular package must not find qt.conf pointing at another Qt. A file
        changed since it was installed is kept."""
        app = Path(".local/lib/tomacco")
        keep = set()
        if source is not None:
            for path in sorted(p for p in source.rglob("*") if p.is_file()):
                relative = app / "runtime" / path.relative_to(source)
                self.write(relative, path.read_bytes(), 0o755 if os.access(path, os.X_OK) else 0o644)
                keep.add(relative)
            self.write(app / "qt.conf", QT_CONF)
            keep.add(app / "qt.conf")
        self.flush()
        for recorded, entries in sorted(self.history().items()):
            relative = Path(recorded)
            if relative in keep:
                continue
            if relative != app / "qt.conf" and not relative.is_relative_to(app / "runtime"):
                continue
            if self.as_installed(entries, self.user / relative):
                self.remove(relative)
        self.prune(app / "runtime")

    def prune(self, directory):
        """Publishes what is staged, then removes the empty directories below
        `directory` and it, and the staged files an interrupted run left there."""
        self.flush()
        root = self.user / directory
        if not root.is_dir() or root.is_symlink():
            return
        for path in sorted((p for p in [root, *root.rglob("*")] if p.is_dir() and not p.is_symlink()), key=lambda p: len(p.parts), reverse=True):
            for staged in path.glob(STAGED + "*"):
                if staged.is_file() and not staged.is_symlink():
                    staged.unlink()
            if not any(path.iterdir()):
                path.rmdir()

    def agent_skill(self, link=True, update=False):
        """Installs the agent skill beside the program and, with `link`, links
        it into ~/.claude/skills, ~/.codex/skills and ~/.agents/skills where
        that agent's configuration directory exists. An entry already there
        that is not Tomacco's own link is left alone. An update adds no link
        the person removed after an earlier installation made it. Without
        `link`, links to the installed skill are removed."""
        source = source_path("integrations/agent-skill") / "tomacco"
        keep = set()
        for path in sorted(p for p in source.rglob("*") if p.is_file()):
            relative = SKILL / path.relative_to(source)
            self.write(relative, path.read_bytes())
            keep.add(relative)
        self.flush()
        history = self.history()
        for recorded, entries in sorted(history.items()):
            relative = Path(recorded)
            if relative.is_relative_to(SKILL) and relative not in keep and self.as_installed(entries, self.user / relative):
                self.remove(relative)
        self.prune(SKILL)
        if not link:
            self.remove_skill_links(history)
            return {"linked": [], "skipped": []}
        target = str(self.user / SKILL)
        linked, skipped = [], []
        for relative in AGENT_SKILL_LINKS:
            path = self.user / relative
            if not path.parent.parent.is_dir():
                continue
            try:
                self.destination(relative, link=True)
            except SystemExit:
                skipped.append(str(path))
                continue
            entries = history.get(str(relative), [])
            if path.is_symlink():
                current = os.readlink(path)
                if self.ours(path):
                    linked.append(str(path))
                    continue
                if not entries or entries[-1].get("after") != {"symlink": current}:
                    skipped.append(str(path))
                    continue
                # Tomacco's own link from an installation in another place.
                self.unlink(relative)
            elif path.exists():
                skipped.append(str(path))
                continue
            elif update and entries:
                continue
            self.link(relative, target)
            linked.append(str(path))
        return {"linked": linked, "skipped": skipped}

    def ours(self, path):
        """Whether the symbolic link at `path` points at the installed skill."""
        target = self.user / SKILL
        return path.is_symlink() and (os.readlink(path) == str(target)
                                      or (target.is_dir() and os.path.realpath(path) == os.path.realpath(target)))

    def remove_skill_links(self, history=None):
        """Removes the agent links to the installed skill, whoever made them,
        and the skills directories an installation created for them once empty."""
        history = self.history() if history is None else history
        created = []
        for relative in AGENT_SKILL_LINKS:
            path = self.user / relative
            try:
                self.destination(relative, link=True)
            except SystemExit:
                continue
            if self.ours(path):
                self.unlink(relative)
                created += [name for entry in history.get(str(relative), []) for name in entry.get("created", [])]
        self.remove_created(created)

    def widget_placed(self):
        """Whether the bar names the widget."""
        try:
            layout = json.loads((self.user / ".config/omarchy/shell.json").read_text())["bar"]["layout"]
            return any((item if isinstance(item, str) else item.get("id")) == APP_ID
                       for items in layout.values() if isinstance(items, list) for item in items)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return False

    def omarchy(self, enable=True):
        config = self.user / ".config/omarchy/shell.json"
        if not config.is_file():
            raise SystemExit("Omarchy shell.json was not found. Install without --omarchy on this desktop.")
        value = json.loads(config.read_text())
        layout = value.get("bar", {}).get("layout")
        if not isinstance(layout, dict):
            default = Path("/usr/share/omarchy/config/omarchy/shell.json")
            if not default.is_file():
                raise SystemExit("No Omarchy bar layout found; configure a bar first.")
            layout = json.loads(default.read_text())["bar"]["layout"]
            value.setdefault("bar", {})["layout"] = layout
        if not any((item if isinstance(item, str) else item.get("id")) == APP_ID for items in layout.values() if isinstance(items, list) for item in items):
            right = layout.setdefault("right", [])
            # Beside the bar's agents widget (omarchy.agents on the default
            # bar, whoever provides it here), else first in the section.
            identifiers = [item if isinstance(item, str) else item.get("id") if isinstance(item, dict) else None for item in right]
            index = next((i + 1 for i, identifier in enumerate(identifiers) if isinstance(identifier, str) and identifier.rpartition(".")[2] == "agents"), 0)
            right.insert(index, {"id": APP_ID})
        # An update nobody asked to enable anything for: a disabled widget stays disabled.
        if enable and "disabledPlugins" in value:
            value["disabledPlugins"] = [item for item in value["disabledPlugins"] if item != APP_ID]
        # The shell compiles a plugin file once per URL and keeps that result
        # until it restarts. Each build therefore gets its own directory; the
        # shell's change watcher then loads an upgrade within a second. (Qt
        # rejects other file names for the component, so the name stays.)
        source = source_path("integrations/omarchy")
        plugin = Path(".config/omarchy/plugins") / APP_ID
        panel = (source / "MobilePanel.qml").read_bytes()
        build = hashlib.sha256(panel).hexdigest()[:12]
        entry = f"{build}/MobilePanel.qml"
        manifest = json.loads((source / "manifest.json").read_text())
        manifest["entryPoints"]["barWidget"] = entry
        self.write(plugin / entry, panel.replace(b"@TOMACCO_PANEL_BUILD@", build.encode()))
        for extra in sorted(source.iterdir()):
            if extra.is_file() and extra.name not in ("MobilePanel.qml", "manifest.json"):
                self.write(plugin / extra.name, extra.read_bytes())
        # The manifest switches the entry point only after its file exists.
        self.write(plugin / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        # Remove earlier copies of the widget, and nothing else in this directory:
        # the file an older installer put at the top level, and its build directories.
        # A user's own MobilePanelLocal.qml or MobilePanel2.qml is not ours to delete.
        installed = self.user / plugin
        stale = [installed / "MobilePanel.qml"]
        stale += [path for path in installed.glob("*/MobilePanel.qml")
                  if re.fullmatch(r"[0-9a-f]{12}", path.parent.name) and path.parent.name != build]
        for path in sorted(stale):
            if path.is_file() and not path.is_symlink() and not path.parent.is_symlink():
                self.remove(path.relative_to(self.user))
                self.flush()
                if path.parent != installed and not any(path.parent.iterdir()):
                    path.parent.rmdir()
        self.write(Path(".config/omarchy/shell.json"), json.dumps(value, ensure_ascii=False, indent=2) + "\n")

    def history(self):
        """Every file the installations recorded, with its journal entries, oldest first."""
        history = {}
        for manifest in self.manifests():
            for entry in manifest["files"]:
                history.setdefault(entry["path"], []).append(entry)
        return history

    @staticmethod
    def as_installed(entries, target):
        """Whether `target` is exactly as an installation left it, by its
        journal entries. The newest entry may never have been published, when
        its run was interrupted: then the file is as the entry found it."""
        if not target.is_file() or target.is_symlink():
            return False
        digest = file_digest(target)
        newest = entries[-1]
        if newest.get("after") is not None:
            if newest["after"]["sha256"] == digest:
                return True
            before = newest.get("before")
            # An interrupted update: the file is what the installation before wrote.
            return bool(before and before["sha256"] == digest and len(entries) > 1
                        and (entries[-2].get("after") or {}).get("sha256") == digest)
        # A removal is journalled only for a file as installed; an interrupted
        # one left that file in place.
        return bool(newest.get("before") and newest["before"]["sha256"] == digest)

    def manifests(self):
        """Every journalled installation, rollback and uninstallation, oldest first."""
        found = []
        for path in sorted((self.user / BACKUPS).glob("*/manifest.json")):
            try:
                value = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if value.get("format") == 1 and isinstance(value.get("files"), list):
                found.append(value)
        return found

    def uninstall(self):
        """Remove what the installations wrote, as recorded in their journals.

        Files Tomacco owns are removed only while they are exactly as an
        installation left them; a file the user changed since is kept and
        reported. The edits to the user's own Hyprland and Omarchy
        configuration are undone in place. User data under
        ~/.local/share/tomacco (projects, device profiles, settings and
        these backups) is never touched, and the uninstallation is itself
        journalled, so --rollback with its ID reinstalls everything it removed.
        """
        history = self.history()
        kept = []
        # The user's own configuration first, so it never names a removed file.
        self.unedit_window_rule()
        self.unedit_omarchy()
        self.unedit_menu()
        self.remove_skill_links(history)
        with self.batch():
            for relative, entries in sorted(history.items()):
                relative = Path(relative)
                if relative.is_relative_to(USER_DATA) or not any(relative == owned or relative.is_relative_to(owned) for owned in OWNED):
                    continue
                if (self.user / relative).is_symlink():
                    kept.append(str(self.user / relative))
                    continue
                try:
                    target = self.destination(relative)
                except SystemExit:
                    kept.append(str(self.user / relative))
                    continue
                if not target.is_file():
                    continue
                if not self.as_installed(entries, target):
                    kept.append(str(target))
                    continue
                self.remove(relative)
        for directory in OWNED_DIRECTORIES:
            self.prune(directory)
        self.finish()
        return {"removed": len(self.changed), "kept": kept,
                "backups": str(self.backup) if self.records else None,
                "user_data": str(self.user / USER_DATA)}

    def unedit_window_rule(self):
        config = self.user / ".config/hypr/hyprland.lua"
        if not config.is_file() or config.is_symlink():
            return
        text = config.read_text()
        ours = (HYPR_REQUIRE, HYPR_COMMENT, HYPR_BINDINGS_REQUIRE, HYPR_BINDINGS_COMMENT)
        if HYPR_REQUIRE not in text and HYPR_BINDINGS_REQUIRE not in text:
            return
        lines = [line for line in text.splitlines() if line.strip() not in ours]
        self.write(Path(".config/hypr/hyprland.lua"), "\n".join(lines).rstrip() + "\n", config.stat().st_mode & 0o777)

    def unedit_omarchy(self):
        config = self.user / ".config/omarchy/shell.json"
        if not config.is_file() or config.is_symlink():
            return
        try:
            value = json.loads(config.read_text())
        except ValueError:
            return
        layout = value.get("bar", {}).get("layout") if isinstance(value.get("bar"), dict) else None
        if not isinstance(layout, dict):
            return
        ours = lambda item: (item if isinstance(item, str) else item.get("id") if isinstance(item, dict) else None) == APP_ID
        if not any(ours(item) for items in layout.values() if isinstance(items, list) for item in items):
            return
        for key, items in layout.items():
            if isinstance(items, list):
                layout[key] = [item for item in items if not ours(item)]
        self.write(Path(".config/omarchy/shell.json"), json.dumps(value, ensure_ascii=False, indent=2) + "\n", config.stat().st_mode & 0o777)

    def window_rule(self, bindings=False):
        """Install the rules, and with `bindings` the keys, and require them,
        once: a rollback checks each journal entry against the file, so
        hyprland.lua gets one write a run."""
        config = self.user / ".config/hypr/hyprland.lua"
        if not config.is_file():
            return
        self.write(Path(".config/hypr/tomacco.lua"), HYPR_RULES)
        if bindings:
            self.write(Path(".config/hypr/tomacco-bindings.lua"), HYPR_BINDINGS)
        text = config.read_text()
        edited = text
        if HYPR_REQUIRE not in text:
            edited = edited.rstrip() + "\n\n" + HYPR_COMMENT + "\n" + HYPR_REQUIRE + "\n"
        if bindings and HYPR_BINDINGS_REQUIRE not in text:
            edited = edited.rstrip() + "\n\n" + HYPR_BINDINGS_COMMENT + "\n" + HYPR_BINDINGS_REQUIRE + "\n"
        if edited != text:
            self.write(Path(".config/hypr/hyprland.lua"), edited, config.stat().st_mode & 0o777)

    def menu_in_use(self):
        """Whether the menu extensions hold Tomacco's entries."""
        try:
            return menu_without_block((self.user / MENU_FILE).read_text()) is not None
        except OSError:
            return False

    def menu(self):
        """Add Tomacco's entries to Omarchy's menu extensions, between marker
        lines, or replace them there. A file that is not a JSON object, or
        that wraps its entries in "items", is left alone; returns False then."""
        path = self.user / MENU_FILE
        if path.is_symlink():
            return False
        text = path.read_text() if path.is_file() else ""
        edited = menu_with_block(text, MENU_BEGIN + "\n" + MENU_ENTRIES + MENU_END + "\n")
        if edited is None:
            return False
        if edited != text:
            self.write(MENU_FILE, edited, path.stat().st_mode & 0o777 if path.is_file() else 0o644)
        return True

    def unedit_menu(self):
        """Take Tomacco's menu entries out again; a file left without any entry goes with them."""
        path = self.user / MENU_FILE
        if not path.is_file() or path.is_symlink():
            return
        edited = menu_without_block(path.read_text())
        if edited is None:
            return
        if "".join(strip_jsonc(edited).split()) == "{}":
            self.remove(MENU_FILE)
        else:
            self.write(MENU_FILE, edited, path.stat().st_mode & 0o777)

    def panel_source(self, source):
        """Keep the panel's sources beside the program, for `omarchy repair`."""
        if source.is_dir():
            for path in sorted(source.iterdir()):
                if path.is_file():
                    self.write(PANEL_SOURCE / path.name, path.read_bytes())


# Set to 1, the Python installer runs even where a tomaccoctl that installs is found.
PYTHON_ENVIRONMENT = "TOMACCO_INSTALL_PYTHON"


def native_installer(args):
    """The tomaccoctl to hand over to, with the arguments it needs before this
    file's own, or None. A package's own bin/tomaccoctl, the one beside an
    installed copy of this file and, to roll back or uninstall, the one
    installed in the home; each only when it answers `_install --version`
    (tomaccoctl 1.3 and later). A checkout keeps the Python installer: it
    generates the documents it installs from their sources."""
    if os.environ.get(PYTHON_ENVIRONMENT) == "1":
        return None
    if PACKAGED:
        candidates = [(ROOT / "bin/tomaccoctl", ["--package", str(ROOT)])]
    elif INSTALLED:
        candidates = [(ROOT / "tomaccoctl", [])]
    else:
        return None
    if args.rollback or args.uninstall or args.list_backups:
        candidates.append((args.user_home / ".local/lib/tomacco/tomaccoctl", []))
    for program, extra in candidates:
        try:
            probe = subprocess.run([str(program), "_install", "--version"], stdin=subprocess.DEVNULL,
                                   capture_output=True, text=True, timeout=60, check=False)
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0 and probe.stdout.startswith("Tomacco installer "):
            return [str(program), "_install", *extra]
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=ROOT / ("bin/tomacco" if PACKAGED else "build/tomacco"))
    parser.add_argument("--cli", type=Path, help="Native tomaccoctl binary; defaults to the desktop binary's directory")
    parser.add_argument("--user-home", type=Path, default=Path.home(), help="Alternate home for packaging/installation tests")
    parser.add_argument("--omarchy", action="store_true", help="Install and enable the Omarchy bar/popup plugin, and add Tomacco to Omarchy's menu")
    parser.add_argument("--omarchy-bindings", action="store_true",
                        help="Add Tomacco's keys (SUPER + CTRL + SHIFT + T, P, S) as a Hyprland bindings file")
    parser.add_argument("--no-omarchy-menu", action="store_true", help="With --omarchy, leave Omarchy's menu as it is")
    parser.add_argument("--no-window-rule", action="store_true")
    parser.add_argument("--no-agent-skill", action="store_true",
                        help="Do not link the agent skill into ~/.claude/skills, ~/.codex/skills and ~/.agents/skills, and remove such links an earlier installation made")
    parser.add_argument("--update", action="store_true", help="Keep the integrations as they are: refresh the Omarchy panel and window rules only where they are already in use")
    parser.add_argument("--list-backups", action="store_true", help="List installations with a verified rollback manifest")
    parser.add_argument("--rollback", metavar="BACKUP_ID", help="Restore an installation, preserving files subsequently edited by the user")
    parser.add_argument("--uninstall", action="store_true", help="Remove the installed files and integrations; keeps user data, device profiles, backups and files you changed")
    parser.add_argument("--version", action="version", version=f"Tomacco installer {VERSION}")
    parser.add_argument("--package-docs", type=Path, metavar="DIRECTORY", help="Write the generated documents a package installs below DIRECTORY and exit (used by CMake)")
    args = parser.parse_args()
    if args.package_docs:
        if PACKAGED:
            parser.error("--package-docs needs the source tree.")
        print(json.dumps({"written": package_documents(args.package_docs)}));return
    native = native_installer(args)
    if native:
        import sys
        os.execv(native[0], native + sys.argv[1:])
    if args.list_backups:
        entries=[]
        for manifest in sorted((args.user_home / BACKUPS).glob('*/manifest.json')):
            value=json.loads(manifest.read_text())
            entries.append({"id":manifest.parent.name,"version":value.get('version'),"files":len(value.get('files',[]))})
        print(json.dumps(entries,indent=2));return
    if args.rollback:
        print(json.dumps(Installer(args.user_home).rollback(args.rollback),indent=2));return
    if args.uninstall:
        installer = Installer(args.user_home)
        result = installer.uninstall()
        if installer.user == Path.home() and shutil.which("update-desktop-database"):
            subprocess.run(["update-desktop-database", str(installer.user / ".local/share/applications")], check=False)
        print(json.dumps(result, indent=2));return
    if INSTALLED:
        parser.error("This installed copy only uninstalls (--uninstall), rolls back (--rollback) and lists backups "
                     "(--list-backups). Install from a release package or a checkout.")
    if not args.binary.is_file():
        parser.error(f"No desktop binary at {args.binary}. Build with CMake first or pass --binary.")
    cli = args.cli or args.binary.resolve().parent / "tomaccoctl"
    if not cli.is_file() or cli.read_bytes()[:4] != b"\x7fELF":
        parser.error("Build the Rust control plane first, or pass --cli pointing to its native ELF binary.")
    runtime = ROOT / RUNTIME if PACKAGED and (ROOT / RUNTIME).is_dir() else None
    installer = Installer(args.user_home)
    # The program's own files in batches; the integrations below edit the
    # user's configuration one file at a time, in order.
    with installer.batch():
        installer.launchers(portable=runtime is not None)
        app = Path(".local/lib/tomacco")
        installer.write(app / "tomacco", args.binary.read_bytes(), 0o755)
        installer.write(app / "tomaccoctl", cli.read_bytes(), 0o755)
        installer.runtime(runtime)
        for source in sorted(source_path("tools").glob("*.py")):
            installer.write(app / "tools" / source.name, source.read_bytes(), 0o755)
        for source in sorted(source_path("examples/ios-restore-native").iterdir()):
            if source.is_file():
                installer.write(app / "examples/ios-restore-native" / source.name, source.read_bytes())
        for name, data in integration_files().items():
            installer.write(app / INTEGRATION / name, data)
        for source in sorted(source_path("assets").glob("*.json")):
            installer.write(app / "assets" / source.name, source.read_bytes())
        installer.write(app / "VERSION", VERSION + "\n")
        installer.write(app / "install.py", Path(__file__).read_bytes(), 0o755)
        # The guides and legal documents; the About dialog opens LICENSE,
        # THIRD_PARTY_NOTICES.md and legal.md from here.
        doc = Path(".local/share/doc/tomacco")
        for name, data in documents().items():
            installer.write(doc / name, data)
        installer.write(MANUAL_PAGE, source_path("docs/tomaccoctl.1").read_bytes())
        installer.write(Path(".local/share/icons/hicolor/scalable/apps") / (APP_ID + ".svg"), source_path("assets/icon.svg").read_bytes())
        launcher = installer.user / ".local/bin/tomacco"
        installer.write(Path(".local/share/applications") / (APP_ID + ".desktop"), desktop_entry(str(launcher)))
        # The software-center listing; it names the menu entry above.
        installer.write(Path(".local/share/metainfo") / (APP_ID + ".metainfo.xml"), source_path(f"assets/{APP_ID}.metainfo.xml").read_bytes())
    skill = installer.agent_skill(link=not args.no_agent_skill, update=args.update)
    if args.omarchy:
        installer.omarchy()
    elif args.update and installer.widget_placed():
        installer.omarchy(enable=False)
    if args.omarchy or (args.update and installer.widget_placed()):
        installer.panel_source(source_path("integrations/omarchy"))
    if (args.omarchy and not args.no_omarchy_menu) or (args.update and installer.menu_in_use()):
        installer.menu()
    bindings = args.omarchy_bindings or (args.update and (installer.user / ".config/hypr/tomacco-bindings.lua").exists())
    if not args.no_window_rule and (not args.update or bindings or (installer.user / ".config/hypr/tomacco.lua").exists()):
        installer.window_rule(bindings)
    installer.finish()
    if installer.user == Path.home() and shutil.which("update-desktop-database"):
        subprocess.run(["update-desktop-database", str(installer.user / ".local/share/applications")], check=True)
    print(json.dumps({"installed": str(launcher), "cli": str(installer.user / ".local/bin/tomaccoctl"),
                      "changed": len(installer.changed), "backups": str(installer.backup) if installer.backup.exists() else None,
                      "omarchy": args.omarchy, "agent_skill": skill}, indent=2))


if __name__ == "__main__":
    main()

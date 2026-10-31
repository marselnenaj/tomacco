#!/usr/bin/env python3
"""Install the desktop, control plane and optional Omarchy panel for one user."""
import argparse
import datetime
import hashlib
import json
import os
import re
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
         Path(".config/omarchy/plugins") / APP_ID, Path(".config/hypr/tomacco.lua"))
# Directories the installation creates for itself, removed once empty.
OWNED_DIRECTORIES = (Path(".local/lib/tomacco"), Path(".local/share/doc/tomacco"),
                     Path(".config/omarchy/plugins") / APP_ID)
HYPR_REQUIRE = 'require("hypr.tomacco")'
HYPR_COMMENT = "-- Tomacco mobile workspace."


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


def launcher_script(name, target, portable=False):
    env = "export QT_QPA_PLATFORMTHEME=generic\nexport QT_QUICK_CONTROLS_STYLE=Basic\n" if name == "tomacco" else ""
    if portable and name == "tomacco":
        # The bundled Qt loads only its own plugins and QML modules.
        env += "unset QT_PLUGIN_PATH QML_IMPORT_PATH QML2_IMPORT_PATH\n"
    return "#!/bin/sh\n" + MARKER + "\n" + env + "exec " + shlex.quote(str(target / name)) + ' "$@"\n'


class Installer:
    def __init__(self, user):
        self.user = user.resolve()
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        self.backup = self.user / BACKUPS / stamp
        self.changed = []
        self.records = []

    def private_backup(self):
        """Creates this run's backup directory and the data directories above
        it owner-only: backups are copies of the user's configuration, and
        tomaccoctl keeps job records and credentials' paths beside them."""
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

    def destination(self, relative):
        relative = Path(relative)
        if relative.is_absolute() or '..' in relative.parts or not relative.parts:
            raise SystemExit("Installation paths must stay inside the selected home")
        path = self.user / relative
        if not path.parent.resolve().is_relative_to(self.user) or path.is_symlink():
            raise SystemExit(f"Refusing to follow an installation symlink: {path}")
        return path

    def write(self, relative, data, mode=0o644):
        path = self.destination(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = data.encode() if isinstance(data, str) else data
        if path.is_symlink():
            raise SystemExit(f"Refusing to replace symlink: {path}")
        old = None
        if path.exists():
            if path.read_bytes() == data and path.stat().st_mode & 0o777 == mode:
                return
            saved = self.backup / relative
            self.private_backup()
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, saved)
            old = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "mode": path.stat().st_mode & 0o777}
        fd, temp = tempfile.mkstemp(prefix=".tomacco-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
            os.chmod(temp, mode)
            # Write-ahead journal: an interrupted installation can be restored.
            self.records.append({"path": str(relative), "before": old,
                                 "after": {"sha256": hashlib.sha256(data).hexdigest(), "mode": mode}})
            self.finish()
            os.replace(temp, path)
            self.changed.append(str(path))
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    def finish(self):
        if self.records:
            self.private_backup()
            manifest = self.backup / 'manifest.json'
            fd, temporary = tempfile.mkstemp(prefix='.manifest-', dir=self.backup)
            try:
                with os.fdopen(fd, 'w') as output:
                    json.dump({"format": 1, "created": self.backup.name,
                               "version": VERSION, "files": self.records}, output, indent=2)
                    output.write('\n'); output.flush(); os.fsync(output.fileno())
                os.replace(temporary, manifest)
            finally:
                if os.path.exists(temporary): os.unlink(temporary)
        return self.backup if self.records else None

    def remove(self, relative):
        target = self.destination(relative)
        data = target.read_bytes()
        previous = {"sha256": hashlib.sha256(data).hexdigest(), "mode": target.stat().st_mode & 0o777}
        saved = self.backup / relative
        self.private_backup()
        saved.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, saved)
        self.records.append({"path": str(relative), "before": previous, "after": None})
        self.finish()
        target.unlink(); self.changed.append(str(target))

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
            relative = Path(entry['path']); target = self.destination(relative)
            if target.exists() and not target.is_file():
                raise SystemExit(f"Preserved a file changed since installation: {target}")
            current = {"sha256": hashlib.sha256(target.read_bytes()).hexdigest(), "mode": target.stat().st_mode & 0o777} if target.is_file() else None
            if current == entry['before']:
                # A journalled write may not have been published before interruption.
                continue
            if current != entry['after']:
                raise SystemExit(f"Preserved a file changed since installation: {target}")
            previous = entry['before']
            content = None
            if previous:
                saved = source / relative
                if saved.is_symlink() or not saved.resolve().is_relative_to(source.resolve()) or not saved.is_file():
                    raise SystemExit(f"Missing backup file: {relative}")
                content = saved.read_bytes()
                if hashlib.sha256(content).hexdigest() != previous['sha256']:
                    raise SystemExit(f"Backup checksum mismatch: {relative}")
            changes.append((relative, target, previous, content))
        for relative, target, previous, content in changes:
            if previous:
                self.write(relative, content, previous['mode'])
            else:
                # Only a file recorded as newly created by this installation is removed.
                self.remove(relative)
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
        for recorded, entry in sorted(self.latest().items()):
            relative = Path(recorded)
            if relative in keep or entry.get("after") is None:
                continue
            if relative != app / "qt.conf" and not relative.is_relative_to(app / "runtime"):
                continue
            target = self.user / relative
            if target.is_file() and not target.is_symlink() and \
                    hashlib.sha256(target.read_bytes()).hexdigest() == entry["after"]["sha256"]:
                self.remove(relative)
        root = self.user / app / "runtime"
        if root.is_dir() and not root.is_symlink():
            for path in sorted((p for p in root.rglob("*") if p.is_dir() and not p.is_symlink()), key=lambda p: len(p.parts), reverse=True):
                if not any(path.iterdir()):
                    path.rmdir()
            if not any(root.iterdir()):
                root.rmdir()

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
                if path.parent != installed and not any(path.parent.iterdir()):
                    path.parent.rmdir()
        self.write(Path(".config/omarchy/shell.json"), json.dumps(value, ensure_ascii=False, indent=2) + "\n")

    def latest(self):
        """The newest journal entry of every file the installations recorded."""
        latest = {}
        for manifest in self.manifests():
            for entry in manifest["files"]:
                latest[entry["path"]] = entry
        return latest

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
        latest = self.latest()
        kept = []
        # The user's own configuration first, so it never names a removed file.
        self.unedit_window_rule()
        self.unedit_omarchy()
        for relative, entry in sorted(latest.items()):
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
            current = {"sha256": hashlib.sha256(target.read_bytes()).hexdigest(), "mode": target.stat().st_mode & 0o777}
            if entry.get("after") is None or current["sha256"] != entry["after"]["sha256"]:
                kept.append(str(target))
                continue
            self.remove(relative)
        for directory in OWNED_DIRECTORIES:
            root = self.user / directory
            if root.is_dir() and not root.is_symlink():
                for path in sorted((p for p in root.rglob("*") if p.is_dir() and not p.is_symlink()), key=lambda p: len(p.parts), reverse=True):
                    if not any(path.iterdir()):
                        path.rmdir()
                if not any(root.iterdir()):
                    root.rmdir()
        self.finish()
        return {"removed": len(self.changed), "kept": kept,
                "backups": str(self.backup) if self.records else None,
                "user_data": str(self.user / USER_DATA)}

    def unedit_window_rule(self):
        config = self.user / ".config/hypr/hyprland.lua"
        if not config.is_file() or config.is_symlink():
            return
        text = config.read_text()
        ours = (HYPR_REQUIRE, HYPR_COMMENT)
        if HYPR_REQUIRE not in text:
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

    def window_rule(self):
        """Install the rules and require them, once: a rollback checks each
        journal entry against the file, so hyprland.lua gets one write a run."""
        config = self.user / ".config/hypr/hyprland.lua"
        if not config.is_file():
            return
        self.write(Path(".config/hypr/tomacco.lua"), HYPR_RULES)
        text = config.read_text()
        if HYPR_REQUIRE not in text:
            self.write(Path(".config/hypr/hyprland.lua"), text.rstrip() + "\n\n" + HYPR_COMMENT + "\n" + HYPR_REQUIRE + "\n", config.stat().st_mode & 0o777)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=ROOT / ("bin/tomacco" if PACKAGED else "build/tomacco"))
    parser.add_argument("--cli", type=Path, help="Native tomaccoctl binary; defaults to the desktop binary's directory")
    parser.add_argument("--user-home", type=Path, default=Path.home(), help="Alternate home for packaging/installation tests")
    parser.add_argument("--omarchy", action="store_true", help="Install and enable the Omarchy bar/popup plugin")
    parser.add_argument("--no-window-rule", action="store_true")
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
    if args.omarchy:
        installer.omarchy()
    elif args.update and installer.widget_placed():
        installer.omarchy(enable=False)
    if not args.no_window_rule and (not args.update or (installer.user / ".config/hypr/tomacco.lua").exists()):
        installer.window_rule()
    installer.finish()
    if installer.user == Path.home() and shutil.which("update-desktop-database"):
        subprocess.run(["update-desktop-database", str(installer.user / ".local/share/applications")], check=True)
    print(json.dumps({"installed": str(launcher), "cli": str(installer.user / ".local/bin/tomaccoctl"),
                      "changed": len(installer.changed), "backups": str(installer.backup) if installer.backup.exists() else None,
                      "omarchy": args.omarchy}, indent=2))


if __name__ == "__main__":
    main()

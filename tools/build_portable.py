#!/usr/bin/env python3
"""Build the portable release package in a container, and check it in clean
containers of other distributions.

The portable package is the release package with the desktop's Qt 6, Qt
WebEngine and FFmpeg libraries bundled, built on Ubuntu 22.04 (glibc 2.35), so
it runs on distributions whose own Qt or FFmpeg is too old or too new for the
regular package (built on Arch). Its layout is the regular one plus
lib/tomacco/runtime, and install.py installs it the same way.

    tools/build_portable.py build                 # -> build-portable/tomacco-VERSION-Linux-x86_64-portable.tar.gz
    tools/build_portable.py verify build-portable/tomacco-VERSION-Linux-x86_64-portable.tar.gz

`build` needs Docker and network access (the builder image's pinned downloads,
crates.io). The image is tagged tomacco-portable-builder:<digest of its inputs>.
"""
import argparse
import hashlib
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTEXT = ROOT / "packaging/portable"
IMAGE = "tomacco-portable-builder"
OUTPUT = "build-portable"
QT = Path("/opt/qt")
FFMPEG = Path("/opt/ffmpeg")
SYSTEM_LIBRARY_DIRECTORY = Path("/usr/lib/x86_64-linux-gnu")
RUNTIME = Path("lib/tomacco/runtime")
# The newest symbol versions the package may need: Ubuntu 22.04's glibc and
# libstdc++ (GCC 12).
GLIBC_LIMIT = (2, 35)
GLIBCXX_LIMIT = (3, 4, 30)
# Qt plugins the desktop can use. The other platforms (eglfs, linuxfb, vnc,
# vkkhrdisplay) need a console or a server setup the desktop has no use for,
# and the GTK theme would load the host's GTK, which the launcher turns off.
PLUGINS = {
    "platforms": ("libqxcb.so", "libqwayland.so", "libqoffscreen.so", "libqminimal.so"),
    "platformthemes": ("libqxdgdesktopportal.so",),
    "platforminputcontexts": None, "imageformats": None, "iconengines": None, "tls": None,
    "xcbglintegrations": None, "wayland-decoration-client": None,
    "wayland-graphics-integration-client": None, "wayland-shell-integration": None,
    "networkinformation": None,
}
# QML modules the desktop never imports, and the object files and static
# libraries Qt keeps beside some modules for static linking.
QML_SKIP = ("QtTest", "QmlTime", "Qt/test")
BUILD_ONLY = (".o", ".a", ".prl")
# Small X11 helper libraries Qt's xcb plugin needs, which desktops often lack
# (Ubuntu 22.04 has no libxcb-cursor0 by default). MIT/X11 licensed; copied
# from the builder. They depend only on libxcb. libxkbcommon-x11 is not one of
# them: it must match the host's libxkbcommon (an older copy crashed on Fedora 44).
BUNDLED_SYSTEM = ("libxcb-cursor.so.0", "libxcb-icccm.so.4", "libxcb-image.so.0", "libxcb-keysyms.so.1",
                  "libxcb-render-util.so.0", "libxcb-util.so.1")
# What the package expects from the host: glibc, the C++ runtime, graphics,
# X11/Wayland, fonts, D-Bus, NSS (Chromium's certificate store), ALSA, and
# Brotli and GSSAPI (Qt Network's decompression and Kerberos login). Any
# other library the build finds missing from the bundle stops it.
SYSTEM = re.compile(r"""(
    (ld-linux-x86-64|libc|libm|libdl|libpthread|librt|libresolv)\.so\..*
  | lib(stdc\+\+|gcc_s|z|zstd|expat|udev|asound|dbus-1|fontconfig|freetype|gbm|drm)\.so\..*
  | lib(GL|EGL|OpenGL|GLX)\.so\..*
  | lib(glib|gobject|gio|gthread|gmodule)-2\.0\.so\.0
  | lib(nss3|nssutil3|smime3|nspr4|plc4|plds4)\.so
  | libX(11|11-xcb|composite|damage|ext|fixes|randr|tst|i|render|cursor|xf86vm)\.so\..*
  | libxkbcommon(-x11)?\.so\.0 | libxkbfile\.so\.1
  | libbrotli(common|dec)\.so\.1 | libgssapi_krb5\.so\.2
  | libxcb(-[a-z0-9-]+)?\.so\..*
  | libwayland-(client|cursor|egl)\.so\..*
)$""", re.X)
# Packages a clean container of each distribution needs for the checks: the
# libraries above, python3 for install.py, a font, and Xvfb for the X11 check.
APT_PACKAGES = ("python3 ca-certificates fonts-dejavu-core xvfb xauth libgl1 libegl1 libopengl0 libfontconfig1 libfreetype6 "
                "libglib2.0-0 libdbus-1-3 libnss3 libxkbcommon0 libxkbcommon-x11-0 libxkbfile1 libx11-6 libx11-xcb1 libxcb1 libxcomposite1 "
                "libxdamage1 libxext6 libxfixes3 libxrandr2 libxtst6 libasound2 libgbm1 libudev1 libexpat1 libzstd1 "
                "libwayland-client0 libwayland-cursor0 libwayland-egl1 libxcb-glx0 libxcb-randr0 libxcb-render0 libxcb-shape0 "
                "libxcb-shm0 libxcb-sync1 libxcb-xfixes0 libxcb-xkb1 libxcb-dri3-0 libxcb-xinput0 libxcb-xinerama0 libbrotli1 "
                "libgssapi-krb5-2").split()
DNF_PACKAGES = ("python3 ca-certificates dejavu-sans-fonts xorg-x11-server-Xvfb xorg-x11-xauth libglvnd-glx libglvnd-egl "
                "libglvnd-opengl fontconfig freetype glib2 dbus-libs nss nspr libxkbcommon libxkbcommon-x11 libxkbfile libX11 libX11-xcb libxcb "
                "libXcomposite libXdamage libXext libXfixes libXrandr libXtst alsa-lib mesa-libgbm systemd-libs expat libzstd "
                "libwayland-client libwayland-cursor libwayland-egl brotli krb5-libs tar gzip findutils procps-ng").split()
VERIFY_IMAGES = ("debian:12-slim", "ubuntu:22.04", "ubuntu:24.04", "fedora:44")


class Failed(Exception):
    pass


def run(argv, **options):
    print("+", shlex.join(str(a) for a in argv), flush=True)
    done = subprocess.run([str(a) for a in argv], check=False, **options)
    if done.returncode:
        raise Failed(f"{shlex.join(str(a) for a in argv)} failed with {done.returncode}")
    return done


def project_version(root=ROOT):
    found = re.search(r"project\(\s*\w+\s+VERSION\s+([0-9][0-9.]*)", (root / "CMakeLists.txt").read_text())
    if not found:
        raise Failed("CMakeLists.txt names no version")
    return found.group(1)


def package_prefix(root=ROOT):
    found = re.search(r'pub const PACKAGE_PREFIX: &str = "([^"]*)";', (root / "native/product.rs").read_text())
    return found.group(1) if found else "tomacco"


def package_name(version, prefix="tomacco"):
    """The package's file name without .tar.gz: the regular name plus -portable."""
    return f"{prefix}-{version}-Linux-x86_64-portable"


def image_tag(context=CONTEXT):
    """The builder image is named after its inputs, so a changed pin builds a new one."""
    digest = hashlib.sha256()
    for name in ("Dockerfile", "qt-archives.txt"):
        digest.update(name.encode() + b"\0" + (context / name).read_bytes())
    return f"{IMAGE}:{digest.hexdigest()[:12]}"


# ---------------------------------------------------------------------------
# On the host
# ---------------------------------------------------------------------------

def source_files(root):
    """Tracked files and new ones Git does not ignore: what a clean checkout plus your edits holds."""
    listed = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                            capture_output=True, check=True).stdout
    return sorted({name for name in listed.decode().split("\0") if name and (root / name).is_file()})


def export_source(root, destination):
    """Copies the source files with their time stamps, so CMake and Cargo rebuild only what changed."""
    if destination.exists():
        shutil.rmtree(destination)
    for name in source_files(root):
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / name, target, follow_symlinks=False)


def source_commit(root):
    sys.path.insert(0, str(root))
    try:
        import install
        return install.source_commit()
    finally:
        sys.path.pop(0)


def build(options):
    root = ROOT
    version = project_version(root)
    output = Path(options.output).resolve()
    work = output / "work"
    for directory in (work / "build", work / "cargo"):
        directory.mkdir(parents=True, exist_ok=True)
    tag = options.image or image_tag()
    if subprocess.run(["docker", "image", "inspect", tag], capture_output=True, check=False).returncode:
        run(["docker", "build", "--tag", tag, str(CONTEXT)])
    export_source(root, work / "src")
    environment = {"TOMACCO_SOURCE_COMMIT": source_commit(root), "CARGO_HOME": "/cargo", "HOME": "/tmp"}
    epoch = subprocess.run(["git", "-C", str(root), "log", "-1", "--format=%ct"], capture_output=True, text=True, check=False).stdout.strip()
    if epoch:
        environment["SOURCE_DATE_EPOCH"] = epoch
    argv = ["docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}",
            "-v", f"{work / 'src'}:/src:ro", "-v", f"{work / 'build'}:/build", "-v", f"{work / 'cargo'}:/cargo",
            "-v", f"{output}:/out"]
    for key, value in environment.items():
        argv += ["-e", f"{key}={value}"]
    run(argv + [tag, "python3", "/src/tools/build_portable.py", "inside", "--version", version, "--prefix", package_prefix(root)])
    package = output / (package_name(version, package_prefix(root)) + ".tar.gz")
    print(f"{package} ({package.stat().st_size / 1e6:.1f} MB)")
    return package


VERIFY_SCRIPT = r'''
set -u
name="$1"; family="$2"; shift 2
log() { printf '== %s\n' "$*"; }
result() { printf 'RESULT %s %s\n' "$1" "$2"; }
log "$(. /etc/os-release; echo "$PRETTY_NAME"), glibc $(ldd --version | head -n 1 | sed 's/.* //')"
if [ "$family" = apt ]; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq >/dev/null || { result packages fail; exit 1; }
    # Ubuntu 24.04 and Debian 13 renamed some libraries in the 64-bit time transition (libasound2t64).
    wanted=""
    for package in "$@"; do
        if apt-cache show "${package}t64" >/dev/null 2>&1; then wanted="$wanted ${package}t64"; else wanted="$wanted $package"; fi
    done
    apt-get install -y -qq --no-install-recommends $wanted >/dev/null || { result packages fail; exit 1; }
else
    dnf install -y -q --setopt=install_weak_deps=False "$@" >/dev/null || { result packages fail; exit 1; }
fi
result packages ok
mkdir -p /tmp/x && tar -xzf "/pkg/$name.tar.gz" -C /tmp/x || { result unpack fail; exit 1; }
top="/tmp/x/$name"
status=0
check() {
    label="$1"; shift; start=$(date +%s)
    if "$@"; then result "$label" "ok($(( $(date +%s) - start ))s)"; else result "$label" "fail($?)"; status=1; fi
}
missing() {
    found=$(find "$top/bin" "$top/lib/tomacco/runtime/lib" "$top/lib/tomacco/runtime/plugins" "$top/lib/tomacco/runtime/qml" \
        "$top/lib/tomacco/runtime/libexec" -type f \( -name '*.so*' -o -perm -u+x \) -exec sh -c 'file="$1"; head -c 4 "$file" | grep -q ELF || exit 0; ldd "$file" 2>/dev/null | grep "not found" | sed "s|^|$file: |"' _ {} \; )
    [ -z "$found" ] || { printf '%s\n' "$found" | sort -u | head -n 20; return 1; }
}
export QT_QPA_PLATFORMTHEME=generic QT_QUICK_CONTROLS_STYLE=Basic QTWEBENGINE_DISABLE_SANDBOX=1 QTWEBENGINE_CHROMIUM_FLAGS=--disable-gpu TOMACCO_UPDATES=off
check tomaccoctl-version "$top/bin/tomaccoctl" --version
"$top/bin/tomaccoctl" doctor > /tmp/doctor.json; code=$?
python3 -c 'import json,sys; v=json.load(open("/tmp/doctor.json")); print("doctor ok=%s, %d keys" % (v.get("ok"), len(v.get("result", v))))' 2>&1
[ $code = 0 ] && result tomaccoctl-doctor ok || { result tomaccoctl-doctor "fail($code)"; head -c 600 /tmp/doctor.json; status=1; }
check libraries-resolve missing
check smoke-offscreen env QT_QPA_PLATFORM=offscreen QT_QUICK_BACKEND=software timeout 300 "$top/bin/tomacco" --smoke-test
check smoke-xcb-xvfb env QT_QUICK_BACKEND=software timeout 300 xvfb-run -a -s "-screen 0 1920x1200x24" "$top/bin/tomacco" --smoke-test
home=/tmp/home; mkdir -p "$home"
check install-py python3 "$top/install.py" --user-home "$home" --no-window-rule
check installed-tomaccoctl "$home/.local/bin/tomaccoctl" --version
check installed-smoke-offscreen env QT_QPA_PLATFORM=offscreen QT_QUICK_BACKEND=software timeout 300 "$home/.local/bin/tomacco" --smoke-test
check uninstall python3 "$home/.local/lib/tomacco/install.py" --uninstall --user-home "$home"
check uninstall-left-nothing test ! -e "$home/.local/lib/tomacco"
exit $status
'''


def verify(options):
    package = Path(options.package).resolve()
    if not package.name.endswith(".tar.gz") or not package.is_file():
        raise Failed(f"No package at {package}")
    name = package.name[:-len(".tar.gz")]
    images = options.image or list(VERIFY_IMAGES)
    script = package.parent / "verify-portable.sh"
    script.write_text(VERIFY_SCRIPT)
    outcomes = {}
    for image in images:
        family = "dnf" if "fedora" in image else "apt"
        packages = DNF_PACKAGES if family == "dnf" else APT_PACKAGES
        log = package.parent / f"verify-{image.replace(':', '-').replace('/', '-')}.log"
        argv = ["docker", "run", "--rm", "-v", f"{package.parent}:/pkg:ro", image,
                "sh", "/pkg/verify-portable.sh", name, family, *packages]
        print("+", shlex.join(argv[:8]), "...", flush=True)
        with log.open("w") as out:
            done = subprocess.run(argv, stdout=out, stderr=subprocess.STDOUT, check=False)
        text = log.read_text(errors="replace")
        results = re.findall(r"^RESULT (\S+) (\S+)$", text, re.M)
        outcomes[image] = (done.returncode, results)
        header = next((line[3:] for line in text.splitlines() if line.startswith("== ")), image)
        print(f"{image}: {header}")
        for label, value in results:
            print(f"  {label}: {value}")
        print(f"  exit {done.returncode}, log {log}")
    failed = [image for image, (code, _) in outcomes.items() if code]
    if failed:
        raise Failed("The portable package failed on " + ", ".join(failed))
    return outcomes


# ---------------------------------------------------------------------------
# In the builder container
# ---------------------------------------------------------------------------

def is_elf(path):
    with path.open("rb") as data:
        return data.read(4) == b"\x7fELF"


def needed(path):
    output = subprocess.run(["readelf", "-dW", str(path)], capture_output=True, text=True, check=True).stdout
    return re.findall(r"\(NEEDED\)\s+Shared library: \[([^\]]+)\]", output)


def symbol_versions(path, family):
    output = subprocess.run(["objdump", "-T", str(path)], capture_output=True, text=True, check=True).stdout
    return {tuple(int(part) for part in version.split(".")) for version in re.findall(rf"\b{family}_([0-9.]+[0-9])\b", output)}


def copy_tree(source, destination, skip=()):
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if path.is_dir() or path.suffix in BUILD_ONLY or any(part.startswith("objects-") for part in relative.parts):
            continue
        if any(relative.as_posix() == name or relative.as_posix().startswith(name + "/") for name in skip):
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)  # follows symbolic links: the package holds files only


def assemble(top, qt=QT, ffmpeg=FFMPEG):
    """Adds the runtime to an installed tree: Qt's plugins, QML modules,
    WebEngine's helper process and resources, and every library they and the
    desktop need from Qt, FFmpeg and BUNDLED_SYSTEM. Returns the libraries left
    to the host."""
    runtime = top / RUNTIME
    for directory, names in PLUGINS.items():
        source = qt / "plugins" / directory
        for plugin in sorted(source.iterdir()) if source.is_dir() else ():
            if names is None or plugin.name in names:
                (runtime / "plugins" / directory).mkdir(parents=True, exist_ok=True)
                shutil.copy2(plugin, runtime / "plugins" / directory / plugin.name)
    copy_tree(qt / "qml", runtime / "qml", skip=QML_SKIP)
    copy_tree(qt / "resources", runtime / "resources")
    copy_tree(qt / "translations/qtwebengine_locales", runtime / "translations/qtwebengine_locales")
    for translation in sorted((qt / "translations").glob("qtbase_*.qm")):
        shutil.copy2(translation, runtime / "translations" / translation.name)
    (runtime / "libexec").mkdir(parents=True, exist_ok=True)
    shutil.copy2(qt / "libexec/QtWebEngineProcess", runtime / "libexec/QtWebEngineProcess")
    # Qt finds its plugins, QML modules and WebEngine's files through qt.conf
    # beside each executable; the installed copy is written by install.py.
    (top / "bin/qt.conf").write_text("[Paths]\nPrefix=../lib/tomacco/runtime\n")
    (runtime / "libexec/qt.conf").write_text("[Paths]\nPrefix=..\n")
    library = runtime / "lib"
    library.mkdir(parents=True, exist_ok=True)
    pending = [top / "bin/tomacco"] + [p for p in sorted(runtime.rglob("*")) if p.is_file() and is_elf(p)]
    seen, host = set(), set()
    while pending:
        for name in needed(pending.pop()):
            if name in seen:
                continue
            seen.add(name)
            for directory in (qt / "lib", ffmpeg / "lib"):
                if (directory / name).exists():
                    shutil.copy2(directory / name, library / name)
                    pending.append(library / name)
                    break
            else:
                if name in BUNDLED_SYSTEM:
                    shutil.copy2(SYSTEM_LIBRARY_DIRECTORY / name, library / name)
                    pending.append(library / name)
                else:
                    host.add(name)
    # The desktop finds the bundle from the package's bin/ and from the
    # installed ~/.local/lib/tomacco; Qt's own files already say $ORIGIN.
    run(["patchelf", "--set-rpath", "$ORIGIN/../lib/tomacco/runtime/lib:$ORIGIN/runtime/lib", top / "bin/tomacco"])
    for name in sorted(library.iterdir()):
        if name.name.startswith(("libav", "libsw")) or name.name in BUNDLED_SYSTEM:
            run(["patchelf", "--set-rpath", "$ORIGIN", name])
    return sorted(host)


def check(top, host):
    """Refuses a package that needs a newer glibc or libstdc++ than the base, or
    a host library outside SYSTEM."""
    problems = []
    unexpected = [name for name in host if not SYSTEM.match(name)]
    if unexpected:
        problems.append("libraries neither bundled nor expected from the host: " + ", ".join(unexpected))
    newest = {}
    for path in sorted(p for p in top.rglob("*") if p.is_file() and not p.is_symlink() and is_elf(p)):
        for family, limit in (("GLIBC", GLIBC_LIMIT), ("GLIBCXX", GLIBCXX_LIMIT)):
            versions = symbol_versions(path, family)
            if not versions:
                continue
            highest = max(versions)
            newest[family] = max(newest.get(family, highest), highest)
            if highest > limit:
                problems.append(f"{path.relative_to(top)} needs {family}_{'.'.join(map(str, highest))}")
    if problems:
        raise Failed("The portable package would not run on glibc 2.35:\n  " + "\n  ".join(problems))
    return {family: ".".join(map(str, version)) for family, version in newest.items()}


def notice(version, host, newest, qt=QT, ffmpeg=FFMPEG):
    """share/doc/tomacco/PORTABLE.md: what is bundled, under which license, where its source is."""
    ffmpeg_version = re.search(r"^Version: (\S+)", (ffmpeg / "lib/pkgconfig/libavcodec.pc").read_text(), re.M).group(1)
    configuration = re.search(r"^FFMPEG_CONFIGURATION=(.*)$", (ffmpeg / "share/licenses/config.mak").read_text(), re.M)
    configuration = configuration.group(1).strip() if configuration else "see packaging/portable/Dockerfile"
    lines = [
        f"# The portable package of Tomacco {version}",
        "",
        "This package bundles the libraries the desktop needs from Qt and FFmpeg, so it runs without the",
        "distribution's own. It was built on Ubuntu 22.04 by `tools/build_portable.py` from",
        "`packaging/portable/Dockerfile`, which pins every download by SHA-256.",
        "",
        f"Newest symbol versions needed: glibc {newest.get('GLIBC', '?')}, libstdc++ {newest.get('GLIBCXX', '?')}.",
        "",
        "## Bundled in lib/tomacco/runtime",
        "",
        "- **Qt 6.12.0** (Core, Gui, Qml, Quick, Quick Controls 2, Network, Svg, DBus, OpenGL, Wayland client,",
        "  XCB QPA, WebEngine Core and Quick, WebChannel, Positioning) and ICU 73.2, the official binaries from",
        "  <https://download.qt.io/online/qtsdkrepository/linux_x64/>, unmodified. Qt is used under LGPL-3.0-only;",
        "  Qt WebEngine includes Chromium (BSD-3-Clause) and its third-party components. Their software bills of",
        "  materials, with each component's license, are in `lib/tomacco/runtime/sbom/`. Source:",
        "  <https://download.qt.io/official_releases/qt/6.12/6.12.0/submodules/>, and for Qt WebEngine the source",
        "  of its build in Qt's online repository; `packaging/portable/qt-sources.txt` in Tomacco's source tarball",
        "  lists each URL with its SHA-256, and a release made with `make_release.py --with-qt-sources` offers",
        "  them as assets. ICU's license is `LICENSE.ICU` beside this file.",
        f"- **FFmpeg {ffmpeg_version}** (libavcodec, libavutil, libswscale), built from",
        "  <https://ffmpeg.org/releases/> under LGPL-2.1-or-later, without GPL, version 3 or nonfree parts, with",
        f"  this configuration: `{configuration}`.",
        "- **xcb-util libraries** (" + ", ".join(BUNDLED_SYSTEM) + ") from Ubuntu 22.04, MIT/X11 licenses.",
        "",
        "All are shared libraries: replace any of them in `lib/tomacco/runtime/lib` with a compatible build of",
        "your own. The LGPL texts are `COPYING.LGPL-2.1` and `COPYING.LGPL-3.0` beside this file; the GPL",
        "version 3 is `COPYING.GPL-3.0`. THIRD_PARTY_NOTICES.md has how the licenses combine.",
        "",
        "## Expected from the system",
        "",
        "glibc 2.35 or newer and these libraries, which desktop distributions install by default:",
        "",
        ", ".join(f"`{name}`" for name in host) + ".",
        "",
    ]
    return "\n".join(lines)


def tarball(stage, name, output, epoch):
    package = output / f"{name}.tar.gz"
    temporary = package.with_name(package.name + ".partial")
    with temporary.open("wb") as out:
        tar = subprocess.Popen(["tar", "--sort=name", "--owner=0", "--group=0", "--numeric-owner", f"--mtime=@{epoch}",
                                "--format=gnu", "-C", str(stage), "-cf", "-", name], stdout=subprocess.PIPE)
        gzip = subprocess.run(["gzip", "-n", "-6"], stdin=tar.stdout, stdout=out, check=False)
        tar.stdout.close()
        if tar.wait() or gzip.returncode:
            raise Failed("Packing the tarball failed")
    temporary.replace(package)
    digest = hashlib.sha256(package.read_bytes()).hexdigest()
    package.with_name(package.name + ".sha256").write_text(f"{digest}  {package.name}\n")
    return package


def inside(options):
    source, build_root, output = Path("/src"), Path("/build"), Path("/out")
    name = package_name(options.version, options.prefix)
    cmake = build_root / "cmake"
    environment = {**os.environ, "PKG_CONFIG_PATH": str(FFMPEG / "lib/pkgconfig")}
    run(["cmake", "-S", source, "-B", cmake, "-G", "Ninja", "-DCMAKE_BUILD_TYPE=Release",
         f"-DCMAKE_PREFIX_PATH={QT}", "-DBUILD_TESTING=OFF"], env=environment)
    run(["cmake", "--build", cmake], env=environment)
    stage = build_root / "stage"
    if stage.exists():
        shutil.rmtree(stage)
    top = stage / name
    run(["cmake", "--install", cmake, "--prefix", top])
    host = assemble(top)
    sbom = top / RUNTIME / "sbom"
    sbom.mkdir()
    for path in sorted((QT / "sbom").glob("*.spdx")):
        if not path.name.endswith(".source.spdx"):
            shutil.copy2(path, sbom / path.name)
    doc = top / "share/doc/tomacco"
    shutil.copy2(FFMPEG / "share/licenses/COPYING.LGPLv2.1", doc / "COPYING.LGPL-2.1")
    shutil.copy2(FFMPEG / "share/licenses/COPYING.LGPLv3", doc / "COPYING.LGPL-3.0")
    shutil.copy2(QT / "licenses/LICENSE.ICU", doc / "LICENSE.ICU")
    newest = check(top, host)
    (doc / "PORTABLE.md").write_text(notice(options.version, host, newest))
    print("Expected from the host:", " ".join(host))
    print("Newest symbol versions:", newest)
    epoch = int(os.environ.get("SOURCE_DATE_EPOCH", "0") or 0)
    package = tarball(stage, name, output, epoch)
    print(f"Wrote {package}")


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    making = commands.add_parser("build", help="Build the portable package in the builder container")
    making.add_argument("--output", default=str(ROOT / OUTPUT), help=f"Directory for the package and the build tree (default: {OUTPUT}/)")
    making.add_argument("--image", help="Use this builder image instead of building one from packaging/portable")
    checking = commands.add_parser("verify", help="Run the package's checks in clean containers")
    checking.add_argument("package")
    checking.add_argument("--image", action="append", help=f"An image to check in; repeatable (default: {', '.join(VERIFY_IMAGES)})")
    within = commands.add_parser("inside")  # run by `build` in the container
    within.add_argument("--version", required=True)
    within.add_argument("--prefix", default="tomacco")
    options = parser.parse_args(arguments)
    try:
        {"build": build, "verify": verify, "inside": inside}[options.command](options)
    except (Failed, subprocess.CalledProcessError) as error:
        print(f"build_portable: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

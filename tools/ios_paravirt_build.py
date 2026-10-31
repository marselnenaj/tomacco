#!/usr/bin/env python3
"""Build ownership and provenance for the private Linux paravirtual engine."""
import configparser
from contextlib import ExitStack
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import signal
import stat
import subprocess
import tempfile
import time


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(value, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def stable_file(path, *, executable=False):
    """Hash a regular artifact through one descriptor, refusing replacement."""
    path = Path(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        with os.fdopen(descriptor, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or (executable and not before.st_mode & 0o111):
                raise RuntimeError(f'Not a regular {"executable" if executable else "file"}: {path}')
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            after = os.fstat(stream.fileno())
        current = path.lstat()
        epoch = lambda entry: (entry.st_dev, entry.st_ino, entry.st_size, entry.st_mtime_ns,
                               entry.st_ctime_ns, entry.st_mode)
        if epoch(before) != epoch(after) or epoch(after) != epoch(current):
            raise RuntimeError(f'Artifact changed while being recorded: {path}')
        return {'path': str(path), 'sha256': digest, 'bytes': before.st_size,
                'mode': oct(stat.S_IMODE(before.st_mode)), 'mtime_ns': before.st_mtime_ns}
    except BaseException:
        raise


def validate_roles(root, tooling, sources):
    paths = list(sources.values())
    for index, left in enumerate(paths):
        for right in paths[index + 1:]:
            if left == right or left in right.parents or right in left.parents:
                raise RuntimeError(f'Source roles overlap: {left} and {right}')
    build = root / 'sptm-build'
    if build.is_symlink():
        raise RuntimeError(f'Build directory cannot be a symlink: {build}')
    for path in paths:
        if path == root or path in root.parents or path == build or build in path.parents or path in build.parents:
            raise RuntimeError(f'Source overlaps build output: {path}')
        if path == tooling or path in tooling.parents or tooling in path.parents:
            raise RuntimeError(f'Source overlaps base tooling: {path}')
    if root == tooling or root in tooling.parents or tooling in root.parents:
        raise RuntimeError('Engine output and base tooling must be separate')
    # These paths are embedded in generated Meson literals and tool arguments.
    if any(any(ord(c) < 32 for c in str(path)) for path in [root, tooling, *paths]):
        raise RuntimeError('Build paths cannot contain control characters')


def preflight_pins(sources, revisions, run, *, offline):
    for role, path in sources.items():
        if not path.exists():
            if offline:
                raise RuntimeError(f'Offline source missing: {path}')
            continue
        actual = run(['git', '-C', path, 'rev-parse', 'HEAD'], capture=True).stdout.strip()
        if actual != revisions[role]:
            raise RuntimeError(f'Preserved {role}: expected {revisions[role]}, found {actual}')


def patch_descriptors(families):
    record = {}
    seen = set()
    for family, patches in families.items():
        entries = []
        for patch in patches:
            identity = Path(patch).resolve()
            if identity in seen:
                raise RuntimeError(f'Duplicate patch: {patch}')
            seen.add(identity)
            item = stable_file(patch)
            if not item['bytes']:
                raise RuntimeError(f'Empty patch: {patch}')
            entries.append({'name': Path(patch).name, 'sha256': item['sha256']})
        record[family] = entries
    return record


def offline_dependencies(source, run):
    """Derive exact dependency pins and overlays from this pinned QEMU tree."""
    records = {}
    for name in ('dtc', 'keycodemapdb', 'berkeley-softfloat-3', 'berkeley-testfloat-3'):
        wrap_path = source / 'subprojects' / (name + '.wrap')
        wrap = configparser.ConfigParser(interpolation=None)
        wrap.read(wrap_path)
        spec = wrap['wrap-git']
        revision = spec['revision']
        if len(revision) != 40 or any(c not in '0123456789abcdef' for c in revision):
            raise RuntimeError(f'Offline dependency is not commit pinned: {wrap_path}')
        directory = source / 'subprojects' / spec.get('directory', name)
        actual = run(['git', '-C', directory, 'rev-parse', 'HEAD'], capture=True).stdout.strip()
        if actual != revision:
            raise RuntimeError(f'Offline dependency {name}: expected {revision}, found {actual}')
        overlays = {}
        patch_directory = spec.get('patch_directory')
        if patch_directory:
            overlay = source / 'subprojects/packagefiles' / patch_directory
            if not overlay.is_dir():
                raise RuntimeError(f'Missing Meson overlay: {overlay}')
            for item in sorted(overlay.rglob('*')):
                if item.is_dir():
                    continue
                relative = item.relative_to(overlay)
                original = stable_file(item)
                applied = stable_file(directory / relative)
                if original['sha256'] != applied['sha256']:
                    raise RuntimeError(f'Offline overlay differs: {directory / relative}')
                overlays[str(relative)] = original['sha256']
        records[name] = {'commit': actual, 'wrap_sha256': stable_file(wrap_path)['sha256'],
                         'overlays': overlays, 'tree': source_fingerprint(directory, run)}
    return records


def source_fingerprint(source, run):
    listed = run(['git', '-C', source, 'ls-files', '--cached', '--others',
                  '--exclude-standard', '-z'], capture=True).stdout.split('\0')[:-1]
    entries = []
    for name in sorted(set(listed)):
        path = source / name
        if path.is_symlink():
            item = {'path': name, 'symlink': os.readlink(path)}
        elif path.is_dir():
            # Gitlinks are identities, not an invitation to walk build caches.
            commit = run(['git', '-C', path, 'rev-parse', 'HEAD'], capture=True).stdout.strip()
            item = {'path': name, 'gitlink': commit}
        else:
            info = stable_file(path)
            item = {'path': name, 'sha256': info['sha256'], 'mode': info['mode']}
        entries.append(item)
    digest = hashlib.sha256(json.dumps(entries, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {'sha256': digest, 'entries': len(entries),
            'scope': 'tracked and nonignored untracked files; symlink text and gitlink commit'}


def effective_rust_flags(env, native):
    encoded = env.get('CARGO_ENCODED_RUSTFLAGS')
    flags = (encoded.split('\x1f') if encoded else []) if encoded is not None else shlex.split(env.get('RUSTFLAGS', ''))
    if native:
        flags.append('-Ctarget-cpu=native')
    return flags


def toolchain_receipt(env, run):
    record = {}
    for name in ('git', 'cargo', 'rustc', 'ninja', 'pkg-config', 'cc', 'c++'):
        resolved = shutil.which(name, path=env['PATH'])
        if not resolved:
            raise RuntimeError(f'Missing tool: {name}')
        path = Path(resolved).resolve()
        version = run([path, '--version'], env=env, capture=True).stdout.splitlines()
        record[name] = {**stable_file(path, executable=True), 'version': version[:3]}
    return record


def host_receipt(native):
    result = {'architecture': platform.machine(), 'kernel': platform.release(), 'native': native}
    if native:
        cpu = Path('/proc/cpuinfo').read_text()
        fields = {}
        for line in cpu.split('\n\n', 1)[0].splitlines():
            if ':' in line:
                key, value = line.split(':', 1)
                if key.strip() in ('vendor_id', 'model name', 'cpu family', 'model', 'stepping', 'flags', 'Features'):
                    fields[key.strip()] = value.strip()
        result['cpu'] = fields
        result['compatibility'] = 'Host-specific compiler target; no portability or speedup qualification'
    return result


def compile_profile(build):
    path = build / 'compile_commands.json'
    commands = json.loads(path.read_text())
    selected = {}
    patterns = {'tcg': '/tcg/tcg.c', 'arm_translate': '/target/arm/tcg/translate.c',
                'vphone': '/hw/arm/vphone-research.c', 'gpu_shim': '/tomacco-reims/reims-vgpu-shim.c'}
    for role, suffix in patterns.items():
        matches = [entry for entry in commands if entry['file'].endswith(suffix)]
        if not matches:
            raise RuntimeError(f'Missing compile profile evidence: {role}')
        selected[role] = [{'file': entry['file'],
                           'arguments': entry.get('arguments') or shlex.split(entry['command'])}
                          for entry in matches]
    return {'database_sha256': stable_file(path)['sha256'], 'observed': selected}


class BuildSession:
    """One locked build attempt; complete manifests survive failed attempts."""
    def __init__(self, root, paths, run):
        self.root = root
        self.paths = sorted(set([root, *paths]), key=str)
        self.base_run = run
        self.stack = ExitStack()
        self.record = {'schema': 1, 'pid': os.getpid(), 'started_at': utc_now(),
                       'status': 'running', 'phase': 'ownership', 'phases': [], 'commands': []}
        self.started = time.monotonic()
        self.phase_started = self.started

    def __enter__(self):
        try:
            locks = Path('/tmp') / f'tomacco-build-locks-{os.getuid()}'
            locks.mkdir(mode=0o700, exist_ok=True)
            owner = locks.lstat()
            if not stat.S_ISDIR(owner.st_mode) or owner.st_uid != os.getuid() or owner.st_mode & 0o077:
                raise RuntimeError(f'Unsafe build lock directory: {locks}')
            for path in self.paths:
                key = hashlib.sha256(os.fsencode(path)).hexdigest()
                descriptor = os.open(locks / (key + '.lock'), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                self.stack.callback(os.close, descriptor)
                entry = os.fstat(descriptor)
                if not stat.S_ISREG(entry.st_mode) or entry.st_uid != os.getuid() or entry.st_nlink != 1:
                    raise RuntimeError(f'Unsafe lock file for {path}')
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise RuntimeError(f'Another build owns {path}') from None
                os.ftruncate(descriptor, 0)
                os.write(descriptor, json.dumps({'pid': os.getpid(), 'path': str(path), 'at': utc_now()}).encode())
            self.record['locked_paths'] = [str(path) for path in self.paths]
            self.root.mkdir(parents=True, exist_ok=True)
            self.publish()
            return self
        except BaseException:
            self.stack.close()
            raise

    def publish(self):
        atomic_json(self.root / 'build-attempt.json', self.record)

    def phase(self, name):
        now = time.monotonic()
        self.record['phases'].append({'name': self.record['phase'], 'elapsed_seconds': now - self.phase_started})
        self.record['phase'] = name
        self.phase_started = now
        self.publish()
        print(f'Build phase: {name}', flush=True)

    def run(self, argv, *, cwd=None, env=None, capture=False):
        entry = {'argv': list(map(str, argv)), 'cwd': str(cwd) if cwd else None,
                 'phase': self.record['phase'], 'started_at': utc_now()}
        self.record['commands'].append(entry)
        self.publish()
        started = time.monotonic()
        try:
            print("+", " ".join(map(str, argv)), flush=True)
            process = subprocess.Popen(list(map(str, argv)), cwd=cwd, env=env,
                                       stdout=subprocess.PIPE if capture else None,
                                       text=capture, start_new_session=True)
            try:
                stdout, _ = process.communicate()
            except BaseException:
                # Retain source locks until the whole owned compiler group has
                # been signalled and the direct child has been reaped.
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                # A descendant may survive after its parent exits. End the
                # owned group before releasing the artifact/source locks.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                if process.stdout is not None:
                    process.stdout.close()
                raise
            entry['exit_code'] = process.returncode
            if process.returncode:
                raise subprocess.CalledProcessError(process.returncode, list(map(str, argv)), output=stdout)
            return subprocess.CompletedProcess(list(map(str, argv)), process.returncode, stdout)
        except subprocess.CalledProcessError as error:
            entry['exit_code'] = error.returncode
            raise
        except BaseException as error:
            entry['interrupted_by'] = type(error).__name__
            raise
        finally:
            entry['elapsed_seconds'] = time.monotonic() - started
            self.publish()

    def complete(self, manifest):
        manifest['build_attempt'] = {key: self.record[key] for key in ('started_at', 'pid', 'locked_paths')}
        manifest['build_elapsed_seconds'] = time.monotonic() - self.started
        atomic_json(self.root / 'build-manifest.json', manifest)
        self.record['manifest_sha256'] = stable_file(self.root / 'build-manifest.json')['sha256']
        self.record['status'] = 'complete'

    def __exit__(self, kind, error, traceback):
        try:
            if error is not None:
                self.record['status'] = 'cancelled' if isinstance(error, KeyboardInterrupt) else 'failed'
                self.record['error'] = {'type': type(error).__name__, 'message': str(error)}
                if isinstance(error, subprocess.CalledProcessError):
                    self.record['error']['exit_code'] = error.returncode
            elif self.record['status'] != 'complete':
                self.record['status'] = 'failed'
                self.record['error'] = {'type': 'IncompleteBuild', 'message': 'No complete artifact manifest published'}
            self.record['phases'].append({'name': self.record['phase'], 'elapsed_seconds': time.monotonic() - self.phase_started})
            self.record['elapsed_seconds'] = time.monotonic() - self.started
            self.record['finished_at'] = utc_now()
            self.publish()
        finally:
            self.stack.close()
        return False

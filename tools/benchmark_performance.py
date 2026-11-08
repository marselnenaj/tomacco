#!/usr/bin/env python3
"""Offline performance qualification, with correctness and bounded hardware traps.

Run from a source checkout (Pillow optional for changing-content OCR fixtures):
  python3 tools/benchmark_performance.py --cli /path/to/tomaccoctl --output qa/performance/raw.json
  python3 tools/benchmark_performance.py --cli /path/to/candidate --compare qa/performance/raw.json

Fixture captures copy a prebuilt PNG; they do not measure hardware, video decode,
live PNG encoding, display FPS, touch latency, or competitor performance. Cold
means a fresh MCP/CLI process, NOT a flushed OS page cache. Warm is sequential
requests on one server. OCR warm repeats deliberately exercise content caches.
Every failure is retained and makes the command fail. Missing optional workloads
are explicit skips. No device selector exists: only generated fixture sessions
and existing image files are admitted.
"""
import argparse
import hashlib
import json
import math
import os
import platform
import queue
import random
import resource
import statistics
import struct
import subprocess
import tempfile
import threading
import time
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS = ROOT / 'tests/fixtures/ocr-corpus'
if not DEFAULT_CORPUS.is_dir():
    DEFAULT_CORPUS = ROOT / 'benchmarks/ocr-corpus'
TRAPS = ('adb', 'fastboot', 'ssh', 'scp', 'sftp', 'iproxy', 'go-ios', 'idevice_id',
         'ideviceinfo', 'idevicescreenshot', 'xtool', 'scrcpy', 'uxplay', 'emulator',
         'usbmuxd', 'bluetoothctl', 'ios', 'flutter', 'gradle', 'xcodebuild')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def distribution(values, unit):
    values = sorted(v for v in values if v is not None)
    if not values:
        return {'n': 0, 'unit': unit}
    return {'n': len(values), 'unit': unit, 'p50': statistics.median(values),
            'p95': values[math.ceil(.95 * len(values)) - 1],
            'min': values[0], 'max': values[-1]}


class Sandbox:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='tomacco-perf-')
        self.path = Path(self.tmp.name)
        for directory in ('bin', 'run', 'fixtures', 'tmp'):
            (self.path / directory).mkdir(mode=0o700)
        self.traplog = self.path / 'forbidden-tools'
        for name in TRAPS:
            p = self.path / 'bin' / name
            p.write_text('#!/bin/sh\nprintf "%s\\n" ' + name + ' >> ' + str(self.traplog) + '\nexit 97\n')
            p.chmod(0o700)
        self.env = dict(os.environ)
        self.env.update(HOME=str(self.path), TOMACCO_HOME=str(self.path / 'tomacco'),
                        XDG_CONFIG_HOME=str(self.path / '.config'), XDG_DATA_HOME=str(self.path / '.local/share'),
                        XDG_STATE_HOME=str(self.path / '.local/state'), XDG_CACHE_HOME=str(self.path / '.cache'),
                        XDG_RUNTIME_DIR=str(self.path / 'run'), TMPDIR=str(self.path / 'tmp'),
                        TOMACCO_FIXTURE_RUNTIME=str(self.path / 'fixtures'), TOMACCO_UPDATES='off',
                        DBUS_SYSTEM_BUS_ADDRESS='unix:path=' + str(self.path / 'no-system-bus'),
                        DBUS_SESSION_BUS_ADDRESS='unix:path=' + str(self.path / 'no-session-bus'),
                        PATH=str(self.path / 'bin') + ':/usr/bin:/bin', LANG='C.UTF-8',
                        TESSDATA_PREFIX='/usr/share/tessdata')
        for key in ('TOMACCO_PARENT_JOB', 'TOMACCO_TEST_ROOT', 'TOMACCO_POLICY', 'TOMACCO_USBMUXD_SOCKET_ADDRESS'):
            self.env.pop(key, None)
        # Standard route must remain eligible for the built-in worker. A separate
        # changing-image sequence explicitly tests the custom-binary contract.
        self.env.pop('TOMACCO_TESSERACT', None)

    def close(self):
        trapped = self.traplog.read_text() if self.traplog.exists() else ''
        self.tmp.cleanup()
        return trapped


def png(path, width, height):
    """Seeded RGB pattern: prebuilt outside timed sections, not a PNG encoder test."""
    rng = random.Random(391)
    # Mostly flat UI bands with a seeded textured portion (neither all black nor a photograph).
    pixels = b''.join(b'\0' + (rng.randbytes(width * 3) if y % 16 == 0 else
                              bytes((238 - y % 16, 241 - y % 16, 244 - y % 16)) * width)
                      for y in range(height))
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    path.write_bytes(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)) +
                     chunk(b'IDAT', zlib.compress(pixels, 6)) + chunk(b'IEND', b''))


def fixture(box, name, width, height, count):
    directory = box.path / 'fixtures' / name
    directory.mkdir()
    png(directory / 'home.png', width, height)
    nodes = []
    for index in range(count):
        text = 'Sign in' if index == count - 1 else f'Row {index:04d} account preference'
        y = (index * 43) % (height - 44)
        nodes.append(f'<node index="{index}" class="android.widget.Button" resource-id="example:id/item_{index}" '
                     f'text="{text}" enabled="true" clickable="true" bounds="[10,{y}][{width-10},{y+40}]"/>')
    xml = f'<hierarchy rotation="0"><node class="android.widget.FrameLayout" bounds="[0,0][{width},{height}]">' + ''.join(nodes) + '</node></hierarchy>'
    (directory / 'home.xml').write_text(xml)
    (directory / 'loading.xml').write_text(f'<hierarchy><node class="android.widget.TextView" text="Loading" bounds="[10,10][{width-10},60]"/></hierarchy>')
    scenario = {'initial': 'home', 'latency_ms': 0, 'states': {
        'home': {'frame': 'home.png', 'hierarchy': 'home.xml', 'keyboard': False, 'focused': False,
                 'taps': [{'left': 0, 'top': 0, 'right': width, 'bottom': height, 'to': 'loading', 'after_ms': 0}]},
        'loading': {'frame': 'home.png', 'hierarchy': 'loading.xml', 'keyboard': False, 'focused': False,
                    'key_to': {'back': 'home'}}}}
    (directory / 'scenario.json').write_text(json.dumps(scenario))
    return {'name': name, 'width': width, 'height': height, 'nodes': count,
            'png_bytes': (directory / 'home.png').stat().st_size, 'png_sha256': sha(directory / 'home.png'),
            'xml_bytes': len(xml.encode()), 'xml_sha256': hashlib.sha256(xml.encode()).hexdigest()}


def processes(roots):
    """RSS and CPU for roots + living descendants, including their reaped children.

    Linux proc CPU ticks are coarse; samples can be zero for a fast call. RSS is
    sampled at 10ms, not a guaranteed kernel high-water mark; shared pages count
    per process. No GPU memory or host/UI CPU is included.
    """
    records = {}
    for path in Path('/proc').iterdir():
        if not path.name.isdigit():
            continue
        try:
            fields = (path / 'stat').read_text().rsplit(')', 1)[1].split()
            records[int(path.name)] = (int(fields[1]), sum(int(fields[x]) for x in (11, 12, 13, 14)), int(fields[21]))
        except (OSError, ValueError, IndexError):
            continue
    selected = set(roots) & records.keys()
    while True:
        children = {pid for pid, row in records.items() if row[0] in selected}
        if children <= selected:
            break
        selected |= children
    return {'cpu_ms': sum(records[p][1] for p in selected) * 1000 / os.sysconf('SC_CLK_TCK'),
            'rss_kib': sum(records[p][2] for p in selected) * os.sysconf('SC_PAGE_SIZE') / 1024,
            'processes': len(selected)}


class Sampler:
    def __init__(self, roots):
        self.roots = roots
        self.stop = threading.Event()
        self.peak = 0
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        while not self.stop.is_set():
            self.peak = max(self.peak, processes(self.roots)['rss_kib'])
            self.stop.wait(.01)

    def close(self):
        self.stop.set()
        self.thread.join()


class MCP:
    def __init__(self, cli, box):
        started = time.perf_counter_ns()
        self.process = subprocess.Popen([str(cli), 'mcp'], env=box.env, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.roots = {self.process.pid}
        self.ident = 0
        self.replies = queue.Queue()
        self.stderr = b''
        def read():
            for line in self.process.stdout:
                try:
                    self.replies.put((json.loads(line), len(line)))
                except ValueError:
                    self.replies.put(({'invalid_json': line.decode(errors='replace')}, len(line)))
        def drain():
            self.stderr = self.process.stderr.read()
        threading.Thread(target=read, daemon=True).start()
        threading.Thread(target=drain, daemon=True).start()
        reply, _ = self.request('initialize', {'protocolVersion': '2025-11-25', 'capabilities': {},
                                             'clientInfo': {'name': 'offline-performance', 'version': '1'}})
        if reply.get('result', {}).get('serverInfo', {}).get('name') != 'tomacco':
            raise RuntimeError(reply)
        self.send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        self.start_ms = (time.perf_counter_ns() - started) / 1e6

    def send(self, value):
        self.process.stdin.write((json.dumps(value) + '\n').encode())
        self.process.stdin.flush()

    def request(self, method, params):
        self.ident += 1
        self.send({'jsonrpc': '2.0', 'id': self.ident, 'method': method, 'params': params})
        while True:
            reply, size = self.replies.get(timeout=120)
            if reply.get('id') == self.ident:
                return reply, size
            if 'invalid_json' in reply:
                raise RuntimeError(reply)

    def call(self, operation, arguments):
        before = processes(self.roots)
        sampler = Sampler(self.roots)
        started = time.perf_counter_ns()
        try:
            reply, size = self.request('tools/call', {'name': operation, 'arguments': arguments})
            elapsed = (time.perf_counter_ns() - started) / 1e6
            result = reply.get('result', {})
            text = next((c['text'] for c in result.get('content', []) if c.get('type') == 'text'), '{}')
            value = json.loads(text)
            if 'error' in reply or result.get('isError', True):
                raise RuntimeError(reply)
            after = processes(self.roots)
            row = {'wall_ms': elapsed, 'wire_utf8_bytes': size, 'text_utf8_bytes': len(text.encode()),
                   'cpu_ms': max(0, after['cpu_ms'] - before['cpu_ms']),
                   'rss_kib_after': after['rss_kib'], 'sampled_peak_rss_kib': max(sampler.peak, before['rss_kib'], after['rss_kib'])}
            return value, row
        finally:
            sampler.close()

    def close(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--cli', type=Path, required=True)
    parser.add_argument('--desktop', type=Path, help='optional Qt binary: ONLY offscreen --version, never opens the application')
    parser.add_argument('--trials', type=int, default=20)
    parser.add_argument('--cold-trials', type=int, default=3)
    parser.add_argument('--ocr-trials', type=int, default=3)
    parser.add_argument('--changing-trials', type=int, default=5, help='uncached changing-pixel reads per persistent engine (minimum 2)')
    parser.add_argument('--corpus', type=Path, default=DEFAULT_CORPUS)
    parser.add_argument('--output', type=Path, default=ROOT / 'qa/performance/raw.json')
    parser.add_argument('--compare', type=Path, help='previous raw report; only compare matching workload hashes')
    args = parser.parse_args()
    if min(args.trials, args.cold_trials, args.ocr_trials) < 1 or args.changing_trials < 2:
        parser.error('all trial counts must be positive')
    cli = args.cli.resolve(strict=True)
    report = {'schema': 'tomacco-offline-performance/1', 'evidence': 'offline fixtures and existing image files',
              'binary': {'path': str(cli), 'sha256': sha(cli)}, 'tool_sha256': sha(__file__),
              'environment': {'platform': platform.platform(), 'python': platform.python_version(), 'cpus': os.cpu_count(),
                              'load_start': os.getloadavg(), 'started_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())},
              'settings': {'warm_trials': args.trials, 'cold_trials': args.cold_trials, 'ocr_trials': args.ocr_trials,
                           'changing_trials': args.changing_trials},
              'boundaries': {'wall_ms': 'MCP request write to complete raw JSON line received, excludes verifier and proc sampling',
                             'wire_utf8_bytes': 'actual complete JSON-RPC response line including newline; image files separate',
                             'cold': 'fresh MCP or CLI process; OS page cache NOT flushed; same isolated data home',
                             'warm': 'sequential calls on same MCP server; warm_cached OCR repeats exercise content cache; warm_fresh_image changes pixels outside labelled target and asserts engine_runs>0',
                             'rss': 'Linux /proc 10ms sampled sum of MCP + fixture worker + descendants; shared pages counted per process; not a guaranteed peak',
                             'cpu': 'Linux proc user+system + waited children and live descendants; coarse clock ticks; excludes client CPU',
                             'observer': 'A background /proc sampler runs every 10ms during requests; observer scheduling/CPU overhead can affect timings',
                             'p95': 'nearest rank ceil(.95*n); with n<20 this is the observed maximum, not a stable production-tail estimate',
                             'correctness': 'PNG hash+geometry, known late tree target retained, action verified by hierarchy postcondition, OCR labelled target matches'},
              'pending': ['hardware capture/decode/touch/verify latency', 'display FPS and UI responsiveness',
                          'mobile/desktop real build and launch', 'competitor hardware head-to-head', 'GPU memory'],
              'rows': [], 'failures': [], 'skips': [], 'fixtures': []}
    box = Sandbox()
    tesseract = Path('/usr/bin/tesseract')
    traineddata = Path('/usr/share/tessdata/eng.traineddata')
    report['ocr_engine'] = {'standard_path': str(tesseract), 'executable_sha256': sha(tesseract),
                            'eng_traineddata_sha256': sha(traineddata),
                            'standard_route': 'TOMACCO_TESSERACT absent; PATH resolves /usr/bin/tesseract after hardware traps',
                            'custom_route': 'same executable explicitly selected with TOMACCO_TESSERACT',
                            'OMP_THREAD_LIMIT': box.env.get('OMP_THREAD_LIMIT'), 'OMP_NUM_THREADS': box.env.get('OMP_NUM_THREADS')}
    sessions = []
    def record(name, phase, operation, params, check, mcp, extra=None):
        try:
            value, row = mcp.call(operation, params)
            check(value)
            row.update(workload=name, phase=phase, operation=operation, correct=True, **(extra or {}))
            report['rows'].append(row)
            return value
        except Exception as error:
            report['failures'].append({'workload': name, 'phase': phase, 'error': str(error)})
            return None
    def require(condition, detail):
        if not condition:
            raise AssertionError(detail)
    try:
        binaries = [('cli_version', cli)]
        if args.desktop:
            desktop = args.desktop.resolve(strict=True)
            report['desktop_binary'] = {'path': str(desktop), 'sha256': sha(desktop),
                                        'scope': 'offscreen --version only; excludes window initialization, first frame, responsiveness and readiness'}
            binaries.append(('desktop_version_only', desktop))
        else:
            report['skips'].append('Desktop --version process sample: no --desktop provided')
        for workload, binary in binaries:
            for trial in range(args.cold_trials):
                env = dict(box.env)
                if workload == 'desktop_version_only':
                    env.update(QT_QPA_PLATFORM='offscreen', QT_QPA_PLATFORMTHEME='generic')
                    for key in ('DISPLAY', 'WAYLAND_DISPLAY', 'TOMACCO_RESTART_AFTER'):
                        env.pop(key, None)
                before = resource.getrusage(resource.RUSAGE_CHILDREN)
                started = time.perf_counter_ns()
                process = subprocess.Popen([str(binary), '--version'], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                sampler = Sampler({process.pid})
                try:
                    stdout, stderr = process.communicate(timeout=30)
                    elapsed = (time.perf_counter_ns() - started) / 1e6
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait()
                    sampler.close()
                after = resource.getrusage(resource.RUSAGE_CHILDREN)
                report['rows'].append({'workload': workload, 'phase': 'cold_process', 'wall_ms': elapsed,
                                       'wire_utf8_bytes': len(stdout), 'sampled_peak_rss_kib': sampler.peak,
                                       'cpu_ms': ((after.ru_utime + after.ru_stime) - (before.ru_utime + before.ru_stime)) * 1000,
                                       'correct': process.returncode == 0})
                require(process.returncode == 0 and b'tomacco' in stdout.lower(), stderr.decode(errors='replace'))
        fixtures = [fixture(box, 'small', 360, 640, 8), fixture(box, 'dense', 1080, 1920, 600)]
        report['fixtures'] = fixtures
        for trial in range(args.cold_trials):
            mcp = MCP(cli, box)
            sessions.append(mcp)
            report['rows'].append({'workload': 'mcp_initialize', 'phase': 'cold_process', 'wall_ms': mcp.start_ms, 'correct': True})
            for data in fixtures:
                name = data['name']
                state = record(name + '.start', 'cold_session', 'runtime.start',
                               {'platform': 'android', 'device': 'fixture:' + name, 'audio': False},
                               lambda v: require(v.get('evidence') == 'fixture', v), mcp)
                if not state:
                    continue
                sid = state['session']
                mcp.roots.add(state['pid'])
                def check_frame(value):
                    f = value['frame']
                    require(f['sha256'] == data['png_sha256'] == sha(f['path']) and
                            (f['width'], f['height']) == (data['width'], data['height']), 'frame hash or geometry mismatch')
                def tree_check(value):
                    require('Sign in' in value['text'], 'late target omitted')
                workloads = [
                    ('capture', 'runtime.observe', {'session': sid, 'context': False}, check_frame),
                    ('observe', 'runtime.observe', {'session': sid, 'context': True}, check_frame),
                    ('tree_full', 'screen.tree', {'device': sid, 'mode': 'tree', 'max_chars': 100000}, tree_check),
                    ('tree_compact', 'screen.tree', {'device': sid, 'mode': 'compact', 'query': 'Sign in', 'max_chars': 6000}, tree_check)]
                for label, op, params, check in workloads:
                    record(name + '.' + label, 'first_session_call', op, params, check, mcp,
                           {'fixture_sha256': data['xml_sha256'] if op == 'screen.tree' else data['png_sha256']})
                if trial == 0:
                    # Round-robin to distribute host-load drift between matched workloads.
                    for repeat in range(args.trials):
                        for label, op, params, check in workloads[repeat % len(workloads):] + workloads[:repeat % len(workloads)]:
                            record(name + '.' + label, 'warm', op, params, check, mcp,
                                   {'fixture_sha256': data['xml_sha256'] if op == 'screen.tree' else data['png_sha256']})
                        record(name + '.tap_verify', 'warm', 'runtime.actions',
                               {'session': sid, 'steps': [{'action': 'tap', 'x': .5, 'y': .5,
                                'expect': {'text': 'Loading', 'timeout_ms': 1000, 'interval_ms': 50}}]},
                               lambda v: require(v.get('verified') == 1 and v['steps'][0]['expect']['met'], v), mcp)
                        record(name + '.key_ack', 'warm', 'runtime.input', {'session': sid, 'action': 'key', 'key': 'back'},
                               lambda v: require(v.get('delivered') is True, v), mcp)
                record(name + '.stop', 'cleanup', 'runtime.stop', {'session': sid},
                       lambda v: require(v.get('stopped'), v), mcp)
                mcp.roots.discard(state['pid'])
            mcp.close()
            sessions.remove(mcp)
        manifest_path = args.corpus / 'manifest.json'
        if not manifest_path.exists():
            report['skips'].append('OCR: no corpus at ' + str(args.corpus))
        else:
            manifest = json.loads(manifest_path.read_text())
            wanted = {'light-panel', 'real-landscape', 'real-landscape-inversion-target'}
            cases = [case for case in manifest['cases'] if case['id'] in wanted]
            report['ocr_cases'] = cases
            require(len(cases) == 3, 'expected OCR cases unavailable')
            for trial in range(args.ocr_trials):
                # A fresh process for EACH variant: identical cache opportunity.
                variants = [(c, mode, detail) for c in cases for mode in ('auto', 'adaptive') for detail in ('full', 'matches')]
                variants = variants[trial % len(variants):] + variants[:trial % len(variants)]
                for case, mode, detail in variants:
                    image = args.corpus / case['image']
                    require(sha(image) == case['sha256'], 'OCR fixture hash mismatch')
                    mcp = MCP(cli, box)
                    sessions.append(mcp)
                    params = {'image': str(image.resolve()), 'language': 'eng', 'min_confidence': 40,
                              'invert': mode, 'detail': detail, 'text_contains': case['primary_target']}
                    name = 'ocr.' + case['id'] + '.' + mode + '.' + detail
                    def check_ocr(value):
                        require(len(value.get('matches', [])) == case.get('expected_primary_matches', 1), 'labelled match count differs')
                        require(('words' in value) == (detail == 'full'), 'wrong detail payload')
                        rects = case.get('primary_rects') or [case['primary_rect']]
                        for match in value['matches']:
                            centre = match['rect']
                            require(any(x <= centre['x'] < x + width and y <= centre['y'] < y + height
                                        for x, y, width, height in rects), 'match outside independently labelled target')
                    for phase in ('cold_process', 'warm_cached'):
                        value = record(name, phase, 'screen.ocr', params, check_ocr, mcp,
                                       {'fixture_sha256': case['sha256']})
                        if value:
                            report['rows'][-1].update({k: value.get(k) for k in ('passes', 'engine_runs', 'cached', 'fallback_reason', 'read_ms', 'capture_ms', 'total_ms')})
                    mcp.close()
                    sessions.remove(mcp)
            try:
                from PIL import Image, __version__ as pillow_version
            except ImportError:
                report['skips'].append('Changing-pixel OCR: optional Pillow unavailable')
            else:
                report['changing_image_method'] = {'pillow_version': pillow_version,
                    'transform': 'RGB conversion and one top-left pixel changed per call, outside all labelled primary target rectangles; same dimensions and text. PNG preparation excluded from timing.',
                    'limit': 'Deliberate cache-bypass recognition workload; not a moving-camera/video distribution.'}
                for case in cases:
                    generated = []
                    source = Image.open(args.corpus / case['image']).convert('RGB')
                    for trial in range(args.changing_trials):
                        image = source.copy()
                        image.putpixel((0, 0), ((trial * 37 + 11) % 256, (trial * 53 + 17) % 256, (trial * 71 + 23) % 256))
                        path = box.path / (case['id'] + '-' + str(trial) + '.png')
                        image.save(path)
                        generated.append((path, sha(path)))
                    require(len({digest for _, digest in generated}) == args.changing_trials, 'changing fixtures repeated')
                    for route in ('standard', 'custom_override'):
                        if route == 'custom_override':
                            box.env['TOMACCO_TESSERACT'] = str(tesseract)
                        for mode in ('auto', 'adaptive'):
                            mcp = MCP(cli, box)
                            sessions.append(mcp)
                            for trial, (path, digest) in enumerate(generated):
                                params = {'image': str(path), 'language': 'eng', 'min_confidence': 40,
                                          'invert': mode, 'detail': 'matches', 'text_contains': case['primary_target']}
                                def check_fresh(value):
                                    require(not value.get('cached') and value.get('engine_runs', 0) > 0, 'fresh pixels did not run OCR')
                                    require(len(value.get('matches', [])) == case.get('expected_primary_matches', 1), 'changing pixels lost labelled target')
                                    rects = case.get('primary_rects') or [case['primary_rect']]
                                    require(all(any(x <= m['rect']['x'] < x + w and y <= m['rect']['y'] < y + h for x, y, w, h in rects)
                                                for m in value['matches']), 'changing-pixel target outside labelled region')
                                value = record('ocr_changing.' + case['id'] + '.' + mode + '.' + route,
                                               'first_fresh_image' if trial == 0 else 'warm_fresh_image',
                                               'screen.ocr', params, check_fresh, mcp, {'fixture_sha256': digest, 'engine_route': route})
                                if value:
                                    report['rows'][-1].update({k: value.get(k) for k in ('passes', 'engine_runs', 'cached', 'fallback_reason', 'read_ms', 'total_ms')})
                            mcp.close()
                            sessions.remove(mcp)
                        box.env.pop('TOMACCO_TESSERACT', None)
    except Exception as error:
        report['failures'].append({'workload': 'harness', 'error': str(error)})
    finally:
        # Stop any surviving fixture workers before dropping isolated HOME.
        for mcp in sessions:
            try:
                value, _ = mcp.call('runtime.list', {})
                for session in value:
                    mcp.call('runtime.stop', {'session': session['session']})
            except Exception as error:
                report['failures'].append({'workload': 'cleanup', 'error': str(error)})
            mcp.close()
        report['trapped_helpers'] = box.close()
        if report['trapped_helpers']:
            report['failures'].append({'workload': 'hardware_trap', 'error': report['trapped_helpers']})
    metrics = {'wall_ms': 'ms', 'wire_utf8_bytes': 'bytes', 'text_utf8_bytes': 'bytes', 'cpu_ms': 'ms',
               'rss_kib_after': 'KiB', 'sampled_peak_rss_kib': 'KiB'}
    report['summary'] = {}
    for row in report['rows']:
        key = row['workload'] + ':' + row['phase']
        if key not in report['summary']:
            group = [r for r in report['rows'] if r['workload'] == row['workload'] and r['phase'] == row['phase']]
            report['summary'][key] = {m: distribution([r.get(m) for r in group], unit) for m, unit in metrics.items()}
            report['summary'][key]['correct'] = sum(r['correct'] for r in group)
    if args.compare:
        previous = json.loads(args.compare.read_text())
        if previous.get('tool_sha256') != report['tool_sha256']:
            raise SystemExit('Comparison requires the identical benchmark tool; rerun baseline after tool changes')
        comparisons = {}
        for key, current in report['summary'].items():
            old = previous.get('summary', {}).get(key)
            hashes = {r.get('fixture_sha256') for r in report['rows'] if r['workload'] + ':' + r['phase'] == key}
            old_hashes = {r.get('fixture_sha256') for r in previous['rows'] if r['workload'] + ':' + r['phase'] == key}
            if old and hashes == old_hashes and current['wall_ms']['n'] and old['wall_ms']['n']:
                comparisons[key] = {'wall_p50_ratio': current['wall_ms']['p50'] / old['wall_ms']['p50'],
                                    'old_n': old['wall_ms']['n'], 'new_n': current['wall_ms']['n']}
        report['comparison'] = {'baseline_sha256': sha(args.compare), 'binary': previous['binary'], 'matched': comparisons,
                                'warning': 'Unpaired runs; ratios describe this host/run only, not causal regressions or hardware speed.'}
    report['environment']['load_end'] = os.getloadavg()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'output': str(args.output), 'rows': len(report['rows']), 'failures': report['failures'], 'skips': report['skips']}))
    raise SystemExit(bool(report['failures']))


if __name__ == '__main__':
    main()

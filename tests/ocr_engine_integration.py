"""Local-file OCR worker integration; no discovery/device/global-config access.

python3 tests/ocr_engine_integration.py --cli target/release/tomaccoctl
Requires the optional system Tesseract5 C API and eng model for native cases.
"""
import argparse, hashlib, json, os, pathlib, queue, select, struct, subprocess, tempfile, threading, time, unittest
ROOT = pathlib.Path(__file__).resolve().parents[1]
CORPUS = ROOT / 'tests/fixtures/ocr-corpus'
CLI = ROOT / 'target/release/tomaccoctl'
TRAPS = ('adb', 'fastboot', 'ssh', 'scp', 'uxplay', 'bluetoothctl', 'ideviceinfo', 'idevicepair', 'iproxy', 'xtool', 'xcrun', 'pymobiledevice3', 'tidevice', 'go-ios', 'hyprctl', 'pactl', 'wpctl', 'tomacco')

class Client:

    def __init__(self, home, extra=None):
        self.cli = CLI
        self.home = home
        home.mkdir()
        (home / 'bin').mkdir()
        (home / 'run').mkdir(mode=448)
        self.trap = home / 'forbidden'
        for name in TRAPS:
            f = home / 'bin' / name
            f.write_text('#!/bin/sh\nprintf "%s\\n" ' + name + ' >> ' + str(self.trap) + '\nexit 97\n')
            f.chmod(448)
        self.env = {**os.environ, 'HOME': str(home), 'TOMACCO_HOME': str(home / 'tomacco'), 'XDG_CONFIG_HOME': str(home / 'config'), 'XDG_DATA_HOME': str(home / 'data'), 'XDG_STATE_HOME': str(home / 'state'), 'XDG_CACHE_HOME': str(home / 'cache'), 'XDG_RUNTIME_DIR': str(home / 'run'), 'PATH': str(home / 'bin') + ':/usr/bin:/bin', 'TESSDATA_PREFIX': '/usr/share/tessdata', 'LANG': 'C.UTF-8', 'DBUS_SYSTEM_BUS_ADDRESS': 'unix:path=' + str(home / 'no-bus'), 'DBUS_SESSION_BUS_ADDRESS': 'unix:path=' + str(home / 'no-bus')}
        for k in ('TOMACCO_TESSERACT', 'TOMACCO_PARENT_JOB', 'TOMACCO_TEST_ROOT', 'TOMACCO_POLICY', 'TOMACCO_USBMUXD_SOCKET_ADDRESS', 'OMP_THREAD_LIMIT', 'OMP_NUM_THREADS'):
            self.env.pop(k, None)
        self.env.update(extra or {})
        self.p = None
        self.ident = 0
        self.pending = {}
        self.replies = queue.Queue()

    def start(self):
        self.p = subprocess.Popen([str(self.cli), 'mcp'], env=self.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        def read():
            for line in self.p.stdout:
                try:
                    self.replies.put(json.loads(line))
                except ValueError:
                    self.replies.put({'invalid_json': line.decode(errors='replace')})
        self.threads = [threading.Thread(target=read, daemon=True), threading.Thread(target=lambda: self.p.stderr.read(), daemon=True)]
        for thread in self.threads:
            thread.start()
        i = self.send('initialize', dict(protocolVersion='2025-11-25', capabilities={}, clientInfo=dict(name='ocr-worker-tests', version='1')))
        self.reply(i)
        self.notify('notifications/initialized', {})
        return self

    def send(self, method, params):
        self.ident += 1
        self.p.stdin.write((json.dumps(dict(jsonrpc='2.0', id=self.ident, method=method, params=params)) + '\n').encode())
        self.p.stdin.flush()
        return self.ident

    def notify(self, method, params):
        self.p.stdin.write((json.dumps(dict(jsonrpc='2.0', method=method, params=params)) + '\n').encode())
        self.p.stdin.flush()

    def reply(self, i):
        while i not in self.pending:
            r = self.replies.get(timeout=120)
            if 'id' in r:
                self.pending[r['id']] = r
        return self.pending.pop(i)

    def finish(self, i):
        r = self.reply(i).get('result', {})
        text = next((c['text'] for c in r.get('content', []) if c.get('type') == 'text'), '{}')
        return (bool(r.get('isError')), json.loads(text))

    def begin(self, args):
        return self.send('tools/call', dict(name='screen.ocr', arguments=args))

    def call(self, args):
        return self.finish(self.begin(args))

    def workers(self):
        ids = set()
        out = []
        for task in pathlib.Path(f'/proc/{self.p.pid}/task').iterdir():
            try:
                ids.update((task / 'children').read_text().split())
            except FileNotFoundError:
                pass
        for pid in ids:
            try:
                if b'_ocr_engine' in pathlib.Path(f'/proc/{pid}/cmdline').read_bytes():
                    out.append(int(pid))
            except FileNotFoundError:
                pass
        return out

    def close(self):
        if self.p is not None:
            self.p.stdin.close()
            try:
                self.p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.p.kill()
                self.p.wait()
            for thread in self.threads:
                thread.join(timeout=1)
            self.p.stdout.close()
            self.p.stderr.close()
        if self.trap.exists():
            raise AssertionError('Hardware trap invoked: ' + self.trap.read_text())

def image(name, mode='auto', **kw):
    d = dict(image=str(CORPUS / (name + '.png')), language='eng', invert=mode, max_words=5000)
    d.update(kw)
    return d

def quality(v):
    return {k: v.get(k) for k in ('words', 'lines', 'matches', 'word_count', 'width', 'height', 'passes', 'fallback_reason')}

def rss(pid):
    try:
        return int(next((l.split()[1] for l in pathlib.Path(f'/proc/{pid}/status').read_text().splitlines() if l.startswith('VmRSS:'))))
    except (FileNotFoundError, StopIteration):
        return 0

def cpu(pid):
    s = pathlib.Path(f'/proc/{pid}/stat').read_text().split(') ', 1)[1].split()
    return int(s[11]) + int(s[12])

def read_frame(p):
    deadline = time.monotonic() + 10

    def exact(n):
        b = b''
        while len(b) < n:
            remain = deadline - time.monotonic()
            if remain <= 0 or not select.select([p.stdout], [], [], remain)[0]:
                raise AssertionError('private worker init timeout')
            chunk = os.read(p.stdout.fileno(), n - len(b))
            if not chunk:
                raise AssertionError('private worker exited: ' + str(p.poll()))
            b += chunk
        return b
    h = exact(5)
    length = struct.unpack('>I', h[1:])[0]
    assert length <= 4096
    return (h[0], exact(length))

class WorkerTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='tomacco-ocr-worker-test-')
        self.root = pathlib.Path(self.tmp.name)
        self.clients = []

    def tearDown(self):
        for c in self.clients:
            c.close()
        self.tmp.cleanup()

    def client(self, name, extra=None):
        c = Client(self.root / name, extra)
        self.clients.append(c)
        return c.start()

    def ok(self, c, a):
        error, value = c.call(a)
        self.assertFalse(error, value)
        return value

    def native(self, c):
        self.ok(c, image('light-panel', text_contains='Continue'))
        pids = c.workers()
        self.assertEqual(len(pids), 1, 'System native engine should be eligible')
        return pids[0]

    def test_fresh_alternating_images_match_cli_and_are_memory_bounded(self):
        a = self.client('native')
        b = self.client('forced-cli', {'TOMACCO_TESSERACT': '/usr/bin/tesseract'})
        pid = self.native(a)
        for name in ['real-portrait', 'minority-dark', 'real-landscape', 'small-text', 'duplicate-targets', 'dark-panel', 'noise-no-text', 'empty', 'real-build-controls', 'real-portrait']:
            args = image(name, text_contains='Continue')
            x = self.ok(a, args)
            y = self.ok(b, args)
            self.assertEqual(quality(x), quality(y), (name, x, y))
            self.assertGreater(x['engine_runs'], 0)
            self.assertEqual(a.workers(), [pid])
            self.assertLess(rss(pid), 256 * 1024)
        limits = pathlib.Path(f'/proc/{pid}/limits').read_text()
        self.assertIn('536870912', limits)
        environment = pathlib.Path(f'/proc/{pid}/environ').read_bytes().split(b'\x00')
        self.assertIn(b'OMP_THREAD_LIMIT=1', environment)

    def test_custom_and_path_wrappers_keep_arguments_and_explicit_omp(self):
        for custom in (True, False):
            name = 'custom' if custom else 'path'
            home = self.root / name
            c = Client(home, {'OMP_NUM_THREADS': '3'})
            log = home / 'calls'
            wrapper = home / 'bin' / 'tesseract'
            wrapper.write_text('#!/usr/bin/python3\nimport json,os,sys,pathlib\nwith open(' + repr(str(log)) + ',"a") as f: f.write(json.dumps({"args":sys.argv[1:],"limit":os.environ.get("OMP_THREAD_LIMIT"),"num":os.environ.get("OMP_NUM_THREADS")})+"\\n")\nprint("level\\tpage_num\\tblock_num\\tpar_num\\tline_num\\tword_num\\tleft\\ttop\\twidth\\theight\\tconf\\ttext\\n5\\t1\\t1\\t1\\t1\\t1\\t10\\t10\\t30\\t15\\t95\\tSave")\n')
            wrapper.chmod(448)
            if custom:
                c.env['TOMACCO_TESSERACT'] = str(wrapper)
            self.clients.append(c)
            c.start()
            v = self.ok(c, image('light-panel', 'never', text='Save'))
            self.assertEqual(v['matches'][0]['text'], 'Save')
            self.assertEqual(c.workers(), [])
            calls = [json.loads(s) for s in log.read_text().splitlines()]
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]['args'][1:], ['stdout', '-l', 'eng', '--psm', '11', 'tsv'])
            self.assertIsNone(calls[0]['limit'])
            self.assertEqual(calls[0]['num'], '3')

    def test_missing_language_falls_back_and_valid_language_rewarms(self):
        c = self.client('language')
        error, v = c.call(image('light-panel', language='zzztomacco_missing'))
        self.assertTrue(error, v)
        self.assertEqual(v.get('code', v.get('error', {}).get('code')), 'ocr_language_missing')
        self.native(c)

    def test_busy_requests_do_not_create_multiple_workers(self):
        c = self.client('busy')
        pid = self.native(c)
        a = c.begin(image('real-portrait'))
        b = c.begin(image('real-build-controls'))
        maximum = 0
        end = time.monotonic() + 2
        while time.monotonic() < end:
            maximum = max(maximum, len(c.workers()))
            time.sleep(0.01)
        for i in (a, b):
            error, v = c.finish(i)
            self.assertFalse(error, v)
            self.assertGreater(v['engine_runs'], 0)
        self.assertLessEqual(maximum, 1)
        self.assertEqual(c.workers(), [pid])

    def test_cancellation_reaps_active_worker_and_next_read_recovers(self):
        from PIL import Image, ImageDraw, ImageFont
        c = self.client('cancel')
        pid = self.native(c)
        p = self.root / 'dense4k.png'
        im = Image.new('RGB', (3840, 2160), 'white')
        draw = ImageDraw.Draw(im)
        font = ImageFont.truetype(str(ROOT / 'assets/fonts/archivo-var.ttf'), 24)
        for y in range(20, 2100, 40):
            draw.text((20, y), 'Account settings Save changes Continue Email address ' * 5, font=font, fill='black')
        im.save(p)
        ticks = cpu(pid)
        i = c.begin(dict(image=str(p), language='eng', invert='auto'))
        end = time.monotonic() + 15
        while cpu(pid) - ticks < 2 and time.monotonic() < end:
            time.sleep(0.002)
        self.assertGreaterEqual(cpu(pid) - ticks, 2, 'Worker must be active before cancellation')
        c.notify('notifications/cancelled', dict(requestId=i, reason='owned OCR worker cancellation test'))
        end = time.monotonic() + 3
        while pathlib.Path(f'/proc/{pid}').exists() and time.monotonic() < end:
            time.sleep(0.02)
        self.assertFalse(pathlib.Path(f'/proc/{pid}').exists())
        self.ok(c, image('dark-panel'))
        self.assertEqual(len(c.workers()), 1)
        self.assertNotEqual(c.workers()[0], pid)

    def test_140_changing_frames_recycle_one_worker_without_cache_hits(self):
        from PIL import Image, ImageDraw, ImageFont
        c = self.client('recycle')
        pid = self.native(c)
        p = self.root / 'changing.png'
        base = Image.new('RGB', (600, 180), '#f4f2ec')
        draw = ImageDraw.Draw(base)
        font = ImageFont.truetype(str(ROOT / 'assets/fonts/archivo-var.ttf'), 26)
        draw.text((30, 40), 'Save changes Continue', font=font, fill='black')
        pids = {pid}
        first = None
        for n in range(140):
            im = base.copy()
            im.putpixel((599, 179), (n, 240, 240))
            im.save(p)
            v = self.ok(c, dict(image=str(p), language='eng', invert='never', text_contains='Save changes'))
            self.assertFalse(v['cached'])
            self.assertEqual(v['engine_runs'], 1)
            self.assertEqual(v['matches'][0]['text'], 'Save changes')
            if first is None:
                first = quality(v)
            else:
                self.assertEqual(quality(v), first)
            workers = c.workers()
            self.assertEqual(len(workers), 1)
            pids.update(workers)
            self.assertLess(rss(workers[0]), 256 * 1024)
        self.assertEqual(len(pids), 2, 'The128-read budget must recycle the retained model exactly once')

    def test_clean_shutdown_reaps_and_host_death_kills_own_worker(self):
        c = self.client('shutdown')
        pid = self.native(c)
        c.close()
        end = time.monotonic() + 3
        while pathlib.Path(f'/proc/{pid}').exists() and time.monotonic() < end:
            time.sleep(0.01)
        self.assertFalse(pathlib.Path(f'/proc/{pid}').exists(), 'Normal shutdown must reap worker')
        c = self.client('hostdeath')
        pid = self.native(c)
        c.p.kill()
        c.p.wait(timeout=3)
        end = time.monotonic() + 3
        while rss(pid) > 0 and time.monotonic() < end:
            time.sleep(0.01)
        self.assertEqual(rss(pid), 0, 'PDEATHSIG/EOF must release the model when MCP dies')

    def test_cancel_during_initialization_does_not_leave_a_worker(self):
        c = self.client('init-cancel')
        i = c.begin(image('real-portrait'))
        end = time.monotonic() + 3
        pids = []
        while not pids and time.monotonic() < end:
            pids = c.workers()
            time.sleep(0.001)
        self.assertEqual(len(pids), 1)
        c.notify('notifications/cancelled', dict(requestId=i, reason='cancel native initialization'))
        end = time.monotonic() + 3
        while any((pathlib.Path(f'/proc/{pid}').exists() for pid in pids)) and time.monotonic() < end:
            time.sleep(0.01)
        self.assertFalse(any((pathlib.Path(f'/proc/{pid}').exists() for pid in pids)))
        self.ok(c, image('dark-panel'))
        self.assertEqual(len(c.workers()), 1)

    def test_unlinked_host_restarts_same_image_worker(self):
        import shutil
        c = Client(self.root / 'unlinked')
        binary = c.home / 'tomacco-unlinked'
        shutil.copyfile(CLI, binary)
        binary.chmod(448)
        c.cli = binary
        self.clients.append(c)
        c.start()
        pid = self.native(c)
        binary.unlink()
        self.assertIn('(deleted)', os.readlink(f'/proc/{c.p.pid}/exe'))
        os.kill(pid, 9)
        end = time.monotonic() + 3
        while rss(pid) > 0 and time.monotonic() < end:
            time.sleep(0.01)
        v = self.ok(c, image('dark-panel'))
        self.assertGreater(v['word_count'], 0)
        self.assertEqual(len(c.workers()), 1)
        self.assertNotEqual(c.workers()[0], pid)

    def test_idle_and_partial_request_release_models_then_rewarm(self):
        c = self.client('idle')
        pid = self.native(c)
        partial = subprocess.Popen([str(CLI), '_ocr_engine', 'eng'], env=c.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        try:
            status, version = read_frame(partial)
            self.assertEqual(status, 0)
            self.assertTrue(version.startswith(b'5.'))
            partial.stdin.write(b'\x00\x00')
            partial.stdin.flush()
            time.sleep(61.5)
            self.assertEqual(partial.poll(), 0, 'Partial header must not hold native model forever')
            self.assertFalse(pathlib.Path(f'/proc/{pid}').exists(), 'Idle MCP worker must be fully reaped, not left as a zombie')
            self.ok(c, image('dark-panel'))
            self.assertEqual(len(c.workers()), 1)
            self.assertNotEqual(c.workers()[0], pid)
        finally:
            if partial.poll() is None:
                partial.kill()
            partial.wait()
            partial.stdin.close()
            partial.stdout.close()
            partial.stderr.close()
if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--cli', type=pathlib.Path, default=CLI)
    args, remaining = parser.parse_known_args()
    CLI = args.cli.resolve()
    unittest.main(argv=[__file__, *remaining], verbosity=2)

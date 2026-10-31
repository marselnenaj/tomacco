"""Closed-disk metadata and original Cryptex integrity boundaries."""
import hashlib
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest

from test_ios_paravirt_firmware import im4p
from ios_apfs_layout import inspect
from ios_paravirt_storage import initialize_root_gpt
from prepare_ios_cryptex import (CACHES, DESTINATION_SHELL, IGNITION_LAYOUT_SHELL,
                                GRAFT_DESTINATION_SHELL, combine_caches, preboot_volume)
from test_product import Isolated
from native_client import StudioError, dispatch, jobs


def seal_block(block):
    a = b = 0
    for (word,) in struct.iter_unpack('<I', block[8:]):
        a = (a + word) % 0xffffffff
        b = (b + a) % 0xffffffff
    low = 0xffffffff - ((a + b) % 0xffffffff)
    high = 0xffffffff - ((a + low) % 0xffffffff)
    struct.pack_into('<II', block, 0, low, high)
    return bytes(block)


def container():
    blocks = [bytearray(4096) for _ in range(16)]
    nx = blocks[0]
    struct.pack_into('<QQII', nx, 8, 1, 5, 0x80000001, 0)
    nx[32:36] = b'NXSB'
    struct.pack_into('<IQ', nx, 36, 4096, 16)
    struct.pack_into('<I', nx, 0x68, 3)
    struct.pack_into('<Q', nx, 0x70, 1)
    struct.pack_into('<Q', nx, 0xa0, 4)
    struct.pack_into('<QQ', nx, 0xb8, 100, 200)
    blocks[1][:] = nx
    struct.pack_into('<Q', blocks[1], 16, 6)
    blocks[2][:] = nx  # Invalid newer checkpoint must not win selection.
    struct.pack_into('<Q', blocks[2], 16, 7)
    struct.pack_into('<QQII', blocks[4], 8, 4, 6, 0x4000000b, 0)
    struct.pack_into('<Q', blocks[4], 48, 5)
    node = blocks[5]
    struct.pack_into('<QQII', node, 8, 5, 6, 0x40000002, 0)
    struct.pack_into('<HHIHH', node, 32, 7, 0, 2, 0, 8)
    for index, (oid, address) in enumerate(((100, 6), (200, 7))):
        struct.pack_into('<HH', node, 56 + 4 * index, 16 * index, 16 * (index + 1))
        struct.pack_into('<QQ', node, 64 + 16 * index, oid, 6)
        struct.pack_into('<IIQ', node, 4056 - 16 * (index + 1), 0, 4096, address)
        volume = blocks[address]
        struct.pack_into('<QQII', volume, 8, oid, 6, 13, 0)
        volume[32:36] = b'APSB'
        struct.pack_into('<Q', volume, 56, 0x28 if index == 0 else 8)
        struct.pack_into('<Q', volume, 0xd8, 1 if index == 0 else 0)
        struct.pack_into('<H', volume, 0x3c4, 1 if index == 0 else 16)
        name = b'System' if index == 0 else b'Preboot'
        volume[0x2c0:0x2c0 + len(name)] = name
    struct.pack_into('<Q', blocks[6], 0x80, 8)
    struct.pack_into('<Q', blocks[6], 0x400, 300)
    struct.pack_into('<QQII', blocks[8], 8, 8, 6, 0x4000000b, 0)
    struct.pack_into('<Q', blocks[8], 48, 9)
    node = blocks[9]
    struct.pack_into('<QQII', node, 8, 9, 6, 0x40000002, 0)
    struct.pack_into('<HHIHH', node, 32, 7, 0, 1, 0, 4)
    struct.pack_into('<HH', node, 56, 0, 16)
    struct.pack_into('<QQ', node, 60, 300, 6)
    struct.pack_into('<IIQ', node, 4040, 0, 4096, 10)
    struct.pack_into('<QQII', blocks[10], 8, 300, 6, 0x1e, 0)
    struct.pack_into('<4I', blocks[10], 32, 2, 0, 1, 128)
    blocks[10][128:160] = b'H' * 32
    result = [seal_block(block) if index != 2 else bytes(block) for index, block in enumerate(blocks)]
    return b''.join(result)


class LayoutTests(unittest.TestCase):
    def test_follows_checkpoint_and_object_map_inside_real_gpt(self):
        with tempfile.TemporaryFile() as stream:
            stream.truncate(1 << 20)
            layout = initialize_root_gpt(stream, 1 << 20)
            stream.seek(layout['first_lba'] * 512)
            stream.write(container())
            stream.flush()
            actual = inspect(stream.fileno())
            self.assertEqual((actual['xid'], actual['checkpoint'], actual['partition']), (6, 1, 1))
            self.assertEqual([(v['oid'], v['role']) for v in actual['volumes']], [(100, 1), (200, 16)])
            self.assertEqual(preboot_volume(actual, (b'H' * 32).hex())['index'], 1)
            with self.assertRaisesRegex(ValueError, 'does not match'):
                preboot_volume(actual, '00' * 32)
            stream.seek(layout['first_lba'] * 512 + 6 * 4096 + 100)
            stream.write(b'corruption')
            stream.flush()
            with self.assertRaisesRegex(ValueError, 'checksum'):
                inspect(stream.fileno())

    def test_rejects_bad_gpt_and_unsealed_or_ambiguous_destination(self):
        with tempfile.TemporaryFile() as stream:
            stream.truncate(1 << 20)
            initialize_root_gpt(stream, 1 << 20)
            stream.seek(512 + 80)
            stream.write(b'bad')
            stream.flush()
            with self.assertRaisesRegex(ValueError, 'checksum'):
                inspect(stream.fileno())
        with self.assertRaisesRegex(ValueError, 'sealed System'):
            preboot_volume({'partition': 1, 'volumes': [{'role': 1, 'sealed': False, 'snapshots': 0}]})

    def test_detects_object_map_cycles_and_bounds(self):
        data = bytearray(container())
        node = bytearray(data[5 * 4096:6 * 4096])
        struct.pack_into('<HH', node, 32, 5, 1)  # Internal node references itself.
        for offset in (4040, 4024):
            struct.pack_into('<Q', node, offset, 5)
        data[5 * 4096:6 * 4096] = seal_block(node)
        with tempfile.TemporaryFile() as stream:
            stream.write(data)
            stream.flush()
            with self.assertRaisesRegex(ValueError, 'cycle'):
                inspect(stream.fileno())


class DestinationTests(unittest.TestCase):
    def prepare(self, path):
        return subprocess.run(['sh', '-eu', '-c', DESTINATION_SHELL + '\nprepare_destination "$1"',
                               'cryptex-destination', str(path)], capture_output=True, text=True)

    def test_accepts_original_empty_directories_and_preserves_siblings(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'Cryptexes'
            (root / 'OS').mkdir(parents=True)
            (root / 'App').mkdir()
            (root / 'Supplemental').mkdir()
            (root / 'Supplemental/keep').write_text('retained')
            result = self.prepare(root)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / 'Supplemental/keep').read_text(), 'retained')
            self.assertEqual(self.prepare(Path(temporary) / 'new').returncode, 0)

    def test_refuses_existing_contents_and_links_before_creating_directories(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'Cryptexes'
            (root / 'OS').mkdir(parents=True)
            (root / 'OS/keep').write_text('retained')
            self.assertIn('not empty', self.prepare(root).stderr)
            self.assertFalse((root / 'App').exists())
            self.assertEqual((root / 'OS/keep').read_text(), 'retained')
            alias = Path(temporary) / 'alias'
            alias.symlink_to(root, target_is_directory=True)
            self.assertIn('not an ordinary directory', self.prepare(alias).stderr)
            (root / 'App').symlink_to(Path(temporary) / 'missing', target_is_directory=True)
            self.assertIn('not an ordinary directory', self.prepare(root).stderr)

    def test_ignition_layout_requires_real_directory_and_retains_existing_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            def prepare():
                return subprocess.run(['sh', '-eu', '-c', IGNITION_LAYOUT_SHELL
                                       + '\nprepare_ignition_layout "$1"', 'ignition-layout', str(root)],
                                      capture_output=True, text=True)
            self.assertEqual(prepare().returncode, 0)
            self.assertTrue((root / 'cryptex1').is_dir())
            (root / 'cryptex1/metadata').write_text('retained')
            self.assertEqual(prepare().returncode, 0)
            self.assertEqual((root / 'cryptex1/metadata').read_text(), 'retained')
            (root / 'cryptex1').rename(root / 'original')
            (root / 'cryptex1').symlink_to(root / 'original', target_is_directory=True)
            self.assertIn('not an ordinary directory', prepare().stderr)
            (root / 'cryptex1').unlink()
            (root / 'cryptex1').write_text('retained file')
            self.assertIn('not an ordinary directory', prepare().stderr)

    def test_graft_destination_never_overwrites_existing_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            def prepare():
                return subprocess.run(['sh', '-eu', '-c', IGNITION_LAYOUT_SHELL + GRAFT_DESTINATION_SHELL
                                       + '\nprepare_ignition_layout "$1"\nprepare_graft_destination "$1"',
                                       'graft-layout', str(root)], capture_output=True, text=True)
            self.assertEqual(prepare().returncode, 0)
            destination = root / 'cryptex1/current'
            (destination / 'os.dmg').write_text('existing guest data')
            self.assertIn('not empty', prepare().stderr)
            self.assertEqual((destination / 'os.dmg').read_text(), 'existing guest data')
            destination.rename(root / 'retained')
            destination.symlink_to(root / 'retained', target_is_directory=True)
            self.assertIn('not an ordinary directory', prepare().stderr)


class TrustCacheTests(unittest.TestCase):
    def test_preserves_original_flags_and_rejects_corruption_and_conflicting_hashes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'components').mkdir()
            entries = {}
            items = [bytes([value]) * 20 + bytes([2, value, 0, 0]) for value in (1, 2, 3)]
            for index, name in enumerate(CACHES):
                cache = struct.pack('<I16sI', 2, bytes(16), 2) + items[index] + items[(index + 1) % 3]
                data = im4p(b'trst', cache)
                path = f'{index}.im4p'
                (root / 'components' / path).write_bytes(data)
                entries[name] = {'Info': {'Path': path}, 'Digest': hashlib.sha384(data).digest()}
            combined, record = combine_caches(root, entries)
            self.assertEqual(combined[24:], b''.join(items))
            self.assertEqual(record['entries'], 3)
            data = bytearray((root / 'components/0.im4p').read_bytes())
            data[-1] ^= 1
            (root / 'components/0.im4p').write_bytes(data)
            with self.assertRaisesRegex(ValueError, 'original manifest'):
                combine_caches(root, entries)
            entries[CACHES[0]]['Digest'] = hashlib.sha384(data).digest()
            with self.assertRaisesRegex(ValueError, 'conflicting'):
                combine_caches(root, entries)


class CryptexJobTests(Isolated):
    def test_real_job_retains_failure_receipt_and_source_disk(self):
        args = {'seconds': 20, 'accel': 'tcg'}
        for name in ('userland', 'support', 'bios'):
            path = self.root / name
            path.mkdir()
            args[name] = str(path)
        for name in ('disk', 'helper_engine', 'helper_kernel'):
            path = self.root / name
            path.write_text('synthetic, not a guest')
            args[name] = str(path)
        args['output'] = str(self.root / 'output')
        (Path(args['userland']) / 'userland.json').write_text('{"state":"failed"}')
        with self.assertRaisesRegex(StudioError, 'requires kernel, boot_manifest and device_tree'):
            dispatch('ios.emulator.prepare_cryptexes', {**args, 'kernel': args['disk']})
        with self.assertRaisesRegex(StudioError, 'GPU preparation requires'):
            dispatch('ios.emulator.prepare_cryptexes', {**args, 'gpu_trustcache': args['disk']})
        with self.assertRaisesRegex(StudioError, 'Installer control requires'):
            dispatch('ios.emulator.prepare_cryptexes', {**args, 'installer_control': True})
        with self.assertRaisesRegex(StudioError, 'Development mapping requires'):
            dispatch('ios.emulator.prepare_cryptexes', {**args, 'development_tools': args['userland']})
        self.assertFalse(Path(args['output']).exists())
        job = dispatch('ios.emulator.prepare_cryptexes', args)
        done = jobs.wait(job['id'], 10)
        self.assertEqual(done['state'], 'failed', jobs.logs(job['id']))
        receipt = dispatch('ios.emulator.report', {'job_id': job['id']})['report']
        self.assertEqual(receipt['state'], 'failed')
        self.assertIn('completed original', receipt['error'])
        self.assertEqual(Path(args['disk']).read_text(), 'synthetic, not a guest')
        self.assertFalse(receipt['guest_boot_attempted'])

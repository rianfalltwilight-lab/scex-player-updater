"""Cloud failure injection and real localhost sync; no cloud credentials."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import cloud_publish as cloud
from test_updater import ROOT, PS, write, json_write, serve, run


class MemoryStore:
    def __init__(self):
        self.objects = {}
        self.events = []
        self.fail = None

    def head(self, key):
        item = self.objects.get(key)
        if item is None:
            return None
        return {'ContentLength': len(item[0]), 'Metadata': {'sha256': item[2]},
                'ETag': hashlib.md5(item[0]).hexdigest()}

    def put(self, key, source, cache, digest):
        if self.fail and self.fail in key:
            raise OSError('injected upload failure')
        data = source if isinstance(source, bytes) else source.read_bytes()
        self.objects[key] = (data, cache, digest)
        self.events.append(key)

    def activate(self, raw, digest, previous_etag):
        current = self.head('server-manifest.json')
        if (current['ETag'] if current else None) != previous_etag:
            raise ValueError('concurrent publication')
        self.put('server-manifest.json', raw, cloud.LIVE, digest)

    def verify(self, key, digest, size):
        raw = self.objects[key][0]
        if len(raw) != size or hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError('bad readback')


def source_pack(root):
    rows = [write(root, 'mods/[测试] example.jar', b'mod v1'),
            write(root, 'resourcepacks/任务.zip', b'pack'),
            write(root, 'config/custom.txt', b'server config'),
            write(root, 'UPDATE-URL.txt', b'https://old.example/private-path/server-manifest.json\n')]
    obj = {'packId': 'fixture', 'version': '1.0', 'files': rows,
           'preservePlayerCustomizations': True, 'preserveLocalChangeGlobs': ['config/*'],
           'preserveLocalDeletionGlobs': ['config/*'], 'additiveDirs': ['resourcepacks', 'saves'],
           'cleanup': {'removeConnectorCache': False, 'disableLauncherRepairIndex': False,
                       'disableDuplicateMods': False}, 'playerOptions': {}, 'serverList': {'enabled': False}}
    json_write(root / 'server-manifest.json', obj)
    write(root, 'secret-not-listed.txt', b'private sentinel')
    return obj


class CloudPublishTests(unittest.TestCase):
    def test_origin_bridge_backup_direct_layout_and_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source'
            source.mkdir()
            source_pack(source)
            plan = cloud.prepare(source, 'https://cloud.example/pack', ROOT / 'tools')
            before = {p.relative_to(source).as_posix(): p.read_bytes() for p in source.rglob('*') if p.is_file()}
            real_replace = cloud.os.replace
            attempts = []
            def fail_once(src, dest):
                attempts.append(str(dest))
                if len(attempts) == 2:
                    raise OSError('injected bridge failure')
                return real_replace(src, dest)
            with patch.object(cloud.os, 'replace', side_effect=fail_once):
                with self.assertRaises(OSError):
                    cloud.bridge_source(plan, source, root / 'backups')
            for rel, data in before.items():
                self.assertEqual((source / rel).read_bytes(), data)
            receipt = cloud.bridge_source(plan, source, root / 'backups')
            self.assertEqual((Path(receipt['backup']) / 'server-manifest.json').read_bytes(), before['server-manifest.json'])
            direct = json.loads((source / 'server-manifest.json').read_bytes())
            self.assertFalse(any('downloadPath' in row for row in direct['files']))
            cloud.prepare(source, 'https://cloud.example/pack')
            self.assertEqual((source / 'UPDATE-URL.txt').read_text(), 'https://cloud.example/pack/server-manifest.json\n')
            with self.assertRaises(ValueError):
                cloud.bridge_source(plan, source, root / 'backups')

    def test_new_configured_source_precedes_retired_last_good(self):
        new = 'https://cloud.example/server-manifest.json'
        old = 'http://old.example/server-manifest.json'
        for file, function in [('player-update-generic.py', 'order_manifest_urls'),
                               ('portable-stage-daemon.py', 'order_update_urls')]:
            spec = importlib.util.spec_from_file_location('source_order_test', ROOT / 'tools' / file)
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            order = getattr(module, function)
            self.assertEqual(order([new], old), [new, old])
            self.assertEqual(order([new, old], old), [old, new])
            self.assertEqual(order([new], 'http://192.168.1.2/m.json'), [new])

    def test_incremental_last_manifest_and_old_snapshot_retention(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_pack(root)
            store = MemoryStore()
            plan = cloud.prepare(root, 'https://cdn.example/packs')
            first = cloud.publish(plan, store)
            self.assertEqual(store.events[-1], 'server-manifest.json')
            self.assertFalse(any(b'private sentinel' in v[0] for v in store.objects.values()))
            self.assertNotIn(b'private-path', store.objects['server-manifest.json'][0])
            original = dict(store.objects)
            again = cloud.publish(plan, store)
            self.assertEqual(again['uploaded'], 0)
            self.assertEqual(again['reused'], first['uploaded'])
            # A changed file gets a new immutable address; the old object survives.
            obj = json.loads((root / 'server-manifest.json').read_text(encoding='utf-8'))
            obj['version'] = '2.0'
            obj['files'][0] = write(root, 'mods/[测试] example.jar', b'mod v2')
            json_write(root / 'server-manifest.json', obj)
            second = cloud.publish(cloud.prepare(root, 'https://cdn.example/packs'), store)
            self.assertEqual(second['uploaded'], 1)
            self.assertIn(first['snapshot'], store.objects)
            for key in original:
                if key.startswith('blobs/'):
                    self.assertEqual(original[key], store.objects[key])

    def test_failed_upload_and_corrupt_readback_never_activate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_pack(root)
            plan = cloud.prepare(root, 'https://cdn.example/packs')
            store = MemoryStore()
            store.fail = 'blobs/'
            with self.assertRaises(OSError):
                cloud.publish(plan, store)
            self.assertNotIn('server-manifest.json', store.objects)
            store.fail = None
            with patch.object(store, 'verify', side_effect=ValueError('corrupt CDN')):
                with self.assertRaises(ValueError):
                    cloud.publish(plan, store)
            self.assertNotIn('server-manifest.json', store.objects)

    def test_missing_provider_metadata_requires_full_byte_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_pack(root)
            plan = cloud.prepare(root, 'https://cdn.example/packs')
            store = MemoryStore()
            cloud.publish(plan, store)
            original_head = store.head
            def no_metadata(key):
                info = original_head(key)
                if info:
                    info['Metadata'] = {}
                return info
            store.head = no_metadata
            repeated = cloud.publish(plan, store)
            self.assertEqual(repeated['uploaded'], 0)
            blob_key = next(iter(plan['blobs']))
            raw, cache, digest = store.objects[blob_key]
            store.objects[blob_key] = (b'X' * len(raw), cache, digest)
            store.events.clear()
            with self.assertRaises(ValueError):
                cloud.publish(plan, store)
            self.assertNotIn('server-manifest.json', store.events)

    def test_identity_drift_path_escape_and_duplicate_fail_before_upload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            obj = source_pack(root)
            (root / 'mods/[测试] example.jar').write_bytes(b'tampered')
            with self.assertRaises(ValueError):
                cloud.prepare(root, 'https://cdn.example/packs')
            source_pack(root)
            obj['files'].append(dict(obj['files'][0]))
            json_write(root / 'server-manifest.json', obj)
            with self.assertRaises(ValueError):
                cloud.prepare(root, 'https://cdn.example/packs')
            for value in ('../secrets', 'C:/private', '/absolute', 'a\\b', 'a/../b'):
                with self.assertRaises(ValueError):
                    cloud.relative(value)

    def test_concurrent_switch_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_pack(root)
            plan = cloud.prepare(root, 'https://cdn.example/packs')
            store = MemoryStore()
            original_put = store.put
            def racing_put(key, source, cache, digest):
                original_put(key, source, cache, digest)
                if key.startswith('manifests/'):
                    original_put('server-manifest.json', b'other publisher', cloud.LIVE, 'other')
            store.put = racing_put
            with self.assertRaises(ValueError):
                cloud.publish(plan, store)
            self.assertEqual(store.objects['server-manifest.json'][0], b'other publisher')

    def test_signed_input_is_not_silently_downgraded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            obj = source_pack(root)
            obj['signature'] = {'algorithm': 'ed25519', 'value': 'fixture'}
            json_write(root / 'server-manifest.json', obj)
            with self.assertRaises(ValueError):
                cloud.prepare(root, 'https://cdn.example/packs')

    def exercise_sync(self, engine):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, web, client = root / 'source', root / 'web', root / 'client'
            source.mkdir(); web.mkdir(); client.mkdir()
            obj = source_pack(source)
            plan = cloud.prepare(source, 'https://cdn.example/packs', ROOT / 'tools')
            store = MemoryStore()
            cloud.publish(plan, store)
            for key, value in store.objects.items():
                write(web, key, value[0])
            personal = {'config/custom.txt': b'player config', 'mods/player.jar': b'personal mod',
                        'saves/world/player.dat': b'world sentinel'}
            for rel, raw in personal.items():
                write(client, rel, raw)
            json_write(client / '.portable-sync-state.json', {'version': '0', 'files': obj['files']})
            with serve(web) as port:
                url = f'http://127.0.0.1:{port}/server-manifest.json'
                if engine == 'python':
                    cmd = [sys.executable, str(ROOT / 'tools/player-update-generic.py'),
                           '--instance-dir', str(client), '--manifest-url', url]
                else:
                    cmd = [PS, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                           str(ROOT / 'tools/player-update-generic.ps1'), '-InstanceDir', str(client),
                           '-ManifestUrl', url, '-NoPause']
                run(cmd, root)
                # Repeat is idempotent, even though remote content uses blob paths.
                run(cmd, root)
            self.assertEqual((client / 'mods/[测试] example.jar').read_bytes(), b'mod v1')
            self.assertEqual((client / 'resourcepacks/任务.zip').read_bytes(), b'pack')
            for rel, data in personal.items():
                self.assertEqual((client / rel).read_bytes(), data)

    def test_python_cloud_sync_and_player_preservation(self):
        self.exercise_sync('python')

    @unittest.skipUnless(PS and os.name == 'nt', 'Requires Windows PowerShell')
    def test_powershell_cloud_sync_and_player_preservation(self):
        self.exercise_sync('powershell')

    def test_python_background_download_and_invalid_cloud_path(self):
        spec = importlib.util.spec_from_file_location('stage_cloud_test', ROOT / 'tools/portable-stage-daemon.py')
        stage = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = stage
        spec.loader.exec_module(stage)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = b'background download'
            key = 'blobs/' + hashlib.sha256(raw).hexdigest()
            write(root, key, raw)
            item = {'downloadPath': key}
            with serve(root) as port:
                base = f'http://127.0.0.1:{port}/'
                stage.download_staged_entry(item, base, root / 'staged.jar', hashlib.sha1(raw).hexdigest(),
                                            'mods/test.jar', root / 'test.log')
                self.assertEqual((root / 'staged.jar').read_bytes(), raw)
                with self.assertRaises(ValueError):
                    stage.download_staged_entry({'downloadPath': '../secret'}, base, root / 'bad.jar',
                                                '0' * 40, 'mods/test.jar', root / 'test.log')


if __name__ == '__main__':
    unittest.main()

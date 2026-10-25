"""Staged cloud publication: synthetic candidates and an in-memory store only."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from test_cloud_publish import cloud, MemoryStore, source_pack
from test_updater import json_write, write
from test_portable_server import fixture, server


class StagedCloudTests(unittest.TestCase):
    def test_stage_check_and_activate_keep_live_files_until_activation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_pack(root)
            plan = cloud.prepare(root, 'https://cdn.example/pack')
            store = MemoryStore()
            old = b'previous live manifest'
            store.put('server-manifest.json', old, cloud.LIVE, hashlib.sha256(old).hexdigest())
            store.events.clear()
            receipt = cloud.stage(plan, store)
            self.assertEqual(receipt['status'], 'STAGED_NOT_ACTIVATED')
            self.assertEqual(store.objects['server-manifest.json'][0], old)
            self.assertNotIn('server-manifest.json', store.events)
            self.assertNotIn('UPDATE-URL.txt', store.events)
            before = list(store.events)
            self.assertEqual(cloud.check_staged(plan, store, receipt)['status'], 'STAGED_CHECK_PASS')
            self.assertEqual(store.events, before)
            result = cloud.activate_staged(plan, store, receipt)
            self.assertEqual(result['status'], 'PUBLISHED_AND_READ_BACK')
            self.assertEqual(store.events[-1], 'server-manifest.json')
            self.assertEqual(json.loads(store.objects['server-manifest.json'][0]), plan['manifest'])

    def test_changed_policy_store_or_objects_cannot_activate(self):
        for change in ('policy', 'store', 'object', 'live'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source_pack(root)
                plan = cloud.prepare(root, 'https://cdn.example/pack')
                store = MemoryStore()
                store.identity = 'store-one'
                receipt = cloud.stage(plan, store)
                if change == 'policy':
                    plan['manifest']['forceDeleteGlobs'] = ['mods/personal.jar']
                elif change == 'store':
                    store.identity = 'store-two'
                elif change == 'object':
                    key = next(iter(plan['blobs']))
                    raw, cache, digest = store.objects[key]
                    store.objects[key] = (b'X' * len(raw), cache, digest)
                else:
                    store.put('server-manifest.json', b'another release', cloud.LIVE, 'other')
                before = copy.deepcopy(store.objects)
                with self.assertRaises(ValueError):
                    cloud.activate_staged(plan, store, receipt)
                self.assertEqual(store.objects, before)

    def test_timestamp_and_route_refresh_keep_staged_content_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_pack(root)
            plan = cloud.prepare(root, 'https://cdn.example/pack')
            store = MemoryStore()
            receipt = cloud.stage(plan, store)
            plan['manifest']['generatedAt'] = '2026-10-25T00:00:00Z'
            plan['manifest']['files'][0]['url'] = 'https://cdn.modrinth.com/test.jar'
            result = cloud.activate_staged(plan, store, receipt)
            self.assertNotEqual(result['snapshot'], receipt['snapshot'])
            self.assertIn(receipt['snapshot'], store.objects)
            self.assertEqual(store.events[-1], 'server-manifest.json')

    def test_mirrors_written_to_manifest_and_player_address(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_pack(root)
            mirror = 'https://mirror.example/pack/server-manifest.json'
            plan = cloud.prepare(root, 'https://cdn.example/pack', mirror_urls=[mirror, mirror])
            self.assertEqual(plan['manifest']['mirrorManifestUrls'], [mirror])
            key = plan['bootstrap']['UPDATE-URL.txt']
            self.assertEqual(plan['blobs'][key]['source'].decode(),
                             'https://cdn.example/pack/server-manifest.json\n' + mirror + '\n')
            with self.assertRaises(ValueError):
                cloud.prepare(root, 'https://cdn.example/pack', mirror_urls=['https://user:secret@mirror.example/m.json'])

    def test_release_plan_overlay_without_copying_or_modifying_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source, candidate = base / 'source', base / 'candidate'
            source.mkdir()
            candidate.mkdir()
            baseline = source_pack(source)
            original = {p.relative_to(source).as_posix(): p.read_bytes() for p in source.rglob('*') if p.is_file()}
            old_row = baseline['files'][0]
            new_row = write(candidate, 'mods/new.jar', b'new mod')
            proposed = copy.deepcopy(baseline)
            proposed['version'] = '1.1'
            proposed['files'] = baseline['files'][1:] + [new_row]
            release = {
                'baseline_root': str(source),
                'baseline_manifest_sha256': hashlib.sha256(original['server-manifest.json']).hexdigest(),
                'version': '1.1',
                'remove_paths': [{'path': old_row['path'], 'sha256': hashlib.sha256(original[old_row['path']]).hexdigest()}],
                'files': [{'path': new_row['path'], 'source': str(candidate / new_row['path']),
                           'sha256': hashlib.sha256(b'new mod').hexdigest(), 'size': len(b'new mod')}],
            }
            json_write(candidate / 'release-plan.json', release)
            json_write(candidate / 'manifest.proposed.json', proposed)
            plan = cloud.prepare_release_plan(candidate, source, 'https://cdn.example/pack')
            self.assertEqual(plan['manifest']['version'], '1.1')
            self.assertEqual({p.relative_to(source).as_posix(): p.read_bytes() for p in source.rglob('*') if p.is_file()}, original)
            (candidate / new_row['path']).write_bytes(b'changed candidate')
            with self.assertRaisesRegex(ValueError, 'candidate changed'):
                cloud.prepare_release_plan(candidate, source, 'https://cdn.example/pack')
            (candidate / new_row['path']).write_bytes(b'new mod')
            (source / 'server-manifest.json').write_bytes(original['server-manifest.json'] + b'\n')
            with self.assertRaisesRegex(ValueError, 'baseline changed'):
                cloud.prepare_release_plan(candidate, source, 'https://cdn.example/pack')


class PortableDownloadPolicyTests(unittest.TestCase):
    def test_published_worker_count_and_revision_follow_config(self):
        with fixture() as (root, config):
            config['downloadWorkers'] = 2
            config['updaterRevision'] = 'fixture-current'
            server.publish(root, config)
            manifest = server.read_json(root / config['publishDir'] / 'server-manifest.json')
            self.assertEqual(manifest['downloadWorkers'], 2)
            self.assertEqual(manifest['updaterRevision'], 'fixture-current')

    def test_worker_limits_match_clients(self):
        with fixture() as (root, config):
            for supplied, expected in [(0, 1), (32, 8), (4, 4)]:
                config['downloadWorkers'] = supplied
                self.assertEqual(server.resolve_config(root, config)[0]['downloadWorkers'], expected)


if __name__ == '__main__':
    unittest.main()
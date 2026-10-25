"""Exact-file official source indexing with offline provider responses."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from test_updater import PS, ROOT, install_publish_key, json_write, run, write


def load_index():
    spec = importlib.util.spec_from_file_location('source_index', ROOT / 'tools/official-source-index.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SourceIndexTests(unittest.TestCase):
    def setUp(self):
        self.index = load_index()

    def test_exact_hash_sources_preference_and_private_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            official = write(root, 'mods/original.jar', b'original mod')
            patched = write(root, 'mods/patched.jar', b'locally patched mod')
            mr = 'https://cdn.modrinth.com/data/example/versions/exact/original.jar'
            cf = 'https://edge.forgecdn.net/files/1234/567/original.jar'
            secret = 'test-only-private-api-key'

            def lookup(url, body, key=''):
                if 'modrinth' in url:
                    self.assertEqual(set(body['hashes']), {official['sha1'], patched['sha1']})
                    return {official['sha1']: {'files': [{'hashes': {'sha1': official['sha1']}, 'url': mr}]},
                            patched['sha1']: {'files': [{'hashes': {'sha1': '0' * 40}, 'url': mr}]}}
                self.assertEqual(key, secret)
                self.assertEqual(len(body['fingerprints']), 2)
                return {'data': {'exactMatches': [
                    {'file': {'hashes': [{'algo': 1, 'value': official['sha1']}], 'downloadUrl': cf}},
                    {'file': {'hashes': [{'algo': 1, 'value': '0' * 40}], 'downloadUrl': cf}}]}}

            manifest = {'files': [official, patched], 'downloadWorkers': 7}
            cache = {}
            with mock.patch.object(self.index, 'request_json', side_effect=lookup):
                counts = self.index.enrich(manifest, root, cache, secret)
            self.assertEqual(official['downloads'], [cf, mr])
            self.assertNotIn('downloads', patched)
            self.assertEqual(counts, {'curseforge': 1, 'modrinth': 1, 'official_unique': 1})
            self.assertEqual(manifest['downloadWorkers'], 7)
            self.assertNotIn(secret, json.dumps(manifest) + json.dumps(cache))

    def test_cached_source_survives_provider_failure(self):
        known = {'path': 'mods/cached.jar', 'sha1': 'a' * 40}
        unknown = {'path': 'mods/new.jar', 'sha1': 'b' * 40}
        url = 'https://cdn.modrinth.com/data/example/versions/exact/cached.jar'
        cache = {'files': {known['sha1']: {'modrinth': url}}}
        manifest = {'files': [known, unknown]}
        with mock.patch.object(self.index, 'request_json', side_effect=RuntimeError('metadata unavailable')):
            self.index.enrich(manifest, Path('.'), cache)
        self.assertEqual(known['downloads'], [url])
        self.assertNotIn('downloads', unknown)
        self.assertEqual(manifest['downloadSourceStatus']['curseforgeStatus'], 'key_required')

    def test_changed_file_rejected_before_fingerprint_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            row = write(root, 'mods/changed.jar', b'before')
            write(root, row['path'], b'after')
            with mock.patch.object(self.index, 'request_json', return_value={}):
                with self.assertRaisesRegex(ValueError, 'changed'):
                    self.index.enrich({'files': [row]}, root, {}, 'test-key')

    def test_provider_whitelist_and_fingerprint_whitespace(self):
        for url in ('http://cdn.modrinth.com/a', 'https://cdn.modrinth.com.example.org/a',
                    'https://user:secret@edge.forgecdn.net/a', 'https://forgecdn.net.example.org/a'):
            self.assertIsNone(self.index.provider(url))
        self.assertEqual(self.index.provider('https://edge.forgecdn.net/a'), 'curseforge')
        self.assertEqual(self.index.fingerprint(b'ab cd\n\t\ref'), self.index.fingerprint(b'abcdef'))

    def test_worker_default_and_bounds(self):
        for value, expected in ((None, 4), (0, 1), (99, 8)):
            manifest = {'files': []}
            if value is not None:
                manifest['downloadWorkers'] = value
            self.index.enrich(manifest, Path('.'), {})
            self.assertEqual(manifest['downloadWorkers'], expected)


@unittest.skipUnless(PS and os.name == 'nt', 'Publisher requires Windows PowerShell')
class SourceIndexPublisherTests(unittest.TestCase):
    def test_manifest_settings_endpoint_order_and_failed_index_retains_manifest(self):
        with tempfile.TemporaryDirectory(prefix='updater-source-publish-') as tmp:
            root = Path(tmp)
            shutil.copytree(ROOT / 'tools', root / 'tools')
            shutil.copytree(ROOT / '一键脚本', root / '一键脚本')
            install_publish_key(root)
            config = json.loads((root / 'tools/portable-pack.example.json').read_text('utf-8'))
            config['includeRoots'] = ['config']
            config['downloadWorkers'] = 6
            config['updaterRevision'] = 'test-revision'
            config['update'].update(pythonExe=sys.executable, host='update.example.com', lanHost='192.0.2.10')
            json_write(root / 'tools/portable-pack.json', config)
            write(root, 'main-client/config/example.txt', b'synthetic config')
            command = [PS, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                       str(root / 'tools/portable-publish.ps1')]
            run(command, root)
            public = root / 'modpack-public/portable'
            manifest_path = public / 'server-manifest.json'
            before = manifest_path.read_bytes()
            manifest = json.loads(before)
            self.assertEqual(manifest['downloadWorkers'], 6)
            self.assertEqual(manifest['updaterRevision'], 'test-revision')
            self.assertEqual(manifest['downloadSourceStatus']['official_unique'], 0)
            urls = (public / '_updater/UPDATE-URL.txt').read_text('utf-8').splitlines()
            self.assertEqual(len(urls), 2)
            self.assertTrue(urls[0].startswith('http://update.example.com:'))
            self.assertTrue(urls[1].startswith('http://192.0.2.10:'))
            # ROOT_SLIM_V1: address files moved to _updater; the root keeps the
            # entry points plus 使用说明.txt.
            for name in ('UPDATE-URL.txt', 'PORTABLE-UPDATE-URL.txt', 'UPDATE-URL-LAN.txt',
                         'SERVER.txt', 'README-sync.txt'):
                self.assertFalse((public / name).exists(), name)
            listed = {row['path'] for row in manifest['files']}
            for rel in ('_updater/UPDATE-URL.txt', '_updater/PORTABLE-UPDATE-URL.txt',
                        '_updater/UPDATE-URL-LAN.txt', '使用说明.txt'):
                self.assertIn(rel, listed)
            self.assertFalse({'UPDATE-URL.txt', 'SERVER.txt', 'README-sync.txt'} & listed)
            guide = (public / '使用说明.txt').read_bytes()
            self.assertTrue(guide.startswith(b'\xef\xbb\xbf'))
            self.assertEqual(guide.count(b'\n'), guide.count(b'\r\n'))
            self.assertIn('更新诊断包.zip', guide.decode('utf-8-sig'))
            # PUBLISH_PREVIEW_V1: -Preview reports and leaves every published byte alone.
            published = {p.relative_to(public).as_posix(): p.read_bytes()
                         for p in public.rglob('*') if p.is_file()}
            write(root, 'main-client/config/added.txt', b'preview only')
            preview = subprocess.run(command + ['-Version', '1.0.1', '-Preview'], cwd=root,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=90)
            # Console code page differs per machine; only check the ASCII path.
            text = preview.stdout.decode('utf-8', errors='replace')
            self.assertEqual(preview.returncode, 0, text)
            self.assertIn('config/added.txt', text)
            self.assertEqual({p.relative_to(public).as_posix(): p.read_bytes()
                              for p in public.rglob('*') if p.is_file()}, published)
            (root / 'main-client/config/added.txt').unlink()
            (root / 'tools/official-source-index.py').write_text('raise SystemExit(9)\n', encoding='utf-8')
            result = subprocess.run(command + ['-Version', '1.0.1'], cwd=root,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=90)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(manifest_path.read_bytes(), before)
            self.assertFalse((public / '.server-manifest.json.next').exists())

    def test_powershell_only_publish_keeps_worker_settings(self):
        with tempfile.TemporaryDirectory(prefix='updater-source-no-python-') as tmp:
            root = Path(tmp)
            shutil.copytree(ROOT / 'tools', root / 'tools')
            shutil.copytree(ROOT / '一键脚本', root / '一键脚本')
            install_publish_key(root)
            config = json.loads((root / 'tools/portable-pack.example.json').read_text('utf-8'))
            config['includeRoots'] = ['config']
            config['downloadWorkers'] = 99
            config['update']['pythonExe'] = str(root / 'not-installed-python.exe')
            json_write(root / 'tools/portable-pack.json', config)
            write(root, 'main-client/config/example.txt', b'synthetic config')
            run([PS, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                 str(root / 'tools/portable-publish.ps1')], root)
            manifest = json.loads((root / 'modpack-public/portable/server-manifest.json').read_text('utf-8'))
            self.assertEqual(manifest['downloadWorkers'], 8)
            self.assertNotIn('downloadSourceStatus', manifest)


if __name__ == '__main__':
    unittest.main()

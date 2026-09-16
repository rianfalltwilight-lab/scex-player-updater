"""Synthetic, localhost-only integration tests; never read a real client/config."""
import contextlib
import functools
import hashlib
import http.client
import http.server
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
PS = os.environ.get('UPDATER_TEST_POWERSHELL') or shutil.which('powershell.exe')


def write(root, rel, data):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {'path': rel, 'sha1': hashlib.sha1(data).hexdigest(), 'size': len(data)}


def json_write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')


class SilentHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@contextlib.contextmanager
def serve(root, handler=SilentHandler, **kwargs):
    server = http.server.ThreadingHTTPServer(
        ('127.0.0.1', 0), functools.partial(handler, directory=str(root), **kwargs))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def run(command, cwd):
    env = dict(os.environ, PYTHONUTF8='1')
    result = subprocess.run(command, cwd=cwd, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, timeout=90)
    if result.returncode:
        raise AssertionError(result.stdout.decode('utf-8', errors='replace'))
    return result.stdout


class SyncTests(unittest.TestCase):
    def exercise_sync(self, engine):
        with tempfile.TemporaryDirectory(prefix='updater-regression-') as tmp:
            base = Path(tmp)
            web, client = base / 'web', base / 'client'
            web.mkdir()
            client.mkdir()
            old_data = {'mods/demo-1.jar': b'old mod',
                        'resourcepacks/demo.zip': b'old resource',
                        'config/custom.txt': b'original config',
                        'config/deleted.txt': b'original deleted'}
            old = [write(client, rel, data) for rel, data in old_data.items()]
            (client / 'config/deleted.txt').unlink()
            protected = {'mods/player-extra.jar': b'personal mod',
                         'config/custom.txt': b'personal config',
                         'saves/world/sentinel.txt': b'world data',
                         'voxy/cache/sentinel.txt': b'cache data',
                         'options.txt': b'renderDistance:12\n'}
            for rel, data in protected.items():
                write(client, rel, data)
            new_data = {'mods/demo-2.jar': b'new mod',
                        'resourcepacks/demo.zip': b'new resource',
                        'config/custom.txt': b'new server config',
                        'config/deleted.txt': b'new deleted config'}
            rows = [write(web, rel, data) for rel, data in new_data.items()]
            json_write(client / '.portable-sync-state.json', {'version': '1.0', 'files': old})
            manifest = {'version': '2.0', 'files': rows,
                        'preservePlayerCustomizations': True,
                        'preserveLocalChangeGlobs': ['config/*'],
                        'preserveLocalDeletionGlobs': ['config/*'],
                        'additiveDirs': ['resourcepacks', 'saves'], 'adoptExistingFiles': True,
                        'forceSyncGlobs': ['mods/demo-2.jar', 'resourcepacks/demo.zip'],
                        'forceDeleteGlobs': ['mods/demo-1.jar'],
                        'cleanup': {'removeConnectorCache': False,
                                    'disableLauncherRepairIndex': False,
                                    'disableDuplicateMods': False},
                        'playerOptions': {}, 'serverList': {'enabled': False}}
            json_write(web / 'server-manifest.json', manifest)
            with serve(web) as port:
                url = f'http://127.0.0.1:{port}/server-manifest.json'
                if engine == 'python':
                    command = [sys.executable, str(ROOT / 'tools/player-update-generic.py'),
                               '--instance-dir', str(client), '--manifest-url', url]
                else:
                    command = [PS, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                               str(ROOT / 'tools/player-update-generic.ps1'),
                               '-InstanceDir', str(client), '-ManifestUrl', url, '-NoPause']
                for iteration in range(2):
                    run(command, base)
                    for rel in ('mods/demo-2.jar', 'resourcepacks/demo.zip'):
                        self.assertEqual((client / rel).read_bytes(), new_data[rel])
                    self.assertFalse((client / 'mods/demo-1.jar').exists())
                    self.assertFalse((client / 'config/deleted.txt').exists())
                    for rel, data in protected.items():
                        self.assertEqual((client / rel).read_bytes(), data, rel)
                    backups = [p for p in (client / '.portable-sync-backups').rglob('*') if p.is_file()]
                    for rel in ('mods/demo-1.jar', 'resourcepacks/demo.zip'):
                        self.assertTrue(any(p.name == Path(rel).name and p.read_bytes() == old_data[rel]
                                            for p in backups), rel)
                    state = json.loads((client / '.portable-sync-state.json').read_text(encoding='utf-8-sig'))
                    self.assertEqual(state['version'], '2.0')
                    self.assertEqual(len(state['files']), len(rows))
                    if iteration == 0:
                        snapshot = {p.relative_to(client): p.read_bytes() for p in backups}
                    else:
                        self.assertEqual(snapshot, {p.relative_to(client): p.read_bytes() for p in backups})

    def test_python_incremental_update_preservation_backup_and_repeat(self):
        self.exercise_sync('python')

    @unittest.skipUnless(PS and os.name == 'nt', 'Requires Windows PowerShell')
    def test_powershell_incremental_update_preservation_backup_and_repeat(self):
        self.exercise_sync('powershell')


class DownloadServiceTests(unittest.TestCase):
    def test_download_authentication_listing_methods_and_removed_kit_route(self):
        spec = importlib.util.spec_from_file_location('update_server', ROOT / 'tools/secure-update-server.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory(prefix='updater-service-') as tmp:
            root = Path(tmp)
            write(root, 'demo.txt', b'first version')
            token = 'test-only-' + '0' * 24
            with serve(root, module.SecureUpdateHandler, token=token) as port:
                def request(method, path):
                    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
                    try:
                        conn.request(method, path)
                        response = conn.getresponse()
                        return response.status, response.read()
                    finally:
                        conn.close()
                self.assertEqual(request('GET', f'/{token}/demo.txt'), (200, b'first version'))
                self.assertEqual(request('HEAD', f'/{token}/demo.txt'), (200, b''))
                for path in ('/demo.txt', '/wrong/demo.txt', f'/{token}/', '/kit/demo.zip',
                             f'/{token}/../demo.txt', f'/{token}/%2e%2e/demo.txt'):
                    self.assertEqual(request('GET', path)[0], 404, path)
                for method in ('POST', 'PUT', 'DELETE'):
                    self.assertEqual(request(method, f'/{token}/demo.txt')[0], 405)
                write(root, 'replacement.txt', b'second version')
                (root / 'replacement.txt').replace(root / 'demo.txt')
                self.assertEqual(request('GET', f'/{token}/demo.txt'), (200, b'second version'))


@unittest.skipUnless(PS and os.name == 'nt', 'Publisher requires Windows PowerShell')
class PublishTests(unittest.TestCase):
    def test_windows_bootstrap_fallback_download(self):
        batch = (ROOT / 'tools/portable-windows-sync.bat').read_text(encoding='utf-8-sig')
        command_line = next(line for line in batch.splitlines()
                            if '-Command "' in line and 'function Save-DirectFile' in line)
        command = command_line.split('-Command "', 1)[1].rsplit('"', 1)[0]
        function_block = command.split("; Write-Host '====", 1)[0]
        with tempfile.TemporaryDirectory(prefix='updater-bootstrap-') as tmp:
            root = Path(tmp)
            web = root / 'web'
            write(web, 'example.txt', b'bootstrap download')
            script = root / 'fallback.ps1'
            with serve(web) as port:
                script.write_text(function_block + "\n$ErrorActionPreference='Stop'\n"
                                  + f"Save-DirectFile -Url 'http://127.0.0.1:{port}/example.txt' -Path 'download.txt'\n",
                                  encoding='utf-8-sig')
                run([PS, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(script)], root)
            self.assertEqual((root / 'download.txt').read_bytes(), b'bootstrap download')

    def test_standalone_publish_twice_and_service_launcher(self):
        with tempfile.TemporaryDirectory(prefix='updater-publish-') as tmp:
            root = Path(tmp)
            shutil.copytree(ROOT / 'tools', root / 'tools')
            shutil.copytree(ROOT / '一键脚本', root / '一键脚本')
            config = json.loads((root / 'tools/portable-pack.example.json').read_text(encoding='utf-8'))
            config['includeRoots'] = ['config']  # no remote Modrinth lookup in tests
            config['update']['tokenFile'] = '.test-token'
            config['packName'] = 'Synthetic pack'
            config['releaseNotes'] = ['Initial synthetic release']
            json_write(root / 'tools/portable-pack.json', config)
            write(root, 'main-client/config/demo.txt', b'first')
            write(root, 'main-client/config/obsolete.txt', b'obsolete')
            command = [PS, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                       str(root / 'tools/portable-publish.ps1')]
            run(command, root)
            publish = root / 'modpack-public/portable'
            for version, expected in [('1.0.0', b'first'), ('1.0.1', b'second')]:
                if version == '1.0.1':
                    write(root, 'main-client/config/demo.txt', expected)
                    (root / 'main-client/config/obsolete.txt').unlink()
                    run(command + ['-Version', version], root)
                    self.assertFalse((publish / 'config/obsolete.txt').exists())
                    self.assertTrue((root / 'logs/last-mod-update.txt').is_file())
                manifest = json.loads((publish / 'server-manifest.json').read_text(encoding='utf-8-sig'))
                self.assertEqual(manifest['version'], version)
                self.assertEqual((publish / 'config/demo.txt').read_bytes(), expected)
                for entry in manifest['files']:
                    data = (publish / entry['path']).read_bytes()
                    self.assertEqual(hashlib.sha1(data).hexdigest(), entry['sha1'].lower())
                    self.assertEqual(len(data), entry['size'])
                self.assertTrue((publish / '_updater/portable-bootstrap-refresh.ps1').is_file())
            # Exercise launcher arguments without creating an unowned child server process.
            # The server itself is exercised through actual HTTP above.
            stub = root / 'tools/secure-update-server.py'
            stub.write_text('import json, pathlib, sys\npathlib.Path("args.json").write_text(json.dumps(sys.argv[1:]))\n')
            run([PS, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                 str(root / 'tools/start-portable-update-server.ps1'), '-Python', sys.executable], root)
            args = json.loads((root / 'args.json').read_text())
            # Windows runners may expose TEMP through its equivalent 8.3 short path.
            self.assertTrue(Path(args[args.index('--directory') + 1]).samefile(publish))
            self.assertEqual(args[args.index('--bind') + 1], '127.0.0.1')
            self.assertEqual(args[args.index('--token') + 1], (root / '.test-token').read_text().strip())
            self.assertNotIn('--kitdir', args)


if __name__ == '__main__':
    unittest.main()

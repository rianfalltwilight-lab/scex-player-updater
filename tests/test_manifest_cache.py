"""MANIFEST_CACHE_V1 / UPDATE_ADOPTION_V1: the ETag manifest cache in every reader, gzip
and 304 from the download service, and the manifest request log. Localhost only."""
import functools
import gzip
import http.client
import http.server
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import contextlib
import unittest

from test_updater import PS, ROOT, write

TOOLS = ROOT / 'tools'
TOKEN = 'cache-token-5521'
PY_COPIES = ['player-update-generic.py', 'portable-stage-daemon.py', 'portable-macos-sync.command']
PS_COPIES = ['player-update-generic.ps1', 'portable-bootstrap-refresh.ps1', 'portable-stage-daemon.ps1',
             'player-self-repair.ps1', 'portable-windows-repair.ps1']
BLOCK = re.compile(r'(?m)^# MANIFEST_CACHE_BEGIN.*?^# MANIFEST_CACHE_END$', re.S)


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SERVER = load(TOOLS / 'secure-update-server.py', 'cache_server_under_test')


class Recording(SERVER.SecureUpdateHandler):
    events = None

    def log_request(self, code='-', size='-'):
        self.events.append((self.path.split('?')[0], int(code), self.headers.get('If-None-Match'),
                            self.headers.get('User-Agent', '')))


@contextlib.contextmanager
def serve(directory):
    events = []
    handler = type('Handler', (Recording,), {'events': events})
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(handler, directory=str(directory), token=TOKEN))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/{TOKEN}/server-manifest.json', events
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def manifest_for(rows):
    return {'packId': 'cache', 'version': 'v1', 'files': rows, 'downloadWorkers': 2, 'adoptExistingFiles': False,
            'preservePlayerCustomizations': False, 'forceSyncGlobs': ['*'],
            'cleanup': {'removeConnectorCache': False, 'disableLauncherRepairIndex': False, 'disableDuplicateMods': False},
            'playerOptions': {}, 'serverList': {'enabled': False}}


class CopyConsistencyTests(unittest.TestCase):
    def blocks(self, names):
        return {name: BLOCK.search((TOOLS / name).read_text(encoding='utf-8-sig').replace('\r\n', '\n')).group(0) for name in names}

    def test_python_and_powershell_copies_are_identical(self):
        for names in (PY_COPIES, PS_COPIES):
            self.assertEqual(len(set(self.blocks(names).values())), 1, names)

    def test_versions_match(self):
        py = self.blocks(PY_COPIES[:1])[PY_COPIES[0]]
        ps = self.blocks(PS_COPIES[:1])[PS_COPIES[0]]
        self.assertEqual(re.search(r'^UPDATER_VERSION = "([\d.]+)"$', py, re.M).group(1),
                         re.search(r"^\$script:UpdaterVersion = '([\d.]+)'$", ps, re.M).group(1))

    def test_no_signature_or_cloud_code_left(self):
        for path in TOOLS.iterdir():
            if path.suffix in ('.py', '.ps1', '.bat', '.command') and path.name != 'release-updater.py':
                text = path.read_text(encoding='utf-8-sig', errors='replace')
                for needle in ('MANIFEST_SIGNATURE', 'server-manifest.json.sig', 'mirrorManifestUrls', 'downloadPath'):
                    self.assertNotIn(needle, text, path.name)


class ServerTests(unittest.TestCase):
    def test_gzip_etag_variants_and_304(self):
        with tempfile.TemporaryDirectory() as tmp:
            web = Path(tmp)
            body = json.dumps({'files': [{'path': f'f{i}', 'sha1': '0' * 40, 'size': i} for i in range(200)]}).encode()
            (web / 'server-manifest.json').write_bytes(body)
            with serve(web) as (url, _events):
                host, port = url.split('/')[2].split(':')

                def get(headers):
                    connection = http.client.HTTPConnection(host, int(port), timeout=10)
                    connection.request('GET', f'/{TOKEN}/server-manifest.json', headers=headers)
                    response = connection.getresponse()
                    result = response.status, dict(response.getheaders()), response.read()
                    connection.close()
                    return result
                status, headers, raw = get({'Accept-Encoding': 'gzip'})
                self.assertEqual((status, headers.get('Content-Encoding')), (200, 'gzip'))
                self.assertEqual(gzip.decompress(raw), body)
                self.assertTrue(headers['ETag'].endswith('-gz"'))
                self.assertEqual(get({'Accept-Encoding': 'gzip', 'If-None-Match': headers['ETag']})[0], 304)
                status, plain, raw = get({})
                self.assertEqual((status, raw), (200, body))
                self.assertEqual(get({'If-None-Match': plain['ETag']})[0], 304)
                self.assertEqual(get({'Range': 'bytes=0-9', 'Accept-Encoding': 'gzip'})[2], body[:10])

    def test_access_log_has_version_but_no_address(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = SERVER.AccessLog(str(Path(tmp) / 'logs'))
            log.record(200, 'SCEX-updater/2.27.25 (python 3.12; win32)', '198.51.100.7')
            log.record(304, 'portable-server-kit-sync/1.0', '198.51.100.7')
            text = next((Path(tmp) / 'logs').iterdir()).read_text(encoding='utf-8')
            self.assertNotIn('198.51.100.7', text)
            rows = [line.split('\t') for line in text.splitlines()]
            self.assertEqual(rows[0][2], rows[1][2])  # same visitor within a day
            summary = subprocess.run([sys.executable, str(TOOLS / 'update-adoption.py'), '--folder', str(Path(tmp) / 'logs')],
                                     capture_output=True, timeout=30, env=dict(os.environ, PYTHONIOENCODING='utf-8'))
            output = summary.stdout.decode('utf-8')
            self.assertIn('1 位玩家', output)
            self.assertIn('2.27.25', output)
            self.assertIn('2.27.23 或更早', output)


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='scex-cache-')
        base = Path(self.temp.name)
        self.web = base / 'web'
        self.client = base / 'client'
        self.client.mkdir()
        rows = [write(self.web, 'mods/a.jar', b'payload a')]
        (self.web / 'server-manifest.json').write_text(json.dumps(manifest_for(rows)), encoding='utf-8')

    def tearDown(self):
        self.temp.cleanup()

    def run_engine(self, engine, url):
        if engine == 'python':
            command = [sys.executable, str(TOOLS / 'player-update-generic.py'), '--instance-dir', str(self.client), '--manifest-url', url]
        else:
            command = [PS, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(TOOLS / 'player-update-generic.ps1'),
                       '-InstanceDir', str(self.client), '-ManifestUrl', url, '-NoPause']
        env = dict(os.environ, PYTHONUTF8='1', NO_PROXY='127.0.0.1', no_proxy='127.0.0.1')
        env.pop('PORTABLE_SYNC_INTERACTIVE', None)
        result = subprocess.run(command, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
        return result.returncode, result.stdout.decode('utf-8-sig', errors='replace')

    def check_engine(self, engine):
        with serve(self.web) as (url, events):
            code, output = self.run_engine(engine, url)
            self.assertEqual(code, 0, output)
            self.assertTrue((self.client / '.portable-manifest-cache/server-manifest.json').is_file())
            agents = {event[3] for event in events if event[0].endswith('server-manifest.json')}
            self.assertTrue(all(agent.startswith('SCEX-updater/') for agent in agents), agents)
            events.clear()
            code, output = self.run_engine(engine, url)
            self.assertEqual(code, 0, output)
            statuses = [(event[0].rsplit('/', 1)[-1], event[1]) for event in events if 'server-manifest' in event[0]]
            self.assertEqual(statuses, [('server-manifest.json', 304)], output)
            # A new manifest is picked up despite the cache.
            (self.web / 'server-manifest.json').write_text(json.dumps(manifest_for([write(self.web, 'mods/a.jar', b'payload b')])), encoding='utf-8')
            code, output = self.run_engine(engine, url)
            self.assertEqual(code, 0, output)
            self.assertEqual((self.client / 'mods/a.jar').read_bytes(), b'payload b')

    def test_python_updater(self):
        self.check_engine('python')

    @unittest.skipUnless(PS and os.name == 'nt', 'Requires Windows PowerShell')
    def test_powershell_updater(self):
        self.check_engine('powershell')

    def test_python_daemon_probe_uses_the_cache(self):
        daemon = load(TOOLS / 'portable-stage-daemon.py', 'cache_daemon_under_test')
        with serve(self.web) as (url, events):
            self.assertIsNotNone(daemon.fetch_manifest_probe(url, self.client))
            self.assertIsNotNone(daemon.fetch_manifest_probe(url, self.client))
            self.assertEqual([event[1] for event in events], [200, 304])

    def test_corrupt_cache_is_ignored(self):
        u = load(TOOLS / 'player-update-generic.py', 'cache_reader_under_test')
        with serve(self.web) as (url, events):
            first = u.fetch_manifest(url, self.client, timeout=10)
            (self.client / '.portable-manifest-cache/server-manifest.json').write_bytes(b'tampered')
            self.assertEqual(u.fetch_manifest(url, self.client, timeout=10), first)
            self.assertEqual([event[1] for event in events], [200, 200])


if __name__ == '__main__':
    unittest.main()

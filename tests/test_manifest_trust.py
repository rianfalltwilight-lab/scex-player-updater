"""MANIFEST_SIGNATURE_V1 / MANIFEST_CACHE_V1 / UPDATE_ADOPTION_V1: signed manifests, the
sticky signature requirement, the verified ETag cache, gzip and the adoption log. Readers
run from a copied tools folder that trusts the committed test key; localhost only."""
import base64
import contextlib
import functools
import gzip
import hashlib
import http.client
import http.server
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

from test_updater import (PS, ROOT, FIXTURES, TEST_SIGNING_KEY, install_publish_key, json_write, sign_manifest,
                          signing_key_strings, trust_test_key, write)

TOOLS = ROOT / 'tools'
TOKEN = 'trust-token-5521'
PY_COPIES = ['player-update-generic.py', 'portable-stage-daemon.py', 'portable-macos-sync.command']
PS_COPIES = ['player-update-generic.ps1', 'portable-bootstrap-refresh.ps1', 'portable-stage-daemon.ps1',
             'player-self-repair.ps1', 'portable-windows-repair.ps1']
BLOCK = re.compile(r'(?m)^# MANIFEST_TRUST_BEGIN.*?^# MANIFEST_TRUST_END$', re.S)


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SERVER = load(TOOLS / 'secure-update-server.py', 'trust_server_under_test')


class Recording(SERVER.SecureUpdateHandler):
    events = None

    def log_request(self, code='-', size='-'):
        self.events.append((self.path.split('?')[0], int(code), self.headers.get('If-None-Match'),
                            self.headers.get('User-Agent', ''), self.headers.get('Accept-Encoding', '')))


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
    return {'packId': 'trust', 'version': 'v1', 'files': rows, 'downloadWorkers': 2, 'adoptExistingFiles': False,
            'preservePlayerCustomizations': False, 'forceSyncGlobs': ['*'],
            'cleanup': {'removeConnectorCache': False, 'disableLauncherRepairIndex': False, 'disableDuplicateMods': False},
            'playerOptions': {}, 'serverList': {'enabled': False}}


class CopyConsistencyTests(unittest.TestCase):
    def blocks(self, names):
        return {name: BLOCK.search((TOOLS / name).read_text(encoding='utf-8-sig').replace('\r\n', '\n')).group(0) for name in names}

    def test_python_and_powershell_copies_are_identical(self):
        for names in (PY_COPIES, PS_COPIES):
            blocks = self.blocks(names)
            self.assertEqual(len(set(blocks.values())), 1, list(blocks))

    def test_embedded_keys_and_versions_match_the_fixture(self):
        trusted = json.loads((FIXTURES / 'manifest-signing-keys.json').read_text(encoding='utf-8'))['trusted']
        py = self.blocks(PY_COPIES[:1])[PY_COPIES[0]]
        ps = self.blocks(PS_COPIES[:1])[PS_COPIES[0]]
        self.assertEqual(json.loads(re.search(r'^TRUSTED_MANIFEST_KEYS = (\{.*\})$', py, re.M).group(1)), trusted)
        ps_keys = dict(re.findall(r"'([0-9a-f]{16})' = '([A-Za-z0-9+/=]+)'", re.search(r'^\$script:TrustedManifestKeys = .*$', ps, re.M).group(0)))
        self.assertEqual(ps_keys, trusted)
        self.assertEqual(re.search(r'^UPDATER_VERSION = "([\d.]+)"$', py, re.M).group(1),
                         re.search(r"^\$script:UpdaterVersion = '([\d.]+)'$", ps, re.M).group(1))
        batch = (TOOLS / 'portable-windows-sync.bat').read_text(encoding='utf-8')
        (key_id, modulus), = trusted.items()
        self.assertIn("$i.keyId -ne '" + key_id + "'", batch)
        self.assertIn("FromBase64String('" + modulus + "')", batch)

    def test_production_private_key_is_not_in_the_repository(self):
        tracked = subprocess.run(['git', '-C', str(ROOT), 'ls-files'], capture_output=True, text=True, check=True).stdout.split()
        for rel in tracked:
            path = ROOT / rel
            if path.suffix in ('.xml', '.pem', '.key') and path.is_file():
                self.assertEqual(path.resolve(), TEST_SIGNING_KEY.resolve(), rel)


class VerifierTests(unittest.TestCase):
    def setUp(self):
        self.u = load(TOOLS / 'player-update-generic.py', 'trust_verifier_under_test')
        _prod_id, _prod, test_id, test_modulus = signing_key_strings()
        self.u.TRUSTED_MANIFEST_KEYS = {test_id: test_modulus}
        self.signing = load(TOOLS / 'manifest_signing.py', 'trust_signing_under_test')
        self.key = TEST_SIGNING_KEY.read_text(encoding='utf-8-sig')

    def test_valid_tampered_untrusted_and_malformed(self):
        data = b'{"files": []}'
        signature = self.signing.sign(data, self.key)
        self.assertEqual(len(self.u.verify_manifest_signature(data, signature)), 16)
        for bad_data, bad_sig in ((data + b' ', signature), (data, signature.replace(b'"keyId": "', b'"keyId": "0')),
                                  (data, b'not json'), (data, signature.replace(b'RSA-PKCS1', b'RSA-PSS'))):
            with self.assertRaises(self.u.ManifestTrustError):
                self.u.verify_manifest_signature(bad_data, bad_sig)
        self.u.TRUSTED_MANIFEST_KEYS = json.loads((FIXTURES / 'manifest-signing-keys.json').read_text(encoding='utf-8'))['trusted']
        with self.assertRaisesRegex(self.u.ManifestTrustError, '不受信任'):
            self.u.verify_manifest_signature(data, signature)


class ServerTests(unittest.TestCase):
    def test_gzip_etag_variants_and_304(self):
        with tempfile.TemporaryDirectory() as tmp:
            web = Path(tmp)
            body = json.dumps({'files': [{'path': f'f{i}', 'sha1': '0' * 40, 'size': i} for i in range(200)]}).encode()
            (web / 'server-manifest.json').write_bytes(body)
            with serve(web) as (url, events):
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
            log.record(200, 'SCEX-updater/2.27.24 (python 3.12; win32)', '198.51.100.7')
            log.record(304, 'portable-server-kit-sync/1.0', '198.51.100.7')
            text = next((Path(tmp) / 'logs').iterdir()).read_text(encoding='utf-8')
            self.assertNotIn('198.51.100.7', text)
            rows = [line.split('\t') for line in text.splitlines()]
            self.assertEqual(rows[0][2], rows[1][2])  # same visitor within a day
            summary = subprocess.run([sys.executable, str(TOOLS / 'update-adoption.py'), '--folder', str(Path(tmp) / 'logs')],
                                     capture_output=True, timeout=30, env=dict(os.environ, PYTHONIOENCODING='utf-8'))
            output = summary.stdout.decode('utf-8')
            self.assertIn('1 位玩家', output)
            self.assertIn('2.27.24', output)
            self.assertIn('2.27.23 或更早', output)


class ReaderTests(unittest.TestCase):
    """Real readers from a tools copy that trusts the test key."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='scex-trust-')
        base = Path(self.temp.name)
        self.tools = base / 'tools'
        shutil.copytree(TOOLS, self.tools)
        trust_test_key(self.tools)
        self.web = base / 'web'
        self.client = base / 'client'
        self.client.mkdir()
        rows = [write(self.web, 'mods/a.jar', b'payload a'), write(self.web, '_updater/player-update-generic.py', b'# helper\n')]
        self.manifest_path = self.web / 'server-manifest.json'
        self.manifest_path.write_text(json.dumps(manifest_for(rows)), encoding='utf-8')
        sign_manifest(self.manifest_path)

    def tearDown(self):
        self.temp.cleanup()

    def state(self):
        return json.loads((self.client / '.portable-sync-state.json').read_text(encoding='utf-8-sig'))

    def run_engine(self, engine, url):
        if engine == 'python':
            command = [sys.executable, str(self.tools / 'player-update-generic.py'), '--instance-dir', str(self.client), '--manifest-url', url]
        else:
            command = [PS, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(self.tools / 'player-update-generic.ps1'),
                       '-InstanceDir', str(self.client), '-ManifestUrl', url, '-NoPause']
        env = dict(os.environ, PYTHONUTF8='1', NO_PROXY='127.0.0.1', no_proxy='127.0.0.1')
        env.pop('PORTABLE_SYNC_INTERACTIVE', None)
        result = subprocess.run(command, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
        return result.returncode, result.stdout.decode('utf-8-sig', errors='replace')

    def check_engine(self, engine):
        with serve(self.web) as (url, events):
            code, output = self.run_engine(engine, url)
            self.assertEqual(code, 0, output)
            self.assertIn('清单签名校验通过', output)
            self.assertEqual(self.state()['manifestSignature'], 'required')
            self.assertTrue((self.client / '.portable-manifest-cache/server-manifest.json.sig').is_file())
            agents = {event[3] for event in events if event[0].endswith('server-manifest.json')}
            self.assertTrue(all(agent.startswith('SCEX-updater/') for agent in agents), agents)
            events.clear()
            code, output = self.run_engine(engine, url)
            self.assertEqual(code, 0, output)
            statuses = [(event[0].rsplit('/', 1)[-1], event[1]) for event in events if 'server-manifest' in event[0]]
            self.assertEqual(statuses, [('server-manifest.json', 304), ('server-manifest.json.sig', 304)], output)
            # Sticky: an unsigned manifest is refused and nothing changes.
            (self.web / 'server-manifest.json.sig').unlink()
            self.manifest_path.write_text(json.dumps(manifest_for([write(self.web, 'mods/a.jar', b'evil')])), encoding='utf-8')
            code, output = self.run_engine(engine, url)
            self.assertEqual(code, 1, output)
            self.assertIn('E-SIGN', output)
            self.assertEqual((self.client / 'mods/a.jar').read_bytes(), b'payload a')
            # A signature that does not match the bytes is refused as well.
            sign_manifest(self.manifest_path)
            self.manifest_path.write_text(self.manifest_path.read_text(encoding='utf-8') + ' ', encoding='utf-8')
            code, output = self.run_engine(engine, url)
            self.assertEqual(code, 1, output)
            self.assertIn('签名', output)
            self.assertEqual((self.client / 'mods/a.jar').read_bytes(), b'payload a')

    def test_python_updater(self):
        self.check_engine('python')

    @unittest.skipUnless(PS and os.name == 'nt', 'Requires Windows PowerShell')
    def test_powershell_updater(self):
        self.check_engine('powershell')

    def test_python_daemon_probe_ignores_bad_signature(self):
        daemon = load(self.tools / 'portable-stage-daemon.py', 'trust_daemon_under_test')
        with serve(self.web) as (url, _events):
            self.assertIsNotNone(daemon.fetch_manifest_probe(url, self.client))
            sig = self.web / 'server-manifest.json.sig'
            sig.write_bytes(sig.read_bytes().replace(b'"signature": "', b'"signature": "AA'))
            self.assertIsNone(daemon.fetch_manifest_probe(url, self.client))

    @unittest.skipUnless(PS and os.name == 'nt', 'Requires Windows PowerShell')
    def test_bootstrap_skips_refresh_when_a_required_signature_is_missing(self):
        scripts = self.client / '_updater'
        scripts.mkdir()
        (scripts / 'player-update-generic.py').write_bytes(b'# old helper\n')
        json_write(self.client / '.portable-sync-state.json', {'version': 'v0', 'files': [], 'manifestSignature': 'required'})
        (self.web / 'server-manifest.json.sig').unlink()
        with serve(self.web) as (url, events):
            result = subprocess.run([PS, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(self.tools / 'portable-bootstrap-refresh.ps1'),
                                     '-InstanceDir', str(self.client), '-ScriptDir', str(scripts), '-ManifestUrl', url],
                                    capture_output=True, timeout=60)
        output = result.stdout.decode('utf-8-sig', errors='replace')
        self.assertEqual(result.returncode, 0, output)
        self.assertIn('签名校验没有通过', output)
        self.assertEqual((scripts / 'player-update-generic.py').read_bytes(), b'# old helper\n')
        self.assertFalse(any(event[0].endswith('.py') for event in events))

    @unittest.skipUnless(PS and os.name == 'nt', 'Requires Windows PowerShell')
    def test_windows_entry_inline_fallback_checks_the_signature(self):
        batch = (self.tools / 'portable-windows-sync.bat').read_text(encoding='utf-8')
        line = next(l for l in batch.splitlines() if 'function Fetch(' in l and '.sig' in l)
        command = line.split('-Command "', 1)[1].rsplit('"', 1)[0]
        script = Path(self.temp.name) / 'inline.ps1'
        script.write_text(command, encoding='utf-8-sig')
        bootstrap = write(self.web, '_updater/portable-bootstrap-refresh.ps1', b"Write-Host 'fresh bootstrap'\r\n")
        manifest = json.loads(self.manifest_path.read_text(encoding='utf-8'))
        manifest['files'].append(bootstrap)
        self.manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
        target = self.client / '_updater' / 'portable-bootstrap-refresh.ps1'
        target.parent.mkdir()
        with serve(self.web) as (url, _events):
            env = dict(os.environ, PORTABLE_INSTANCE_DIR=str(self.client), PORTABLE_SCRIPT_DIR=str(target.parent),
                       PORTABLE_MANIFEST_URL=url, PORTABLE_BOOTSTRAP_REFRESH=str(target))
            run = lambda: subprocess.run([PS, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(script)],
                                         env=env, capture_output=True, timeout=60)
            self.assertEqual(run().returncode, 1)  # stale signature for the edited manifest
            self.assertFalse(target.exists())
            sign_manifest(self.manifest_path)
            self.assertEqual(run().returncode, 0)
            self.assertEqual(target.read_bytes(), b"Write-Host 'fresh bootstrap'\r\n")


@unittest.skipUnless(PS and os.name == 'nt', 'Publisher requires Windows PowerShell')
class PublisherTests(unittest.TestCase):
    def prepare(self, root):
        shutil.copytree(TOOLS, root / 'tools')
        shutil.copytree(ROOT / '一键脚本', root / '一键脚本')
        config = json.loads((root / 'tools/portable-pack.example.json').read_text(encoding='utf-8'))
        config['includeRoots'] = ['config']
        config['update']['pythonExe'] = str(root / 'no-python.exe')
        json_write(root / 'tools/portable-pack.json', config)
        write(root, 'main-client/config/example.txt', b'synthetic config')
        return [PS, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(root / 'tools/portable-publish.ps1')]

    def test_publish_signs_and_refuses_without_a_trusted_key(self):
        with tempfile.TemporaryDirectory(prefix='scex-trust-publish-') as tmp:
            root = Path(tmp)
            command = self.prepare(root)
            missing = subprocess.run(command, cwd=root, capture_output=True, timeout=120)
            self.assertNotEqual(missing.returncode, 0)
            self.assertFalse((root / 'modpack-public/portable').exists())
            shutil.copyfile(TEST_SIGNING_KEY, root / '.update-signing-key.xml')  # readers still trust only the real key
            untrusted = subprocess.run(command, cwd=root, capture_output=True, timeout=120)
            self.assertNotEqual(untrusted.returncode, 0)
            self.assertFalse((root / 'modpack-public/portable/server-manifest.json').exists())
            install_publish_key(root)
            done = subprocess.run(command, cwd=root, capture_output=True, timeout=180)
            self.assertEqual(done.returncode, 0, done.stdout.decode('utf-8', 'replace')[-2000:])
            public = root / 'modpack-public/portable'
            u = load(root / 'tools/player-update-generic.py', 'trust_publish_reader')
            key_id = u.verify_manifest_signature((public / 'server-manifest.json').read_bytes(),
                                                 (public / 'server-manifest.json.sig').read_bytes())
            self.assertEqual(key_id, signing_key_strings()[2])
            listed = {row['path'] for row in json.loads((public / 'server-manifest.json').read_text(encoding='utf-8-sig'))['files']}
            self.assertNotIn('server-manifest.json.sig', listed)
            self.assertIn(signing_key_strings()[3], (public / '_updater/player-update-generic.py').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()

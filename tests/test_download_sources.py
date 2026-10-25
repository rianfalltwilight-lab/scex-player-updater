"""Download routing, replacement and launcher repair; synthetic data only."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.error
import zipfile

from test_updater import ROOT, PS, SilentHandler, json_write, serve, write


def load_updater():
    spec = importlib.util.spec_from_file_location('download_sources_test', ROOT / 'tools/player-update-generic.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def run_ps(body):
    """Load function definitions without executing the updater's main routine."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / 'test.ps1'
        path.write_text("$ErrorActionPreference='Stop'\n" +
            "$ast=[Management.Automation.Language.Parser]::ParseFile(" +
            ps_quote(ROOT / 'tools/player-update-generic.ps1') + ",[ref]$null,[ref]$null)\n" +
            "$defs=$ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst]},$false)\n" +
            "$defs | ForEach-Object { Invoke-Expression $_.Extent.Text }\n" +
            "$script:ProxyResolved=$false; $script:OfficialMinBytes=1MB; $script:OfficialMinRateBps=128KB\n" + body,
            encoding='utf-8-sig')
        result = subprocess.run([PS, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(path)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=40)
        if result.returncode:
            raise AssertionError(result.stdout.decode('utf-8', errors='replace'))
        return result.stdout


class DownloadRoutingTests(unittest.TestCase):
    def setUp(self):
        self.updater = load_updater()

    def test_direct_then_existing_proxy_then_next_url_and_per_file_fallback(self):
        u = self.updater
        proxy = object()
        first = 'https://cdn.example/first.jar'
        second = 'https://cdn.example/second.jar'
        calls = []

        def download(url, *args, **kwargs):
            calls.append((url, kwargs['opener']))
            if url == first:
                raise OSError('blocked')

        with mock.patch.object(u, 'download_file', side_effect=download), \
                mock.patch.object(u, 'opener_for_official_url', return_value=proxy):
            result = u.download_manifest_entry({'downloads': [first], 'url': second},
                'https://pack.example/', Path('unused'), '0' * 40, 'mods/demo.jar')
            self.assertEqual(result, 'official')
            self.assertEqual(calls, [(first, u._DIRECT_OPENER), (first, proxy), (second, u._DIRECT_OPENER)])
            calls.clear()
            result = u.download_manifest_entry({'url': first}, 'https://pack.example/',
                Path('unused'), '0' * 40, 'mods/demo.jar')
            self.assertEqual(result, 'home')
            self.assertEqual(calls[-1], ('https://pack.example/mods/demo.jar', u._DIRECT_OPENER))
            calls.clear()
            u.download_manifest_entry({'url': second}, 'https://pack.example/',
                Path('unused'), '0' * 40, 'mods/other.jar')
            self.assertEqual(calls, [(second, u._DIRECT_OPENER)])

    def test_no_proxy_setting_and_explicit_lan_fallback(self):
        u = self.updater
        with mock.patch.dict(os.environ, {'PORTABLE_SYNC_NOPROXY': '1', 'HTTPS_PROXY': 'http://proxy.example:8080'}):
            self.assertEqual(u.get_proxy_url(), '')
            self.assertIs(u.opener_for_official_url('https://cdn.example/demo.jar'), u._DIRECT_OPENER)
        urls = ['https://first.example/m.json', 'https://second.example/m.json', 'http://192.168.1.2/m.json']
        # URL_RULES_V1 (scex): the first public URL, then the listed LAN fallback.
        self.assertEqual(u.order_manifest_urls(urls), [urls[0], urls[2]])

    def test_busy_retry_and_hash_failure_preserve_previous_file(self):
        u = self.updater
        content = b'new content'
        digest = hashlib.sha1(content).hexdigest()
        opener = mock.Mock()
        opener.open.side_effect = [urllib.error.HTTPError('https://example.test', 503, 'busy', {'Retry-After': '3'}, io.BytesIO()), io.BytesIO(content)]
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / 'demo.jar'
            dest.write_bytes(b'old content')
            with mock.patch.object(u.time, 'sleep') as sleep:
                u.download_file('https://example.test/demo', dest, digest, opener=opener, retries=0)
            self.assertEqual(dest.read_bytes(), content)
            self.assertEqual(opener.open.call_count, 2)
            self.assertGreaterEqual(sleep.call_args.args[0], 3)
            opener.open.side_effect = [io.BytesIO(b'wrong bytes')]
            with self.assertRaisesRegex(u.UpdateError, '校验失败'):
                u.download_file('https://example.test/demo', dest, digest, opener=opener)
            self.assertEqual(dest.read_bytes(), content)
            self.assertEqual(list(Path(tmp).glob('.portable-sync-*')), [])
        with contextlib.closing(urllib.error.HTTPError('u', 404, 'missing', {}, io.BytesIO())) as error:
            self.assertIsNone(u.busy_retry_delay(error, 0))
        with contextlib.closing(urllib.error.HTTPError('u', 503, 'busy', {}, io.BytesIO())) as error:
            self.assertIsNone(u.busy_retry_delay(error, 5))

    def test_atomic_replace_failure_keeps_previous_file_and_mode(self):
        u = self.updater
        with tempfile.TemporaryDirectory() as tmp:
            dest, temp = Path(tmp) / 'old', Path(tmp) / 'temp'
            dest.write_bytes(b'old')
            temp.write_bytes(b'new')
            mode = dest.stat().st_mode
            with mock.patch.object(u.os, 'replace', side_effect=PermissionError('locked')):
                with self.assertRaisesRegex(u.LocalFileUpdateError, '权限不足'):
                    u.install_verified_file(temp, dest)
            self.assertEqual(dest.read_bytes(), b'old')
            self.assertEqual(dest.stat().st_mode, mode)
            self.assertEqual(temp.read_bytes(), b'new')

    @unittest.skipUnless(PS and os.name == 'nt', 'Requires Windows PowerShell')
    def test_powershell_direct_proxy_and_per_file_fallback(self):
        run_ps(r'''
$script:calls=New-Object 'System.Collections.Generic.List[string]'
function Test-ShouldUseProxyForOfficial { return $true }
function Download-ManifestFile {
    param($Url,$Destination,$ExpectedSha1,$TimeoutSec,$Retries,[switch]$AllowProxy,[switch]$NoBackoff,$MinBytesAfterTimeout,$MinRateBps)
    [void]$script:calls.Add("$Url|$([bool]$AllowProxy)")
    if ($Url -like '*first.jar') { throw 'blocked' }
}
$file=[pscustomobject]@{downloads=@('https://cdn.example/first.jar');url='https://cdn.example/second.jar'}
$result=Download-ManifestEntry $file 'https://pack.example/' 'unused' ('0'*40) 'mods/demo.jar'
if ($result -ne 'official' -or ($script:calls -join ',') -ne 'https://cdn.example/first.jar|False,https://cdn.example/first.jar|True,https://cdn.example/second.jar|False') { throw 'Wrong source order' }
$script:calls.Clear()
$file=[pscustomobject]@{url='https://cdn.example/first.jar'}
$result=Download-ManifestEntry $file 'https://pack.example/' 'unused' ('0'*40) 'mods/demo.jar'
if ($result -ne 'home' -or $script:calls[-1] -ne 'https://pack.example/mods/demo.jar|False') { throw 'Missing direct pack fallback' }
$script:calls.Clear()
$file=[pscustomobject]@{url='https://cdn.example/second.jar'}
[void](Download-ManifestEntry $file 'https://pack.example/' 'unused' ('0'*40) 'mods/other.jar')
if ($script:calls.Count -ne 1 -or $script:calls[0] -ne 'https://cdn.example/second.jar|False') { throw 'Failure disabled another file' }
''')

    @unittest.skipUnless(PS and os.name == 'nt', 'Requires Windows PowerShell')
    def test_powershell_locked_destination_preserves_old_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest, temp = Path(tmp) / 'old', Path(tmp) / 'temp'
            dest.write_bytes(b'old')
            temp.write_bytes(b'new')
            run_ps('$lock=[IO.File]::Open(' + ps_quote(dest) + ",[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)\n" +
                "try { $failed=$false; try { Install-VerifiedFile " + ps_quote(temp) + ' ' + ps_quote(dest) +
                " } catch { $failed=$true }; if (-not $failed) { throw 'Expected locked file failure' } } finally { $lock.Dispose() }")
            self.assertEqual(dest.read_bytes(), b'old')
            self.assertEqual(temp.read_bytes(), b'new')

    @unittest.skipUnless(PS and os.name == 'nt', 'Requires Windows PowerShell')
    def test_powershell_busy_response_retries_before_install(self):
        class BusyHandler(SilentHandler):
            requests = 0

            def do_GET(self):
                type(self).requests += 1
                if self.requests == 1:
                    self.send_response(503)
                    self.send_header('Retry-After', '3')
                    self.end_headers()
                    return
                return super().do_GET()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = b'retried content'
            row = write(root, 'remote.bin', data)
            dest = root / 'client.bin'
            dest.write_bytes(b'old')
            with serve(root, BusyHandler) as port:
                run_ps("$script:waits=0\nfunction Start-Sleep { param($Milliseconds,$Seconds) $script:waits++; if ($Milliseconds -lt 3000) { throw 'Ignored Retry-After' } }\n" +
                    'Download-ManifestFile -Url ' + ps_quote(f'http://127.0.0.1:{port}/remote.bin') +
                    ' -Destination ' + ps_quote(dest) + ' -ExpectedSha1 ' + ps_quote(row['sha1']) +
                    " -Retries 0\nif ($script:waits -ne 1) { throw 'Wrong busy retry count' }")
            self.assertEqual(BusyHandler.requests, 2)
            self.assertEqual(dest.read_bytes(), data)


class LauncherRepairTests(unittest.TestCase):
    def exercise_repair(self, engine):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            root = home / '.minecraft/versions/demo'
            root.mkdir(parents=True)
            version = '1.21.1-20240808.144430'
            jar = home / f'.minecraft/libraries/net/minecraft/client/{version}/client-{version}-extra.jar'
            jar.parent.mkdir(parents=True)
            path = root / 'demo.json'
            original = {'id': 'demo', 'mainClass': 'test.Main', 'customPlayerSetting': {'keep': True},
                'arguments': {'game': ['--fml.neoForgeVersion', '21.1.1', '--fml.mcVersion', '1.21.1',
                    '--fml.neoFormVersion', '20240808.144430'], 'jvm': ['-Xmx4G', '-Dplayer=yes']}, 'libraries': []}
            json_write(path, original)
            manifest = {'loader': {'type': 'neoforge'}, 'minecraftVersion': '1.21.1'}
            def repair():
                if engine == 'python':
                    load_updater().ensure_neoforge_client_extra(root, manifest, root / 'backup')
                else:
                    run_ps("$manifest=[pscustomobject]@{loader=[pscustomobject]@{type='neoforge'};minecraftVersion='1.21.1'}\n" +
                        '[void](Ensure-NeoForgeClientExtra ' + ps_quote(root) + ' $manifest ' + ps_quote(root / 'backup') + ')')
            # Executable content must never be added as an ordinary resource library.
            with zipfile.ZipFile(jar, 'w') as archive:
                archive.writestr('version.json', json.dumps({'id': '1.21.1'}))
                archive.writestr('test.class', b'invalid')
            before = path.read_bytes()
            repair()
            self.assertEqual(path.read_bytes(), before)
            with zipfile.ZipFile(jar, 'w') as archive:
                archive.writestr('version.json', json.dumps({'id': '1.21.1'}))
            repair()
            updated = json.loads(path.read_text(encoding='utf-8-sig'))
            self.assertEqual(updated['arguments'], original['arguments'])
            self.assertEqual(updated['customPlayerSetting'], original['customPlayerSetting'])
            self.assertEqual(updated['libraries'][0]['name'], f'net.minecraft:client:{version}:extra')
            self.assertEqual((root / 'backup/demo.json').read_bytes(), before)
            once = path.read_bytes()
            repair()
            self.assertEqual(path.read_bytes(), once)

    def test_python_resource_only_repair_preserves_arguments_and_is_idempotent(self):
        self.exercise_repair('python')

    @unittest.skipUnless(PS and os.name == 'nt', 'Requires Windows PowerShell')
    def test_powershell_resource_only_repair_preserves_arguments_and_is_idempotent(self):
        self.exercise_repair('powershell')


class ParallelDownloadTests(unittest.TestCase):
    def exercise_parallel(self, engine):
        class SlowHandler(SilentHandler):
            lock = threading.Lock()
            active = 0
            maximum = 0

            def do_GET(self):
                if not self.path.endswith('.jar'):
                    return super().do_GET()
                with self.lock:
                    type(self).active += 1
                    type(self).maximum = max(self.maximum, self.active)
                try:
                    time.sleep(0.2)
                    super().do_GET()
                finally:
                    with self.lock:
                        type(self).active -= 1

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            web, client = root / 'web', root / 'client'
            web.mkdir()
            client.mkdir()
            rows = [write(web, f'mods/demo-{i}.jar', f'bytes-{i}'.encode()) for i in range(5)]
            manifest = {'version': '2', 'files': rows, 'downloadWorkers': 2,
                'cleanup': {'removeConnectorCache': False, 'disableDuplicateMods': False, 'disableLauncherRepairIndex': False}}
            json_write(web / 'server-manifest.json', manifest)
            with serve(web, SlowHandler) as port:
                url = f'http://127.0.0.1:{port}/server-manifest.json'
                command = ([sys.executable, str(ROOT / 'tools/player-update-generic.py'), '--instance-dir', str(client), '--manifest-url', url]
                    if engine == 'python' else [PS, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(ROOT / 'tools/player-update-generic.ps1'), '-InstanceDir', str(client), '-ManifestUrl', url, '-NoPause'])
                env = dict(os.environ, PYTHONUTF8='1')
                result = subprocess.run(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=45)
                self.assertEqual(result.returncode, 0, result.stdout.decode('utf-8', errors='replace'))
                self.assertEqual(SlowHandler.maximum, 2)
                state = (client / '.portable-sync-state.json').read_bytes()
                manifest['version'] = '3'
                # One required job fails, after other parallel jobs have completed.
                manifest['files'] = [dict(rows[0], sha1='0' * 40)] + rows[1:]
                json_write(web / 'server-manifest.json', manifest)
                (client / rows[0]['path']).write_bytes(b'prior local file')
                result = subprocess.run(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=45)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual((client / '.portable-sync-state.json').read_bytes(), state)
                self.assertEqual((client / rows[0]['path']).read_bytes(), b'prior local file')

    def test_python_configured_parallelism_and_required_failure(self):
        self.exercise_parallel('python')

    @unittest.skipUnless(PS and os.name == 'nt', 'Requires Windows PowerShell')
    def test_powershell_configured_parallelism_and_required_failure(self):
        self.exercise_parallel('powershell')

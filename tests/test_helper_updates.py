"""Regression coverage for deployed updater helper fixes, using synthetic data."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request

from test_updater import PS, ROOT, run, serve, write


def load_tool(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), ROOT / 'tools' / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ps_functions(script_name):
    """Load function definitions without starting a daemon or inspecting a real client."""
    source = str(ROOT / 'tools' / script_name).replace("'", "''")
    return f"""
$ErrorActionPreference = 'Stop'
$ast = [Management.Automation.Language.Parser]::ParseFile('{source}', [ref]$null, [ref]$null)
foreach ($definition in $ast.FindAll({{param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst]}}, $false)) {{
    . ([scriptblock]::Create($definition.Extent.Text))
}}
"""


class StageReplacementTests(unittest.TestCase):
    def test_python_bad_hash_preserves_previous_download_then_replaces_readonly_file(self):
        stage = load_tool('portable-stage-daemon.py')
        with tempfile.TemporaryDirectory(prefix='updater-stage-') as tmp:
            root = Path(tmp)
            web = root / 'web'
            write(web, 'resource.zip', b'new archive')
            destination = root / 'staging' / 'resource.zip'
            write(root, 'staging/resource.zip', b'previous archive')
            with serve(web) as port:
                url = f'http://127.0.0.1:{port}/resource.zip'
                with self.assertRaisesRegex(RuntimeError, 'SHA1 mismatch'):
                    stage.download_verify(url, destination, '0' * 40)
                self.assertEqual(destination.read_bytes(), b'previous archive')
                self.assertEqual(list(destination.parent.glob('.portable-stage-*.tmp')), [])
                destination.chmod(0o444)
                try:
                    stage.download_verify(url, destination, hashlib.sha1(b'new archive').hexdigest())
                    self.assertEqual(destination.read_bytes(), b'new archive')
                    self.assertEqual(list(destination.parent.glob('.portable-stage-*.tmp')), [])
                finally:
                    destination.chmod(0o600)

    @unittest.skipUnless(PS and os.name == 'nt', 'Requires Windows PowerShell')
    def test_powershell_locked_target_survives_and_readonly_target_can_be_replaced(self):
        with tempfile.TemporaryDirectory(prefix='updater-stage-ps-') as tmp:
            root = Path(tmp)
            web = root / 'web'
            write(web, 'resource.zip', b'new archive')
            write(root, 'staging/resource.zip', b'previous archive')
            destination = root / 'staging/resource.zip'
            script = root / 'exercise.ps1'
            with serve(web) as port:
                script.write_text(ps_functions('portable-stage-daemon.ps1') + f"""
$target = Join-Path $PSScriptRoot 'staging/resource.zip'
$url = 'http://127.0.0.1:{port}/resource.zip'
$hash = '{hashlib.sha1(b'new archive').hexdigest()}'
$lock = [IO.File]::Open($target, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
$failed = $false
try {{ Save-StagedFile -Url $url -Destination $target -ExpectedSha1 $hash }}
catch {{ $failed = $true }}
finally {{ $lock.Dispose() }}
if (-not $failed) {{ throw 'Locked target unexpectedly replaced' }}
if ([IO.File]::ReadAllText($target) -ne 'previous archive') {{ throw 'Old target was lost' }}
if (@(Get-ChildItem (Split-Path $target) -Filter '.portable-stage-*.tmp' -Force).Count) {{ throw 'Temporary file leaked' }}
[IO.File]::SetAttributes($target, [IO.FileAttributes]::ReadOnly)
Save-StagedFile -Url $url -Destination $target -ExpectedSha1 $hash
""", encoding='utf-8-sig')
                run([PS, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(script)], root)
            self.assertEqual(destination.read_bytes(), b'new archive')


@unittest.skipUnless(PS and os.name == 'nt', 'Requires Windows PowerShell')
class HelperFallbackTests(unittest.TestCase):
    def test_repair_tries_multiline_addresses_after_http_and_manifest_errors(self):
        with tempfile.TemporaryDirectory(prefix='updater-repair-') as tmp:
            root = Path(tmp)
            web, client = root / 'web', root / 'client'
            client.mkdir()
            entry = write(web, 'mods/example.jar', b'local synthetic mod')
            write(client, entry['path'], b'local synthetic mod')
            write(web, 'malformed.json', b'{}')
            write(web, 'manifest.json', json.dumps({'version': 'test-fallback', 'files': [entry]}).encode())
            with serve(web) as port:
                selected = f'http://127.0.0.1:{port}/manifest.json'
                (client / 'UPDATE-URL.txt').write_text(
                    f'# configured alternatives\nhttp://127.0.0.1:{port}/missing.json\n'
                    f'http://127.0.0.1:{port}/malformed.json\n{selected}\n{selected}\n', encoding='utf-8')
                result = subprocess.run(
                    [PS, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                     str(ROOT / 'tools/player-self-repair.ps1'), '-InstanceDir', str(client),
                     '-Quiet', '-NoPause'], cwd=root, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, timeout=90)
            self.assertIn(result.returncode, (0, 1), result.stdout.decode('utf-8', errors='replace'))
            report_path = client / '_repair/latest/meta.json'
            self.assertTrue(report_path.exists(), result.stdout.decode('utf-8', errors='replace'))
            report = json.loads(report_path.read_text('utf-8-sig'))
            self.assertEqual(report['manifestUrl'], selected)
            self.assertEqual(report['packVersion'], 'test-fallback')
            self.assertTrue(report['serverReachable'])
            self.assertEqual(report['okCount'], 1)

    def test_bootstrap_uses_first_public_then_explicit_lan_fallback(self):
        with tempfile.TemporaryDirectory(prefix='updater-bootstrap-order-') as tmp:
            root = Path(tmp)
            urls = ['https://updates.example.invalid/manifest.json',
                    'http://192.168.1.2/manifest.json',
                    'https://mirror.example.invalid/manifest.json']
            (root / 'UPDATE-URL.txt').write_text('\n'.join(urls), encoding='utf-8')
            source = (ROOT / 'tools/portable-bootstrap-refresh.ps1').read_text('utf-8-sig')
            ordering = source[source.index('$instanceRoot ='):source.index("$selected = ''")]
            script = root / 'order.ps1'
            script.write_text(ps_functions('portable-bootstrap-refresh.ps1') +
                              '\n$InstanceDir = $PSScriptRoot; $ScriptDir = $PSScriptRoot; $ManifestUrl = ""\n' +
                              ordering + '\nConvertTo-Json -InputObject @($urls) -Compress\n', encoding='utf-8-sig')
            output = run([PS, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(script)], root)
            # URL_RULES_V1 (scex): the first public URL only, then the listed LAN fallback.
            self.assertEqual(json.loads(output.decode('utf-8-sig')), [urls[0], urls[1]])


class ServiceLifecycleTests(unittest.TestCase):
    def test_pid_record_cleanup_preserves_a_new_owner(self):
        with tempfile.TemporaryDirectory(prefix='updater-pid-') as tmp:
            root = Path(tmp)
            pidfile = root / 'server.pid'
            script = root / 'pid.py'
            source = str(ROOT / 'tools/secure-update-server.py')
            prefix = (f'import runpy\nfrom pathlib import Path\n'
                      f'module = runpy.run_path({source!r})\n'
                      f'module["install_pid_file"]({str(pidfile)!r})\n'
                      f'assert Path({str(pidfile)!r}).read_text().strip().isdigit()\n')
            script.write_text(prefix, encoding='utf-8')
            run([sys.executable, str(script)], root)
            self.assertFalse(pidfile.exists())
            script.write_text(prefix + f'Path({str(pidfile)!r}).write_text("new owner")\n', encoding='utf-8')
            run([sys.executable, str(script)], root)
            self.assertEqual(pidfile.read_text(), 'new owner')

    def test_failed_bind_does_not_replace_existing_pid_record(self):
        with tempfile.TemporaryDirectory(prefix='updater-pid-bind-') as tmp, socket.socket() as listener:
            root = Path(tmp)
            pidfile = root / 'server.pid'
            pidfile.write_text('existing process')
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            result = subprocess.run(
                [sys.executable, str(ROOT / 'tools/secure-update-server.py'), '--directory', str(root),
                 '--port', str(listener.getsockname()[1]), '--token', 'test-token', '--bind', '127.0.0.1',
                 '--pid-file', str(pidfile)], cwd=root, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(pidfile.read_text(), 'existing process')

    def test_busy_buffer_streams_another_download_without_blocking(self):
        module = load_tool('secure-update-server.py')
        streamed = threading.Event()

        class Handler(module.SecureUpdateHandler):
            def copyfile(self, source, outputfile):
                streamed.set()
                return super().copyfile(source, outputfile)

        with tempfile.TemporaryDirectory(prefix='updater-buffer-') as tmp:
            root = Path(tmp)
            payload = b'synthetic payload' * 1024
            write(root, 'file.zip', payload)
            with serve(root, Handler, token='test-token') as port:
                Handler._buffer_gate.acquire()
                try:
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    with opener.open(f'http://127.0.0.1:{port}/test-token/file.zip', timeout=5) as response:
                        self.assertEqual(response.read(), payload)
                    self.assertTrue(streamed.is_set())
                finally:
                    Handler._buffer_gate.release()


if __name__ == '__main__':
    unittest.main()

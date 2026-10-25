"""Run real helper entry scripts against synthetic HTTP manifests and payloads."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from test_updater import PS, ROOT, json_write, serve, write

TOOLS = Path(os.environ.get('UPDATER_HELPER_TOOLS_PATH') or ROOT / 'tools')
BOOT = TOOLS / 'portable-bootstrap-refresh.ps1'
BAT = TOOLS / 'portable-windows-sync.bat'
MAC = TOOLS / 'portable-macos-sync.command'

def mac_source():
    return MAC.read_text('utf-8').split("<<'PY'\n", 1)[1].split('\nPY\n', 1)[0]

def inline_source():
    return next(line for line in BAT.read_text('utf-8').splitlines() if 'function Fetch(' in line).split('-Command "', 1)[1][:-1]

class RefreshFixture(unittest.TestCase):
    def exercise_refresh(self, engine, corrupt):
        with tempfile.TemporaryDirectory(prefix='entry-refresh-') as tmp:
            root = Path(tmp)
            web, client = root / 'web', root / 'client'
            tools = client / '_updater'
            tools.mkdir(parents=True)
            helper = 'player-update-generic.py'
            payload = b'raise RuntimeError("validation must never execute this code")\n'
            row = write(web, '_updater/' + helper, payload)
            if corrupt:
                write(web, '_updater/' + helper, b'<html>intercepted download</html>')
            json_write(web / 'manifest.json', {'files': [row]})
            write(client, '_updater/' + helper, b'# old helper\n')
            entry = tools / 'macOS-sync.command'
            shutil.copyfile(MAC, entry)
            with serve(web) as port:
                (client / 'UPDATE-URL.txt').write_text(f'http://127.0.0.1:{port}/absent.json\nhttp://127.0.0.1:{port}/manifest.json\n')
                command = ([sys.executable, '-', '', str(tools), str(entry)] if engine == 'mac' else
                           [PS, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(BOOT), '-InstanceDir', str(client), '-ScriptDir', str(tools)])
                result = subprocess.run(command, input=mac_source().encode() if engine == 'mac' else None,
                                        env=dict(os.environ, PYTHONIOENCODING='utf-8'),
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
            output = result.stdout.decode('utf-8-sig', errors='replace')
            self.assertEqual(result.returncode, 0, output)
            self.assertEqual((tools / helper).read_bytes(), b'# old helper\n' if corrupt else payload)
            for marker in ('Traceback', 'CategoryInfo'):
                self.assertNotIn(marker, output)
            if corrupt:
                self.assertIn('校验失败', output)

class MacRefreshTests(RefreshFixture):
    def test_multiline_fallback_parses_validated_python_without_executing(self):
        self.exercise_refresh('mac', False)
    def test_html_cannot_replace_helper(self):
        self.exercise_refresh('mac', True)
    def test_watch_identity_requires_exact_instance_and_watch_mode(self):
        source = mac_source()
        namespace = {'re': re, 'subprocess': subprocess}
        exec(source[source.index('def watch_matches('):source.index('def stop_previous_watch(')], namespace)
        root = Path('/Users/player/My Instance')
        # Use the platform's rendered root to test regex boundaries on every OS.
        for command, expected in [
                (f'python3 /tools/portable-stage-daemon.py --instance-dir {root} --mode watch', True),
                (f'python3 /tools/portable-stage-daemon.py --instance-dir {root}-other --mode watch', False),
                (f'python3 /tools/portable-stage-daemon.py --instance-dir {root} --mode promote', False),
                (f'python3 /tools/unrelated.py --instance-dir {root} --mode watch', False)]:
            with mock.patch.object(subprocess, 'check_output', return_value=command):
                self.assertEqual(namespace['watch_matches'](123, root), expected, command)

@unittest.skipUnless(PS and os.name == 'nt', 'Requires Windows PowerShell')
class WindowsRefreshTests(RefreshFixture):
    def test_bootstrap_stops_only_watch_for_exact_instance(self):
        with tempfile.TemporaryDirectory(prefix='watch-identity-') as tmp:
            root = Path(tmp)
            client = root / 'client with spaces'
            client.mkdir()
            fake = root / 'portable-stage-daemon.py'
            fake.write_text('import time\ntime.sleep(60)\n')
            for suffix, should_stop in [('-other', False), ('', True)]:
                child = subprocess.Popen([sys.executable, str(fake), '--instance-dir', str(client) + suffix, '--mode', 'watch'],
                                         creationflags=subprocess.CREATE_NO_WINDOW)
                try:
                    write(client, '.portable-staging/daemon.lock', str(child.pid).encode())
                    result = subprocess.run([PS, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(BOOT),
                                             '-InstanceDir', str(client), '-ScriptDir', str(client)],
                                            capture_output=True, timeout=20)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(child.poll() is not None, should_stop, result.stdout)
                    self.assertFalse((client / '.portable-staging/daemon.lock').exists())
                finally:
                    if child.poll() is None:
                        child.terminate()
                    child.wait(timeout=10)

    def test_html_cannot_replace_helper(self):
        self.exercise_refresh('ps', True)
    def test_multiline_selected_manifest_supplies_expected_hash(self):
        self.exercise_refresh('ps', False)
    def test_inline_only_installs_verified_bootstrap(self):
        with tempfile.TemporaryDirectory(prefix='bat-bootstrap-hash-') as tmp:
            root = Path(tmp)
            web, client = root / 'web', root / 'client'
            client.mkdir()
            row = write(web, '_updater/portable-bootstrap-refresh.ps1', BOOT.read_bytes())
            json_write(web / 'manifest.json', {'files': [row]})
            write(client, 'player-update-generic.py', b'keep other helper')
            env = dict(os.environ, PORTABLE_INSTANCE_DIR=str(client), PORTABLE_SCRIPT_DIR=str(client),
                       PORTABLE_BOOTSTRAP_REFRESH=str(client / 'portable-bootstrap-refresh.ps1'))
            with serve(web) as port:
                env['PORTABLE_MANIFEST_URL'] = f'http://127.0.0.1:{port}/manifest.json'
                write(web, '_updater/portable-bootstrap-refresh.ps1', b'Write-Output "bad response"')
                bad = subprocess.run([PS, '-NoProfile', '-Command', inline_source()], env=env, capture_output=True, timeout=20)
                self.assertEqual(bad.returncode, 1, bad.stdout + bad.stderr)
                self.assertFalse((client / 'portable-bootstrap-refresh.ps1').exists())
                write(web, '_updater/portable-bootstrap-refresh.ps1', BOOT.read_bytes())
                good = subprocess.run([PS, '-NoProfile', '-Command', inline_source()], env=env, capture_output=True, timeout=20)
            self.assertEqual(good.returncode, 0, good.stdout + good.stderr)
            self.assertEqual((client / 'portable-bootstrap-refresh.ps1').read_bytes(), BOOT.read_bytes())
            self.assertEqual((client / 'player-update-generic.py').read_bytes(), b'keep other helper')
    def test_full_bat_recovers_bootstrap_before_stub_sync(self):
        with tempfile.TemporaryDirectory(prefix='bat-entry-order-') as tmp:
            root = Path(tmp)
            web, client = root / 'web', root / 'client'
            client.mkdir()
            stub = b'param($InstanceDir,[switch]$NoPause)\n[IO.File]::WriteAllText((Join-Path $InstanceDir "synced.txt"),"yes")\nexit 0\n'
            rows = [write(web, '_updater/portable-bootstrap-refresh.ps1', BOOT.read_bytes()), write(web, '_updater/player-update-generic.ps1', stub)]
            json_write(web / 'manifest.json', {'files': rows})
            shutil.copyfile(BAT, client / 'Windows-sync.bat')
            with serve(web) as port:
                (client / 'UPDATE-URL.txt').write_text(f'http://127.0.0.1:{port}/manifest.json')
                env = dict(os.environ, PORTABLE_SYNC_ONLY='1')
                env.pop('PORTABLE_SYNC_BOOTSTRAPPED', None)
                env.pop('PORTABLE_SYNC_HOME', None)
                result = subprocess.run(['cmd.exe', '/d', '/c', str(client / 'Windows-sync.bat')], env=env, cwd=client,
                                        input=b'\r\n', stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout.decode('utf-8', errors='replace'))
            self.assertEqual((client / 'synced.txt').read_text(), 'yes')
            self.assertEqual((client / 'portable-bootstrap-refresh.ps1').read_bytes(), BOOT.read_bytes())

if __name__ == '__main__':
    unittest.main()

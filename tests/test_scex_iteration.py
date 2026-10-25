"""Updater 2.27.24: entry points run the _updater copies (ROOT_DELEGATE_V1), stale root
helper copies are removed, and --verify-all repairs damage the fast path cannot see
(MAC_REPAIR_V1). Synthetic files and localhost only."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from test_updater import PS, ROOT, json_write, serve, write

TOOLS = ROOT / 'tools'
HELPERS = ['player-update-generic.py', 'player-update-generic.ps1', 'portable-stage-daemon.py', 'portable-stage-daemon.ps1',
           'portable-bootstrap-refresh.ps1', 'Windows-sync.bat', 'macOS-sync.command']


def base_manifest(rows):
    return {'packId': 'iter', 'version': 'v1', 'files': rows, 'downloadWorkers': 2, 'adoptExistingFiles': False,
            'preservePlayerCustomizations': False, 'forceSyncGlobs': ['*'],
            'cleanup': {'removeConnectorCache': False, 'disableLauncherRepairIndex': False, 'disableDuplicateMods': False},
            'playerOptions': {}, 'serverList': {'enabled': False}}


def engine_command(engine, client, url, *extra):
    if engine == 'python':
        return [sys.executable, str(TOOLS / 'player-update-generic.py'), '--instance-dir', str(client), '--manifest-url', url, *extra]
    return [PS, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(TOOLS / 'player-update-generic.ps1'),
            '-InstanceDir', str(client), '-ManifestUrl', url, '-NoPause']


def run(command, cwd, **env):
    environment = dict(os.environ, PYTHONUTF8='1', NO_PROXY='127.0.0.1', no_proxy='127.0.0.1', **env)
    environment.pop('PORTABLE_SYNC_INTERACTIVE', None)
    result = subprocess.run(command, cwd=cwd, env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, timeout=120)
    return result.returncode, result.stdout.decode('utf-8-sig', errors='replace')


class RootHelperCleanupTests(unittest.TestCase):
    def check(self, engine):
        with tempfile.TemporaryDirectory(prefix='scex-root-clean-') as tmp:
            base = Path(tmp)
            web, client = base / 'web', base / 'client'
            client.mkdir()
            rows = [write(web, '_updater/' + name, ('current ' + name).encode()) for name in HELPERS[:3]]
            rows.append(write(web, 'mods/a.jar', b'a'))
            json_write(web / 'server-manifest.json', base_manifest(rows))
            for name in HELPERS[:4]:
                write(client, name, b'old root copy of ' + name.encode())
            with serve(web) as port:
                url = f'http://127.0.0.1:{port}/server-manifest.json'
                # First run: _updater copies arrive, root copies of those names go away.
                code, output = run(engine_command(engine, client, url), base)
                self.assertEqual(code, 0, output)
            for name in HELPERS[:3]:
                self.assertFalse((client / name).exists(), name)
            # portable-stage-daemon.ps1 has no _updater row: the root copy stays.
            self.assertTrue((client / HELPERS[3]).is_file())
            self.assertIn('旧的更新器副本', output)

    def test_python_removes_verified_duplicates_only(self):
        self.check('python')

    @unittest.skipUnless(PS and os.name == 'nt', 'Requires Windows PowerShell')
    def test_powershell_removes_verified_duplicates_only(self):
        self.check('powershell')

    def test_root_copy_kept_when_updater_copy_differs_or_root_is_managed(self):
        spec_dir = Path(tempfile.mkdtemp(prefix='scex-root-unit-'))
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location('iter_updater', TOOLS / 'player-update-generic.py')
            u = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(u)
            root = spec_dir.resolve()
            good = write(root, '_updater/player-update-generic.py', b'good')
            write(root, '_updater/portable-stage-daemon.py', b'damaged')
            write(root, 'player-update-generic.py', b'root copy')
            write(root, 'portable-stage-daemon.py', b'root copy')
            write(root, 'Windows-sync.bat', b'root managed')
            daemon_row = {'path': '_updater/portable-stage-daemon.py', 'sha1': '0' * 40, 'size': 4}
            bat_rows = [write(root, '_updater/Windows-sync.bat', b'bat'), {'path': 'Windows-sync.bat', 'sha1': '1' * 40, 'size': 1}]
            self.assertEqual(u.remove_root_helper_copies(root, [good, daemon_row] + bat_rows), 1)
            self.assertFalse((root / 'player-update-generic.py').exists())
            self.assertTrue((root / 'portable-stage-daemon.py').exists())
            self.assertTrue((root / 'Windows-sync.bat').exists())
        finally:
            shutil.rmtree(spec_dir, ignore_errors=True)


class VerifyAllTests(unittest.TestCase):
    def test_verify_all_repairs_same_size_damage_the_fast_path_skips(self):
        with tempfile.TemporaryDirectory(prefix='scex-verify-all-') as tmp:
            base = Path(tmp)
            web, client = base / 'web', base / 'client'
            client.mkdir()
            json_write(web / 'server-manifest.json', base_manifest([write(web, 'mods/a.jar', b'payload a')]))
            with serve(web) as port:
                url = f'http://127.0.0.1:{port}/server-manifest.json'
                self.assertEqual(run(engine_command('python', client, url), base)[0], 0)
                target = client / 'mods/a.jar'
                stamp = target.stat()
                target.write_bytes(b'payload b')
                os.utime(target, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
                self.assertEqual(run(engine_command('python', client, url), base)[0], 0)
                self.assertEqual(target.read_bytes(), b'payload b')  # size+mtime record trusted
                code, output = run(engine_command('python', client, url, '--verify-all'), base)
                self.assertEqual(code, 0, output)
                self.assertIn('完整校验', output)
                self.assertEqual(target.read_bytes(), b'payload a')


@unittest.skipUnless(PS and os.name == 'nt', 'Windows entry point')
class WindowsEntryDelegationTests(unittest.TestCase):
    def test_root_entry_runs_the_updater_copies(self):
        with tempfile.TemporaryDirectory(prefix='scex-delegate-') as tmp:
            client = Path(tmp) / 'client'
            scripts = client / '_updater'
            scripts.mkdir(parents=True)
            shutil.copyfile(TOOLS / 'portable-windows-sync.bat', client / '更新mod-Windows端.bat')
            marker = Path(tmp) / 'marker.txt'
            (scripts / 'portable-bootstrap-refresh.ps1').write_text(
                "param($InstanceDir, $ScriptDir, $ManifestUrl)\n[IO.File]::AppendAllText('" + str(marker) + "', 'bootstrap ' + $ScriptDir + \"`n\")\nexit 0\n",
                encoding='utf-8-sig')
            (scripts / 'player-update-generic.ps1').write_text(
                "param($InstanceDir, [switch]$NoPause)\n[IO.File]::AppendAllText('" + str(marker) + "', 'engine ' + $PSCommandPath + \"`n\")\nexit 0\n",
                encoding='utf-8-sig')
            (scripts / 'player-update-generic.py').write_text(
                "import pathlib, sys\nwith open(r'" + str(marker) + "', 'a', encoding='utf-8') as f: f.write('engine ' + __file__ + '\\n')\n",
                encoding='utf-8')
            result = subprocess.run(['cmd', '/d', '/c', str(client / '更新mod-Windows端.bat')], cwd=client,
                                    env=dict(os.environ, PORTABLE_SYNC_ONLY='1'), stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
            log = marker.read_text(encoding='utf-8')
            self.assertEqual(result.returncode, 0, result.stdout.decode('utf-8', 'replace') + log)
            lines = [line for line in log.splitlines() if line]
            self.assertTrue(lines[0].startswith('bootstrap ') and lines[0].rstrip('\\').endswith('_updater'), log)
            self.assertIn('_updater', lines[1], log)


@unittest.skipUnless(os.name != 'nt' and shutil.which('bash'), 'Requires bash')
class MacEntryTests(unittest.TestCase):
    def test_root_command_execs_the_updater_copy(self):
        with tempfile.TemporaryDirectory(prefix='scex-mac-delegate-') as tmp:
            client = Path(tmp) / 'client'
            (client / '_updater').mkdir(parents=True)
            shutil.copyfile(TOOLS / 'portable-macos-sync.command', client / '更新mod-Mac端.command')
            (client / '_updater/macOS-sync.command').write_text('#!/usr/bin/env bash\necho "delegated:$PORTABLE_ROOT_DELEGATED:$(pwd)"\n', encoding='utf-8')
            result = subprocess.run(['bash', str(client / '更新mod-Mac端.command')], stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
            output = result.stdout.decode('utf-8', 'replace')
            self.assertEqual(result.returncode, 0, output)
            self.assertIn('delegated:1:', output)

    def test_repair_command_parses(self):
        result = subprocess.run(['bash', '-n', str(TOOLS / 'portable-macos-repair.command')], capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', 'replace'))


if __name__ == '__main__':
    unittest.main()

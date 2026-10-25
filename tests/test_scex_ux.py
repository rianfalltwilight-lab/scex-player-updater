"""Updater 2.27.23 player-facing behaviour: progress lines with per-file details in
the log only, error codes plus a masked diagnostic zip, the interactive lock wait,
self-repair restore options, the daemon banner and the macOS python3 probe.
Synthetic files and localhost servers only."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

from test_updater import PS, ROOT, json_write, serve, write

TOOLS = ROOT / 'tools'
DIAGNOSTIC = '更新诊断包.zip'
TOKEN = 'fixture-secret-7781'


def load(name):
    spec = importlib.util.spec_from_file_location('scex_ux_' + name.replace('-', '_').replace('.', '_'), TOOLS / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def clean_env(**extra):
    env = dict(os.environ, PYTHONUTF8='1', NO_PROXY='127.0.0.1,localhost', no_proxy='127.0.0.1,localhost')
    env.pop('PORTABLE_SYNC_INTERACTIVE', None)
    env.update(extra)
    return env


def base_manifest(rows, version='2.0'):
    return {'packId': 'ux', 'version': version, 'files': rows, 'downloadWorkers': 2,
            'adoptExistingFiles': False, 'preservePlayerCustomizations': False,
            'additiveDirs': [], 'forceSyncGlobs': ['*'],
            'cleanup': {'removeConnectorCache': False, 'disableLauncherRepairIndex': False,
                        'disableDuplicateMods': False},
            'playerOptions': {}, 'serverList': {'enabled': False}}


class Fixture:
    """A web root served below a secret token directory and an empty client."""

    def __init__(self, base):
        self.client = base / 'client'
        self.web = base / 'web' / TOKEN
        self.client.mkdir()
        self.web.mkdir(parents=True)
        self.files = {f'mods/f{i}.jar': b'payload %d' % i for i in range(4)}
        self.rows = [write(self.web, rel, data) for rel, data in self.files.items()]

    def publish(self, rows=None):
        json_write(self.web / 'server-manifest.json', base_manifest(rows or self.rows))

    def command(self, engine, port):
        url = f'http://127.0.0.1:{port}/{TOKEN}/server-manifest.json'
        if engine == 'python':
            return [sys.executable, str(TOOLS / 'player-update-generic.py'),
                    '--instance-dir', str(self.client), '--manifest-url', url]
        return [PS, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                str(TOOLS / 'player-update-generic.ps1'), '-InstanceDir', str(self.client),
                '-ManifestUrl', url, '-NoPause']


def run_updater(command, cwd, **env):
    result = subprocess.run(command, cwd=cwd, env=clean_env(**env), stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
    return result.returncode, result.stdout.decode('utf-8-sig', errors='replace')


class UpdaterConsoleTests(unittest.TestCase):
    def check_progress_and_log(self, engine):
        with tempfile.TemporaryDirectory(prefix='scex-ux-progress-') as tmp:
            fx = Fixture(Path(tmp))
            fx.publish()
            with serve(fx.web.parent) as port:
                code, output = run_updater(fx.command(engine, port), tmp)
                self.assertEqual(code, 0, output)
                self.assertIn('[同步] 需要更新 4 个文件', output)
                self.assertIn('[进度] 4/4 个文件', output)
                self.assertNotIn('mods/f0.jar', output)  # per-file lines go to the log only
                self.assertNotIn('Python', output)  # the install hint is for 100+ files
                log = (fx.client / '.portable-sync-logs/latest.log').read_text('utf-8')
                for rel in fx.files:
                    self.assertIn(rel, log)
                self.assertNotIn(TOKEN, log)
                code, output = run_updater(fx.command(engine, port), tmp)
                self.assertEqual(code, 0, output)
            previous = (fx.client / '.portable-sync-logs/previous.log').read_text('utf-8')
            self.assertIn('mods/f0.jar', previous)

    def check_failure_package(self, engine):
        with tempfile.TemporaryDirectory(prefix='scex-ux-diag-') as tmp:
            fx = Fixture(Path(tmp))
            missing = write(fx.web, 'mods/missing.jar', b'not served')
            (fx.web / 'mods/missing.jar').unlink()
            fx.publish(fx.rows + [missing])
            with serve(fx.web.parent) as port:
                code, output = run_updater(fx.command(engine, port), tmp)
                self.assertEqual(code, 1, output)
                self.assertIn('[错误编号] E-', output)
                self.assertNotIn('[错误编号] E-OTHER', output)
                self.assertIn(DIAGNOSTIC, output)
                package = fx.client / DIAGNOSTIC
                self.assertTrue(package.is_file(), output)
                with zipfile.ZipFile(package) as archive:
                    names = set(archive.namelist())
                    self.assertIn('summary.json', names)
                    self.assertIn('.portable-sync-logs/latest.log', names)
                    summary = json.loads(archive.read('summary.json').decode('utf-8'))
                    self.assertIn('[错误编号] ' + summary['errorCode'], output)
                    self.assertEqual(summary['engine'], engine)
                    for name in names:
                        self.assertNotIn(TOKEN.encode(), archive.read(name), name)
                self.assertFalse(Path(str(package) + '.tmp').exists())
                write(fx.web, 'mods/missing.jar', b'not served')
                code, output = run_updater(fx.command(engine, port), tmp)
                self.assertEqual(code, 0, output)
            self.assertFalse((fx.client / DIAGNOSTIC).exists())

    def test_python_progress_lines_and_details_in_log(self):
        self.check_progress_and_log('python')

    def test_python_failure_writes_masked_diagnostic_and_success_removes_it(self):
        self.check_failure_package('python')

    @unittest.skipUnless(PS and os.name == 'nt', 'Requires Windows PowerShell')
    def test_powershell_progress_lines_and_details_in_log(self):
        self.check_progress_and_log('powershell')

    @unittest.skipUnless(PS and os.name == 'nt', 'Requires Windows PowerShell')
    def test_powershell_failure_writes_masked_diagnostic_and_success_removes_it(self):
        self.check_failure_package('powershell')

    @unittest.skipUnless(PS and os.name == 'nt', 'Requires Windows PowerShell')
    def test_interactive_flag_with_redirected_input_never_waits_on_a_lock(self):
        from test_powershell_file_preflight import hold_read_lock
        with tempfile.TemporaryDirectory(prefix='scex-ux-lock-') as tmp:
            fx = Fixture(Path(tmp))
            old = write(fx.client, 'mods/f0.jar', b'old')
            json_write(fx.client / '.portable-sync-state.json', {'version': '1.0', 'files': [old]})
            fx.publish()
            with serve(fx.web.parent) as port, hold_read_lock(fx.client / 'mods/f0.jar'):
                for engine in ('python', 'powershell'):
                    code, output = run_updater(fx.command(engine, port), tmp, PORTABLE_SYNC_INTERACTIVE='1')
                    self.assertEqual(code, 1, output)
                    self.assertIn('E-LOCK', output)
                    self.assertNotIn('[等待]', output)


class PythonHelperTests(unittest.TestCase):
    def setUp(self):
        self.u = load('player-update-generic.py')
        self.directory = tempfile.TemporaryDirectory(prefix='scex-ux-py-')
        self.root = Path(self.directory.name).resolve()

    def tearDown(self):
        self.directory.cleanup()

    def test_duration_progress_and_masking(self):
        u = self.u
        self.assertEqual(u.format_duration(0.2), '1 秒')
        self.assertEqual(u.format_duration(59), '59 秒')
        self.assertEqual(u.format_duration(61), '2 分钟')
        self.assertEqual(u.format_duration(3700), '1 小时 2 分钟')
        progress = u.DownloadProgress([1048576, 1048576])
        progress.done_files, progress.done_bytes = 1, 1048576
        line = progress.render(progress.started + 1)
        self.assertTrue(line.startswith('[进度] 1/2 个文件，1.0/2.0 MB'), line)
        self.assertIn('预计还需 1 秒', line)
        masked = u.mask_urls(f'see http://example.org:18088/{TOKEN}/server-manifest.json?x=1 now')
        self.assertEqual(masked, 'see http://example.org/*** now')
        self.assertEqual(u.error_code('[文件占用] mods/a.jar'), 'E-LOCK')
        self.assertEqual(u.error_code('something else'), 'E-OTHER')

    def run_main(self, manifest, payloads):
        class Response(io.BytesIO):
            def __init__(self, content):
                super().__init__(content)
                self.status = 200
                self.headers = {'Content-Length': str(len(content))}

        def fetch(request, **kwargs):
            return Response(payloads[request.full_url.split('/secret/', 1)[-1]])
        opener = mock.Mock()
        opener.open.side_effect = fetch
        with mock.patch.object(sys, 'argv', ['updater', '--instance-dir', str(self.root),
                     '--manifest-url', 'http://127.0.0.1/secret/server-manifest.json']), \
             mock.patch.object(self.u, 'fetch_bytes', return_value=json.dumps(manifest).encode()), \
             mock.patch.object(self.u, '_DIRECT_OPENER', opener), \
             mock.patch.object(self.u.time, 'sleep'), contextlib.redirect_stdout(io.StringIO()) as out, \
             contextlib.redirect_stderr(io.StringIO()):
            try:
                return self.u.main(), out.getvalue()
            except self.u.UpdateError:
                return 1, out.getvalue()

    def lock_wait(self, interactive):
        u = self.u
        data = b'fresh'
        manifest = base_manifest([{'path': 'mods/a.jar', 'sha1': hashlib.sha1(data).hexdigest(), 'size': len(data)}])
        busy = u.LocalFileUpdateError('[文件占用] mods/a.jar 正被其他程序使用。')
        with mock.patch.object(u, 'preflight_managed_files', side_effect=[busy, None]) as probe, \
             mock.patch.object(u, 'interactive_session', return_value=interactive), \
             mock.patch('builtins.input', return_value='') as answer:
            code, output = self.run_main(manifest, {'mods/a.jar': data})
        return code, output, probe.call_count, answer.call_count

    def test_lock_wait_retries_after_enter_in_an_interactive_session(self):
        code, output, probes, answers = self.lock_wait(True)
        self.assertEqual(code, 0, output)
        self.assertEqual((probes, answers), (2, 1))
        self.assertIn('[等待]', output)
        self.assertEqual((self.root / 'mods/a.jar').read_bytes(), b'fresh')

    def test_lock_wait_is_skipped_without_an_interactive_session(self):
        code, output, probes, answers = self.lock_wait(False)
        self.assertEqual(code, 1, output)
        self.assertEqual((probes, answers), (1, 0))
        self.assertFalse((self.root / 'mods/a.jar').exists())

    def test_interactive_session_requires_the_entry_point_flag(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('PORTABLE_SYNC_INTERACTIVE', None)
            self.assertFalse(self.u.interactive_session())
            os.environ['PORTABLE_SYNC_INTERACTIVE'] = '1'
            with mock.patch.object(sys, 'stdin', io.StringIO('')):
                self.assertFalse(self.u.interactive_session())  # not a terminal


class DaemonBannerTests(unittest.TestCase):
    def test_banner_is_windows_only_and_never_raises(self):
        stage = load('portable-stage-daemon.py')
        with mock.patch.object(stage.os, 'name', 'posix'), contextlib.redirect_stdout(io.StringIO()) as out:
            stage.show_watch_banner()
        self.assertEqual(out.getvalue(), '')
        fake_ctypes = mock.Mock()
        ascii_only = io.TextIOWrapper(io.BytesIO(), encoding='ascii')
        with mock.patch.object(stage.os, 'name', 'nt'), mock.patch.dict(sys.modules, {'ctypes': fake_ctypes}), \
             mock.patch.object(sys, 'stdout', ascii_only):
            stage.show_watch_banner()  # Chinese text into an ASCII pipe is dropped quietly
        fake_ctypes.windll.kernel32.SetConsoleTitleW.assert_called_once()


class MacEntryTests(unittest.TestCase):
    def test_python3_probe_runs_before_any_python_use(self):
        text = (TOOLS / 'portable-macos-sync.command').read_text('utf-8')
        lines = text.splitlines()
        probe = next(i for i, line in enumerate(lines) if "python3 -c 'import sys'" in line)
        for line in lines[:probe]:
            if not line.strip().startswith('#'):
                self.assertNotIn('python3', line)
        self.assertIn('exit 1', '\n'.join(lines[probe:probe + 8]))
        self.assertIn('PORTABLE_SYNC_INTERACTIVE=1 python3', text)
        self.assertIn('_updater', text)

    @unittest.skipUnless(os.name != 'nt' and shutil.which('bash'), 'Requires bash')
    def test_command_parses_in_bash(self):
        result = subprocess.run(['bash', '-n', str(TOOLS / 'portable-macos-sync.command')],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout.decode('utf-8', errors='replace'))


@unittest.skipUnless(PS and os.name == 'nt', 'Windows repair scripts')
class SelfRepairRestoreTests(unittest.TestCase):
    def run_repair(self, root, client, port, *flags):
        env = clean_env()
        for key in ('APPDATA', 'LOCALAPPDATA', 'ProgramFiles'):
            env[key] = str(root / 'empty-programs')
        result = subprocess.run(
            [PS, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(TOOLS / 'player-self-repair.ps1'),
             '-InstanceDir', str(client), '-ManifestUrl',
             f'http://127.0.0.1:{port}/{TOKEN}/server-manifest.json', '-NoPause', *flags],
            env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
        output = result.stdout.decode('utf-8', errors='replace')
        self.assertIn(result.returncode, (0, 1), output)
        return output, json.loads((client / '_repair/latest/meta.json').read_text(encoding='utf-8-sig'))

    def fixture(self, root):
        client, web = root / 'client', root / 'web' / TOKEN
        client.mkdir()
        web.mkdir(parents=True)
        old = [write(client, 'config/custom.txt', b'default'), write(client, 'mods/demo.jar', b'mod')]
        json_write(client / '.portable-sync-state.json', {'files': old})
        (client / 'config/custom.txt').write_bytes(b'player edit')
        rows = [write(web, 'config/custom.txt', b'server v2'), write(web, 'mods/demo.jar', b'mod')]
        json_write(web / 'server-manifest.json', {
            'version': 'fixture', 'minecraftVersion': '1.21.1', 'files': rows,
            'preservePlayerCustomizations': True, 'preserveLocalChangeGlobs': ['config/*']})
        return client, web

    def test_restore_preserved_replaces_kept_edits_with_server_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            client, web = self.fixture(root)
            with serve(web.parent) as port:
                _, meta = self.run_repair(root, client, port)
                self.assertEqual((client / 'config/custom.txt').read_bytes(), b'player edit')
                self.assertEqual(meta['restoredToServer'], [])
                output, meta = self.run_repair(root, client, port, '-Deep', '-RestorePreserved')
            self.assertEqual((client / 'config/custom.txt').read_bytes(), b'server v2', output)
            self.assertEqual(meta['restoredToServer'], ['config/custom.txt'])
            backups = list((client / '_repair').glob('*/backup/config/custom.txt'))
            self.assertEqual([p.read_bytes() for p in backups], [b'player edit'])

    def test_restore_last_update_skips_mods_updater_and_root_entry_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            client, web = self.fixture(root)
            (client / 'config/custom.txt').write_bytes(b'server v2')
            write(client, '更新mod-Windows端.bat', b'new entry')
            write(client, '_updater/player-update-generic.ps1', b'new updater')
            stamp = '.portable-sync-backups/20261025-120000-000-abcd1234/'
            write(client, stamp + 'config/custom.txt', b'before update')
            write(client, stamp + 'mods/demo.jar', b'old mod')
            write(client, stamp + 'mods/retired.jar', b'deleted by the update')
            write(client, stamp + '更新mod-Windows端.bat', b'old entry')
            write(client, stamp + '_updater/player-update-generic.ps1', b'old updater')
            write(client, '.portable-sync-backups/20250101-000000-000-older000/config/custom.txt', b'older')
            with serve(web.parent) as port:
                output, _ = self.run_repair(root, client, port, '-RestoreLastUpdate')
            self.assertEqual((client / 'config/custom.txt').read_bytes(), b'before update', output)
            self.assertEqual((client / 'mods/demo.jar').read_bytes(), b'mod')
            self.assertFalse((client / 'mods/retired.jar').exists())
            self.assertEqual((client / '更新mod-Windows端.bat').read_bytes(), b'new entry')
            self.assertEqual((client / '_updater/player-update-generic.ps1').read_bytes(), b'new updater')
            backups = list((client / '_repair').glob('*/backup/config/custom.txt'))
            self.assertEqual([p.read_bytes() for p in backups], [b'server v2'])


if __name__ == '__main__':
    unittest.main()

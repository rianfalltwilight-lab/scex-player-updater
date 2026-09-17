"""Portable server and standalone BAT integration; synthetic files/local HTTP only."""
import contextlib
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('portable_server', ROOT / 'tools/portable_server.py')
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)
PS = shutil.which('powershell.exe')


def write(base, name, data=b'data'):
    p = base / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


@contextlib.contextmanager
def fixture():
    with tempfile.TemporaryDirectory(prefix='portable synthetic ') as t:
        root = Path(t)
        shutil.copytree(ROOT / 'tools', root / 'tools')
        shutil.copytree(ROOT / '一键脚本', root / '一键脚本')
        c = server.read_json(root / 'tools/portable-pack.example.json')
        c['sourceClient'] = 'auto'
        write(root, 'main-client/mods/example.jar', b'fake test mod')
        write(root, 'main-client/config/example.txt', b'first')
        yield root, c


def run(args, root, env=None, expected=0, stdin=None):
    e = dict(os.environ, PYTHONUTF8='1')
    for name in list(e):
        if name.startswith('PORTABLE_'):
            e.pop(name)
    e.update(env or {})
    p = subprocess.run(args, cwd=root, env=e, input=stdin, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, timeout=60)
    if p.returncode != expected:
        raise AssertionError(f'exit={p.returncode}: {p.stdout.decode("utf-8", errors="replace")}')
    return p.stdout.decode('utf-8-sig', errors='replace')


def batch_command(entry):
    # cmd /s consumes the first and last quotes surrounding the entire command.
    return 'cmd.exe /d /s /c ""' + str(entry) + '""'


class DetectionTests(unittest.TestCase):
    def test_unique_multi_server_and_explicit_instance(self):
        with fixture() as (root, c):
            self.assertEqual(server.resolve_config(root, c)[1], (root / 'main-client').resolve())
            write(root, 'client/config/x')
            with self.assertRaisesRegex(ValueError, 'exactly one'):
                server.resolve_config(root, c)
            c['sourceClient'] = 'client'
            self.assertEqual(server.resolve_config(root, c)[1], (root / 'client').resolve())
            write(root, 'client/server.properties')
            with self.assertRaisesRegex(ValueError, 'server directory'):
                server.resolve_config(root, c)

    def test_launcher_layouts_and_metadata(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            for name in ['.minecraft/versions/Example', 'instances/Prism/.minecraft', 'instances/MultiMC/minecraft']:
                write(root, name + '/mods/demo.jar')
            self.assertEqual(len(server.discover_clients(root)), 3)
            instance = root / '.minecraft/versions/Example'
            write(instance, 'Example.json', json.dumps({'inheritsFrom': '1.21.1', 'libraries': [
                {'name': 'net.neoforged:neoforge:21.1.218'}]}).encode())
            self.assertEqual(server.client_metadata(instance), {'minecraftVersion': '1.21.1',
                'loader': {'type': 'neoforge', 'version': '21.1.218'}})

    def test_environment_precedence_and_no_secret_dump(self):
        with fixture() as (root, c):
            config = root / 'tools/portable-pack.json'
            config.write_text(json.dumps(c), encoding='utf-8')
            e = {'PORTABLE_SOURCE_CLIENT': 'main-client', 'PORTABLE_UPDATE_PORT': '19001',
                 'UNRELATED_PASSWORD': 'never-print-me', 'PORTABLE_UPDATE_BIND': '0.0.0.0'}
            loaded, _, origins = server.load_config(root, overrides={'update.port': 19002}, environ=e)
            self.assertEqual(loaded['update']['port'], 19002)
            self.assertEqual(origins['update.port'], 'command line')
            out = run([sys.executable, str(root / 'tools/portable_server.py'), 'doctor'], root, e)
            self.assertNotIn('never-print-me', out)
            self.assertIn('PORTABLE_UPDATE_PORT', out)
            self.assertIn('local-only', out)

    def test_init_does_not_overwrite_and_is_cwd_independent(self):
        with fixture() as (root, c):
            cli = [sys.executable, str(root / 'tools/portable_server.py'), 'init']
            run(cli, root.parent)
            config = root / 'tools/portable-pack.json'
            before = config.read_bytes()
            run(cli, root.parent, expected=2)
            self.assertEqual(config.read_bytes(), before)

    def test_native_server_entry_and_custom_config(self):
        with fixture() as (root, c):
            if os.name == 'nt':
                entry = root / 'tools/portable-server.bat'
                command = 'cmd.exe /d /s /c ""' + str(entry) + '" doctor"'
            else:
                command = ['sh', str(root / 'tools/portable-server.sh'), 'doctor']
            out = run(command, root.parent)
            self.assertEqual(json.loads(out)['sourceClient'], str((root / 'main-client').resolve()))
            cli = [sys.executable, str(root / 'tools/portable_server.py')]
            run(cli + ['init', '--config', 'new-config.json'], root)
            self.assertTrue((root / 'new-config.json').is_file())
            run(cli + ['doctor', '--config', 'missing.json'], root, expected=2)

    def test_path_port_and_host_guards(self):
        with fixture() as (root, c):
            for bad in ['../outside', '.', 'main-client/config/output', 'tools/output']:
                x = copy.deepcopy(c); x['publishDir'] = bad
                with self.assertRaises(ValueError, msg=bad):
                    server.resolve_config(root, x)
            for port in ['abc', 0, 65536]:
                x = copy.deepcopy(c); x['update']['port'] = port
                with self.assertRaises(ValueError):
                    server.resolve_config(root, x)
            for host in ['0.0.0.0', '::', 'https://example.com', 'example.com/path']:
                x = copy.deepcopy(c); x['update']['host'] = host
                with self.assertRaises(ValueError):
                    server.resolve_config(root, x)
            c['update']['host'] = '::1'
            resolved = server.resolve_config(root, c)[0]
            self.assertIn('http://[::1]:', server.url_for(resolved, 'test-token'))


class PortablePublishTests(unittest.TestCase):
    def test_publish_serve_and_real_client_sync_twice(self):
        with fixture() as (root, c):
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', 0)); port = probe.getsockname()[1]
            c['update']['port'] = port
            c['sourceClient'] = 'main-client'
            (root / 'tools/portable-pack.json').write_text(json.dumps(c), encoding='utf-8')
            cli = [sys.executable, str(root / 'tools/portable_server.py')]
            run(cli + ['publish'], root)
            target = root / c['publishDir']
            url = (target / 'UPDATE-URL.txt').read_text().strip()
            client = root / 'player'
            write(client, 'mods/personal.jar', b'personal')
            write(client, 'saves/World/sentinel', b'world')
            proc = subprocess.Popen(cli + ['serve'], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            try:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                for attempt in range(100):
                    try:
                        with opener.open(url, timeout=1) as response:
                            manifest = json.load(response)
                        break
                    except OSError:
                        if proc.poll() is not None:
                            self.fail(proc.stdout.read().decode(errors='replace'))
                        time.sleep(.05)
                else:
                    self.fail('Server did not listen')
                for version in ['1.0.0', '1.0.1']:
                    if version == '1.0.1':
                        write(root, 'main-client/mods/example2.jar', b'second mod')
                        (root / 'main-client/mods/example.jar').unlink()
                        run(cli + ['publish', '--version', version], root)
                    sync = [sys.executable, str(ROOT / 'tools/player-update-generic.py'),
                            '--instance-dir', str(client), '--manifest-url', url]
                    run(sync, root)
                    state = server.read_json(client / '.portable-sync-state.json')
                    self.assertEqual(state['version'], version)
                    self.assertEqual((client / 'mods/personal.jar').read_bytes(), b'personal')
                    self.assertEqual((client / 'saves/World/sentinel').read_bytes(), b'world')
                self.assertFalse((client / 'mods/example.jar').exists())
                self.assertEqual((client / 'mods/example2.jar').read_bytes(), b'second mod')
                for row in server.read_json(target / 'server-manifest.json')['files']:
                    self.assertEqual(hashlib.sha1((target / row['path']).read_bytes()).hexdigest(), row['sha1'])
                self.assertTrue(list(target.parent.glob('.portable-backup-*')))
            finally:
                proc.terminate(); proc.communicate(timeout=10)

    def test_failure_keeps_existing_source_and_releases_lock(self):
        with fixture() as (root, c):
            server.publish(root, c)
            target = root / c['publishDir']
            before = (target / 'server-manifest.json').read_bytes()
            original = Path.rename
            def fail_stage(path, dst):
                if path.name.startswith('.portable-build-'):
                    raise OSError('synthetic swap failure')
                return original(path, dst)
            with mock.patch.object(Path, 'rename', fail_stage):
                with self.assertRaises(OSError):
                    server.publish(root, c)
            self.assertEqual((target / 'server-manifest.json').read_bytes(), before)
            self.assertFalse((root / '.portable-publish-lock').exists())
            self.assertFalse(list(target.parent.glob('.portable-build-*')))

    def test_unknown_publish_directory_case_collision_and_version(self):
        with fixture() as (root, c):
            target = root / c['publishDir']
            write(target, 'unknown.txt', b'keep')
            with self.assertRaisesRegex(ValueError, 'recognized'):
                server.publish(root, c)
            self.assertEqual((target / 'unknown.txt').read_bytes(), b'keep')
            (target / 'unknown.txt').unlink()
            server.publish(root, c)
            c['version'] = '1.2.0'
            with self.assertRaisesRegex(ValueError, 'increment'):
                server.publish(root, c)
            self.assertEqual(server.read_json(target / 'server-manifest.json')['version'], '1.0.0')

    @unittest.skipIf(os.name == 'nt', 'Unix symlink test; Windows entry tested separately')
    def test_symlink_and_case_collision_rejected(self):
        with fixture() as (root, c):
            write(root, 'private.txt', b'private')
            link = root / 'main-client/config/link'
            link.symlink_to(root / 'private.txt')
            with self.assertRaisesRegex(ValueError, 'Linked'):
                server.publish(root, c)
            link.unlink()
            write(root, 'main-client/config/Example.txt', b'collision')
            with self.assertRaisesRegex(ValueError, 'Case-colliding'):
                server.publish(root, c)

    def test_private_token_and_server_files_rejected(self):
        with fixture() as (root, c):
            c['update']['tokenFile'] = c['publishDir'] + '/token.txt'
            with self.assertRaisesRegex(ValueError, 'Token'):
                server.publish(root, c)
            c['update']['tokenFile'] = '.update-server-token'
            c['includeFiles'] = ['../private.txt']
            with self.assertRaisesRegex(ValueError, 'Unsafe'):
                server.publish(root, c)

    def test_empty_input_and_wrong_config_types_do_not_replace_release(self):
        with fixture() as (root, c):
            server.publish(root, c)
            target = root / c['publishDir']
            before = (target / 'server-manifest.json').read_bytes()
            c['includeRoots'] = []
            with self.assertRaisesRegex(ValueError, 'No distributable'):
                server.publish(root, c)
            self.assertEqual((target / 'server-manifest.json').read_bytes(), before)
            c['includeRoots'] = 'mods'
            with self.assertRaisesRegex(ValueError, 'JSON array'):
                server.publish(root, c)

    def test_existing_lock_and_offline_source_for_serve(self):
        with fixture() as (root, c):
            with server.publish_lock(root):
                with self.assertRaisesRegex(ValueError, 'lock exists'):
                    server.publish(root, c)
            c['sourceClient'] = 'unmounted-client'
            resolved = server.resolve_config(root, c, require_source=False)
            self.assertEqual(resolved[1], (root / 'unmounted-client').resolve())

    @unittest.skipIf(os.name == 'nt', 'Unix client shell entry')
    def test_unix_player_entry_passes_instance_with_spaces(self):
        with tempfile.TemporaryDirectory(prefix='player spaces ') as t:
            root = Path(t)
            shutil.copyfile(ROOT / 'tools/portable-unix-sync.sh', root / 'Update.sh')
            write(root, '_updater/player-update-generic.py',
                b'import pathlib,sys\npathlib.Path(sys.argv[sys.argv.index("--instance-dir")+1],"called").write_text("ok")\n')
            run(['sh', str(root / 'Update.sh')], root.parent)
            self.assertEqual((root / 'called').read_text(), 'ok')


@unittest.skipUnless(os.name == 'nt' and PS, 'Windows standalone BAT')
class StandaloneEntryTests(unittest.TestCase):
    def create_client(self, root, path):
        instance = root / path
        write(instance, 'UPDATE-URL.txt', b'http://127.0.0.1:1/test/server-manifest.json')
        write(instance, '_updater/player-update-generic.ps1', b'# synthetic engine')
        write(instance, '_updater/Windows-sync.bat', b'@echo off\r\necho called>"%~dp0called.txt"\r\nexit /b 7\r\n')
        return instance

    def test_bat_alone_beside_launcher_unicode_spaces_and_exit_code(self):
        with tempfile.TemporaryDirectory(prefix='portable entry ') as t:
            root = Path(t) / '中文 pack & bang!'
            root.mkdir()
            entry = root / 'Update.bat'
            shutil.copyfile(ROOT / 'tools/portable-client-entry.bat', entry)
            instance = self.create_client(root, '.minecraft/versions/Example')
            out = run(batch_command(entry), root,
                {'PORTABLE_ENTRY_DETECT_ONLY': '1', 'PORTABLE_ENTRY_NO_PAUSE': '1'})
            self.assertEqual(json.loads(out), [str(instance)])
            run(batch_command(entry), root, {'PORTABLE_ENTRY_NO_PAUSE': '1'}, expected=7)
            self.assertTrue((instance / '_updater/called.txt').is_file())

    def test_multiple_cancel_and_explicit_selection(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            entry = root / 'Update.bat'
            shutil.copyfile(ROOT / 'tools/portable-client-entry.bat', entry)
            one = self.create_client(root, 'instances/one/.minecraft')
            two = self.create_client(root, 'instances/two/minecraft')
            run(batch_command(entry), root, {'PORTABLE_ENTRY_NO_PAUSE': '1'}, expected=2, stdin=b'\n')
            self.assertFalse((one / '_updater/called.txt').exists())
            run(batch_command(entry), root,
                {'PORTABLE_ENTRY_NO_PAUSE': '1', 'PORTABLE_INSTANCE_DIR': str(two)}, expected=7)
            self.assertTrue((two / '_updater/called.txt').is_file())

    def test_real_engine_bootstrap_special_path_and_entry_replacement(self):
        with tempfile.TemporaryDirectory(prefix='portable engine ') as t:
            root = Path(t) / '中文 & ! package'
            root.mkdir()
            entry = root / 'Update.bat'
            shutil.copyfile(ROOT / 'tools/portable-client-entry.bat', entry)
            instance = self.create_client(root, '.minecraft/versions/Example')
            shutil.copyfile(ROOT / 'tools/portable-windows-sync.bat', instance / '_updater/Windows-sync.bat')
            write(instance, '_updater/portable-bootstrap-refresh.ps1', b'# local test, no downloads')
            write(instance, '_updater/player-update-generic.ps1', b'param($InstanceDir,[switch]$NoPause)\r\n[IO.File]::WriteAllText((Join-Path $InstanceDir "called.txt"),$InstanceDir)\r\n[IO.File]::WriteAllText($env:PORTABLE_ENTRY_FILE,"replaced during update")\r\nexit 0\r\n')
            run(batch_command(entry), root, {'PORTABLE_ENTRY_NO_PAUSE': '1', 'PORTABLE_SYNC_ONLY': '1'})
            self.assertTrue((instance / 'called.txt').exists())
            self.assertEqual(entry.read_text(), 'replaced during update')

    def test_repair_preserves_standalone_entry(self):
        with tempfile.TemporaryDirectory(prefix='portable repair ') as t:
            root = Path(t)
            instance = self.create_client(root, '.minecraft/versions/Example')
            entry = instance / '更新mod-Windows端.bat'
            shutil.copyfile(ROOT / 'tools/portable-client-entry.bat', entry)
            expected = entry.read_bytes()
            shutil.copyfile(ROOT / 'tools/portable-windows-sync.bat', instance / '_updater/Windows-sync.bat')
            run([PS, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                 str(ROOT / 'tools/portable-windows-repair.ps1'), '-RepairHome', str(instance),
                 '-StartDir', str(root), '-NoLaunch'], root)
            self.assertEqual(entry.read_bytes(), expected)


if __name__ == '__main__':
    unittest.main()

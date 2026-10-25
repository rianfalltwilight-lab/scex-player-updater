"""Windows file-lock preflight tests using synthetic clients and localhost HTTP."""
import contextlib
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from urllib.parse import urlsplit

from test_updater import PS, ROOT, SilentHandler, json_write, serve, write

SOURCE = Path(os.environ.get('UPDATER_PREFLIGHT_PS_PATH') or
              ROOT / 'tools/player-update-generic.ps1')


def ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


@contextlib.contextmanager
def hold_read_lock(path):
    """Match an open resource ZIP: reading allowed, replacement not shared."""
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                       wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    handle = create(str(path), 0x80000000, 1, None, 3, 0, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield
    finally:
        close(handle)


def snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in root.rglob('*') if p.is_file()
            and p.relative_to(root).parts[0] not in
            ('.portable-sync.lock', '.portable-sync-logs')}


@unittest.skipUnless(PS and os.name == 'nt', 'Requires Windows PowerShell')
class PowerShellFilePreflightTests(unittest.TestCase):
    def run_updater(self, client, port):
        env = dict(os.environ, NO_PROXY='127.0.0.1,localhost',
                   no_proxy='127.0.0.1,localhost')
        result = subprocess.run(
            [PS, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(SOURCE),
             '-InstanceDir', str(client), '-ManifestUrl',
             f'http://127.0.0.1:{port}/server-manifest.json', '-NoPause'],
            cwd=client.parent, env=env, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, timeout=45)
        return result.returncode, result.stdout.decode('utf-8-sig', errors='replace')

    def assert_clean_lock_message(self, code, output):
        self.assertEqual(code, 1, output)
        self.assertIn('[文件占用]', output)
        self.assertIn('完全退出 Minecraft', output)
        self.assertIn('更新未完成', output)
        for marker in ('CategoryInfo', 'FullyQualifiedErrorId', 'Exception calling',
                       'being used by another process', 'At line:', '所在位置'):
            self.assertNotIn(marker, output)

    def exercise_preflight(self, delete_old=False, readonly=False):
        with tempfile.TemporaryDirectory(prefix='updater-ps-preflight-') as tmp:
            root = Path(tmp)
            client, web = root / 'client', root / 'web'
            client.mkdir()
            web.mkdir()
            old_rel = 'mods/obsolete.jar' if delete_old else 'resourcepacks/任务图片.zip'
            new_rel = 'mods/new.jar' if delete_old else old_rel
            previous = [write(client, 'mods/demo.jar', b'old first'),
                        write(client, old_rel, b'old locked')]
            rows = [write(web, 'mods/demo.jar', b'new first'),
                    write(web, new_rel, b'new second')]
            json_write(client / '.portable-sync-state.json',
                       {'version': '1.0', 'files': previous})
            cache = 'mods/.connector/sentinel.cache'
            write(client, cache, b'cache must survive failed preflight')
            manifest = {
                'version': '2.0', 'files': rows, 'downloadWorkers': 2,
                'adoptExistingFiles': False, 'preservePlayerCustomizations': False,
                'additiveDirs': [], 'forceSyncGlobs': ['*'],
                'cleanup': {'removeConnectorCache': True,
                            'disableLauncherRepairIndex': False,
                            'disableDuplicateMods': False},
                'playerOptions': {}, 'serverList': {'enabled': False}}
            json_write(web / 'server-manifest.json', manifest)
            requests = []

            class RecordingHandler(SilentHandler):
                def do_GET(self):
                    requests.append(urlsplit(self.path).path)
                    super().do_GET()

            locked_path = client / old_rel
            with serve(web, RecordingHandler) as port:
                if readonly:
                    locked_path.chmod(stat.S_IREAD)
                else:
                    before = snapshot(client)
                    with hold_read_lock(locked_path):
                        code, output = self.run_updater(client, port)
                        self.assert_clean_lock_message(code, output)
                        self.assertEqual(snapshot(client), before)
                        self.assertTrue(requests)
                        self.assertEqual(set(requests), {'/server-manifest.json'})
                        self.assertFalse((client / '.portable-sync-backups').exists())
                    requests.clear()
                try:
                    code, output = self.run_updater(client, port)
                    self.assertEqual(code, 0, output)
                    self.assertEqual((client / 'mods/demo.jar').read_bytes(), b'new first')
                    self.assertEqual((client / new_rel).read_bytes(), b'new second')
                    state = json.loads((client / '.portable-sync-state.json').read_text('utf-8-sig'))
                    self.assertEqual(state['version'], '2.0')
                    self.assertFalse((client / cache).exists())
                    self.assertTrue(any(path != '/server-manifest.json' for path in requests))
                    if delete_old:
                        self.assertFalse(locked_path.exists())
                finally:
                    if locked_path.exists():
                        locked_path.chmod(stat.S_IREAD | stat.S_IWRITE)

    def test_locked_resource_prevents_all_writes_and_recovers_after_unlock(self):
        self.exercise_preflight()

    def test_locked_retired_manifest_file_prevents_update_and_deletion(self):
        self.exercise_preflight(delete_old=True)

    def test_readonly_resource_is_not_mistaken_for_running_game(self):
        self.exercise_preflight(readonly=True)

    def run_functions(self, root, body):
        script = root / 'functions.ps1'
        header = ("$ErrorActionPreference='Stop'\n"
                  "[Console]::OutputEncoding=[Text.UTF8Encoding]::new()\n"
                  "$ast=[Management.Automation.Language.Parser]::ParseFile(" +
                  ps_quote(SOURCE) + ",[ref]$null,[ref]$null)\n"
                  "$ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst]},$false) | "
                  "ForEach-Object { Invoke-Expression $_.Extent.Text }\n")
        script.write_text(header + body, encoding='utf-8-sig')
        result = subprocess.run(
            [PS, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(script)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
        output = result.stdout.decode('utf-8-sig', errors='replace')
        self.assertEqual(result.returncode, 0, output)
        return json.loads(output)

    def test_lock_acquired_after_preflight_keeps_old_file_and_retry_works(self):
        with tempfile.TemporaryDirectory(prefix='updater-ps-race-') as tmp:
            root = Path(tmp)
            destination, incoming = root / 'pack.zip', root / 'incoming.tmp'
            destination.write_bytes(b'old')
            incoming.write_bytes(b'new')
            result = self.run_functions(root, r'''
$root=ROOT_VALUE; $dest=DEST_VALUE; $temp=TEMP_VALUE
Assert-UpdateFilesAvailable $root @([pscustomobject]@{path='pack.zip'})
$lock=[IO.File]::Open($dest,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
$errorText=''
try { try {Install-VerifiedFile $temp $dest} catch {$errorText=$_.Exception.Message} }
finally {$lock.Dispose()}
$oldPreserved=([IO.File]::ReadAllText($dest) -eq 'old')
$incomingPreserved=([IO.File]::ReadAllText($temp) -eq 'new')
Install-VerifiedFile $temp $dest
[pscustomobject]@{error=$errorText;oldPreserved=$oldPreserved;incomingPreserved=$incomingPreserved;retried=([IO.File]::ReadAllText($dest) -eq 'new')} | ConvertTo-Json -Compress
'''.replace('ROOT_VALUE', ps_quote(root)).replace('DEST_VALUE', ps_quote(destination))
                .replace('TEMP_VALUE', ps_quote(incoming)))
            self.assertTrue(result['error'].startswith('[文件占用]'), result)
            self.assertTrue(result['oldPreserved'], result)
            self.assertTrue(result['incomingPreserved'], result)
            self.assertTrue(result['retried'], result)

    def test_native_error_categories_and_running_helper_exemption(self):
        with tempfile.TemporaryDirectory(prefix='updater-ps-errors-') as tmp:
            root = Path(tmp)
            helper = root / 'Windows-sync.bat'
            helper.write_bytes(b'entry')
            result = self.run_functions(root, r'''
$root=ROOT_VALUE; $helper=HELPER_VALUE
$held=[IO.File]::Open($helper,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
try {Assert-UpdateFilesAvailable $root @([pscustomobject]@{path='Windows-sync.bat'})} finally {$held.Dispose()}
$messages=@{}
foreach($code in @(32,33,5,112)) {
    $exception=New-Object IO.IOException('synthetic',([int](-2147024896+$code)))
    $messages[[string]$code]=Format-UpdateError $exception -Phase '替换' -Path 'fixture.zip'
}
$messages | ConvertTo-Json -Compress
'''.replace('ROOT_VALUE', ps_quote(root)).replace('HELPER_VALUE', ps_quote(helper)))
            for code, expected in [('32', '文件占用'), ('33', '文件占用'),
                                   ('5', '权限不足'), ('112', '空间不足')]:
                self.assertTrue(result[code].startswith('[' + expected + ']'), result)


if __name__ == '__main__':
    unittest.main()

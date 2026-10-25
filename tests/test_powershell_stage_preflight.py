"""Legacy Promote is read-only; Watch shares the main engines' OS lock."""
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
from test_updater import PS, ROOT, write
from test_powershell_file_preflight import hold_read_lock, snapshot

TOOLS = Path(os.environ.get('UPDATER_HELPER_TOOLS_PATH') or ROOT / 'tools')
SOURCE = Path(os.environ.get('UPDATER_STAGE_PREFLIGHT_PS_PATH') or TOOLS / 'portable-stage-daemon.ps1')

def stage_module():
    spec = importlib.util.spec_from_file_location('stage_lock_test', TOOLS / 'portable-stage-daemon.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

class StageCacheOwnershipTests(unittest.TestCase):
    def test_python_legacy_promote_never_installs_corrupt_or_retired_cache(self):
        stage = stage_module()
        with tempfile.TemporaryDirectory(prefix='stage-noop-') as tmp:
            root = Path(tmp)
            for rel, value in [('mods/a.jar', b'good original'), ('.portable-staging/mods/a.jar', b'corrupt'),
                               ('.portable-staging/mods/retired.jar', b'retired'), ('.portable-staging/READY.json', b'{"version":"old"}')]:
                write(root, rel, value)
            before = snapshot(root)
            stage.run_promote(root, root / '.portable-staging', root / 'notice')
            self.assertEqual(snapshot(root), before)

    def test_python_watch_skips_busy_lock_and_releases_after_round(self):
        stage = stage_module()
        with tempfile.TemporaryDirectory(prefix='stage-watch-lock-') as tmp:
            root = Path(tmp)
            cache = root / '.portable-staging'
            cache.mkdir()
            owner = stage.SyncLock(root)
            self.assertTrue(owner.acquire())
            def exercise():
                with mock.patch.object(stage.time, 'sleep', side_effect=[None, KeyboardInterrupt]), \
                     mock.patch.object(stage.Manifest, 'if_changed', return_value=(False, '')) as fetch, \
                     mock.patch.object(stage, 'get_proxy_url', return_value=''):
                    with self.assertRaises(KeyboardInterrupt):
                        stage.run_watch(root, cache, cache / 'READY.json', root / 'notice', cache / 'daemon.lock',
                                        cache / 'daemon.log', 'http://127.0.0.1/manifest.json', 0, 10, 10, 3)
                    return fetch.call_count
            try:
                self.assertEqual(exercise(), 0)
            finally:
                owner.release()
            self.assertEqual(exercise(), 1)
            self.assertTrue(owner.acquire())
            owner.release()
            self.assertTrue((root / '.portable-sync.lock').exists())

@unittest.skipUnless(PS and os.name == 'nt', 'Requires Windows PowerShell')
class PowerShellStageCompatibilityTests(unittest.TestCase):
    def test_legacy_promote_noop_with_locked_client_and_after_unlock(self):
        with tempfile.TemporaryDirectory(prefix='stage-ps-noop-') as tmp:
            root = Path(tmp)
            for rel, value in [('mods/a.jar', b'original'), ('.portable-staging/mods/a.jar', b'bad cache'),
                               ('.portable-staging/mods/withdrawn.jar', b'retired'), ('.portable-staging/READY.json', b'{"version":"old"}')]:
                write(root, rel, value)
            before = snapshot(root)
            def run():
                result = subprocess.run([PS, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(SOURCE),
                                         '-InstanceDir', str(root), '-Mode', 'Promote'], capture_output=True, timeout=20)
                output = result.stdout.decode('utf-8-sig', errors='replace')
                self.assertEqual(result.returncode, 0, output + repr(result.stderr))
                self.assertIn('不改动客户端或缓存', output)
                self.assertEqual(snapshot(root), before)
            with hold_read_lock(root / 'mods/a.jar'):
                run()
            run()

    def test_powershell_python_stage_locks_exclude_each_other(self):
        stage = stage_module()
        with tempfile.TemporaryDirectory(prefix='stage-cross-lock-') as tmp:
            root = Path(tmp)
            source = str(SOURCE).replace("'", "''")
            script = root / 'lock.ps1'
            script.write_text("$ErrorActionPreference='Stop'\n" +
                f"$ast=[Management.Automation.Language.Parser]::ParseFile('{source}',[ref]$null,[ref]$null)\n" +
                "foreach($f in $ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst]},$false)){. ([scriptblock]::Create($f.Extent.Text))}\n" +
                "$handle=Enter-SyncLock -Root $PSScriptRoot\nif($null -eq $handle){Write-Output 'BUSY';exit 3}\n" +
                "try{Write-Output 'HELD';[Console]::Out.Flush();[Console]::ReadLine()|Out-Null}finally{$handle.Dispose()}\n", encoding='utf-8-sig')
            owner = stage.SyncLock(root)
            self.assertTrue(owner.acquire())
            try:
                result = subprocess.run([PS, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(script)], capture_output=True, timeout=15)
                self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
            finally:
                owner.release()
            child = subprocess.Popen([PS, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(script)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            try:
                self.assertEqual(child.stdout.readline().strip(), b'HELD')
                self.assertFalse(owner.acquire())
            finally:
                child.communicate(b'\n', timeout=10)
            self.assertTrue(owner.acquire())
            owner.release()

if __name__ == '__main__':
    unittest.main()

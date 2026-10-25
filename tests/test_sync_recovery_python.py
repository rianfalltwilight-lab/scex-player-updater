"""Main-updater recovery, containment and resumable downloads; synthetic files only."""
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
import urllib.error
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
UPDATER = Path(os.environ.get('SCEX_UPDATER_TEST_PATH', str(ROOT / 'tools/player-update-generic.py')))
PS = shutil.which('powershell.exe')


def digest(data):
    return hashlib.sha1(data).hexdigest()


def row(path, data):
    return {'path': path, 'sha1': digest(data), 'size': len(data)}


def write(root, relative, data):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return row(relative, data)


class Response(io.BytesIO):
    def __init__(self, content, status=200, headers=None):
        super().__init__(content)
        self.status = status
        self.headers = headers or {'Content-Length': str(len(content))}


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('sync_recovery_test', UPDATER)
        self.u = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.u)
        self.directory = tempfile.TemporaryDirectory(prefix='scex-recovery-')
        self.root = Path(self.directory.name).resolve()

    def tearDown(self):
        self.directory.cleanup()

    def manifest(self, rows, version='v2', **settings):
        return dict(packId='test', version=version, files=rows, downloadWorkers=2,
                    adoptExistingFiles=False, cleanup=dict(removeConnectorCache=False,
                    disableLauncherRepairIndex=False, disableDuplicateMods=False),
                    serverList=dict(enabled=False), **settings)

    def state(self, rows, version='v1'):
        (self.root / '.portable-sync-state.json').write_text(
            json.dumps(dict(format=3, packId='test', version=version, files=rows)), encoding='utf-8')

    def run_sync(self, manifest, payloads=None):
        payloads = payloads or {}
        calls = []
        def fetch(request, **kwargs):
            relative = request.full_url.split('/secret/', 1)[-1]
            calls.append(relative)
            value = payloads.get(relative, TimeoutError('private-token-and-url'))
            if isinstance(value, BaseException):
                raise value
            return Response(value)
        opener = mock.Mock()
        opener.open.side_effect = fetch
        with mock.patch.object(sys, 'argv', ['updater', '--instance-dir', str(self.root),
                     '--manifest-url', 'http://127.0.0.1/secret/server-manifest.json']), \
             mock.patch.object(self.u, 'fetch_bytes', return_value=json.dumps(manifest).encode()), \
             mock.patch.object(self.u, '_DIRECT_OPENER', opener), \
             mock.patch.object(self.u.time, 'sleep'), contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()):
            code = self.u.main()
        return code, calls

    def test_missing_verified_performs_hash_and_repairs_same_size_damage(self):
        write(self.root, 'mods/test.jar', b'BAD!')
        expected = row('mods/test.jar', b'GOOD')
        self.state([expected])
        code, calls = self.run_sync(self.manifest([expected]), {'mods/test.jar': b'GOOD'})
        self.assertEqual(code, 0)
        self.assertIn('mods/test.jar', calls)
        self.assertEqual((self.root / 'mods/test.jar').read_bytes(), b'GOOD')

    def test_duplicate_and_install_delete_conflict_rejected_before_updates(self):
        target = write(self.root, 'keep.txt', b'old')
        fixtures = [self.manifest([row('keep.txt', b'new'), row('KEEP.txt', b'NEW')]),
                    self.manifest([target], forceDeleteGlobs=['keep.txt'])]
        for manifest in fixtures:
            with self.subTest(manifest=manifest):
                with self.assertRaisesRegex(self.u.UpdateError, '清单无效'):
                    self.run_sync(manifest)
                self.assertEqual((self.root / 'keep.txt').read_bytes(), b'old')
                self.assertFalse((self.root / '.portable-sync-state.json').exists())

    @unittest.skipUnless(os.name == 'nt' and PS, 'Windows junctions')
    def test_parent_junction_is_rejected_before_outside_write(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'marker.txt').write_bytes(b'old')
        client = self.root / 'client'
        client.mkdir()
        junction = client / 'config'
        script = "New-Item -ItemType Junction -Path '" + str(junction) + "' -Target '" + str(outside) + "' | Out-Null"
        subprocess.run([PS, '-NoProfile', '-Command', script], check=True, capture_output=True)
        try:
            with self.assertRaisesRegex(self.u.LocalFileUpdateError, '路径错误'):
                self.u.validate_manifest(client, self.manifest([row('config/marker.txt', b'new')]))
            self.assertEqual((outside / 'marker.txt').read_bytes(), b'old')
            self.assertEqual(self.u.existing_hash_index(client), {})
        finally:
            self.assertEqual(os.lstat(junction).st_reparse_tag, 0xA0000003)  # Path.is_junction() needs 3.12
            os.rmdir(junction)

    @unittest.skipUnless(os.name == 'nt' and PS, 'Windows junctions')
    def test_wildcard_cleanup_does_not_walk_into_a_junction(self):
        outside = self.root / 'outside-walk'
        outside.mkdir()
        (outside / 'foreign.disabled').write_bytes(b'not ours')
        client = self.root / 'client-walk'
        write(client, 'mods/old.disabled', b'ours')
        junction = client / 'linked'
        script = "New-Item -ItemType Junction -Path '" + str(junction) + "' -Target '" + str(outside) + "' | Out-Null"
        subprocess.run([PS, '-NoProfile', '-Command', script], check=True, capture_output=True)
        try:
            found = self.u.cleanup_candidates(client, ['*.disabled'])
            self.assertEqual([p.relative_to(client).as_posix() for p in found], ['mods/old.disabled'])
        finally:
            os.rmdir(junction)
        self.assertTrue((outside / 'foreign.disabled').is_file())

    def test_backup_keeps_original_before_defaults_and_resource_pack_merge(self):
        original = b'lang:en_us\nresourcePacks:["vanilla"]\n'
        write(self.root, 'options.txt', original)
        pack = write(self.root, 'resourcepacks/test.zip', b'pack')
        manifest = self.manifest([pack], playerOptions=dict(apply='always', targets=['options.txt'],
             defaults=dict(lang='zh_cn'), requiredResourcePacks=['file/test.zip']))
        self.run_sync(manifest)
        backups = list((self.root / '.portable-sync-backups').glob('*/options.txt'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
        self.assertIn('file/test.zip', (self.root / 'options.txt').read_text())

    def test_instance_lock_rejects_second_process_without_truncating_log(self):
        write(self.root, '.portable-sync-logs/latest.log', b'existing running log')
        with self.u.instance_lock(self.root):
            result = subprocess.run([sys.executable, '-X', 'utf8', str(UPDATER),
                '--instance-dir', str(self.root), '--manifest-url', 'http://127.0.0.1/secret/manifest.json'],
                capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('更新器已运行', (result.stdout + result.stderr).decode('utf-8'))
        self.assertEqual((self.root / '.portable-sync-logs/latest.log').read_bytes(), b'existing running log')
        with self.u.instance_lock(self.root):
            pass  # A persistent lock-file name does not itself keep the lock held.

    @unittest.skipUnless(os.name == 'nt' and PS, 'Windows FileShare interoperability')
    def test_python_and_powershell_file_locks_interoperate(self):
        path = str(self.root / '.portable-sync.lock').replace("'", "''")
        with self.u.instance_lock(self.root):
            command = "$ErrorActionPreference='Stop';try{$h=[IO.File]::Open('" + path + "',[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None);$h.Dispose();exit 2}catch{exit 0}"
            result = subprocess.run([PS, '-NoProfile', '-Command', command], capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
        command = "$ErrorActionPreference='Stop';$h=[IO.File]::Open('" + path + "',[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None);[Console]::WriteLine('LOCKED');[Console]::Out.Flush();[Console]::ReadLine()|Out-Null;$h.Dispose()"
        process = subprocess.Popen([PS, '-NoProfile', '-Command', command], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.assertEqual(process.stdout.readline().strip(), b'LOCKED')
            with self.assertRaisesRegex(self.u.UpdateError, '更新器已运行'):
                with self.u.instance_lock(self.root):
                    self.fail('lock acquired while PowerShell held it')
            process.communicate(b'\n', timeout=10)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_atomic_state_keeps_valid_backup_and_recovers_corrupt_current(self):
        first = dict(format=3, version='v1', files=[row('config/deleted.txt', b'default')])
        second = dict(first, version='v2')
        self.u.save_state(self.root, first)
        self.assertEqual(json.loads((self.root / '.portable-sync-state.prev.json').read_text()), first)
        self.u.save_state(self.root, second)
        (self.root / '.portable-sync-state.json').write_text('{"files":', encoding='utf-8')
        with contextlib.redirect_stdout(io.StringIO()):
            recovered = self.u.read_state(self.root)
        self.assertEqual(recovered, first)
        third = dict(first, version='v3')
        self.u.save_state(self.root, third)
        self.assertEqual(self.u.read_state(self.root), third)
        self.assertEqual(json.loads((self.root / '.portable-sync-state.prev.json').read_text()), first)

    def test_atomic_state_write_failure_preserves_previous_state(self):
        old = dict(format=3, version='v1', files=[row('x', b'a')])
        self.u.save_state(self.root, old)
        original = (self.root / '.portable-sync-state.json').read_bytes()
        replace = self.u.os.replace
        def fail_final(source, target):
            if Path(target).name == '.portable-sync-state.json':
                raise OSError('simulated commit failure')
            return replace(source, target)
        with mock.patch.object(self.u.os, 'replace', side_effect=fail_final):
            with self.assertRaises(OSError):
                self.u.save_state(self.root, dict(old, version='v2'))
        self.assertEqual((self.root / '.portable-sync-state.json').read_bytes(), original)

    def test_corrupt_state_without_backup_stops_before_restoring_deletion(self):
        write(self.root, '.portable-sync-state.json', b'{"files":')
        with self.assertRaisesRegex(self.u.UpdateError, '状态损坏'):
            self.run_sync(self.manifest([row('config/deleted.txt', b'default')]))
        self.assertFalse((self.root / 'config/deleted.txt').exists())

    def test_failed_run_recovery_uses_latest_config_and_removes_withdrawn_jar(self):
        old = [write(self.root, 'config/a.txt', b'v1a'), write(self.root, 'config/b.txt', b'v1b')]
        self.state(old)
        v2 = self.manifest([row('config/a.txt', b'v2a'), row('mods/new.jar', b'new'),
                           row('config/b.txt', b'v2b')], preserveLocalChangeGlobs=['config/**'])
        with self.assertRaisesRegex(self.u.UpdateError, '下载失败'):
            self.run_sync(v2, {'config/a.txt': b'v2a', 'mods/new.jar': b'new'})
        self.assertTrue((self.root / 'mods/new.jar').exists())
        self.assertTrue((self.root / '.portable-sync-pending.json').exists())
        self.assertEqual(json.loads((self.root / '.portable-sync-state.json').read_text())['version'], 'v1')
        log = (self.root / '.portable-sync-logs/latest.log').read_text(encoding='utf-8')
        self.assertIn('下载失败', log)
        self.assertNotIn('/secret/', log)
        self.assertNotIn('private-token', log)
        v3 = self.manifest([row('config/a.txt', b'v3a'), row('config/b.txt', b'v3b')],
                           version='v3', preserveLocalChangeGlobs=['config/**'])
        self.run_sync(v3, {'config/a.txt': b'v3a', 'config/b.txt': b'v3b'})
        self.assertEqual((self.root / 'config/a.txt').read_bytes(), b'v3a')
        self.assertEqual((self.root / 'config/b.txt').read_bytes(), b'v3b')
        self.assertFalse((self.root / 'mods/new.jar').exists())
        self.assertFalse((self.root / '.portable-sync-pending.json').exists())

    def test_pending_write_failure_does_not_replace_old_file(self):
        target = self.root / 'payload.jar'
        target.write_bytes(b'old')
        temporary = self.root / 'ready.tmp'
        temporary.write_bytes(b'new')
        journal = mock.Mock(root=self.root, path=self.root / '.portable-sync-pending.json')
        journal.record.side_effect = PermissionError('private diagnostic')
        with mock.patch.object(self.u, '_ACTIVE_JOURNAL', journal):
            with self.assertRaisesRegex(self.u.LocalFileUpdateError, '权限不足'):
                self.u.install_verified_file(temporary, target, digest(b'new'))
        self.assertEqual(target.read_bytes(), b'old')
        self.assertEqual(temporary.read_bytes(), b'new')

    def test_pending_preserves_later_player_edit_and_ignores_other_pack(self):
        previous = row('config/a.txt', b'v1a')
        write(self.root, 'config/a.txt', b'user')
        pending = dict(format=1, packId='test', version='v2', applied={
            'config/a.txt': dict(path='config/a.txt', sha1=digest(b'v2a'))})
        self.u.atomic_json(self.root / '.portable-sync-pending.json', pending)
        journal = self.u.RecoveryJournal(self.root, self.manifest([row('config/a.txt', b'v3a')]))
        self.assertEqual(journal.previous_files([previous]), [previous])
        write(self.root, 'config/a.txt', b'v2a')
        foreign = self.manifest([row('config/a.txt', b'v3a')])
        foreign['packId'] = 'another-pack'
        journal = self.u.RecoveryJournal(self.root, foreign)
        self.assertEqual(journal.previous_files([previous]), [previous])

    def test_verified_stage_cache_is_used_after_preservation_rules(self):
        write(self.root, 'config/custom.txt', b'USER')
        rows = [row('mods/test.jar', b'new'), row('config/custom.txt', b'pack')]
        write(self.root, '.portable-staging/mods/test.jar', b'new')
        write(self.root, '.portable-staging/config/custom.txt', b'pack')
        code, calls = self.run_sync(self.manifest(rows, preserveLocalChangeGlobs=['config/**']))
        self.assertEqual(code, 0)
        self.assertEqual(calls, [])
        self.assertEqual((self.root / 'mods/test.jar').read_bytes(), b'new')
        self.assertEqual((self.root / 'config/custom.txt').read_bytes(), b'USER')
        self.assertTrue((self.root / '.portable-staging/mods/test.jar').exists())

    def test_wrong_stage_cache_is_rejected_and_exact_current_payload_downloaded(self):
        write(self.root, '.portable-staging/mods/test.jar', b'old')
        code, calls = self.run_sync(self.manifest([row('mods/test.jar', b'new')]), {'mods/test.jar': b'new'})
        self.assertEqual(code, 0)
        self.assertEqual(calls, ['mods/test.jar'])
        self.assertEqual((self.root / 'mods/test.jar').read_bytes(), b'new')

    def test_cleanup_exact_paths_do_not_walk_and_wildcards_share_pruned_walk(self):
        write(self.root, 'mods/old.jar', b'old')
        write(self.root, '.portable-sync-backups/run/mods/old.jar', b'backup')
        with mock.patch.object(self.u.os, 'walk', side_effect=AssertionError('unexpected walk')):
            self.assertEqual(self.u.cleanup_candidates(self.root, ['mods/old.jar']), [self.root / 'mods/old.jar'])
        walk = self.u.os.walk
        with mock.patch.object(self.u.os, 'walk', wraps=walk) as traced:
            found = self.u.cleanup_candidates(self.root, ['mods/*.jar', '**/old.jar'])
        self.assertEqual(traced.call_count, 1)
        self.assertEqual(found, [self.root / 'mods/old.jar'])

    def test_resume_appends_valid_206_and_ignored_range_200_restarts(self):
        content = b'abcdef'
        dest = self.root / 'payload.jar'
        for status in (206, 200):
            with self.subTest(status=status):
                temp = self.u.part_path(dest, digest(content))
                temp.write_bytes(b'abc')
                opener = mock.Mock()
                headers = {'Content-Range': 'bytes 3-5/6', 'Content-Length': '3'}
                opener.open.return_value = Response(b'def', 206, headers) if status == 206 else Response(content)
                self.u.download_file('http://example.invalid/private/file', dest, digest(content), opener=opener, retries=0)
                self.assertEqual(opener.open.call_args.args[0].get_header('Range'), 'bytes=3-')
                self.assertEqual(dest.read_bytes(), content)
                self.assertFalse(temp.exists())

    def test_416_or_invalid_range_restarts_once_and_never_installs_bad_content(self):
        content = b'abcdef'
        dest = self.root / 'payload.jar'
        for first in (urllib.error.HTTPError('http://private/', 416, 'range', {}, io.BytesIO()),
                      Response(b'def', 206, {'Content-Range': 'bytes 1-3/6', 'Content-Length': '3'})):
            with self.subTest(first=type(first).__name__):
                self.u.part_path(dest, digest(content)).write_bytes(b'abc')
                opener = mock.Mock()
                opener.open.side_effect = [first, Response(content)]
                self.u.download_file('http://example.invalid/private/file', dest, digest(content), opener=opener, retries=0)
                self.assertEqual(dest.read_bytes(), content)
                self.assertIsNone(opener.open.call_args_list[1].args[0].get_header('Range'))

    def test_interrupted_body_keeps_partial_and_next_run_resumes(self):
        content = b'abcdef'
        dest = self.root / 'payload.jar'
        class BrokenResponse(Response):
            def read(self, size=-1):
                if self.tell():
                    raise ConnectionError('private diagnostic')
                return super().read(3)
        opener = mock.Mock()
        opener.open.return_value = BrokenResponse(content)
        with self.assertRaisesRegex(self.u.UpdateError, '下载失败'):
            self.u.download_file('http://example.invalid/private/file', dest, digest(content), opener=opener, retries=0)
        temp = self.u.part_path(dest, digest(content))
        self.assertEqual(temp.read_bytes(), b'abc')
        self.assertFalse(dest.exists())
        opener.open.return_value = Response(b'def', 206, {'Content-Range': 'bytes 3-5/6', 'Content-Length': '3'})
        self.u.download_file('http://example.invalid/private/file', dest, digest(content), opener=opener, retries=0)
        self.assertEqual(dest.read_bytes(), content)

    def test_new_expected_hash_cleans_only_own_obsolete_partial(self):
        destination = self.root / 'payload.jar'
        obsolete = self.u.part_path(destination, digest(b'old'))
        other_target = self.u.part_path(self.root / 'other.jar', digest(b'old'))
        obsolete.write_bytes(b'partial')
        other_target.write_bytes(b'other partial')
        opener = mock.Mock()
        opener.open.return_value = Response(b'new')
        self.u.download_file('http://example.invalid/payload', destination, digest(b'new'), opener=opener, retries=0)
        self.assertFalse(obsolete.exists())
        self.assertEqual(other_target.read_bytes(), b'other partial')
        self.assertEqual(destination.read_bytes(), b'new')

    def test_partial_names_do_not_collide_for_same_hash_twins(self):
        sha = digest(b'twins')
        self.assertNotEqual(self.u.part_path(self.root / 'a.txt', sha), self.u.part_path(self.root / 'b.txt', sha))


if __name__ == '__main__':
    unittest.main(verbosity=2)
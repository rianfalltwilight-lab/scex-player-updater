"""Updater 2.27.22 optimizations: batched recovery journal, official-source circuit breaker,
single-backup retention, stage-cache cleanup, size-filtered adoption, single hashing,
daemon reuse of verified records and of the probe download, local-first self-refresh.
Synthetic files and localhost servers only."""
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
import time
import unittest
from unittest import mock

from test_updater import PS, ROOT, json_write, write
from test_sync_recovery_powershell import Web, manifest as ps_manifest, row as ps_row

TOOLS = ROOT / 'tools'
NOTICE = '更新已就绪.txt'


def digest(data):
    return hashlib.sha1(data).hexdigest()


def row(path, data):
    return {'path': path, 'sha1': digest(data), 'size': len(data)}


def load(name):
    spec = importlib.util.spec_from_file_location('scex_opt_' + name.replace('-', '_').replace('.', '_'), TOOLS / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Response(io.BytesIO):
    def __init__(self, content):
        super().__init__(content)
        self.status = 200
        self.headers = {'Content-Length': str(len(content))}


class PythonUpdaterTests(unittest.TestCase):
    def setUp(self):
        self.u = load('player-update-generic.py')
        self.directory = tempfile.TemporaryDirectory(prefix='scex-opt-')
        self.root = Path(self.directory.name).resolve()

    def tearDown(self):
        self.directory.cleanup()

    def manifest(self, rows, version='v2', **settings):
        return dict(packId='test', version=version, files=rows, downloadWorkers=4,
                    adoptExistingFiles=False, cleanup=dict(removeConnectorCache=False,
                    disableLauncherRepairIndex=False, disableDuplicateMods=False),
                    serverList=dict(enabled=False), **settings)

    def run_sync(self, manifest, payloads):
        def fetch(request, **kwargs):
            value = payloads.get(request.full_url.split('/secret/', 1)[-1], TimeoutError('offline'))
            if isinstance(value, BaseException):
                raise value
            return Response(value)
        opener = mock.Mock()
        opener.open.side_effect = fetch
        with mock.patch.object(sys, 'argv', ['updater', '--instance-dir', str(self.root),
                     '--manifest-url', 'http://127.0.0.1/secret/server-manifest.json']), \
             mock.patch.object(self.u, 'fetch_manifest', return_value=json.dumps(manifest).encode()), \
             mock.patch.object(self.u, '_DIRECT_OPENER', opener), \
             mock.patch.object(self.u.time, 'sleep'), contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()):
            return self.u.main()

    def test_journal_is_written_once_for_a_large_update(self):
        rows = [row(f'mods/m{i}.jar', b'payload %d' % i) for i in range(60)]
        payloads = {r['path']: b'payload %d' % i for i, r in enumerate(rows)}
        writes = []
        original = self.u.atomic_json
        def traced(path, value):
            writes.append(Path(path).name)
            return original(path, value)
        with mock.patch.object(self.u, 'atomic_json', side_effect=traced):
            self.assertEqual(self.run_sync(self.manifest(rows), payloads), 0)
        self.assertEqual(writes.count('.portable-sync-pending.json'), 1)
        self.assertFalse((self.root / '.portable-sync-pending.json').exists())

    def test_breaker_skips_a_host_after_three_transport_failures(self):
        u = self.u
        u.reset_download_policy()
        calls = []
        def fake_download(url, dest, expected, **kwargs):
            calls.append(url)
            if 'cdn.example' in url:
                raise u.UpdateError('[下载失败] x：网络连接或读取超时。')
        items = [{'url': f'https://cdn.example/f{i}.jar', 'size': 1} for i in range(5)]
        with mock.patch.object(u, 'download_file', side_effect=fake_download), \
             mock.patch.object(u, 'get_proxy_url', return_value=''), contextlib.redirect_stdout(io.StringIO()):
            for i, item in enumerate(items):
                self.assertEqual(u.download_manifest_entry(item, 'http://home/', self.root / f'f{i}', '0' * 40, f'mods/f{i}.jar'), 'home')
        official = [c for c in calls if 'cdn.example' in c]
        self.assertEqual(len(official), 3)
        self.assertTrue(u.official_host_tripped('cdn.example'))

    def test_breaker_ignores_file_specific_errors_and_success_resets(self):
        u = self.u
        u.reset_download_policy()
        for _ in range(5):
            error = u.UpdateError('[下载失败] x：更新源没有此文件（HTTP 404）。')
            error.http_status = 404
            self.assertFalse(u.counts_against_source(error))
            u.record_official_result('cdn.example', False, u.counts_against_source(error))
        self.assertFalse(u.official_host_tripped('cdn.example'))
        self.assertFalse(u.counts_against_source(u.file_specific_error('[校验失败] x')))
        busy = u.UpdateError('busy')
        busy.http_status = 503
        self.assertTrue(u.counts_against_source(busy))
        for ok in (False, False, True, False, False):
            u.record_official_result('cdn.example', ok)
        self.assertFalse(u.official_host_tripped('cdn.example'))
        with contextlib.redirect_stdout(io.StringIO()):
            u.record_official_result('cdn.example', False)
        self.assertTrue(u.official_host_tripped('cdn.example'))
        u.reset_download_policy()
        self.assertFalse(u.official_host_tripped('cdn.example'))

    def test_only_latest_backup_is_kept_and_survives_a_no_change_run(self):
        write(self.root, '.portable-sync-backups/20260101-000000/mods/older.jar', b'old style')
        write(self.root, '.portable-sync-backups/keep-me/note.txt', b'not a backup stamp')
        write(self.root, 'mods/a.jar', b'v1')
        json_write(self.root / '.portable-sync-state.json', dict(format=3, packId='test', version='v1', files=[row('mods/a.jar', b'v1')]))
        self.assertEqual(self.run_sync(self.manifest([row('mods/a.jar', b'v2')]), {'mods/a.jar': b'v2'}), 0)
        first = [p.name for p in (self.root / '.portable-sync-backups').iterdir() if p.name != 'keep-me']
        self.assertEqual(len(first), 1)
        self.assertEqual(self.run_sync(self.manifest([row('mods/a.jar', b'v3')], version='v3'), {'mods/a.jar': b'v3'}), 0)
        second = [p for p in (self.root / '.portable-sync-backups').iterdir() if p.name != 'keep-me']
        self.assertEqual(len(second), 1)
        self.assertNotEqual(second[0].name, first[0])
        self.assertEqual((second[0] / 'mods/a.jar').read_bytes(), b'v2')
        self.assertEqual(self.run_sync(self.manifest([row('mods/a.jar', b'v3')], version='v3'), {}), 0)
        self.assertTrue((second[0] / 'mods/a.jar').exists())
        self.assertTrue((self.root / '.portable-sync-backups/keep-me/note.txt').exists())

    def test_stage_cache_and_notice_cleared_only_after_success(self):
        for rel, value in [('.portable-staging/mods/a.jar', b'new'), ('.portable-staging/mods/withdrawn.jar', b'old'),
                           ('.portable-staging/READY.json', b'{}'), ('.portable-staging/daemon.log', b'log'), (NOTICE, b'ready')]:
            write(self.root, rel, value)
        failing = self.manifest([row('mods/a.jar', b'new'), row('mods/b.jar', b'bbb')])
        with self.assertRaises(self.u.UpdateError):
            self.run_sync(failing, {})
        self.assertTrue((self.root / '.portable-staging/mods/withdrawn.jar').exists())
        self.assertTrue((self.root / NOTICE).exists())
        self.assertEqual(self.run_sync(failing, {'mods/b.jar': b'bbb'}), 0)
        self.assertEqual((self.root / 'mods/a.jar').read_bytes(), b'new')
        stage = self.root / '.portable-staging'
        self.assertEqual(sorted(p.relative_to(stage).as_posix() for p in stage.rglob('*')), ['daemon.log'])
        self.assertFalse((self.root / NOTICE).exists())

    def test_adoption_index_hashes_only_same_size_files(self):
        write(self.root, 'mods/same.jar', b'1234')
        write(self.root, 'mods/other.jar', b'0123456789')
        hashed = []
        original = self.u.sha1
        with mock.patch.object(self.u, 'sha1', side_effect=lambda p: hashed.append(Path(p).name) or original(p)):
            index = self.u.existing_hash_index(self.root, {4})
        self.assertEqual(hashed, ['same.jar'])
        self.assertEqual(list(index), [digest(b'1234')])
        self.assertEqual(len(self.u.existing_hash_index(self.root)), 2)

    def test_changed_file_is_hashed_once_and_rounded_mtime_is_trusted(self):
        target = write(self.root, 'config/a.txt', b'user')
        path = self.root / 'config/a.txt'
        hashed = []
        original = self.u.sha1
        with mock.patch.object(self.u, 'sha1', side_effect=lambda p: hashed.append(p) or original(p)):
            current, value = self.u.current_file_state(path, digest(b'pack'), 4, '', {})
        self.assertEqual((current, value, len(hashed)), (False, digest(b'user'), 1))
        st = path.stat()
        os.utime(path, (st.st_atime, int(st.st_mtime) + 0.75))
        record = {'sha1': target['sha1'], 'size': 4, 'mtime': int(st.st_mtime) + 1}
        with mock.patch.object(self.u, 'sha1', side_effect=AssertionError('hashed despite record')):
            self.assertEqual(self.u.current_file_state(path, target['sha1'], 4, '', record), (True, target['sha1']))


class PythonDaemonTests(unittest.TestCase):
    def test_first_round_reuses_probe_text_and_verified_records(self):
        stage = load('portable-stage-daemon.py')
        with tempfile.TemporaryDirectory(prefix='scex-daemon-') as tmp:
            root = Path(tmp).resolve()
            cache = root / '.portable-staging'
            cache.mkdir()
            # Content differs from the record on purpose: trusting the record means no hash and no staging.
            write(root, 'mods/a.jar', b'xxxx')
            st = (root / 'mods/a.jar').stat()
            expected = row('mods/a.jar', b'good')
            json_write(root / '.portable-sync-state.json', dict(format=3, packId='t', version='v', files=[expected],
                       verified={'mods/a.jar': dict(sha1=expected['sha1'], size=4, mtime=int(st.st_mtime))}))
            text = json.dumps(dict(packId='t', version='v', files=[expected]))
            with mock.patch.object(stage.time, 'sleep', side_effect=[None, KeyboardInterrupt]), \
                 mock.patch.object(stage.Manifest, 'if_changed', side_effect=AssertionError('manifest downloaded twice')), \
                 mock.patch.object(stage, 'sha1', side_effect=AssertionError('verified file re-hashed')), \
                 mock.patch.object(stage, 'download_staged_entry', side_effect=AssertionError('unexpected staging')), \
                 mock.patch.object(stage, 'get_proxy_url', return_value=''), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(KeyboardInterrupt):
                    stage.run_watch(root, cache, cache / 'READY.json', root / 'notice', cache / 'daemon.lock',
                                    cache / 'daemon.log', 'http://127.0.0.1/m.json', 0, 10, 10, 3,
                                    initial_text=text, initial_last_modified='Sun, 25 Oct 2026 08:00:00 GMT')
            log = (cache / 'daemon.log').read_text(encoding='utf-8')
            self.assertIn('检测到新清单', log)
            self.assertIn('无需预下载', log)
            self.assertNotIn('本轮异常', log)
            self.assertNotIn('清单获取失败', log)

    def test_round_breaker_skips_host_after_three_failures(self):
        stage = load('portable-stage-daemon.py')
        calls = []
        def fake(url, dest, expected, **kwargs):
            calls.append(url)
            if 'cdn.example' in url:
                raise TimeoutError('timed out')
        with tempfile.TemporaryDirectory(prefix='scex-daemon-breaker-') as tmp:
            log = Path(tmp) / 'daemon.log'
            breaker = stage.OfficialBreaker()
            with mock.patch.object(stage, 'download_verify', side_effect=fake), contextlib.redirect_stdout(io.StringIO()):
                for i in range(5):
                    item = {'url': f'https://cdn.example/f{i}.jar'}
                    self.assertEqual(stage.download_staged_entry(item, 'http://home/', Path(tmp) / f'f{i}', '0' * 40, f'mods/f{i}.jar', log, breaker), 'home')
            self.assertEqual(len([c for c in calls if 'cdn.example' in c]), 3)
            self.assertIn('连续 3 个文件下载失败', log.read_text(encoding='utf-8'))


class MacSelfRefreshTests(unittest.TestCase):
    def test_matching_local_helper_is_not_downloaded(self):
        source = (TOOLS / 'portable-macos-sync.command').read_text('utf-8').split("<<'PY'\n", 1)[1].split('\nPY\n', 1)[0]
        with tempfile.TemporaryDirectory(prefix='scex-mac-refresh-') as tmp:
            client = Path(tmp) / 'client'
            tools = client / '_updater'
            tools.mkdir(parents=True)
            helper = b'print("current helper")\n'
            write(tools, 'player-update-generic.py', helper)
            entry = tools / 'macOS-sync.command'
            entry.write_bytes((TOOLS / 'portable-macos-sync.command').read_bytes())
            with Web() as web:
                web.routes = {'/m.json': {'files': [row('_updater/player-update-generic.py', helper)]}}
                (client / 'UPDATE-URL.txt').write_text(web.url('/m.json') + '\n')
                result = subprocess.run([sys.executable, '-', '', str(tools), str(entry)], input=source.encode(),
                                        env=dict(os.environ, PYTHONIOENCODING='utf-8'), capture_output=True, timeout=30)
                requested = [path for path, _ in web.requests]
            output = (result.stdout + result.stderr).decode('utf-8', errors='replace')
            self.assertEqual(result.returncode, 0, output)
            self.assertEqual(requested, ['/m.json'], output)
            self.assertIn('已是最新 player-update-generic.py', output)


@unittest.skipUnless(PS and os.name == 'nt', 'Windows PowerShell required')
class PowerShellTests(unittest.TestCase):
    SOURCE = TOOLS / 'player-update-generic.ps1'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='scex-opt-ps-')
        self.root = Path(self.temp.name) / 'client'
        self.root.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def sync(self, web):
        result = subprocess.run([PS, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(self.SOURCE),
                                 '-InstanceDir', str(self.root), '-ManifestUrl', web.url(), '-NoPause'],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
        output = result.stdout.decode('utf-8-sig', errors='replace')
        self.assertEqual(result.returncode, 0, output)
        return output

    def functions(self, body):
        script = Path(self.temp.name) / 'functions.ps1'
        source = str(self.SOURCE).replace("'", "''")
        script.write_text("$ErrorActionPreference='Stop'\n" +
            f"$a=[Management.Automation.Language.Parser]::ParseFile('{source}',[ref]$null,[ref]$null)\n" +
            "$a.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst]},$false) | ForEach-Object { . ([scriptblock]::Create($_.Extent.Text)) }\n" +
            body, encoding='utf-8-sig')
        result = subprocess.run([PS, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(script)],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
        output = result.stdout.decode('utf-8-sig', errors='replace')
        self.assertEqual(result.returncode, 0, output)
        return output

    def test_backup_retention_and_stage_cleanup(self):
        write(self.root, '.portable-sync-backups/20260101-000000-000-abcdef12/mods/older.jar', b'old')
        write(self.root, 'mods/a.jar', b'v1')
        json_write(self.root / '.portable-sync-state.json', dict(format=3, packId='recovery-test', version='v1', files=[ps_row('mods/a.jar', b'v1')]))
        for rel, value in [('.portable-staging/mods/withdrawn.jar', b'old'), ('.portable-staging/READY.json', b'{}'),
                           ('.portable-staging/daemon.log', b'log'), (NOTICE, b'ready')]:
            write(self.root, rel, value)
        with Web() as web:
            web.routes = {'/server-manifest.json': ps_manifest([ps_row('mods/a.jar', b'v2')]), '/mods/a.jar': b'v2'}
            self.sync(web)
            backups = list((self.root / '.portable-sync-backups').iterdir())
            self.assertEqual(len(backups), 1)
            self.assertEqual((backups[0] / 'mods/a.jar').read_bytes(), b'v1')
            stage = self.root / '.portable-staging'
            self.assertEqual([p.name for p in stage.rglob('*')], ['daemon.log'])
            self.assertFalse((self.root / NOTICE).exists())
            self.sync(web)  # no change, no new backup: the previous one survives
            self.assertEqual(list((self.root / '.portable-sync-backups').iterdir()), backups)

    def test_breaker_hash_reuse_and_size_filter(self):
        mods = self.root / 'mods'
        write(self.root, 'mods/same.jar', b'1234')
        write(self.root, 'mods/other.jar', b'0123456789')
        user = self.root / 'mods/same.jar'
        output = self.functions(f"""
$script:SyncRoot = '{self.root}'
$script:OfficialFailureLimit = 3
$script:OfficialBreaker = [hashtable]::Synchronized(@{{}})
Register-OfficialResult -HostName 'cdn.example' -Ok $false
Register-OfficialResult -HostName 'cdn.example' -Ok $false
Register-OfficialResult -HostName 'cdn.example' -Ok $true
Register-OfficialResult -HostName 'cdn.example' -Ok $false
Register-OfficialResult -HostName 'cdn.example' -Ok $false -Counted $false
if (Test-OfficialHostTripped 'cdn.example') {{ throw 'tripped too early' }}
Register-OfficialResult -HostName 'cdn.example' -Ok $false
Register-OfficialResult -HostName 'cdn.example' -Ok $false
if (-not (Test-OfficialHostTripped 'cdn.example')) {{ throw 'not tripped' }}
if (Test-CountsAgainstSource ([Exception]::new('SHA1 mismatch for x'))) {{ throw 'hash counted' }}
if (Test-CountsAgainstSource ([Exception]::new('HTTP 404 from official proxy download'))) {{ throw '404 counted' }}
if (-not (Test-CountsAgainstSource ([Exception]::new('HTTP 503 from official proxy download')))) {{ throw '503 ignored' }}
if (-not (Test-CountsAgainstSource ([Net.WebException]::new('timed out', [Net.WebExceptionStatus]::Timeout)))) {{ throw 'timeout ignored' }}
$computed = $null
$same = Test-LooksLikeCurrentFile -Path '{user}' -Expected '{digest(b"pack")}' -ManifestSize 4 -PreviousHash '' -Verified $null -HashOut ([ref]$computed)
if ($same -or $computed -ne '{digest(b"1234")}') {{ throw "hash not returned: $computed" }}
[IO.File]::SetLastWriteTimeUtc('{user}', [DateTime]::new(2026, 1, 1, 0, 0, 0, 600, [DateTimeKind]::Utc))
$rounded = [DateTimeOffset]::new(2026, 1, 1, 0, 0, 1, [TimeSpan]::Zero).ToUnixTimeSeconds()
$record = @{{ sha1 = '{digest(b"pack")}'; size = 4; mtime = $rounded }}
if (-not (Test-LooksLikeCurrentFile -Path '{user}' -Expected '{digest(b"pack")}' -ManifestSize 4 -Verified $record)) {{ throw 'rounded mtime rejected' }}
$legacy = [int64][Math]::Round([IO.File]::GetLastWriteTimeUtc('{user}').Subtract([datetime]'1970-01-01Z').TotalSeconds)
$record.mtime = $legacy
if (-not (Test-LooksLikeCurrentFile -Path '{user}' -Expected '{digest(b"pack")}' -ManifestSize 4 -Verified $record)) {{ throw 'legacy PowerShell mtime rejected' }}
Write-Output ('MTIME=' + (Get-FileVerifyRecord -Path '{user}' -Sha1 '{digest(b"1234")}').mtime)
$sizes = New-Object 'System.Collections.Generic.HashSet[long]'
[void]$sizes.Add(4)
$index = Get-ExistingFileHashIndex -Root '{self.root}' -Sizes $sizes
if ($index.Count -ne 1 -or -not $index.ContainsKey('{digest(b"1234")}')) {{ throw ('size filter failed: ' + ($index.Keys -join ',')) }}
$all = Get-ExistingFileHashIndex -Root '{self.root}'
if ($all.Count -ne 2) {{ throw 'unfiltered index incomplete' }}
Write-Output 'PS-OK'
""")
        self.assertIn('PS-OK', output)
        # MTIME_RECORD_V1: a PowerShell record equals the Python updater's int(st_mtime).
        self.assertIn(f'MTIME={int(os.stat(user).st_mtime)}', output)

    def test_bootstrap_does_not_download_matching_helpers(self):
        client = self.root
        tools = client / '_updater'
        tools.mkdir()
        helper = b'# current helper\n'
        write(tools, 'player-update-generic.py', helper)
        with Web() as web:
            web.routes = {'/m.json': {'files': [ps_row('_updater/player-update-generic.py', helper)]}}
            (client / 'UPDATE-URL.txt').write_text(web.url('/m.json') + '\n')
            result = subprocess.run([PS, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(TOOLS / 'portable-bootstrap-refresh.ps1'),
                                     '-InstanceDir', str(client), '-ScriptDir', str(tools)], capture_output=True, timeout=30)
            requested = [path for path, _ in web.requests]
        output = result.stdout.decode('utf-8-sig', errors='replace')
        self.assertEqual(result.returncode, 0, output)
        self.assertEqual(requested, ['/m.json'], output)
        self.assertIn('已是最新 player-update-generic.py', output)


if __name__ == '__main__':
    unittest.main()

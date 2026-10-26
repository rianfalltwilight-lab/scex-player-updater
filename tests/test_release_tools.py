"""Admin tools: the access-log report (update-adoption.py), the version bump script and the
release tool against a local fake host (ssh/scp replaced by local shims; no network)."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / 'tools'
PUBLIC = 'modpack-public/portable/'
ENV = dict(os.environ, PYTHONIOENCODING='utf-8')
VERSIONED = ['player-self-repair.ps1', 'player-update-generic.ps1', 'player-update-generic.py', 'portable-bootstrap-refresh.ps1',
             'portable-macos-sync.command', 'portable-stage-daemon.ps1', 'portable-stage-daemon.py', 'portable-windows-repair.ps1']


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, TOOLS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha1(data):
    return hashlib.sha1(data).hexdigest()


def sha256(data):
    return hashlib.sha256(data).hexdigest()


class AdoptionReportTests(unittest.TestCase):
    def test_visitors_release_checks_and_attention(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp) / 'logs'
            logs.mkdir()
            lines = ['2026-10-26T10:00:00\t200\tv1\tSCEX-updater/2.27.24 (python 3.12; win32)',
                     '2026-10-26T10:01:00\t200\tv2\tSCEX-updater/2.27.28 (bootstrap 5.1; windows)',
                     '2026-10-26T10:01:05\t304\tv2\tSCEX-updater/2.27.28 (powershell 5.1; windows)',
                     '2026-10-26T10:02:00\t403\tv3\tSCEX-updater/2.27.28 (python 3.9; darwin)',
                     '2026-10-26T10:03:00\t200\tv4\tSCEX-release-verify/2.27.28',
                     '2026-10-26T10:03:01\t200\tv4\tSCEX-updater/2.27.27 (bootstrap 5.1; windows)']
            (logs / 'update-access-20261026.log').write_text('\n'.join(lines) + '\n', encoding='utf-8')
            public = Path(tmp) / 'public'
            (public / '_updater').mkdir(parents=True)
            (public / '_updater' / 'player-update-generic.py').write_text('x = 1\nUPDATER_VERSION = "2.27.28"\n', encoding='utf-8')
            (public / 'server-manifest.json').write_text(json.dumps({'version': '2.27.29', 'files': []}), encoding='utf-8')
            result = subprocess.run([sys.executable, str(TOOLS / 'update-adoption.py'), '--folder', str(logs), '--public', str(public)],
                                    capture_output=True, timeout=30, env=ENV)
            out = result.stdout.decode('utf-8')
            self.assertEqual(result.returncode, 0, out)
            self.assertIn('当前更新器版本：2.27.28（整合包清单版本 2.27.29）', out)
            self.assertIn('3 位玩家取过清单，2 位已用更新器 2.27.28；另有 1 个发布验收地址（不计入）', out)
            self.assertIn('v1  10:00:00  2.27.24 python  HTTP 200', out)
            self.assertIn('v2  10:01:05  2.27.28 powershell  HTTP 304；当天还用过 2.27.28 bootstrap', out)
            self.assertNotIn('  v4  ', out)
            attention = out.split('需要关注：', 1)[1]
            self.assertIn('访客 v1：最后用的是 2.27.24（python）', attention)
            self.assertIn('.portable-sync-state.json', attention)
            self.assertIn('访客 v3：最后一次取清单返回 HTTP 403', attention)
            self.assertNotIn('v2', attention)


class VersionBumpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.tools = Path(self.temp.name) / 'tools'
        self.tools.mkdir()
        for name in VERSIONED:
            shutil.copyfile(TOOLS / name, self.tools / name)
        self.bump = load('bump_updater_version', 'bump-updater-version.py')

    def tearDown(self):
        self.temp.cleanup()

    def test_every_copy_changes_and_keeps_its_encoding(self):
        found = self.bump.copies(TOOLS)
        self.assertEqual(sorted(p.name for p in found), VERSIONED)
        before = {name: (self.tools / name).read_bytes() for name in VERSIONED}
        old, changed = self.bump.bump(self.tools, '99.0.0', mac_note='测试说明', out=lambda _line: None)
        self.assertEqual(sorted(p.name for p in changed), VERSIONED)
        for name in VERSIONED:
            after = (self.tools / name).read_bytes()
            self.assertEqual(after.startswith(b'\xef\xbb\xbf'), before[name].startswith(b'\xef\xbb\xbf'), name)
            self.assertEqual(b'\r\n' in after, b'\r\n' in before[name], name)
            restored = after.replace(b'99.0.0', old.encode())
            if name.endswith('.command'):
                text = after.decode('utf-8')
                number = int(before[name].decode('utf-8').split('# @version ', 1)[1].split('\n', 1)[0])
                self.assertIn(f'# @version {number + 1}\n', text)
                self.assertTrue(text.endswith(f'# v{number + 1}：测试说明\n'), text[-80:])
                restored = restored.replace(f'# @version {number + 1}'.encode(), f'# @version {number}'.encode())
                restored = restored[:restored.rindex(f'# v{number + 1}：'.encode())]
                self.assertEqual(restored, before[name].rstrip(b'\n') + b'\n')
            else:
                self.assertEqual(restored, before[name], name)
        check = subprocess.run([sys.executable, str(TOOLS / 'bump-updater-version.py'), '--check', '--tools', str(self.tools)],
                               capture_output=True, timeout=30, env=ENV)
        self.assertEqual(check.returncode, 0)
        self.assertIn('8 个文件一致：99.0.0', check.stdout.decode('utf-8'))

    def test_refuses_lower_versions_and_mismatched_copies(self):
        with self.assertRaises(SystemExit):
            self.bump.bump(self.tools, '0.0.1', out=lambda _line: None)
        with self.assertRaises(SystemExit):
            self.bump.bump(self.tools, '99.0', out=lambda _line: None)
        path = self.tools / 'player-update-generic.py'
        text = path.read_text(encoding='utf-8')
        old = self.bump.copies(self.tools)[path][2][0]
        path.write_text(text.replace(f'UPDATER_VERSION = "{old}"', 'UPDATER_VERSION = "0.0.9"'), encoding='utf-8')
        with self.assertRaises(SystemExit):
            self.bump.bump(self.tools, '99.0.0', out=lambda _line: None)
        check = subprocess.run([sys.executable, str(TOOLS / 'bump-updater-version.py'), '--check', '--tools', str(self.tools)],
                               capture_output=True, timeout=30, env=ENV)
        self.assertEqual(check.returncode, 1)


class CiVerdictTests(unittest.TestCase):
    def setUp(self):
        self.tool = load('release_updater_ci', 'release-updater.py')

    def run_of(self, run_id, status='completed', conclusion='success', attempt=1):
        return {'id': run_id, 'workflow_id': 7, 'name': 'Updater regression', 'status': status, 'conclusion': conclusion,
                'run_number': 3, 'run_attempt': attempt}

    def test_verdicts(self):
        six = lambda _run_id: [{'conclusion': 'success'}] * 6
        verdict = self.tool.ci_verdict([self.run_of(1)], six)
        self.assertEqual((verdict['ok'], verdict['reason']), (True, '6/6 jobs passed'))
        # A successful re-run replaces the failed first attempt.
        self.assertTrue(self.tool.ci_verdict([self.run_of(1, conclusion='failure'), self.run_of(2, attempt=2)], six)['ok'])
        self.assertIn('still running', self.tool.ci_verdict([self.run_of(1, status='in_progress', conclusion=None)], six)['reason'])
        self.assertFalse(self.tool.ci_verdict([self.run_of(1, conclusion='failure')], six)['ok'])
        five = lambda _run_id: [{'conclusion': 'success'}] * 5 + [{'conclusion': 'skipped'}]
        self.assertFalse(self.tool.ci_verdict([self.run_of(1)], five)['ok'])
        self.assertIn('pushed', self.tool.ci_verdict([], six)['reason'])

    def test_api_failure_blocks(self):
        release = self.tool.Release.__new__(self.tool.Release)
        release.commit = 'a' * 40

        def get(_path):
            raise urllib.error.URLError('offline')
        verdict = release.check_ci(get)
        self.assertFalse(verdict['ok'])
        self.assertIn('unavailable', verdict['reason'])


SSH_SHIM = '''import os, shlex, subprocess, sys
env = dict(os.environ, PYTHONIOENCODING="utf-8")  # a real posix host has a UTF-8 locale
sys.exit(subprocess.run(shlex.split(sys.argv[-1]), env=env).returncode)
'''
SCP_SHIM = '''import os, shutil, sys
args = [a for a in sys.argv[1:] if not a.startswith("-")]
local = [a.split(":", 1)[1] if a.startswith("fake:") else a for a in args[-2:]]
(shutil.copytree if os.path.isdir(local[0]) else shutil.copyfile)(local[0], local[1])
'''


def git(repo, *args):
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=test', '-c', 'user.email=test@example.invalid',
                    '-c', 'core.autocrlf=false', '-c', 'commit.gpgsign=false', *args], check=True, capture_output=True)


def commit(repo, message):
    git(repo, 'add', '-A')
    git(repo, 'commit', '-q', '-m', message)
    return subprocess.run(['git', '-C', str(repo), 'rev-parse', 'HEAD'], check=True, capture_output=True, text=True).stdout.strip()


class FakeHostReleaseTests(unittest.TestCase):
    """Two commits in a scratch repository, one posix host that is a local folder."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='release-tool-')
        base = Path(self.temp.name)
        self.tool = load('release_updater_fake', 'release-updater.py')
        repo = base / 'repo'
        (repo / 'tools').mkdir(parents=True)
        git(repo, 'init', '-q')
        publish = ("Copy-ToolFile -SourceName 'player-update-generic.py' -DestinationName 'player-update-generic.py' -DestinationRoot $updaterRoot\n"
                   "Copy-ToolFile -SourceName 'portable-windows-sync.bat' -DestinationName '更新mod-Windows端.bat' -DestinationRoot $publishDir\n")
        files = {'tools/portable-publish.ps1': publish.encode('utf-8'),
                 'tools/player-update-generic.py': b'UPDATER_VERSION = "1.0.1"\nprint("old engine")\n',
                 'tools/portable-windows-sync.bat': b'@echo off\r\necho entry\r\n',
                 'tools/update-adoption.py': (TOOLS / 'update-adoption.py').read_bytes()}
        for rel, data in files.items():
            (repo / rel).write_bytes(data)
        self.commit_a = commit(repo, 'a')
        self.engine_b = b'UPDATER_VERSION = "1.0.2"\nprint("new engine")\n'
        (repo / 'tools/player-update-generic.py').write_bytes(self.engine_b)
        (repo / 'tools/new-tool.py').write_bytes(b'print("new admin tool")\n')
        self.commit_b = commit(repo, 'b')
        self.tool.REPO = repo

        host = base / 'host'
        self.host = host
        self.guide = '使用说明\n'.encode('utf-8')
        public = {'_updater/player-update-generic.py': files['tools/player-update-generic.py'],
                  '更新mod-Windows端.bat': files['tools/portable-windows-sync.bat'], '使用说明.txt': self.guide,
                  'mods/a.jar': b'jar'}
        rows = [{'path': p, 'sha1': sha1(d), 'size': len(d)} for p, d in public.items()]
        # A content release (1.0.1) came after the previous updater release (1.0.0).
        manifest = {'version': '1.0.1', 'releaseNotes': ['内容更新说明'], 'files': rows}
        layout = {'tools/' + n: files['tools/' + n] for n in ('player-update-generic.py', 'portable-windows-sync.bat', 'portable-publish.ps1')}
        layout.update({PUBLIC + p: d for p, d in public.items()})
        layout['tools/portable-pack.json'] = json.dumps({'version': '1.0.1', 'packName': 'T', 'releaseModChanges': ['~ 模组']}).encode()
        layout['tools/old-helper.ps1'] = b'# retired\r\n'
        layout[PUBLIC + 'update-log.txt'] = '> v1.0.0\n- 去掉旧云端分发\n'.encode('utf-8')
        layout[PUBLIC + 'server-manifest.json'] = json.dumps(manifest, ensure_ascii=False).encode('utf-8')
        for rel, data in layout.items():
            (host / rel).parent.mkdir(parents=True, exist_ok=True)
            (host / rel).write_bytes(data)
        previous = {'version': '1.0.0', 'hosts': {'fake': {'files': [
            {'path': 'tools/player-update-generic.py', 'after': sha256(layout['tools/player-update-generic.py'])},
            {'path': PUBLIC + 'server-manifest.json', 'after': 'f' * 64}]}}}
        (base / 'previous.json').write_text(json.dumps(previous), encoding='utf-8')
        (base / 'ssh.py').write_text(SSH_SHIM, encoding='utf-8')
        (base / 'scp.py').write_text(SCP_SHIM, encoding='utf-8')
        (base / 'token').write_text('t0ken', encoding='utf-8')
        config = {'order': ['fake'], 'primary': 'fake', 'backupRoot': str(base / 'backups'), 'tokenFile': str(base / 'token'),
                  'verifyUrls': {}, 'hosts': {'fake': {'kind': 'posix', 'address': 'fake', 'ssh': [sys.executable, str(base / 'ssh.py')],
                                                         'scp': [sys.executable, str(base / 'scp.py')], 'root': host.as_posix(),
                                                         'python': shlex.quote(Path(sys.executable).as_posix()),
                                                         'stage': (base / 'stage').as_posix()}}}
        self.spec = {'version': '1.0.2', 'previousVersion': '1.0.1', 'revision': 'r2', 'commit': self.commit_b,
                     'previousCommit': self.commit_a, 'previousReceipt': str(base / 'previous.json'), 'workdir': str(base / 'work'),
                     'backupName': 'b1', 'restartService': False, 'logTitle': '更新器维护', 'decision': '测试决定',
                     'notes': ['更新器维护说明'], 'removeFiles': ['tools/old-helper.ps1', PUBLIC + 'server-manifest.json.sig'],
                     'logReplace': [['旧云端', '已停用的云端']], 'receiptNotes': ['补充一行'],
                     'validation': {'suite': '本地 1 项通过'}}
        self.config_path, self.spec_path = base / 'hosts.json', base / 'release.json'
        self.config_path.write_text(json.dumps(config), encoding='utf-8')
        self.write_spec()
        self.base = base

    def tearDown(self):
        self.temp.cleanup()

    def write_spec(self, **changes):
        self.spec_path.write_text(json.dumps(dict(self.spec, **changes), ensure_ascii=False), encoding='utf-8')

    def release(self):
        release = self.tool.Release(str(self.config_path), str(self.spec_path))
        release.guide_bytes = lambda _config: self.guide  # the real one needs PowerShell
        return release

    def quiet(self, step):
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()) as out:
            result = step()
        return result, out.getvalue()

    def test_preflight_backup_prepare_dryrun_and_receipt(self):
        release = self.release()
        ok, out = self.quiet(release.preflight)
        self.assertTrue(ok, out)
        self.assertIn('"changedByContentRelease"', out)
        self.assertIn(PUBLIC + 'server-manifest.json', out)
        # Anything the content publisher does not own must still match the previous receipt.
        engine = self.host / 'tools/player-update-generic.py'
        original = engine.read_bytes()
        engine.write_bytes(b'changed by hand\n')
        self.assertFalse(self.quiet(release.preflight)[0])
        engine.write_bytes(original)

        self.quiet(release.run_backup)
        backup = Path(self.base / 'backups' / 'b1' / 'fake')
        # The unchanged root entry is backed up too: e2e runs it (regression for 2.27.26).
        self.assertTrue((backup / PUBLIC / '更新mod-Windows端.bat').is_file())
        self.assertTrue((backup / 'tools/old-helper.ps1').is_file())
        baseline = json.loads((self.base / 'work' / 'baseline-receipt.json').read_text(encoding='utf-8'))
        self.assertIn('tools/new-tool.py', baseline['absentOnHost']['fake'])
        with self.assertRaises(SystemExit):
            self.quiet(release.run_backup)

        self.quiet(release.prepare)
        out_dir = self.base / 'work' / 'release'
        plan = json.loads((out_dir / 'fake' / 'deploy-plan.json').read_text(encoding='utf-8'))
        entries = {e['path']: e for e in plan['files']}
        self.assertEqual(entries['tools/old-helper.ps1']['delete'], True)
        self.assertNotIn(PUBLIC + 'server-manifest.json.sig', entries)  # absent files need no removal
        self.assertIsNone(entries['tools/new-tool.py']['before'])
        self.assertEqual(entries[PUBLIC + '_updater/player-update-generic.py']['after'], sha256(self.engine_b))
        self.assertNotIn(PUBLIC + '更新mod-Windows端.bat', entries)
        payload = out_dir / 'fake' / 'payload'
        manifest = json.loads((payload / PUBLIC / 'server-manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['version'], '1.0.2')
        self.assertEqual(manifest['releaseNotes'], ['更新器维护说明', '（1.0.1）内容更新说明'])
        self.assertEqual(manifest['releaseNoteGroups'], [{'version': '1.0.2', 'title': '更新器维护', 'notes': ['更新器维护说明']},
                                                         {'version': '1.0.1', 'title': '内容更新', 'notes': ['内容更新说明']}])
        rows = {r['path']: r for r in manifest['files']}
        self.assertEqual(rows['_updater/player-update-generic.py']['sha1'], sha1(self.engine_b))
        self.assertEqual(rows['mods/a.jar']['sha1'], sha1(b'jar'))
        log = (payload / PUBLIC / 'update-log.txt').read_text(encoding='utf-8')
        self.assertTrue(log.startswith('> '), log)
        self.assertIn('- 更新器维护说明\n', log)
        self.assertIn('- 去掉已停用的云端分发\n', log)
        self.assertNotIn('去掉旧云端', log)
        pack = json.loads((payload / 'tools/portable-pack.json').read_text(encoding='utf-8'))
        self.assertEqual((pack['version'], pack['releaseModChanges']), ('1.0.2', ['~ 模组']))

        self.quiet(release.dryrun)
        self.assertEqual(json.loads((out_dir / 'dryrun.json').read_text(encoding='utf-8'))['hosts']['fake']['secondRunRefused'], True)

        # The receipt text from the JSON receipts a real run writes after deploy, verify and finalize.
        receipt = json.loads((out_dir / 'release-receipt.json').read_text(encoding='utf-8'))
        receipt.update(status='PUBLISHED_VERIFIED', completedAt='2026-10-26T12:00:00+09:00',
                       ci={'ok': True, 'reason': '6/6 jobs passed', 'runs': [{'id': 42}]})
        (out_dir / 'release-receipt.json').write_text(json.dumps(receipt, ensure_ascii=False), encoding='utf-8')
        (out_dir / 'deployment-receipt.json').write_text(json.dumps({
            'before': {'fake:game': ['p29186'], 'fake:service': ['state', '=', 'running', 'pid', '=', '33127']},
            'restartedServices': [], 'gameUnchanged': True}), encoding='utf-8')
        (out_dir / 'http-verification.json').write_text(json.dumps({'version': '1.0.2', 'endpoints': {'fake': {
            'files': 1, 'rangeStatus': 206, 'gzip': True, 'removedAbsent': []}}, 'liveBootstrap': {
            'previous': {'refreshed': 7}, 'slim': {'current': 7}}}), encoding='utf-8')
        (out_dir / 'e2e-transition.json').write_text(json.dumps({'A': {}, 'B': {}, 'C': {}}), encoding='utf-8')
        self.quiet(release.write_receipt)
        text = (out_dir / '发布回执.txt').read_text(encoding='utf-8')
        for expected in ('更新器 1.0.2 发布回执', 'PUBLISHED_VERIFIED', 'CI：全部通过，6/6 jobs passed（运行 42）', '决定：测试决定',
                         '  - 更新器维护说明', '玩家清单改动 1 项', '删除（删除前已备份）tools/old-helper.ps1',
                         '「旧云端」→「已停用的云端」', '游戏监听进程 29186', '下载服务 PID 33127', '本地回归：本地 1 项通过',
                         '旧根目录 BAT、旧 PowerShell、旧 Python', '演练通过（fake ', 'Range 206，gzip 正常', '更新了 7 个核心脚本',
                         '补充：\n  补充一行', '有过一次内容发布', '回滚：', 'dryrun.json'):
            self.assertIn(expected, text)

    def test_prepare_refuses_a_version_the_engines_do_not_carry(self):
        self.write_spec(version='1.0.3')
        release = self.release()
        self.quiet(release.run_backup)
        with self.assertRaises(SystemExit):
            self.quiet(release.prepare)

    def test_log_replace_must_match(self):
        self.write_spec(logReplace=[['不存在的文字', 'x']])
        release = self.release()
        self.quiet(release.run_backup)
        with self.assertRaises(SystemExit):
            self.quiet(release.prepare)


if __name__ == '__main__':
    unittest.main()

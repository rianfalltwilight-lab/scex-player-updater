"""servers.dat merging (SERVER_LIST_MERGE_V1) in both engines, and the stage daemons' poll
interval (STAGE_POLL_V1). The PowerShell merge must produce the same bytes as the Python one."""
import base64
import gzip
import importlib.util
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

if sys.platform == 'darwin':
    tempfile.tempdir = os.path.realpath(tempfile.gettempdir())

ROOT = Path(__file__).resolve().parents[1]
PS = os.environ.get('UPDATER_TEST_POWERSHELL') or (shutil.which('powershell.exe') if os.name == 'nt' else None)
ADDRESS = 'frp-example.test:18587'
NAME = 'SCEX 推荐线路'


def load(name):
    spec = importlib.util.spec_from_file_location('merge_' + name.replace('-', '_').replace('.', '_'), ROOT / 'tools' / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def nstr(text):
    raw = text.encode('utf-8')
    return struct.pack('>H', len(raw)) + raw


def tag(kind, name, payload):
    return bytes([kind]) + nstr(name) + payload


def entry(name, ip, extra=b''):
    """One servers.dat compound payload (its tags and TAG_End)."""
    return tag(8, 'name', nstr(name)) + tag(8, 'ip', nstr(ip)) + extra + b'\x00'


PREFERRED = tag(8, 'name', nstr(NAME)) + tag(8, 'ip', nstr(ADDRESS)) + tag(1, 'acceptTextures', b'\x01') + b'\x00'
ICON = tag(8, 'icon', nstr('iVBORw0KGgo=')) + tag(1, 'hidden', b'\x00')
ROOT_EXTRA = tag(3, 'version', struct.pack('>i', 3)) + tag(11, 'ints', struct.pack('>iii', 2, 7, 9)) + tag(4, 'stamp', struct.pack('>q', 5))


def servers_dat(entries, root_extra=b''):
    servers = tag(9, 'servers', b'\x0a' + struct.pack('>i', len(entries)) + b''.join(entries))
    return tag(10, '', root_extra + servers + b'\x00')


A = entry('朋友的服', 'play.example.org', ICON)
B = entry('本地', '127.0.0.1:25565')
EXISTING = entry('旧备线', ADDRESS.upper(), ICON)
CASES = {
    'missing': (None, servers_dat([PREFERRED])),
    'prepend': (servers_dat([A, B], ROOT_EXTRA), servers_dat([PREFERRED, A, B], ROOT_EXTRA)),
    'move-to-front': (servers_dat([A, EXISTING, B]), servers_dat([EXISTING, A, B])),
    'already-first': (servers_dat([EXISTING, A]), servers_dat([EXISTING, A])),
    'no-servers-tag': (tag(10, '', ROOT_EXTRA + b'\x00'), tag(10, '', ROOT_EXTRA + tag(9, 'servers', b'\x0a' + struct.pack('>i', 1) + PREFERRED) + b'\x00')),
    'empty-list': (tag(10, '', tag(9, 'servers', b'\x00' + struct.pack('>i', 0)) + b'\x00'), servers_dat([PREFERRED])),
}
BROKEN = {
    'gzip': gzip.compress(servers_dat([A]), mtime=0),  # AV_QUIET_V1: Minecraft cannot read it either
    'truncated': servers_dat([A])[:-3],
    'trailing': servers_dat([A]) + b'x',
    'not-compound': tag(8, '', nstr('x')),
    'string-list': tag(10, '', tag(9, 'servers', b'\x08' + struct.pack('>i', 1) + nstr('x')) + b'\x00'),
    'negative-count': tag(10, '', tag(9, 'servers', b'\x0a' + struct.pack('>i', -1)) + b'\x00'),
}


class PythonMergeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.u = load('player-update-generic.py')

    def test_cases(self):
        for name, (data, expected) in CASES.items():
            with self.subTest(name):
                self.assertEqual(self.u.merge_preferred_server(data, NAME, ADDRESS), expected)
        self.assertIs(self.u.merge_preferred_server(CASES['already-first'][0], NAME, ADDRESS), CASES['already-first'][0])

    def test_broken_files_are_refused(self):
        for name, data in BROKEN.items():
            with self.subTest(name), self.assertRaises((ValueError, IndexError, struct.error)):
                self.u.merge_preferred_server(data, NAME, ADDRESS)

    def test_apply_server_list_merges_once_and_keeps_broken_files(self):
        manifest = {'serverList': {'enabled': True, 'target': 'servers.dat', 'name': NAME, 'address': ADDRESS,
                                   'writeIfMissingOnly': True, 'mergePreferred': True}}
        with tempfile.TemporaryDirectory() as tmp:
            root, backup = Path(tmp) / 'client', Path(tmp) / 'backup'
            root.mkdir()
            (root / 'servers.dat').write_bytes(servers_dat([A]))
            self.u.apply_server_list(root, manifest, backup)
            self.assertEqual((root / 'servers.dat').read_bytes(), servers_dat([PREFERRED, A]))
            self.assertEqual((backup / 'servers.dat').read_bytes(), servers_dat([A]))
            stamp = (root / 'servers.dat').stat().st_mtime_ns
            self.u.apply_server_list(root, manifest, Path(tmp) / 'backup2')
            self.assertEqual((root / 'servers.dat').stat().st_mtime_ns, stamp)
            self.assertFalse((Path(tmp) / 'backup2').exists())
            (root / 'servers.dat').write_bytes(BROKEN['trailing'])
            self.u.apply_server_list(root, manifest, Path(tmp) / 'backup3')  # prints and keeps the file
            self.assertEqual((root / 'servers.dat').read_bytes(), BROKEN['trailing'])


PS_RUNNER = r'''
param([string]$Script, [string]$CasesPath)
$ErrorActionPreference = 'Stop'
$wanted = @('Write-U16BE', 'Write-I32BE', 'Write-NbtStringPayload', 'Write-NamedTagHeader', 'Test-SameBytes', 'Merge-PreferredServerList')
$ast = [Management.Automation.Language.Parser]::ParseFile($Script, [ref]$null, [ref]$null)
foreach ($def in $ast.FindAll({ param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] }, $false)) {
    if ($wanted -contains $def.Name) { . ([scriptblock]::Create($def.Extent.Text)) }
}
$cases = [IO.File]::ReadAllText($CasesPath, [Text.Encoding]::UTF8) | ConvertFrom-Json
$out = @{}
foreach ($case in $cases.cases) {
    [byte[]]$data = $null
    if ($case.data) { $data = [Convert]::FromBase64String([string]$case.data) }
    try { $out[[string]$case.name] = [Convert]::ToBase64String([byte[]](Merge-PreferredServerList -Data $data -Name $cases.name -Address $cases.address)) }
    catch { $out[[string]$case.name] = 'ERROR: ' + $_.Exception.Message }
}
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::Out.Write(($out | ConvertTo-Json -Compress))
'''


@unittest.skipUnless(PS, 'Windows PowerShell')
class PowerShellMergeTests(unittest.TestCase):
    def test_same_bytes_as_python(self):
        u = load('player-update-generic.py')
        inputs = [{'name': k, 'data': base64.b64encode(v[0]).decode() if v[0] else None} for k, v in CASES.items()]
        inputs += [{'name': k, 'data': base64.b64encode(v).decode()} for k, v in BROKEN.items()]
        with tempfile.TemporaryDirectory() as tmp:
            cases = Path(tmp) / 'cases.json'
            cases.write_text(json.dumps({'name': NAME, 'address': ADDRESS, 'cases': inputs}, ensure_ascii=False), encoding='utf-8')
            runner = Path(tmp) / 'runner.ps1'
            runner.write_text(PS_RUNNER, encoding='utf-8-sig')
            result = subprocess.run([PS, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(runner), '-Script',
                                     str(ROOT / 'tools' / 'player-update-generic.ps1'), '-CasesPath', str(cases)],
                                    capture_output=True, timeout=180)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', 'replace'))
        out = json.loads(result.stdout.decode('utf-8'))
        for name, (data, expected) in CASES.items():
            with self.subTest(name):
                self.assertFalse(out[name].startswith('ERROR'), out[name])
                got = base64.b64decode(out[name])
                self.assertEqual(got, expected)
                self.assertEqual(got, u.merge_preferred_server(data, NAME, ADDRESS))
        for name in BROKEN:
            with self.subTest(name):
                self.assertTrue(out[name].startswith('ERROR'), name)


class PollIntervalTests(unittest.TestCase):
    def test_python_daemon(self):
        stage = load('portable-stage-daemon.py')
        self.assertEqual([stage.poll_interval(m, 30, 300, 10) for m in (0, 9.9, 10, 600)], [30, 30, 300, 300])
        self.assertEqual(stage.poll_interval(20, 30, 5, 10), 30)  # never faster than the fast interval
        self.assertEqual(stage.poll_interval(0, 0, 300, 10), 1)

    @unittest.skipUnless(PS, 'Windows PowerShell')
    def test_powershell_daemon(self):
        script = ("$ast=[Management.Automation.Language.Parser]::ParseFile($env:S,[ref]$null,[ref]$null);"
                  "$f=$ast.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Get-PollIntervalSeconds'},$true);"
                  ". ([scriptblock]::Create($f.Extent.Text));"
                  "(@(0,9.9,10,600) | ForEach-Object { Get-PollIntervalSeconds -ElapsedMinutes $_ -Fast 30 -Slow 300 -FastMinutes 10 }) -join ',';"
                  "Get-PollIntervalSeconds -ElapsedMinutes 20 -Fast 30 -Slow 5 -FastMinutes 10")
        result = subprocess.run([PS, '-NoProfile', '-Command', script], capture_output=True, timeout=120,
                                env=dict(os.environ, S=str(ROOT / 'tools' / 'portable-stage-daemon.ps1')))
        self.assertEqual(result.stdout.decode().split(), ['30,30,300,300', '30'], result.stderr.decode('utf-8', 'replace'))


if __name__ == '__main__':
    unittest.main()

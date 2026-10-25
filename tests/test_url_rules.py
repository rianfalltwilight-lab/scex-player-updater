"""Every updater entry point orders update URLs by the same rule (URL_RULES_V1).

The cases live in tests/fixtures/url-order-cases.json; the three Python and three
PowerShell implementations must all produce the expected order for each case.
"""
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
import urllib.parse
import sys

# macOS keeps temp folders under /var, a symlink to /private/var; the updaters refuse
# paths through links, so tests work in the real location (Windows keeps 8.3 TEMP).
if sys.platform == 'darwin':
    tempfile.tempdir = os.path.realpath(tempfile.gettempdir())


ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = ROOT / 'tests' / 'fixtures' / 'url-order-cases.json'
CASES = json.loads(CASES_PATH.read_text(encoding='utf-8'))['cases']
PS = os.environ.get('UPDATER_TEST_POWERSHELL') or (shutil.which('powershell.exe') if os.name == 'nt' else None)
HELPERS = ['Test-LocalOrPrivateHost', 'Test-PrivateUpdateUrl', 'Test-RetiredCloudUrl', 'Test-UsableUpdateUrl']
PS_RUNNER = r'''
param([string]$Script, [string]$Function, [string]$CasesPath)
$ErrorActionPreference = 'Stop'
$wanted = @('Test-LocalOrPrivateHost', 'Test-PrivateUpdateUrl', 'Test-RetiredCloudUrl', 'Test-UsableUpdateUrl', $Function)
$ast = [Management.Automation.Language.Parser]::ParseFile($Script, [ref]$null, [ref]$null)
foreach ($def in $ast.FindAll({ param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] }, $true)) {
    if ($wanted -contains $def.Name) { . ([scriptblock]::Create($def.Extent.Text)) }
}
$cases = ([IO.File]::ReadAllText($CasesPath, [Text.Encoding]::UTF8) | ConvertFrom-Json).cases
$out = New-Object System.Collections.Generic.List[object]
foreach ($case in $cases) {
    $result = @(& $Function -Urls @($case.urls) -LastGood ([string]$case.lastGood))
    $out.Add([pscustomobject]@{ name = [string]$case.name; result = [string[]]$result })
}
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::Out.Write((ConvertTo-Json -InputObject $out.ToArray() -Depth 5 -Compress))
'''


def load_module(name):
    spec = importlib.util.spec_from_file_location('url_rules_' + name.replace('-', '_').replace('.', '_'), ROOT / 'tools' / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def command_order_function():
    text = (ROOT / 'tools' / 'portable-macos-sync.command').read_text(encoding='utf-8')
    block = text[text.index('# URL_RULES_BEGIN'):text.index('# URL_RULES_END')]
    namespace = {'ipaddress': ipaddress, 're': re, 'urllib': urllib}
    exec(compile(block, 'portable-macos-sync.command', 'exec'), namespace)
    return namespace['order_manifest_urls']


class UrlRuleTests(unittest.TestCase):
    def check(self, label, order):
        for case in CASES:
            with self.subTest(implementation=label, case=case['name']):
                self.assertEqual(order(case['urls'], case['lastGood']), case['expected'])

    def test_python_main_updater(self):
        self.check('player-update-generic.py', load_module('player-update-generic.py').order_manifest_urls)

    def test_python_stage_daemon(self):
        self.check('portable-stage-daemon.py', load_module('portable-stage-daemon.py').order_update_urls)

    def test_macos_entry(self):
        self.check('portable-macos-sync.command', command_order_function())

    @unittest.skipUnless(PS, 'Windows PowerShell')
    def test_powershell_implementations(self):
        with tempfile.TemporaryDirectory(prefix='updater-url-rules-') as tmp:
            runner = Path(tmp) / 'runner.ps1'
            runner.write_text(PS_RUNNER, encoding='utf-8-sig')
            for script, function in [('player-update-generic.ps1', 'Get-OrderedManifestUrls'),
                                     ('portable-bootstrap-refresh.ps1', 'Get-OrderedUpdateUrls'),
                                     ('portable-stage-daemon.ps1', 'Get-OrderedUpdateUrls')]:
                result = subprocess.run([PS, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(runner),
                                         '-Script', str(ROOT / 'tools' / script), '-Function', function,
                                         '-CasesPath', str(CASES_PATH)], capture_output=True, timeout=120)
                self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
                rows = json.loads(result.stdout.decode('utf-8-sig'))
                by_name = {row['name']: (row['result'] or []) for row in rows}
                for case in CASES:
                    with self.subTest(implementation=script, case=case['name']):
                        self.assertEqual(by_name[case['name']], case['expected'])


if __name__ == '__main__':
    unittest.main()

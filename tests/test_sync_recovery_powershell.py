"""Real Windows PowerShell recovery and resume behavior on synthetic clients."""
import hashlib
import http.server
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest

from test_updater import PS, ROOT, write, json_write

SOURCE = Path(os.environ.get('UPDATER_RECOVERY_PS_PATH', ROOT / 'tools/player-update-generic.ps1'))

def row(path, data):
    return dict(path=path, sha1=hashlib.sha1(data).hexdigest(), size=len(data))

def manifest(rows, version='v2', **settings):
    return dict(packId='recovery-test', version=version, files=rows, downloadWorkers=2,
                adoptExistingFiles=False, cleanup=dict(removeConnectorCache=False,
                disableLauncherRepairIndex=False, disableDuplicateMods=False),
                serverList=dict(enabled=False), **settings)

class Web:
    def __init__(self):
        self.routes = {}
        self.requests = []
    def __enter__(self):
        owner = self
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                owner.requests.append((self.path, self.headers.get('Range')))
                value = owner.routes.get(self.path, (404, b''))
                if callable(value): value = value(self)
                if isinstance(value, dict): value = (200, json.dumps(value).encode())
                if isinstance(value, bytes): value = (200, value)
                status, data, *extra = value
                self.send_response(status)
                headers = extra[0] if extra else {}
                headers.setdefault('Content-Length', str(len(data)))
                for key, val in headers.items(): self.send_header(key, str(val))
                self.end_headers()
                try: self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError): pass
                self.close_connection = True
            def log_message(self, *_): pass
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self
    def __exit__(self, *_):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
    def url(self, path='/server-manifest.json'):
        return f'http://127.0.0.1:{self.server.server_port}{path}'

@unittest.skipUnless(PS and os.name == 'nt', 'Windows PowerShell required')
class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='scex-ps-recovery-')
        self.root = Path(self.temp.name) / 'client'
        self.root.mkdir()
    def tearDown(self): self.temp.cleanup()
    def state(self, rows, version='v1', name='.portable-sync-state.json'):
        json_write(self.root / name, dict(format=3, packId='recovery-test', version=version, files=rows))
    def launch(self, url):
        return subprocess.Popen([PS, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(SOURCE),
                                 '-InstanceDir', str(self.root), '-ManifestUrl', url, '-NoPause'],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    def finish(self, process):
        data = process.communicate(timeout=35)[0].decode('utf-8-sig', errors='replace')
        return process.returncode, data
    def sync(self, web, expected=0, path='/server-manifest.json'):
        code, output = self.finish(self.launch(web.url(path)))
        self.assertEqual(code, expected, output)
        return output
    def functions(self, body):
        path = Path(self.temp.name) / 'functions.ps1'
        source = str(SOURCE).replace("'", "''")
        path.write_text("$ErrorActionPreference='Stop'\n"+
            f"$a=[Management.Automation.Language.Parser]::ParseFile('{source}',[ref]$null,[ref]$null)\n"+
            "$a.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst]},$false) | ForEach-Object { . ([scriptblock]::Create($_.Extent.Text)) }\n"+body,
            encoding='utf-8-sig')
        result = subprocess.run([PS, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(path)],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=35)
        self.assertEqual(result.returncode, 0, result.stdout.decode('utf-8-sig', errors='replace'))

    def test_legacy_hash_and_original_backup(self):
        bad = write(self.root, 'mods/demo.jar', b'BAD!')
        good = row('mods/demo.jar', b'GOOD')
        options = b'lang:en_us\nresourcePacks:["vanilla"]\n'
        write(self.root, 'options.txt', options)
        self.state([good])
        with Web() as web:
            web.routes = {'/server-manifest.json':manifest([good], playerOptions=dict(apply='always',
                targets=['options.txt'], defaults=dict(lang='zh_cn'), requiredResourcePacks=['file/test.zip'])),
                '/mods/demo.jar':b'GOOD'}
            self.sync(web)
        self.assertEqual((self.root/'mods/demo.jar').read_bytes(), b'GOOD')
        backups = list((self.root/'.portable-sync-backups').glob('*/options.txt'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), options)

    def test_failed_update_resumes_across_release_and_removes_abandoned_addition(self):
        old = [write(self.root, 'config/a.txt', b'v1a'),write(self.root, 'config/b.txt', b'v1b')]
        self.state(old)
        with Web() as web:
            web.routes = {'/server-manifest.json':manifest([row('config/a.txt',b'v2a'),
                row('mods/new.jar',b'v2mod'),row('config/b.txt',b'v2b')],preserveLocalChangeGlobs=['config/**']),
                '/config/a.txt':b'v2a','/mods/new.jar':b'v2mod'}
            self.sync(web, expected=1)
            self.assertTrue((self.root/'.portable-sync-pending.json').exists())
            web.routes = {'/server-manifest.json':manifest([row('config/a.txt',b'v3a'),row('config/b.txt',b'v3b')],
                version='v3',preserveLocalChangeGlobs=['config/**']),'/config/a.txt':b'v3a','/config/b.txt':b'v3b'}
            self.sync(web)
        self.assertEqual((self.root/'config/a.txt').read_bytes(), b'v3a')
        self.assertEqual((self.root/'config/b.txt').read_bytes(), b'v3b')
        self.assertFalse((self.root/'mods/new.jar').exists())
        self.assertFalse((self.root/'.portable-sync-pending.json').exists())

    def test_concurrent_update_is_blocked_without_overwriting_log(self):
        self.state([write(self.root,'mods/demo.jar',b'base')])
        ready, release = threading.Event(), threading.Event()
        def slow(_): ready.set(); release.wait(15); return 200,b'OLD!'
        with Web() as web:
            web.routes = {'/old/server-manifest.json':manifest([row('mods/demo.jar',b'OLD!')],version='v1'),
                '/new/server-manifest.json':manifest([row('mods/demo.jar',b'NEW!')]),
                '/old/mods/demo.jar':slow,'/new/mods/demo.jar':b'NEW!'}
            first = self.launch(web.url('/old/server-manifest.json'))
            self.assertTrue(ready.wait(10))
            before = (self.root/'.portable-sync-logs/latest.log').read_bytes()
            try:
                output = self.sync(web, expected=1, path='/new/server-manifest.json')
                self.assertIn('正在', output)
                self.assertEqual((self.root/'.portable-sync-logs/latest.log').read_bytes(), before)
            finally: release.set()
            code, output = self.finish(first)
            self.assertEqual(code, 0, output)
            self.sync(web, path='/new/server-manifest.json')
        self.assertEqual((self.root/'mods/demo.jar').read_bytes(), b'NEW!')

    def test_corrupt_state_recovers_previous_or_stops(self):
        old = row('config/deleted.txt',b'old')
        (self.root/'.portable-sync-state.json').write_bytes(b'{broken')
        with Web() as web:
            web.routes = {'/server-manifest.json':manifest([row('config/deleted.txt',b'new')],
                         preserveLocalDeletionGlobs=['config/**']),'/config/deleted.txt':b'new'}
            output = self.sync(web, expected=1)
            self.assertIn('状态', output)
            self.assertFalse((self.root/'config/deleted.txt').exists())
            self.state([old],name='.portable-sync-state.prev.json')
            self.sync(web)
            for invalid in ({'files':None}, {'files':[{'path':'config/deleted.txt'}]}):
                json_write(self.root/'.portable-sync-state.json', invalid)
                self.sync(web)
        self.assertFalse((self.root/'config/deleted.txt').exists())

    def test_duplicate_and_junction_are_rejected_before_changes(self):
        write(self.root,'mods/demo.jar',b'old')
        with Web() as web:
            web.routes = {'/server-manifest.json':manifest([row('mods/demo.jar',b'AAAA'),row('mods/demo.jar',b'BBBB')])}
            self.assertIn('重复', self.sync(web,expected=1))
            self.assertEqual((self.root/'mods/demo.jar').read_bytes(),b'old')
            outside=Path(self.temp.name)/'outside';outside.mkdir();write(outside,'marker.txt',b'old')
            junction=self.root/'config'
            command=f"New-Item -ItemType Junction -Path '{junction}' -Target '{outside}' | Out-Null"
            subprocess.run([PS,'-NoProfile','-Command',command],check=True,capture_output=True)
            try:
                web.routes={'/server-manifest.json':manifest([row('config/marker.txt',b'new')]),'/config/marker.txt':b'new'}
                self.assertIn('路径',self.sync(web,expected=1))
                self.assertEqual((outside/'marker.txt').read_bytes(),b'old')
            finally: junction.rmdir()

    def test_only_current_verified_stage_is_installed_with_backup(self):
        old=write(self.root,'resourcepacks/demo.zip',b'old')
        self.state([old]);write(self.root,'.portable-staging/resourcepacks/demo.zip',b'new')
        write(self.root,'.portable-staging/mods/withdrawn.jar',b'withdrawn')
        with Web() as web:
            web.routes={'/server-manifest.json':manifest([row('resourcepacks/demo.zip',b'new')])}
            self.sync(web)
            self.assertFalse(any(path=='/resourcepacks/demo.zip' for path,_ in web.requests))
        self.assertEqual((self.root/'resourcepacks/demo.zip').read_bytes(),b'new')
        self.assertFalse((self.root/'mods/withdrawn.jar').exists())
        self.assertEqual(next((self.root/'.portable-sync-backups').glob('*/resourcepacks/demo.zip')).read_bytes(),b'old')

    def test_download_resumes_interrupted_payload_and_handles_range_fallback(self):
        content=b'abcdefghij';digest=hashlib.sha1(content).hexdigest()
        dest=self.root/'demo.bin';dest.write_bytes(b'old')
        calls=[]
        def response(request):
            requested=request.headers.get('Range');calls.append(requested)
            if len(calls)==1:return 200,content[:4],{'Content-Length':len(content)}
            self.assertEqual(requested,'bytes=4-')
            return 206,content[4:],{'Content-Range':'bytes 4-9/10'}
        with Web() as web:
            web.routes={'/payload':response}
            self.functions(f"""
$dest='{dest}'
$part=Get-PartialDownloadPath -Destination $dest -Sha1 '{digest}'
$failed=$false
try {{ Download-ManifestFile -Url '{web.url('/payload')}' -Destination $dest -ExpectedSha1 '{digest}' -Retries 0 -NoBackoff }} catch {{ $failed=$true }}
if (-not $failed -or -not [IO.File]::Exists($part) -or (Get-Item -LiteralPath $part).Length -ne 4) {{ throw 'Interrupted part not preserved' }}
Download-ManifestFile -Url '{web.url('/payload')}' -Destination $dest -ExpectedSha1 '{digest}' -Retries 0 -NoBackoff
""")
        self.assertEqual(dest.read_bytes(),content)
        for status in (200,416):
            with Web() as web:
                web.routes={'/payload':lambda r:(416,b'') if status==416 and r.headers.get('Range') else (200,content)}
                self.functions(f"""
$part=Get-PartialDownloadPath -Destination '{dest}' -Sha1 '{digest}'
[IO.File]::WriteAllBytes($part,[Text.Encoding]::ASCII.GetBytes('abcd'))
Download-ManifestFile -Url '{web.url('/payload')}' -Destination '{dest}' -ExpectedSha1 '{digest}' -Retries 0 -NoBackoff
""")
            self.assertEqual(dest.read_bytes(),content)

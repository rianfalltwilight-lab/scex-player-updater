"""HTTP range and publication regressions; local synthetic payloads only."""
import functools
import http.client
import http.server
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
TOOLS = Path(os.environ.get('UPDATER_TRANSPORT_TOOLS', ROOT / 'tools'))


class RangeTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('range_server', TOOLS/'secure-update-server.py')
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.body = b'0123456789abcdefghijklmnopqrstuvwxyz'
        (self.root/'file.bin').write_bytes(self.body)
        (self.root/'empty.bin').write_bytes(b'')
        handler = functools.partial(self.module.SecureUpdateHandler, directory=str(self.root), token='fixture')
        self.server = self.module.QuietThreadingHTTPServer(('127.0.0.1',0),handler)
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        self.temp.cleanup()

    def request(self, headers=None, method='GET', path='/fixture/file.bin'):
        conn = http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        conn.request(method,path,headers=headers or {})
        response=conn.getresponse()
        result=response.status,dict(response.headers),response.read()
        conn.close()
        return result

    def test_single_ranges_head_and_empty_file(self):
        for header, expected, content_range in [('bytes=0-3',b'0123','bytes 0-3/36'),('bytes=32-',b'wxyz','bytes 32-35/36'),('bytes=-4',b'wxyz','bytes 32-35/36'),('bytes=30-99',b'uvwxyz','bytes 30-35/36')]:
            status,headers,body=self.request({'Range':header})
            self.assertEqual(status,206); self.assertEqual(body,expected)
            self.assertEqual(headers['Content-Range'],content_range)
            self.assertEqual(int(headers['Content-Length']),len(expected))
        status,headers,body=self.request({'Range':'bytes=2-5'},method='HEAD')
        self.assertEqual((status,body),(206,b''));self.assertEqual(headers['Content-Length'],'4')
        self.assertEqual(self.request(path='/fixture/empty.bin')[2],b'')
        for header in ['bytes=100-','bytes=8-2','bytes=-0','bytes=0-1,4-5','not-a-range']:
            status,headers,body=self.request({'Range':header})
            self.assertEqual(status,416);self.assertEqual(headers['Content-Range'],'bytes */36');self.assertEqual(body,b'')
        self.assertEqual(self.request({'Range':'bytes=0-0'},path='/fixture/empty.bin')[0],416)

    def test_if_range_and_cache_conditions(self):
        status,headers,body=self.request()
        self.assertEqual(status,200);self.assertEqual(body,self.body)
        self.assertEqual(headers['Accept-Ranges'],'bytes')
        etag=headers['ETag'];modified=headers['Last-Modified']
        self.assertEqual(self.request({'Range':'bytes=2-','If-Range':etag})[0],206)
        self.assertEqual(self.request({'Range':'bytes=2-','If-Range':modified})[0],206)
        for validator in ['"stale"','W/'+etag,'invalid-date','Mon, 01 Jan 1990 00:00:00 GMT']:
            status,_,body=self.request({'Range':'bytes=2-','If-Range':validator})
            self.assertEqual((status,body),(200,self.body))
        self.assertEqual(self.request({'If-None-Match':etag})[0],304)
        self.assertEqual(self.request({'If-None-Match':'W/'+etag})[0],304)
        self.assertEqual(self.request({'If-Modified-Since':modified})[0],304)
        self.assertEqual(self.request({'If-None-Match':'"stale"','If-Modified-Since':modified})[0],200)
        (self.root/'file.bin').write_bytes(b'changed payload')
        self.assertEqual(self.request({'Range':'bytes=2-','If-Range':etag})[0],200)

    def test_range_avoids_whole_file_buffer_and_preserves_token_routing(self):
        gate=mock.Mock();gate.acquire.side_effect=AssertionError('Range used full-file buffer')
        with mock.patch.object(self.module.SecureUpdateHandler,'_buffer_gate',gate):
            self.assertEqual(self.request({'Range':'bytes=5-6'})[2],b'56')
        gate.acquire.assert_not_called()
        self.assertEqual(self.request({'Range':'bytes=0-1'},path='/wrong/file.bin')[0],404)


@unittest.skipUnless(os.name=='nt','Windows publisher locking')
class PublisherTests(unittest.TestCase):
    def test_locked_retired_file_stays_out_of_manifest_and_atomic_copy_preserves_old(self):
        source=TOOLS/'portable-publish.ps1'
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);public=root/'public';(public/'mods').mkdir(parents=True)
            (public/'mods/old.jar').write_bytes(b'old');(public/'mods/new.jar').write_bytes(b'new')
            script=root/'probe.ps1'
            script.write_text(r'''$ErrorActionPreference='Stop'
$source='''+"'"+str(source).replace("'","''")+"'"+r'''
$publishDir=Join-Path $PSScriptRoot 'public'
$ast=[Management.Automation.Language.Parser]::ParseFile($source,[ref]$null,[ref]$null)
foreach($d in $ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst]},$false)){. ([scriptblock]::Create($d.Extent.Text))}
$script:DesiredRelKeys=New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
[void]$script:DesiredRelKeys.Add('mods/new.jar');$oldUrlBySha1=@{};$lanManifestUrl=''
$text=[IO.File]::ReadAllText($source);$start=$text.IndexOf('$base = $publishDir.TrimEnd')
$ends=@($text.IndexOf('$sourceConfig =',$start),$text.IndexOf('$freshMap =',$start)) | Where-Object {$_ -gt $start}
$end=($ends | Measure-Object -Minimum).Minimum
$old=Join-Path $publishDir 'mods/old.jar';$new=Join-Path $publishDir 'mods/new.jar'
$lock=[IO.File]::Open($old,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
try {
 . ([scriptblock]::Create($text.Substring($start,$end-$start)))
 if(@($fileRows | ForEach-Object {$_.path}) -contains 'mods/old.jar'){throw 'Retired file re-entered manifest'}
 if($fileRows.Count -ne 1){throw 'Unexpected manifest row count'}
 $failed=$false
 try {Copy-FileWithRetry -Source $new -Dest $old -Attempts 1 -DelaySec 0} catch {$failed=$true}
 if(-not $failed -or [IO.File]::ReadAllText($old) -ne 'old'){throw 'Locked publish overwrote original'}
} finally {$lock.Dispose()}
Copy-FileWithRetry -Source $new -Dest $old -Attempts 1 -DelaySec 0
if([IO.File]::ReadAllText($old) -ne 'new'){throw 'Unlocked atomic publish failed'}
if(@(Get-ChildItem (Split-Path $old) -Filter '.publish-*' -Force).Count){throw 'Temporary payload leaked'}
''',encoding='utf-8-sig')
            run=subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(script)],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=20)
            self.assertEqual(run.returncode,0,run.stdout.decode('utf-8',errors='replace'))

import ctypes,hashlib,http.server,importlib.util,io,json,os,pathlib,subprocess,sys,tempfile,threading,unittest,urllib.error
from ctypes import wintypes
from unittest import mock

# macOS keeps temp folders under /var, a symlink to /private/var; the updaters refuse
# paths through links, so tests work in the real location (Windows keeps 8.3 TEMP).
if sys.platform == 'darwin':
    tempfile.tempdir = os.path.realpath(tempfile.gettempdir())

ROOT=pathlib.Path(__file__).resolve().parents[1]
CANDIDATE=pathlib.Path(os.environ.get('SCEX_UPDATER_TEST_PATH', str(ROOT/'tools'/'player-update-generic.py')))
WINDOWS=os.name == 'nt'
spec=importlib.util.spec_from_file_location('candidate',CANDIDATE);u=importlib.util.module_from_spec(spec);spec.loader.exec_module(u)
if WINDOWS:
 api=ctypes.WinDLL('kernel32',use_last_error=True)
 api.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE];api.CreateFileW.restype=wintypes.HANDLE
 api.CloseHandle.argtypes=[wintypes.HANDLE]
def hold(p):
 h=api.CreateFileW(str(p),0x80000000,1,None,3,0x80,None)
 if h==ctypes.c_void_p(-1).value:raise ctypes.WinError(ctypes.get_last_error())
 return h
class BytesOpener:
 def __init__(self,content=b'new'):self.content=content
 def open(self,*a,**kw):return io.BytesIO(self.content)
class BadOpener:
 def open(self,*a,**kw):raise TimeoutError('sensitive-url-token')
class Tests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory(prefix='scex-updater-fixture-');self.root=pathlib.Path(self.temp.name)
  self.dest=self.root/'resourcepacks'/'SCEX-任务图片资源包.zip';self.dest.parent.mkdir();self.dest.write_bytes(b'old')
 def tearDown(self):self.temp.cleanup()
 @unittest.skipUnless(WINDOWS, 'Windows sharing-lock behavior')
 def test_preflight_and_race_replacement_preserve_then_unlock(self):
  rows=[{'path':'resourcepacks/SCEX-任务图片资源包.zip'}];h=hold(self.dest)
  tmp=self.root/'new.tmp';tmp.write_bytes(b'new')
  try:
   with self.assertRaisesRegex(u.LocalFileUpdateError,'文件占用'):u.preflight_managed_files(self.root,rows)
   with self.assertRaisesRegex(u.LocalFileUpdateError,'文件占用'):u.install_verified_file(tmp,self.dest)
   self.assertEqual(self.dest.read_bytes(),b'old');self.assertEqual(tmp.read_bytes(),b'new')
  finally:api.CloseHandle(h)
  u.preflight_managed_files(self.root,rows);u.install_verified_file(tmp,self.dest)
  self.assertEqual(self.dest.read_bytes(),b'new');self.assertFalse(tmp.exists())
 @unittest.skipUnless(WINDOWS, 'Windows sharing-lock behavior')
 def test_readonly_and_optional_entrypoint(self):
  self.dest.chmod(0o444)
  try:u.preflight_managed_files(self.root,[{'path':self.dest.relative_to(self.root).as_posix()}])
  finally:self.dest.chmod(0o666)
  entry=self.root/'Windows-sync.bat';entry.write_bytes(b'echo test');h=hold(entry)
  try:u.preflight_managed_files(self.root,[{'path':entry.name}])
  finally:api.CloseHandle(h)
 def test_download_and_hash_errors_are_distinct_chinese(self):
  with self.assertRaisesRegex(u.UpdateError,'下载失败.*网络连接或读取超时') as e:u.download_file('http://example.invalid/private/file',self.dest,'a'*40,opener=BadOpener(),retries=0)
  self.assertNotIn('sensitive-url-token',str(e.exception));self.assertNotIn('/private/',str(e.exception))
  with self.assertRaisesRegex(u.UpdateError,'校验失败'):u.download_file('http://example.invalid/private/file',self.dest,'a'*40,opener=BytesOpener(),retries=0)
  self.assertEqual(self.dest.read_bytes(),b'old');self.assertFalse(list(self.dest.parent.glob('.portable-sync-*')))
  u.download_file('http://example.invalid/private/file',self.dest,hashlib.sha1(b'new').hexdigest(),opener=BytesOpener(),retries=0)
  self.assertEqual(self.dest.read_bytes(),b'new')
 def test_local_replace_does_not_retry_other_source(self):
  with mock.patch.object(u,'official_urls',return_value=['https://one.example/a','https://two.example/a']),mock.patch.object(u,'opener_for_official_url',return_value=u._DIRECT_OPENER),mock.patch.object(u,'download_file',side_effect=u.LocalFileUpdateError('[文件占用] test')) as download:
   with self.assertRaises(u.LocalFileUpdateError):u.download_manifest_entry({},'http://fallback/',self.dest,'a'*40,'pack.zip')
  self.assertEqual(download.call_count,1)
 def test_http_busy_retry_preserved(self):
  e=urllib.error.HTTPError('http://secret/',503,'busy',{}, io.BytesIO())
  self.assertIsNotNone(u.busy_retry_delay(e,0));self.assertIsNone(u.busy_retry_delay(e,5))
  self.assertIn('HTTP 503',u.network_error_reason(e));e.close()
 @unittest.skipUnless(WINDOWS, 'Windows sharing-lock behavior')
 def test_main_preflight_no_partial_update_no_traceback(self):
  first=self.root/'config'/'first.txt';first.parent.mkdir();first.write_bytes(b'first-old')
  files=[{'path':'config/first.txt','size':3,'sha1':hashlib.sha1(b'new').hexdigest()},{'path':self.dest.relative_to(self.root).as_posix(),'size':3,'sha1':hashlib.sha1(b'new').hexdigest()}]
  manifest={'version':'test','packId':'test','files':files,'cleanup':{'removeConnectorCache':False,'disableLauncherRepairIndex':False}}
  requests=[]
  class Handler(http.server.BaseHTTPRequestHandler):
   def do_GET(self):
    requests.append(self.path)
    body=json.dumps(manifest).encode() if self.path.endswith('manifest.json') else b'new';self.send_response(200);self.end_headers();self.wfile.write(body)
   def log_message(self,*args):pass
  server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();h=hold(self.dest)
  try:
   p=subprocess.run([sys.executable,'-X','utf8',str(CANDIDATE),'--instance-dir',str(self.root),'--manifest-url',f'http://127.0.0.1:{server.server_port}/private-test/manifest.json'],capture_output=True,text=True,encoding='utf-8',timeout=20)
  finally:api.CloseHandle(h);server.shutdown();server.server_close()
  out=p.stdout+p.stderr
  self.assertEqual(p.returncode,1,out);self.assertIn('[文件占用]',out);self.assertIn('尚未开始替换',out);self.assertNotIn('Traceback',out);self.assertNotIn('private-test',out)
  self.assertEqual(first.read_bytes(),b'first-old');self.assertEqual(self.dest.read_bytes(),b'old')
  self.assertEqual(requests,['/private-test/manifest.json']);self.assertFalse((self.root/'.portable-sync-state.json').exists());self.assertFalse((self.root/'.portable-sync-backups').exists())
if __name__=='__main__':unittest.main(verbosity=2)
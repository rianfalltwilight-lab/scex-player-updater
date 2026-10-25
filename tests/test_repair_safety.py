"""Windows repair workflows use synthetic clients and localhost HTTP only."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from test_updater import ROOT, PS, serve, write, json_write
from test_powershell_file_preflight import hold_read_lock

TOOLS=Path(os.environ.get('UPDATER_TRANSPORT_TOOLS', ROOT/'tools'))


@unittest.skipUnless(PS and os.name=='nt','Windows repair scripts')
class RepairTests(unittest.TestCase):
    def run_ps(self,name,args,root):
        env=dict(os.environ,PYTHONUTF8='1')
        for key in ('APPDATA','LOCALAPPDATA','ProgramFiles'):
            env[key]=str(root/'empty-programs')
        return subprocess.run([PS,'-NoProfile','-ExecutionPolicy','Bypass','-File',str(TOOLS/name),*args],env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=50)

    def test_deep_fix_explicit_url_preserves_custom_and_deleted_files_and_rechecks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);client=root/'client';web=root/'web'/'fixture-token';client.mkdir();web.mkdir(parents=True)
            old=[]
            for rel,body in {'mods/demo.jar':b'old mod','config/custom.txt':b'default','config/unchanged.txt':b'default','config/deleted.txt':b'default','config/forced.txt':b'default'}.items():
                old.append(write(client,rel,body))
            (client/'config/custom.txt').write_bytes(b'player custom')
            (client/'config/forced.txt').write_bytes(b'player forced')
            (client/'config/deleted.txt').unlink()
            json_write(client/'.portable-sync-state.json',{'files':old})
            rows=[write(web,row['path'],b'new '+row['path'].encode()) for row in old]
            json_write(web/'server-manifest.json',{'version':'fixture','minecraftVersion':'1.21.1','files':rows,'preservePlayerCustomizations':True,'preserveLocalChangeGlobs':['config/*'],'preserveLocalDeletionGlobs':['config/*'],'forceSyncGlobs':['config/forced.txt']})
            with serve(web.parent) as port:
                result=self.run_ps('player-self-repair.ps1',['-InstanceDir',str(client),'-ManifestUrl',f'http://127.0.0.1:{port}/fixture-token/server-manifest.json','-Deep','-Fix','-NoPause'],root)
            self.assertIn(result.returncode,(0,1),result.stdout.decode('utf-8',errors='replace'))
            meta=json.loads((client/'_repair/latest/meta.json').read_text(encoding='utf-8-sig'))
            self.assertEqual(meta['missing'],[]);self.assertEqual(meta['mismatch'],[])
            self.assertEqual(set(meta['fixed']),{'mods/demo.jar','config/unchanged.txt','config/forced.txt'})
            self.assertEqual(set(meta['preserved']),{'config/custom.txt','config/deleted.txt'})
            self.assertEqual((client/'config/custom.txt').read_bytes(),b'player custom')
            self.assertFalse((client/'config/deleted.txt').exists())
            self.assertEqual((client/'config/forced.txt').read_bytes(),b'new config/forced.txt')
            self.assertEqual(json.loads((client/'.portable-sync-state.json').read_text())['files'],old)
            self.assertIn('/***/',meta['manifestUrl'])
            backups=list((client/'_repair').glob('*/backup/config/forced.txt'))
            self.assertEqual(len(backups),1);self.assertEqual(backups[0].read_bytes(),b'player forced')

    def test_self_repair_exclusive_lock_and_file_preflight_prevent_mutations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);client=root/'client';web=root/'web';client.mkdir();web.mkdir()
            target=client/'mods/demo.jar';write(client,'mods/demo.jar',b'old')
            row=write(web,'mods/demo.jar',b'new')
            json_write(web/'server-manifest.json',{'files':[row]})
            lock=client/'.portable-sync.lock';lock.write_bytes(b'')
            with serve(web) as port:
                args=['-InstanceDir',str(client),'-ManifestUrl',f'http://127.0.0.1:{port}/server-manifest.json','-Fix','-NoPause']
                with hold_read_lock(lock):result=self.run_ps('player-self-repair.ps1',args,root)
                self.assertEqual(result.returncode,2)
                self.assertIn('更新进行中',result.stdout.decode('utf-8',errors='replace'))
                self.assertFalse((client/'_repair').exists())
                with hold_read_lock(target):result=self.run_ps('player-self-repair.ps1',args,root)
            self.assertEqual(result.returncode,2,result.stdout.decode('utf-8',errors='replace'))
            self.assertIn('文件占用',result.stdout.decode('utf-8',errors='replace'))
            self.assertFalse((client/'_repair').exists());self.assertEqual(target.read_bytes(),b'old')

    def entry_fixture(self,root):
        pack=root/'pack';client=pack/'.minecraft/versions/demo';scripts=client/'_updater';scripts.mkdir(parents=True)
        target=scripts/'Windows-sync.bat';target.write_bytes(b'broken old entry')
        kit=root/'kit';kit.mkdir()
        good=b'@echo off\r\nrem PORTABLE_SYNC_BOOTSTRAPPED player-update-generic\r\n'+b'rem fixture\r\n'*200
        return pack,client,target,kit,good

    def test_windows_entry_multiline_fallback_hash_check_and_verified_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);web=root/'web';web.mkdir()
            pack,client,target,kit,good=self.entry_fixture(root)
            bad_row=write(web,'bad/_updater/Windows-sync.bat',good+b'corrupt')
            bad_row.update(path='_updater/Windows-sync.bat',size=len(good),sha1=hashlib.sha1(good).hexdigest())
            good_row=write(web,'good/_updater/Windows-sync.bat',good);good_row['path']='_updater/Windows-sync.bat'
            json_write(web/'bad/server-manifest.json',{'files':[bad_row]})
            json_write(web/'good/server-manifest.json',{'files':[good_row]})
            with serve(web) as port:
                (client/'UPDATE-URL.txt').write_text(f'# alternatives\nhttp://127.0.0.1:{port}/bad/server-manifest.json\nhttp://127.0.0.1:{port}/good/server-manifest.json\n',encoding='utf-8')
                result=self.run_ps('portable-windows-repair.ps1',['-RepairHome',str(kit),'-StartDir',str(pack),'-NoLaunch'],root)
            self.assertEqual(result.returncode,0,result.stdout.decode('utf-8',errors='replace'))
            self.assertEqual(target.read_bytes(),good)
            self.assertIn('清单大小和哈希校验',result.stdout.decode('utf-8',errors='replace'))

    def test_windows_entry_bad_hash_and_busy_instance_preserve_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);web=root/'web';web.mkdir()
            pack,client,target,kit,good=self.entry_fixture(root)
            (kit/'Windows-sync.bat').write_bytes(good+b'rem old offline copy\r\n')
            row=write(web,'_updater/Windows-sync.bat',good+b'wrong');row.update(size=len(good),sha1=hashlib.sha1(good).hexdigest())
            json_write(web/'server-manifest.json',{'files':[row]})
            with serve(web) as port:
                (client/'UPDATE-URL.txt').write_text(f'http://127.0.0.1:{port}/server-manifest.json',encoding='utf-8')
                args=['-RepairHome',str(kit),'-StartDir',str(pack),'-NoLaunch']
                result=self.run_ps('portable-windows-repair.ps1',args,root)
                self.assertEqual(result.returncode,1)
                self.assertEqual(target.read_bytes(),b'broken old entry')
                lock=client/'.portable-sync.lock'
                with hold_read_lock(lock):result=self.run_ps('portable-windows-repair.ps1',args,root)
            self.assertEqual(result.returncode,1)
            self.assertIn('更新进行中',result.stdout.decode('utf-8',errors='replace'))
            self.assertEqual(target.read_bytes(),b'broken old entry')

    def test_windows_entry_offline_bundle_still_repairs_without_claiming_manifest_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);web=root/'web';web.mkdir()
            pack,client,target,kit,good=self.entry_fixture(root)
            (kit/'Windows-sync.bat').write_bytes(good)
            with serve(web) as port:
                (client/'UPDATE-URL.txt').write_text(f'http://127.0.0.1:{port}/missing.json',encoding='utf-8')
                result=self.run_ps('portable-windows-repair.ps1',['-RepairHome',str(kit),'-StartDir',str(pack),'-NoLaunch'],root)
            self.assertEqual(result.returncode,0,result.stdout.decode('utf-8',errors='replace'))
            self.assertEqual(target.read_bytes(),good)
            self.assertIn('离线修复',result.stdout.decode('utf-8',errors='replace'))
            self.assertNotIn('已通过清单大小和哈希校验',result.stdout.decode('utf-8',errors='replace'))

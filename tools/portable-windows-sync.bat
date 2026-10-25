@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
rem ============================================================
rem Portable player sync launcher (maintenance notes)
rem 1) Keep CRLF line endings + UTF-8 without BOM. LF endings make
rem    cmd desync its byte reader on multibyte content and crash.
rem 2) All Chinese user-facing text must go through powershell
rem    Write-Host; cmd echo garbles CJK under chcp 65001.
rem 3) Keep every cmd-parsed line, rem included, ASCII-only:
rem    Chinese in rem lines also desyncs cmd under chcp 65001 and
rem    part of the comment gets executed as a command.
rem 4) Bootstrap: copy self to TEMP first, because sync may
rem    overwrite this running script and corrupt parsing.
rem 5) Inline powershell uses direct HttpWebRequest and must set
rem    ProgressPreference=SilentlyContinue: the PS5.1 progress bar
rem    redraw overlaps CJK glyphs under chcp 65001.
rem ============================================================
if defined PORTABLE_SYNC_BOOTSTRAPPED goto :Main
rem Wrapper uses delayed expansion to return the child exit code on one
rem line; everything after cmd /c must stay on that same line because
rem this file may get overwritten by the sync right afterwards.
set "PORTABLE_SYNC_HOME=%~dp0"
set "PORTABLE_SYNC_BOOTSTRAPPED=1"
set "_PSYNC_TMP=%TEMP%\portable-sync-run-%RANDOM%%RANDOM%.bat"
copy /y "%~f0" "%_PSYNC_TMP%" >nul 2>nul
if not exist "%_PSYNC_TMP%" goto :Main
setlocal EnableDelayedExpansion
cmd /d /v:off /c ""!_PSYNC_TMP!"" & set "_PSYNC_CODE=!ERRORLEVEL!" & del "%_PSYNC_TMP%" >nul 2>nul & exit /b !_PSYNC_CODE!

:Main
if defined PORTABLE_SYNC_HOME (cd /d "%PORTABLE_SYNC_HOME%") else (cd /d "%~dp0")
set "PORTABLE_SCRIPT_DIR=%CD%"
set "PORTABLE_INSTANCE_DIR=%CD%"
for %%I in ("%PORTABLE_SCRIPT_DIR%") do if /I "%%~nxI"=="_updater" set "PORTABLE_INSTANCE_DIR=%%~dpI"
for %%I in ("%PORTABLE_INSTANCE_DIR%") do set "PORTABLE_INSTANCE_DIR=%%~fI"
rem Strip trailing backslash: %%~dp keeps it, and a trailing backslash before
rem the closing quote in -InstanceDir "..." escapes the quote inside powershell
rem argument parsing, injecting a literal quote into the path (Illegal characters).
if "%PORTABLE_INSTANCE_DIR:~-1%"=="\" set "PORTABLE_INSTANCE_DIR=%PORTABLE_INSTANCE_DIR:~0,-1%"

if exist "%PORTABLE_INSTANCE_DIR%\player-update-generic.ps1" if /I not "%PORTABLE_SCRIPT_DIR%"=="%PORTABLE_INSTANCE_DIR%" powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$s=Join-Path $env:PORTABLE_SCRIPT_DIR 'player-update-generic.ps1';$r=Join-Path $env:PORTABLE_INSTANCE_DIR 'player-update-generic.ps1';$t=$null;$e=$null;if((Test-Path -LiteralPath $s -PathType Leaf) -and (Test-Path -LiteralPath $r -PathType Leaf)){[System.Management.Automation.Language.Parser]::ParseFile($s,[ref]$t,[ref]$e)|Out-Null;if($e.Count -gt 0){$t=$null;$e=$null;[System.Management.Automation.Language.Parser]::ParseFile($r,[ref]$t,[ref]$e)|Out-Null;if($e.Count -eq 0){$tmp=$s+'.repair';Copy-Item -LiteralPath $r -Destination $tmp -Force;Move-Item -LiteralPath $tmp -Destination $s -Force;Write-Host '[repair] restored invalid updater from package root.'}}}"

cls
set "PORTABLE_BOOTSTRAP_REFRESH=%PORTABLE_INSTANCE_DIR%\_updater\portable-bootstrap-refresh.ps1"
if not exist "%PORTABLE_BOOTSTRAP_REFRESH%" set "PORTABLE_BOOTSTRAP_REFRESH=%PORTABLE_SCRIPT_DIR%\portable-bootstrap-refresh.ps1"
if exist "%PORTABLE_BOOTSTRAP_REFRESH%" goto :PortableBootstrapRefresh
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue';try{[Net.ServicePointManager]::SecurityProtocol=[Net.ServicePointManager]::SecurityProtocol -bor 3072}catch{};function Fetch([string]$u){$r=[Net.HttpWebRequest]::Create($u);$r.Proxy=$null;$r.Timeout=8000;$r.ReadWriteTimeout=8000;$r.KeepAlive=$false;$response=$null;$stream=$null;$out=New-Object IO.MemoryStream;try{$response=$r.GetResponse();$stream=$response.GetResponseStream();$stream.CopyTo($out);return ,$out.ToArray()}finally{if($stream){$stream.Dispose()};if($response){$response.Dispose()};$out.Dispose()}};$root=$env:PORTABLE_INSTANCE_DIR;$sd=$env:PORTABLE_SCRIPT_DIR;$urls=New-Object 'Collections.Generic.List[string]';if($env:PORTABLE_MANIFEST_URL){foreach($u in ($env:PORTABLE_MANIFEST_URL -split '[\r\n\x1f]+')){if($u.Trim()){$urls.Add($u.Trim())}}}else{foreach($d in @($root,$sd,(Join-Path $root '_updater'))){foreach($n in @('UPDATE-URL.txt','PORTABLE-UPDATE-URL.txt','TFCR-update-url.txt','UPDATE-URL-LAN.txt')){$p=Join-Path $d $n;if(Test-Path -LiteralPath $p){foreach($u in (Get-Content -LiteralPath $p -Encoding UTF8)){if($u.Trim() -and -not $u.Trim().StartsWith('#')){$urls.Add($u.Trim())}}}}}};foreach($u in $urls){$tmp=$null;try{$m=[Text.Encoding]::UTF8.GetString((Fetch $u)).TrimStart([char]0xfeff)|ConvertFrom-Json;$rows=@($m.files|Where-Object{$_.path -eq '_updater/portable-bootstrap-refresh.ps1'});if($rows.Count -ne 1 -or [string]$rows[0].sha1 -notmatch '^[a-fA-F0-9]{40}$' -or [string]$rows[0].size -notmatch '^\d+$'){continue};$bytes=Fetch ($u.Substring(0,$u.LastIndexOf('/'))+'/_updater/portable-bootstrap-refresh.ps1');$sha=[Security.Cryptography.SHA1]::Create();try{$hash=([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-','').ToLowerInvariant()}finally{$sha.Dispose()};if($bytes.Length -ne [long]$rows[0].size -or $hash -ne [string]$rows[0].sha1){continue};$out=$env:PORTABLE_BOOTSTRAP_REFRESH;$tmp=$out+'.new-'+[guid]::NewGuid().ToString('N');[IO.File]::WriteAllBytes($tmp,$bytes);$tok=$null;$err=$null;[Management.Automation.Language.Parser]::ParseFile($tmp,[ref]$tok,[ref]$err)|Out-Null;if($err.Count){continue};Move-Item -LiteralPath $tmp -Destination $out -Force;exit 0}catch{}finally{if($tmp -and (Test-Path -LiteralPath $tmp)){Remove-Item -LiteralPath $tmp -Force -EA SilentlyContinue}}};Write-Host '[更新器] 无法取得校验通过的启动脚本，请检查更新地址后重试。';exit 1"
if errorlevel 1 (set "code=1" & goto :SyncFailed)
:PortableBootstrapRefresh
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PORTABLE_BOOTSTRAP_REFRESH%" -InstanceDir "%PORTABLE_INSTANCE_DIR%" -ScriptDir "%PORTABLE_SCRIPT_DIR%" -ManifestUrl "%PORTABLE_MANIFEST_URL%"
if errorlevel 1 (set "code=1" & goto :SyncFailed)
:PortableBootstrapRefreshDone

rem The verified bootstrap stops this instance's old Watch before synchronization.
rem Player runs may wait for Enter when a file is locked (LOCK_WAIT_V1).
set "PORTABLE_SYNC_INTERACTIVE=1"
set "PORTABLE_PY="
rem where.exe succeeding is not enough: Windows Store python/py stubs
rem return 9009 when actually launched. Probe with a one-liner first.
py -3 -c "import sys" >nul 2>nul && set "PORTABLE_PY=py -3"
if not defined PORTABLE_PY python -c "import sys" >nul 2>nul && set "PORTABLE_PY=python"
if defined PORTABLE_PY if exist "%PORTABLE_SCRIPT_DIR%\player-update-generic.py" goto :SyncWithPython
:SyncWithPowerShell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PORTABLE_SCRIPT_DIR%\player-update-generic.ps1" -InstanceDir "%PORTABLE_INSTANCE_DIR%" -NoPause
set "code=%ERRORLEVEL%"
goto :AfterSync
:SyncWithPython
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Write-Host '[同步] 使用 Python 同步器'"
%PORTABLE_PY% "%PORTABLE_SCRIPT_DIR%\player-update-generic.py" --instance-dir "%PORTABLE_INSTANCE_DIR%"
set "code=%ERRORLEVEL%"
if "%code%"=="9009" (
  set "PORTABLE_PY="
  powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Write-Host '[同步] Python 无法启动（退出码 9009，多为微软商店假入口），改用 PowerShell'"
  goto :SyncWithPowerShell
)
:AfterSync
if not "%code%"=="0" goto :SyncFailed
if "%PORTABLE_SYNC_ONLY%"=="1" exit /b %code%

rem Launch the background staging daemon as an independent process (survives this
rem window closing). It pre-downloads future updates into .portable-staging while
rem the player is online, and self-exits when the game closes or after a max runtime.
rem NOTE: do NOT pass -WindowStyle Hidden here. "start powershell -WindowStyle Hidden
rem -ExecutionPolicy Bypass -File" trips Huorong heuristic PS.NetLoader and the bat
rem gets deleted on double-click. Start minimized (/min) and let the daemon minimize
rem its own console at runtime instead.
if defined PORTABLE_PY if exist "%PORTABLE_SCRIPT_DIR%\portable-stage-daemon.py" goto :StartPythonWatch
if exist "%PORTABLE_SCRIPT_DIR%\portable-stage-daemon.ps1" start "portable-stage-daemon" /min powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PORTABLE_SCRIPT_DIR%\portable-stage-daemon.ps1" -InstanceDir "%PORTABLE_INSTANCE_DIR%" -Mode Watch -Toast
goto :AfterStartWatch
:StartPythonWatch
start "portable-stage-daemon" /min %PORTABLE_PY% "%PORTABLE_SCRIPT_DIR%\portable-stage-daemon.py" --instance-dir "%PORTABLE_INSTANCE_DIR%" --mode watch
:AfterStartWatch

powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $root=[System.IO.Path]::GetFullPath($env:PORTABLE_INSTANCE_DIR); $launcherDir=Join-Path $root '_launchers'; $mcHome=''; $p=Split-Path -Parent $root; $gp=''; if($p){ $gp=Split-Path -Parent $p }; if($gp -and ((Split-Path -Leaf $p) -ieq 'versions') -and ((Split-Path -Leaf $gp) -ieq '.minecraft')){ $mcHome=Split-Path -Parent $gp }; Write-Host ''; $started=$false; $pclHome=$root; if($mcHome){ $pclHome=$mcHome }; $pcl=$null; foreach($n in @('PCL.exe','Plain Craft Launcher.exe')){ $c=Join-Path $pclHome $n; if(Test-Path -LiteralPath $c -PathType Leaf){ $pcl=$c; break } }; if(-not $pcl -and $mcHome){ foreach($src in @((Join-Path $root 'PCL.exe'),(Join-Path $launcherDir 'PCL.exe'))){ if(Test-Path -LiteralPath $src -PathType Leaf){ $dest=Join-Path $mcHome 'PCL.exe'; try { Copy-Item -LiteralPath $src -Destination $dest -Force; $pcl=$dest; Write-Host ('[启动器] 已把 PCL 放到 ' + $mcHome + '（PCL 只识别自己所在目录下的 .minecraft 实例）') } catch { $pcl=$null }; break } } }; if($pcl){ Write-Host ('[启动器] 正在启动 PCL：' + $pcl); try { Start-Process -FilePath $pcl -WorkingDirectory (Split-Path -Parent $pcl); $started=$true; Write-Host '[启动器] PCL 已启动。' } catch { Write-Host ('[启动器] PCL 启动失败：' + $_.Exception.Message) } }; if(-not $started){ $jar=$null; foreach($c in @((Join-Path $root 'HMCL.jar'),(Join-Path $launcherDir 'HMCL.jar'))){ if(Test-Path -LiteralPath $c -PathType Leaf){ $jar=$c; break } }; if($jar){ $wd=$root; if($mcHome){ $wd=$mcHome; Write-Host ('[启动器] 将从 ' + $wd + ' 启动 HMCL 以识别实例。') }; Write-Host '[启动器] 正在启动 HMCL...'; $java=Get-Command javaw.exe -ErrorAction SilentlyContinue; try { if($java){ Start-Process -FilePath $java.Source -ArgumentList @('-jar',$jar) -WorkingDirectory $wd } else { Start-Process -FilePath $jar -WorkingDirectory $wd }; $started=$true; Write-Host '[启动器] HMCL 已启动。' } catch { Write-Host ('[启动器] HMCL 启动失败：' + $_.Exception.Message) } } }; if(-not $started){ $sakura=Join-Path $root 'SakuraLauncher.exe'; if(Test-Path -LiteralPath $sakura -PathType Leaf){ Write-Host '[启动器] 正在启动 SakuraLauncher...'; try { Start-Process -FilePath $sakura -WorkingDirectory $root; $started=$true; Write-Host '[启动器] SakuraLauncher 已启动。' } catch {} } }; if(-not $started){ Write-Host ('[启动器] 未找到可自动启动的启动器，请检查：' + $root + ' 或 ' + $launcherDir) }; Write-Host ''; Write-Host '按回车键关闭窗口...'"
pause >nul
exit /b %code%

:SyncFailed
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Write-Host ''; Write-Host ('[同步] 失败，退出码 ' + $env:code + '。请把实例目录里的「更新诊断包.zip」发给管理员；没有这个文件时再截图本窗口。'); Write-Host ''; Write-Host '按回车键关闭窗口...'"
pause >nul
exit /b %code%

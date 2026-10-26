param(
    [string]$RepairHome = '',
    [string]$StartDir = '',
    [switch]$NoLaunch
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072 } catch {}

if ($env:PORTABLE_REPAIR_NO_LAUNCH -eq '1') { $NoLaunch = $true }
if ([string]::IsNullOrWhiteSpace($RepairHome)) {
    $RepairHome = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
}
$RepairHome = [IO.Path]::GetFullPath($RepairHome.TrimEnd('\', '/'))
if ([string]::IsNullOrWhiteSpace($StartDir)) { $StartDir = $RepairHome }
$StartDir = [IO.Path]::GetFullPath($StartDir.TrimEnd('\', '/'))

function Test-LfOnly([byte[]]$Bytes) {
    for ($i = 0; $i -lt $Bytes.Length; $i++) {
        if ($Bytes[$i] -eq 10 -and ($i -eq 0 -or $Bytes[$i - 1] -ne 13)) { return $true }
    }
    return $false
}

function Test-GoodSyncBat([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    $bytes = [IO.File]::ReadAllBytes($Path)
    if ($bytes.Length -lt 2000) { return $false }
    if (Test-LfOnly $bytes) { return $false }
    $text = [Text.Encoding]::UTF8.GetString($bytes)
    return ($text -match 'PORTABLE_SYNC_BOOTSTRAPPED' -and $text -match 'player-update-generic')
}

function Find-UpdaterHits([string]$Root) {
    $found = New-Object System.Collections.Generic.List[string]
    $versions = Join-Path $Root '.minecraft\versions'
    if (-not (Test-Path -LiteralPath $versions -PathType Container)) { return @() }
    Get-ChildItem -LiteralPath $versions -Directory -ErrorAction SilentlyContinue | ForEach-Object {
        $inner = Join-Path $_.FullName '_updater\Windows-sync.bat'
        if (Test-Path -LiteralPath $inner -PathType Leaf) { [void]$found.Add($inner) }
    }
    return @($found)
}

function Find-InstanceUpdater([string]$Start) {
    $hits = New-Object System.Collections.Generic.List[string]
    $cursor = $Start
    for ($i = 0; $i -lt 5; $i++) {
        foreach ($hit in (Find-UpdaterHits $cursor)) { [void]$hits.Add($hit) }
        $parent = Split-Path -Parent $cursor
        if ([string]::IsNullOrWhiteSpace($parent) -or $parent -eq $cursor) { break }
        $cursor = $parent
    }
    if ($hits.Count -eq 0) {
        Get-ChildItem -LiteralPath $Start -Directory -ErrorAction SilentlyContinue | ForEach-Object {
            foreach ($hit in (Find-UpdaterHits $_.FullName)) { [void]$hits.Add($hit) }
        }
    }
    if ($hits.Count -eq 0) {
        $parent = Split-Path -Parent $Start
        if (-not [string]::IsNullOrWhiteSpace($parent) -and $parent -ne $Start) {
            Get-ChildItem -LiteralPath $parent -Directory -ErrorAction SilentlyContinue | ForEach-Object {
                foreach ($hit in (Find-UpdaterHits $_.FullName)) { [void]$hits.Add($hit) }
            }
        }
    }
    return @($hits | Select-Object -Unique)
}

function Save-DirectFile {
    param([string]$Url, [string]$Path, [int]$TimeoutSec = 25)
    $target = $Url
    $hostHeader = $null
    try {
        $uri = [Uri]$Url
        if ($uri.Scheme -eq 'http' -and $uri.HostNameType -eq [UriHostNameType]::Dns) {
            $ipv4 = [Net.Dns]::GetHostAddresses($uri.Host) | Where-Object { $_.AddressFamily -eq 'InterNetwork' } | Select-Object -First 1
            if ($ipv4) {
                $builder = New-Object UriBuilder $uri
                $builder.Host = $ipv4.IPAddressToString
                $target = $builder.Uri.AbsoluteUri
                $hostHeader = $uri.Host
            }
        }
    } catch {}
    $request = [Net.HttpWebRequest]::Create($target)
    if ($hostHeader) { $request.Host = $hostHeader }
    $request.Proxy = $null
    $request.Timeout = [Math]::Max(1000, $TimeoutSec * 1000)
    $request.ReadWriteTimeout = [Math]::Max(1000, $TimeoutSec * 1000)
    $request.UserAgent = $script:SyncUserAgent
    $response = $null
    $inputStream = $null
    $outputStream = $null
    try {
        $response = $request.GetResponse()
        $inputStream = $response.GetResponseStream()
        $outputStream = [IO.File]::Open($Path, [IO.FileMode]::Create, [IO.FileAccess]::Write, [IO.FileShare]::None)
        $inputStream.CopyTo($outputStream)
    } finally {
        if ($outputStream) { $outputStream.Dispose() }
        if ($inputStream) { $inputStream.Dispose() }
        if ($response) { $response.Dispose() }
    }
}


# REPAIR_SAFETY_V2: use the same persistent exclusive lock as the main updater.
function Enter-RepairLock([string]$Root) {
    try { return [IO.File]::Open((Join-Path $Root '.portable-sync.lock'), [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None) }
    catch { throw '[更新进行中] 此实例已有更新或修复任务，请等待该窗口结束后重试。' }
}
function Format-RepairError($ErrorRecord, [string]$Path = '', [string]$Phase = '修复') {
    $ex=$ErrorRecord.Exception; $code=0
    while ($ex) {
        if ([string]$ex.Message -match '(\[(文件占用|更新进行中|权限不足|空间不足|校验失败|路径错误|下载失败|修复失败|安装失败)\].*)') { return $Matches[1] }
        $native=$ex.HResult -band 65535
        if ($native -in @(5,32,33,39,112)) { $code=$native }
        $ex=$ex.InnerException
    }
    if ($code -in @(32,33)) { return "[文件占用] $Path。请完全退出 Minecraft，关闭打开此文件的程序后重试；原文件已保留。" }
    if ($code -eq 5) { return "[权限不足] $Path。请检查文件权限和安全软件拦截后重试。" }
    if ($code -in @(39,112)) { return "[空间不足] $Path。请腾出目标磁盘空间后重试。" }
    if ($Phase -eq '下载') { return "[下载失败] $Path。无法取得更新内容，请检查网络或稍后重试。" }
    return "[修复失败] $Path。操作未完成，请保留窗口截图联系管理员。"
}
function Assert-RepairPathsAvailable($Paths) {
    $seen=New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    foreach($path in $Paths) {
        if (-not $seen.Add([string]$path) -or -not [IO.File]::Exists($path)) { continue }
        $handle=$null
        try { $handle=[IO.File]::Open($path,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::None) }
        catch { throw (Format-RepairError $_ $path) }
        finally { if($handle){$handle.Dispose()} }
    }
}
function Install-RepairFile([string]$Temporary,[string]$Destination) {
    $attributes=$null
    try {
        if ([IO.File]::Exists($Destination)) {
            $attributes=[IO.File]::GetAttributes($Destination)
            if ($attributes -band [IO.FileAttributes]::ReparsePoint) { throw "[路径错误] 目标是链接，已停止替换：$Destination" }
            if ($attributes -band [IO.FileAttributes]::ReadOnly) { [IO.File]::SetAttributes($Destination,($attributes -band (-bnot [IO.FileAttributes]::ReadOnly))) }
            [IO.File]::Replace($Temporary,$Destination,[NullString]::Value)
        } else { [IO.File]::Move($Temporary,$Destination) }
    } catch {
        if ($null -ne $attributes -and [IO.File]::Exists($Destination)) { [IO.File]::SetAttributes($Destination,$attributes) }
        throw (Format-RepairError $_ $Destination)
    }
}
function Get-RepairProperty($Object,[string]$Name,$Default=$null) {
    if ($null -ne $Object -and $null -ne $Object.PSObject.Properties[$Name]) { return $Object.$Name }
    return $Default
}
function Test-RepairGlob([string]$Rel,$Globs) {
    foreach($glob in @($Globs)) { if($Rel.Replace('\','/') -like ([string]$glob).Replace('\','/')){return $true} }
    return $false
}
# MANIFEST_CACHE_BEGIN: identical in player-update-generic.ps1, portable-bootstrap-refresh.ps1,
# portable-stage-daemon.ps1, player-self-repair.ps1 and portable-windows-repair.ps1;
# tests/test_manifest_cache.py compares the copies (rules as in player-update-generic.py).
# MANIFEST_CACHE_V1: the last manifest is kept with its ETag, so an unchanged manifest costs
# one small 304 response; the server may send it gzip-compressed.
$script:UpdaterVersion = '2.27.29'
$script:ManifestCacheDir = '.portable-manifest-cache'

function Invoke-ManifestRequest {
    # One direct GET, never through a proxy. Returns @{ Status; ETag; Body }.
    param([string]$Url, [string]$ETag, [int]$TimeoutSec, [string]$UserAgent)
    $request = [Net.HttpWebRequest]::Create($Url)
    $request.Proxy = $null
    $request.Timeout = [Math]::Max(1000, $TimeoutSec * 1000)
    $request.ReadWriteTimeout = [Math]::Max(1000, $TimeoutSec * 1000)
    $request.KeepAlive = $false
    $request.UserAgent = $UserAgent
    $request.AutomaticDecompression = [Net.DecompressionMethods]::GZip
    $request.Headers['Cache-Control'] = 'no-cache'
    if ($ETag) { $request.Headers['If-None-Match'] = $ETag }
    $response = $null
    try {
        try { $response = $request.GetResponse() } catch {
            $web = $_.Exception
            while ($web -and -not ($web -is [Net.WebException])) { $web = $web.InnerException }
            # 304 is an answer; anything else keeps the original WebException for the caller.
            if ($web -and $web.Response -and ([int]$web.Response.StatusCode) -eq 304) { $response = $web.Response } else { throw }
        }
        $status = [int]$response.StatusCode
        $body = $null
        if ($status -eq 200) {
            $stream = $response.GetResponseStream()
            $buffer = New-Object IO.MemoryStream
            try { $stream.CopyTo($buffer); $body = $buffer.ToArray() } finally { $stream.Dispose(); $buffer.Dispose() }
        }
        return @{ Status = $status; ETag = [string]$response.Headers['ETag']; Body = $body }
    } finally { if ($response) { $response.Dispose() } }
}

function Get-ManifestCacheFolder {
    param([string]$Root)
    $folder = Join-Path $Root $script:ManifestCacheDir
    try {
        if ([IO.Directory]::Exists($folder) -and ([IO.File]::GetAttributes($folder) -band [IO.FileAttributes]::ReparsePoint)) { return $null }
    } catch { return $null }
    return $folder
}

function Get-Sha256Hex {
    param([byte[]]$Data)
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash($Data))).Replace('-', '').ToLowerInvariant() } finally { $sha.Dispose() }
}

function Read-ManifestCache {
    param([string]$Root)
    $folder = Get-ManifestCacheFolder $Root
    if (-not $folder) { return $null }
    try {
        $meta = [IO.File]::ReadAllText((Join-Path $folder 'meta.json'), [Text.Encoding]::UTF8) | ConvertFrom-Json
        $manifest = [IO.File]::ReadAllBytes((Join-Path $folder 'server-manifest.json'))
        if ((Get-Sha256Hex $manifest) -ne [string]$meta.manifestSha256) { return $null }
        return @{ Manifest = $manifest; ETag = [string]$meta.manifestEtag }
    } catch { return $null }
}

function Write-ManifestCache {
    param([string]$Root, [byte[]]$Manifest, [string]$ETag)
    $folder = Get-ManifestCacheFolder $Root
    if (-not $folder -or -not $ETag) { return }
    try {
        [void][IO.Directory]::CreateDirectory($folder)
        $meta = [ordered]@{ manifestEtag = $ETag; manifestSha256 = (Get-Sha256Hex $Manifest) }
        foreach ($file in @(@('server-manifest.json', $Manifest), @('meta.json', [Text.Encoding]::UTF8.GetBytes(($meta | ConvertTo-Json -Compress))))) {
            $target = Join-Path $folder $file[0]
            $temporary = $target + '.' + $PID + '.tmp'
            [IO.File]::WriteAllBytes($temporary, [byte[]]$file[1])
            if ([IO.File]::Exists($target)) { [IO.File]::Replace($temporary, $target, [System.Management.Automation.Language.NullString]::Value) } else { [IO.File]::Move($temporary, $target) }
        }
    } catch { }  # the cache only saves bandwidth
}

function Get-CachedManifest {
    # Returns [pscustomobject]@{ Raw = [byte[]] }; throws when the source is unreachable.
    param([string]$Url, [string]$Root = '', [int]$TimeoutSec = 20, [string]$UserAgent = 'SCEX-updater', [switch]$NoCache)
    $cache = if ($Root -and -not $NoCache) { Read-ManifestCache $Root } else { $null }
    $reply = Invoke-ManifestRequest -Url $Url -ETag $(if ($cache) { $cache.ETag } else { '' }) -TimeoutSec $TimeoutSec -UserAgent $UserAgent
    if ($reply.Status -eq 304 -and $cache) { return [pscustomobject]@{ Raw = $cache.Manifest } }
    if ($reply.Status -ne 200) { throw ('HTTP ' + $reply.Status) }
    if ($Root -and -not $NoCache) { Write-ManifestCache -Root $Root -Manifest $reply.Body -ETag $reply.ETag }
    return [pscustomobject]@{ Raw = $reply.Body }
}
# MANIFEST_CACHE_END
$script:SyncUserAgent = "SCEX-updater/$($script:UpdaterVersion) (windows-repair $($PSVersionTable.PSVersion.Major).$($PSVersionTable.PSVersion.Minor); windows)"

function Get-RepairMaskedUrl([string]$Url) {
    if ($Url -like 'file:*') { return '本地清单' }
    try { $uri=[Uri]$Url;return [regex]::Replace($uri.GetLeftPart([UriPartial]::Path), '(?<=^[A-Za-z][A-Za-z0-9+.-]*://[^/]+/)[^/]+(?=/)', '***') } catch { return '更新地址' }
}

function Read-UpdateUrls([string]$InstanceDir, [string]$KitHome) {
    $result=New-Object 'System.Collections.Generic.List[string]'
    $seen=New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    # ROOT_SLIM_V1: since 2.27.23 the URL files live in _updater; older packs keep them in the root.
    foreach($p in @((Join-Path $InstanceDir 'UPDATE-URL.txt'),(Join-Path $InstanceDir 'PORTABLE-UPDATE-URL.txt'),(Join-Path $InstanceDir 'TFCR-update-url.txt'),(Join-Path $InstanceDir '_updater\UPDATE-URL.txt'),(Join-Path $InstanceDir '_updater\PORTABLE-UPDATE-URL.txt'),(Join-Path $KitHome 'UPDATE-URL.txt'))) {
        if (-not (Test-Path -LiteralPath $p -PathType Leaf)) { continue }
        foreach($line in ((Get-Content -LiteralPath $p -Raw -Encoding UTF8) -split "`r?`n")) {
            $url=([string]$line).Trim()
            if ($url -and -not $url.StartsWith('#') -and $seen.Add($url)) { [void]$result.Add($url) }
        }
    }
    return @($result.ToArray())
}
function Read-UpdateUrl([string]$InstanceDir, [string]$KitHome) {
    return @(Read-UpdateUrls $InstanceDir $KitHome) | Select-Object -First 1
}
function Get-RepairSha1([string]$Path) {
    $hash=[Security.Cryptography.SHA1]::Create();$stream=$null
    try { $stream=[IO.File]::OpenRead($Path);return ([BitConverter]::ToString($hash.ComputeHash($stream))).Replace('-','').ToLowerInvariant() }
    finally { if($stream){$stream.Dispose()};$hash.Dispose() }
}
function Assert-RepairTargetRoot([string]$Root,[string]$Path) {
    $rootFull=[IO.Path]::GetFullPath($Root).TrimEnd('\','/')
    $cursor=[IO.Path]::GetFullPath($Path)
    if (-not $cursor.StartsWith($rootFull+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)) { throw '[路径错误] 修复目标越出实例目录。' }
    while($cursor -and $cursor.Length -ge $rootFull.Length) {
        if(Test-Path -LiteralPath $cursor){if([IO.File]::GetAttributes($cursor) -band [IO.FileAttributes]::ReparsePoint){throw '[路径错误] 修复目标包含链接，已停止。'}}
        if($cursor -eq $rootFull){break};$cursor=Split-Path -Parent $cursor
    }
}
function Install-Bat([string]$Source, [string]$Dest) {
    if (Test-Path -LiteralPath $Dest -PathType Leaf) {
        $srcBytes = [IO.File]::ReadAllBytes($Source)
        $dstBytes = [IO.File]::ReadAllBytes($Dest)
        if ($srcBytes.Length -eq $dstBytes.Length -and -not (Test-LfOnly $dstBytes)) {
            $same = $true
            for ($i = 0; $i -lt $srcBytes.Length; $i++) {
                if ($srcBytes[$i] -ne $dstBytes[$i]) { $same = $false; break }
            }
            if ($same) { return $false }
        }
    }
    $dir = Split-Path -Parent $Dest
    New-Item -ItemType Directory -Force $dir | Out-Null
    $tmp = Join-Path $dir ('.Windows-sync.portable-new-' + [guid]::NewGuid().ToString('N') + '.bat')
    try {
        Copy-Item -LiteralPath $Source -Destination $tmp -Force
        Install-RepairFile -Temporary $tmp -Destination $Dest
    } finally {
        if (Test-Path -LiteralPath $tmp -PathType Leaf) { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue }
    }
    return $true
}

$repairLock=$null
$tempDownload=$null
try {
Write-Host '============================================================'
Write-Host '  修复 Windows 更新脚本'
Write-Host '============================================================'

$hits = @(Find-InstanceUpdater $StartDir)
if ($hits.Count -eq 0) {
    Write-Host '[错误] 没有找到 .minecraft\versions\<实例>\_updater\Windows-sync.bat' -ForegroundColor Red
    Write-Host '把本文件夹放到整合包解压根目录（和「更新mod-Windows端.bat」放一起）再双击。' -ForegroundColor Yellow
    if (-not $NoLaunch) { Read-Host '按回车关闭' | Out-Null }
    exit 1
}
if ($hits.Count -gt 1) {
    Write-Host '[错误] 找到多个整合包，请把本脚本放到要修的那个包根目录再运行：' -ForegroundColor Red
    foreach ($h in $hits) { Write-Host ("  " + $h) }
    if (-not $NoLaunch) { Read-Host '按回车关闭' | Out-Null }
    exit 1
}

$inner = [string]$hits[0]
$updaterDir = Split-Path -Parent $inner
$instanceDir = Split-Path -Parent $updaterDir
Write-Host ("实例: " + $instanceDir)
Write-Host ("脚本: " + $inner)

$repairLock=Enter-RepairLock $instanceDir
$instanceCopy=Join-Path $instanceDir '更新mod-Windows端.bat'
Assert-RepairTargetRoot $instanceDir $inner
Assert-RepairTargetRoot $instanceDir $instanceCopy
Assert-RepairPathsAvailable @($inner,$instanceCopy)

$source = $null
$tempDownload = $null
$localSource = $null
foreach ($cand in @((Join-Path $RepairHome 'Windows-sync.bat'), (Join-Path $RepairHome '_updater\Windows-sync.bat'))) {
    if (Test-GoodSyncBat $cand) { $localSource=$cand;break }
}
$urls=@(Read-UpdateUrls -InstanceDir $instanceDir -KitHome $RepairHome)
$sawValidManifest=$false
if ($urls.Count -eq 0) {
    if (-not $localSource) { throw '[修复失败] 没有可用的本地修复包或更新地址。请保留完整修复包，或补充 UPDATE-URL.txt。' }
    $source=$localSource
    Write-Host '[修复] 未配置更新地址，使用本地完整修复包。'
} else {
    $tempDownload=Join-Path ([IO.Path]::GetTempPath()) ('portable-repair-'+[guid]::NewGuid().ToString('N')+'.bat')
    $manifestTemp=Join-Path ([IO.Path]::GetTempPath()) ('portable-repair-manifest-'+[guid]::NewGuid().ToString('N')+'.json')
    try {
        foreach($manifestUrl in $urls) {
            try {
                $manifest=[Text.Encoding]::UTF8.GetString([byte[]](Get-CachedManifest -Url $manifestUrl -Root $instanceDir -TimeoutSec 8 -UserAgent $script:SyncUserAgent -NoCache).Raw).TrimStart([char]0xFEFF) | ConvertFrom-Json
                $rows=@($manifest.files | Where-Object { $_.path -ceq '_updater/Windows-sync.bat' })
                if ($rows.Count -ne 1 -or [string]$rows[0].sha1 -notmatch '^[0-9a-fA-F]{40}$' -or $null -eq $rows[0].size -or [long]$rows[0].size -le 0) { throw '[校验失败] 清单没有有效的 Windows 更新入口。' }
                $row=$rows[0]
                $sawValidManifest=$true
                if ($localSource -and (Get-Item -LiteralPath $localSource).Length -eq [long]$row.size -and (Get-RepairSha1 $localSource) -eq ([string]$row.sha1).ToLowerInvariant()) { $source=$localSource;break }
                $slash=$manifestUrl.LastIndexOf('/')
                if ($slash -lt 0) { throw '[校验失败] 更新地址无效。' }
                $fileUrl=$manifestUrl.Substring(0,$slash+1)+'_updater/Windows-sync.bat'
                Save-DirectFile -Url $fileUrl -Path $tempDownload -TimeoutSec 15
                if ((Get-Item -LiteralPath $tempDownload).Length -ne [long]$row.size -or (Get-RepairSha1 $tempDownload) -ne ([string]$row.sha1).ToLowerInvariant() -or -not (Test-GoodSyncBat $tempDownload)) { throw '[校验失败] 下载的更新入口与清单不一致，旧脚本已保留。' }
                $source=$tempDownload;break
            } catch { Write-Host '[修复] 此更新地址不可用或校验未通过，正在尝试下一地址。' }
        }
    } finally { if([IO.File]::Exists($manifestTemp)){Remove-Item -LiteralPath $manifestTemp -Force -ErrorAction SilentlyContinue} }
    if (-not $source -and $localSource -and -not $sawValidManifest) {
        $source=$localSource
        Write-Host '[离线修复] 更新源暂时不可用，使用本地完整修复包；联网后仍会检查更新。'
    }
    if (-not $source) { throw '[修复失败] 所有更新地址均未提供通过校验的入口；旧脚本已保留，请稍后重试。' }
    if ($sawValidManifest) { Write-Host '[修复] 更新入口已通过清单大小和哈希校验。' }
}

if (Install-Bat -Source $source -Dest $inner) {
    Write-Host '[修复] 已写入 _updater\Windows-sync.bat' -ForegroundColor Green
} else {
    Write-Host '[修复] _updater\Windows-sync.bat 已是好脚本'
}

$instanceCopy = Join-Path $instanceDir '更新mod-Windows端.bat'
if (Test-Path -LiteralPath $instanceCopy -PathType Leaf) {
    $copyBytes = [IO.File]::ReadAllBytes($instanceCopy)
    $copyText = [Text.Encoding]::UTF8.GetString($copyBytes)
    if ($copyText -notmatch '(?m)^# PORTABLE_ENTRY_SCRIPT\r?$' -and ($copyBytes.Length -gt 2000 -or $copyText -match 'PORTABLE_SYNC_BOOTSTRAPPED')) {
        if (Install-Bat -Source $source -Dest $instanceCopy) {
            Write-Host '[修复] 已写入实例目录的 更新mod-Windows端.bat' -ForegroundColor Green
        }
    }
}

if (-not (Test-GoodSyncBat $inner) -or (Get-RepairSha1 $inner) -ne (Get-RepairSha1 $source)) { throw '[校验失败] 修复后的入口复查未通过，请重试。' }

if ($tempDownload -and (Test-Path -LiteralPath $tempDownload -PathType Leaf)) {
    Remove-Item -LiteralPath $tempDownload -Force -ErrorAction SilentlyContinue
}

if ($repairLock) { $repairLock.Dispose();$repairLock=$null }
if ($NoLaunch) { exit 0 }

Write-Host '[修复] 完成，正在启动正常更新...'
$cmd = Join-Path $env:SystemRoot 'System32\cmd.exe'
& $cmd /d /c "`"$inner`""
exit $LASTEXITCODE

} catch {
    Write-Host (Format-RepairError $_) -ForegroundColor Red
    exit 1
} finally {
    if ($repairLock) { $repairLock.Dispose() }
    if ($tempDownload -and [IO.File]::Exists($tempDownload)) { Remove-Item -LiteralPath $tempDownload -Force -ErrorAction SilentlyContinue }
}

param(
    [string]$InstanceDir = '.',
    [string]$ManifestUrl = '',
    [switch]$Fix,
    [switch]$Deep,
    [switch]$QqSummary,
    [switch]$Quiet,
    [switch]$NoPause,
    # REPAIR_INTERACTIVE_V1: the one-click BAT asks before fixing instead of
    # requiring a -Fix command line. Also offers to restore kept personal edits
    # to the server version and to bring back files the last update replaced.
    [switch]$Interactive,
    [switch]$RestorePreserved,
    [switch]$RestoreLastUpdate
)

# 玩家客户端自助修复：对照清单检查 Java / 更新源 / 模组哈希，可选自动修补。
# 在玩家实例目录运行；可 -Fix 只重下异常文件。中文输出 + 诊断编号。
if ($Interactive) { $Deep = $true }  # 问答模式也检查配置，才能找出保留了个人修改的文件

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$explicitManifestUrl = $ManifestUrl
try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch {}
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false } catch {}

function Normalize-Rel([string]$Rel) { return ([string]$Rel).Replace('\', '/').TrimStart('/') }
function Normalize-RelKey([string]$Rel) { return (Normalize-Rel $Rel).ToLowerInvariant() }
function Get-Sha1([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return '' }
    $hash=[Security.Cryptography.SHA1]::Create();$stream=$null
    try { $stream=[IO.File]::OpenRead($Path);return ([BitConverter]::ToString($hash.ComputeHash($stream))).Replace('-','').ToLowerInvariant() }
    finally { if($stream){$stream.Dispose()};$hash.Dispose() }
}
function Test-RelativePathSafe([string]$Rel) {
    $n = Normalize-Rel $Rel
    if ([string]::IsNullOrWhiteSpace($n)) { return $false }
    if ($n.StartsWith('/') -or $n -match '^[A-Za-z]:') { return $false }
    foreach ($p in $n.Split('/')) { if ($p -eq '..') { return $false } }
    return $true
}
function Join-Safe([string]$Root, [string]$Rel) {
    if ([IO.Path]::IsPathRooted($Rel) -or $Rel -match ':' -or $Rel -match '(^|[\\/])\.\.?([\\/]|$)') { throw "[路径错误] 清单路径不安全：$Rel" }
    if (-not (Test-RelativePathSafe $Rel)) { throw "[路径错误] 清单路径不安全：$Rel" }
    $rootFull=[IO.Path]::GetFullPath($Root).TrimEnd('\','/')
    $target=[IO.Path]::GetFullPath((Join-Path $rootFull ($Rel.Replace('/',[IO.Path]::DirectorySeparatorChar))))
    if (-not $target.StartsWith($rootFull+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)) { throw "[路径错误] 文件越出实例目录：$Rel" }
    $cursor=$target
    while ($cursor -and $cursor.Length -ge $rootFull.Length) {
        if (Test-Path -LiteralPath $cursor) { if ([IO.File]::GetAttributes($cursor) -band [IO.FileAttributes]::ReparsePoint) { throw "[路径错误] 路径包含链接，已停止修复：$Rel" } }
        if ($cursor -eq $rootFull) { break };$cursor=Split-Path -Parent $cursor
    }
    return $target
}
function New-DirectHttpRequest([string]$Url, [int]$TimeoutSec = 30, [switch]$AllowProxy) {
    # 家宽和官方源都直连。系统代理下 Timeout 经常不生效，会卡数分钟。
    $request = [System.Net.HttpWebRequest]::Create($Url)
    if (-not $AllowProxy) { $request.Proxy = $null }
    $request.Timeout = [Math]::Max(1000, $TimeoutSec * 1000)
    $request.ReadWriteTimeout = [Math]::Max(1000, $TimeoutSec * 1000)
    $request.KeepAlive = $false
    try { $request.ServicePoint.Expect100Continue = $false } catch {}
    $request.UserAgent = $script:SyncUserAgent
    return $request
}
function Test-OfficialHttpsUrl([string]$Url) {
    if ([string]::IsNullOrWhiteSpace($Url)) { return $false }
    try { $uri = [Uri]$Url } catch { return $false }
    if (-not $uri.IsAbsoluteUri) { return $false }
    if ($uri.Scheme -ne 'https') { return $false }
    $h = [string]$uri.Host
    if ([string]::IsNullOrWhiteSpace($h)) { return $false }
    if ($h -eq 'localhost' -or $h -eq '127.0.0.1' -or $h -eq '::1') { return $false }
    return $true
}
function Get-OfficialFileUrls($File) {
    $out = New-Object System.Collections.Generic.List[string]
    $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    $candidates = New-Object System.Collections.Generic.List[string]
    if ($File -and $File.PSObject.Properties['url'] -and -not [string]::IsNullOrWhiteSpace([string]$File.url)) {
        [void]$candidates.Add(([string]$File.url).Trim())
    }
    if ($File -and $File.PSObject.Properties['downloads']) {
        foreach ($u in @($File.downloads)) {
            if (-not [string]::IsNullOrWhiteSpace([string]$u)) { [void]$candidates.Add(([string]$u).Trim()) }
        }
    }
    foreach ($u in $candidates) {
        if ((Test-OfficialHttpsUrl $u) -and $seen.Add($u)) { [void]$out.Add($u) }
    }
    return @($out)
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
$script:SyncUserAgent = "SCEX-updater/$($script:UpdaterVersion) (self-repair $($PSVersionTable.PSVersion.Major).$($PSVersionTable.PSVersion.Minor); windows)"

function Read-UrlText([string]$Url, [int]$TimeoutSec = 30) {
    $request = New-DirectHttpRequest -Url $Url -TimeoutSec $TimeoutSec
    $response = $null
    $reader = $null
    try {
        $response = $request.GetResponse()
        $reader = New-Object IO.StreamReader($response.GetResponseStream(), [Text.Encoding]::UTF8)
        return ($reader.ReadToEnd()).TrimStart([char]0xFEFF)
    } finally {
        if ($reader) { $reader.Dispose() }
        if ($response) { $response.Dispose() }
    }
}
function Save-UrlToFile([string]$Url, [string]$Path, [int]$TimeoutSec = 90, [switch]$AllowProxy) {
    $request = New-DirectHttpRequest -Url $Url -TimeoutSec $TimeoutSec -AllowProxy:$AllowProxy
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
function Download-FileChecked([string]$Url, [string]$Dest, [string]$ExpectedSha1, [switch]$AllowProxy, [int]$TimeoutSec = 90) {
    New-Item -ItemType Directory -Force (Split-Path -Parent $Dest) | Out-Null
    $tmp=Join-Path (Split-Path -Parent $Dest) ('.repair-'+[guid]::NewGuid().ToString('N')+'.tmp')
    try {
        try { Save-UrlToFile -Url $Url -Path $tmp -TimeoutSec $TimeoutSec -AllowProxy:$AllowProxy }
        catch { throw (Format-RepairError $_ $Dest '下载') }
        if ((Get-Sha1 $tmp) -ne $ExpectedSha1.ToLowerInvariant()) { throw "[校验失败] 下载内容与清单不一致：$Dest；原文件已保留。" }
        Install-RepairFile -Temporary $tmp -Destination $Dest
    } finally { if ([IO.File]::Exists($tmp)) { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue } }
}
function Download-RepairEntry($File, [string]$FileBaseUrl, [string]$Dest, [string]$ExpectedSha1, [string]$Rel) {
    foreach ($url in @(Get-OfficialFileUrls $File)) {
        try {
            Download-FileChecked -Url $url -Dest $Dest -ExpectedSha1 $ExpectedSha1 -TimeoutSec 8
            return
        } catch { if ($_.Exception.Message -match '^\[(文件占用|权限不足|空间不足|安装失败|路径错误)\]') { throw } }
    }
    $parts = ($Rel -replace '\\', '/').Split('/') | ForEach-Object { [Uri]::EscapeDataString($_) }
    Download-FileChecked -Url ($FileBaseUrl + ($parts -join '/')) -Dest $Dest -ExpectedSha1 $ExpectedSha1
}
function Strip-JsonComments([string]$Text) {
    # 去掉可能出现的 // 行注释（部分本地 manifest 带说明字段导致解析失败）
    $lines = $Text -split "`r?`n"
    $out = New-Object System.Collections.Generic.List[string]
    foreach ($line in $lines) {
        $t = $line
        # 不处理字符串内 // 的复杂情况；清单里注释多在键说明行
        if ($t -match '^\s*//') { continue }
        $out.Add($t) | Out-Null
    }
    return ($out -join "`n")
}
function Get-RequiredJavaMajor([string]$MinecraftVersion) {
    if ([string]::IsNullOrWhiteSpace($MinecraftVersion) -or
        $MinecraftVersion -notmatch '^(?<major>\d+)\.(?<minor>\d+)(?:\.(?<patch>\d+))?') {
        return 0
    }
    $major = [int]$matches.major
    $minor = [int]$matches.minor
    $patch = if ($matches.patch) { [int]$matches.patch } else { 0 }
    if ($major -ne 1) {
        if ($major -ge 26) { return 25 }
        return 0
    }
    if ($minor -le 16) { return 8 }
    if ($minor -le 19) { return 17 }
    if ($minor -eq 20 -and $patch -le 4) { return 17 }
    return 21
}
function Find-JavaInfo([int]$RequiredMajor = 0) {
    $info = [ordered]@{ found = $false; path = ''; version = ''; major = 0; requiredMajor = $RequiredMajor; compatible = $false }
    $candidates = @()
    if ($env:JAVA_HOME) { $candidates += (Join-Path $env:JAVA_HOME 'bin\java.exe') }
    $cmd = Get-Command java -EA SilentlyContinue
    if ($cmd) { $candidates += $cmd.Source }
    # 启动器托管 Java 和常见 JDK 安装目录。不能找到 PATH 里的第一份就停：
    # 很多电脑 PATH 仍是另一代 Java，但 HMCL/PCL 可能已经为目标 MC 版本准备了正确运行时。
    foreach ($javaRoot in @(
        (Join-Path $env:APPDATA '.hmcl\java'),
        (Join-Path $env:APPDATA '.minecraft\runtime'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Eclipse Adoptium'),
        (Join-Path $env:ProgramFiles 'Eclipse Adoptium'),
        (Join-Path $env:ProgramFiles 'Java'),
        (Join-Path $env:ProgramFiles 'Microsoft')
    )) {
        if (Test-Path -LiteralPath $javaRoot -PathType Container) {
            Get-ChildItem -LiteralPath $javaRoot -Recurse -File -Filter 'java.exe' -EA SilentlyContinue |
                Select-Object -First 20 | ForEach-Object { $candidates += $_.FullName }
        }
    }
    $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    foreach ($c in $candidates) {
        if (-not $c -or -not (Test-Path -LiteralPath $c -PathType Leaf)) { continue }
        $candidateFull = [IO.Path]::GetFullPath([string]$c)
        if (-not $seen.Add($candidateFull)) { continue }
        try {
            $psi = New-Object Diagnostics.ProcessStartInfo
            $psi.FileName = $candidateFull
            $psi.Arguments = '-version'
            $psi.RedirectStandardError = $true
            $psi.RedirectStandardOutput = $true
            $psi.UseShellExecute = $false
            $psi.CreateNoWindow = $true
            $p = [Diagnostics.Process]::Start($psi)
            $err = $p.StandardError.ReadToEnd()
            $out = $p.StandardOutput.ReadToEnd()
            [void]$p.WaitForExit(5000)
            $text = $err + $out
            if ($text -match 'version "([^"]+)"') {
                $found = [ordered]@{ found = $true; path = $candidateFull; version = $matches[1]; major = 0; requiredMajor = $RequiredMajor; compatible = $false }
                if ($found.version -match '^1\.(\d+)') { $found.major = [int]$matches[1] }
                elseif ($found.version -match '^(\d+)') { $found.major = [int]$matches[1] }
                $found.compatible = ($RequiredMajor -le 0 -or $found.major -eq $RequiredMajor)
                if ($found.compatible) { return $found }
                if (-not $info.found) { $info = $found }
            }
        } catch { }
    }
    return $info
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
    return "[修复失败] $Path。操作未完成，请把实例目录里的 _repair\latest\report.txt 发给管理员。"
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
function Read-YesNo([string]$Question) {
    Write-Host ''
    Write-Host ($Question + ' 输入 Y 后回车确认；直接回车跳过：') -ForegroundColor Yellow -NoNewline
    $answer = [Console]::ReadLine()
    return ($null -ne $answer -and $answer.Trim() -match '^(?i:y|yes|是|好)$')
}
function Get-LastUpdateBackup([string]$Root) {
    # Newest .portable-sync-backups\<stamp>; the updater keeps only one since 2.27.22.
    $backups = Join-Path $Root '.portable-sync-backups'
    if (-not [IO.Directory]::Exists($backups)) { return $null }
    return Get-ChildItem -LiteralPath $backups -Directory -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '^\d{8}-\d{6}' -and -not ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) } |
        Sort-Object Name -Descending | Select-Object -First 1
}
function Get-RestorableBackupFiles($Backup, [string]$Root) {
    # Only files the update replaced in place, never mods or updater scripts:
    # restoring a deleted old mod jar next to its new version would load both.
    if (-not $Backup) { return @() }
    $out = New-Object System.Collections.Generic.List[object]
    foreach ($file in @(Get-ChildItem -LiteralPath $Backup.FullName -Recurse -File -Force -ErrorAction SilentlyContinue)) {
        if ($file.Attributes -band [IO.FileAttributes]::ReparsePoint) { continue }
        $rel = Normalize-Rel $file.FullName.Substring($Backup.FullName.Length).TrimStart('\', '/')
        if (($rel -like 'mods/*') -or ($rel -like '_updater/*') -or (($rel -notmatch '/') -and ($rel -match '\.(bat|command|txt)$'))) { continue }
        $dest = Join-Safe $Root $rel
        if (-not [IO.File]::Exists($dest)) { continue }
        if ((Get-Sha1 $dest) -eq (Get-Sha1 $file.FullName)) { continue }
        [void]$out.Add([pscustomobject]@{ Rel = $rel; Source = $file.FullName; Destination = $dest })
    }
    return @($out.ToArray())
}
function Get-RepairMaskedUrl([string]$Url) {
    if ($Url -like 'file:*') { return '本地清单' }
    try { $uri=[Uri]$Url;return [regex]::Replace($uri.GetLeftPart([UriPartial]::Path), '(?<=^[A-Za-z][A-Za-z0-9+.-]*://[^/]+/)[^/]+(?=/)', '***') } catch { return '更新地址' }
}

$repairLock=$null
try {
$instanceRoot = if ([IO.Path]::IsPathRooted($InstanceDir)) {
    [IO.Path]::GetFullPath($InstanceDir)
} else {
    [IO.Path]::GetFullPath((Join-Path (Get-Location) $InstanceDir))
}
if (-not (Test-Path -LiteralPath $instanceRoot -PathType Container)) {
    throw "实例目录不存在: $instanceRoot"
}

$repairLock=Enter-RepairLock $instanceRoot
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
$diagId = 'RPR-' + $stamp + '-' + ([guid]::NewGuid().ToString('N').Substring(0, 6).ToUpperInvariant())
$outDir = Join-Path $instanceRoot ('_repair\' + $stamp)


$missing = New-Object System.Collections.Generic.List[object]
$mismatch = New-Object System.Collections.Generic.List[object]
$okCount = 0
$checked = 0
$errors = New-Object System.Collections.Generic.List[string]
$warnings = New-Object System.Collections.Generic.List[string]
$fixed = New-Object System.Collections.Generic.List[string]
$manifestUrl = ''
$manifestUrls = New-Object 'System.Collections.Generic.List[string]'
$packVersion = ''
$serverReachable = $false

# UPDATE-URL：支持多行候选地址，按顺序尝试；局域网用户可在公网地址失败后回落到 LAN。
$rawUrlValues = New-Object 'System.Collections.Generic.List[string]'
if (-not [string]::IsNullOrWhiteSpace($explicitManifestUrl)) { [void]$rawUrlValues.Add($explicitManifestUrl) }
else {
    foreach ($rel in @('UPDATE-URL.txt', 'PORTABLE-UPDATE-URL.txt', 'TFCR-update-url.txt', '_updater\UPDATE-URL.txt', '_updater\PORTABLE-UPDATE-URL.txt')) {
        $urlPath = Join-Path $instanceRoot $rel
        if (Test-Path -LiteralPath $urlPath -PathType Leaf) {
            try { [void]$rawUrlValues.Add((Get-Content -LiteralPath $urlPath -Raw -Encoding UTF8)) } catch { }
        }
    }
}
$seenManifestUrls = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
foreach ($rawUrls in $rawUrlValues) {
    foreach ($line in ([string]$rawUrls -split "`r?`n")) {
        $candidate = ([string]$line).Trim()
        if (-not [string]::IsNullOrWhiteSpace($candidate) -and -not $candidate.StartsWith('#') -and $seenManifestUrls.Add($candidate)) { [void]$manifestUrls.Add($candidate) }
    }
}

# 本地清单兜底
if ($manifestUrls.Count -eq 0) {
    $localMan = Join-Path $instanceRoot 'server-manifest.json'
    if (Test-Path $localMan) {
        $warnings.Add('未找到 UPDATE-URL.txt，改用本地 server-manifest.json（可能不是最新）') | Out-Null
        [void]$manifestUrls.Add('file:///' + ($localMan -replace '\\', '/'))
    } else {
        $errors.Add('找不到 UPDATE-URL.txt 与本地清单。请确认玩家包完整，或用 -ManifestUrl 指定。') | Out-Null
    }
}

$manifest = $null
$fileBaseUrl = ''
$manifestErrors = New-Object 'System.Collections.Generic.List[string]'
foreach ($candidateUrl in @($manifestUrls)) {
    if ($manifest -or $errors.Count -gt 0) { break }
    try {
        if ($candidateUrl -like 'file:///*') {
            $path = $candidateUrl -replace '^file:///', '' -replace '/', [IO.Path]::DirectorySeparatorChar
            $raw = [IO.File]::ReadAllText($path, [Text.Encoding]::UTF8)
            $candidateBaseUrl = ''
            $candidateReachable = $false
        } else {
            $raw = [Text.Encoding]::UTF8.GetString([byte[]](Get-CachedManifest -Url $candidateUrl -Root $instanceRoot -TimeoutSec 30 -UserAgent $script:SyncUserAgent).Raw).TrimStart([char]0xFEFF)
            $slash = $candidateUrl.LastIndexOf('/')
            $candidateBaseUrl = if ($slash -gt 0) { $candidateUrl.Substring(0, $slash + 1) } else { '' }
            $candidateReachable = $true
        }
        $raw = Strip-JsonComments $raw
        $candidateManifest = $raw | ConvertFrom-Json
        if ($null -eq $candidateManifest -or $null -eq $candidateManifest.files) { throw 'manifest 缺少 files。' }
        $manifest = $candidateManifest
        $manifestUrl = $candidateUrl
        $fileBaseUrl = $candidateBaseUrl
        $serverReachable = $candidateReachable
        if ($manifest.version) { $packVersion = [string]$manifest.version }
        if ($manifest.PSObject.Properties['updateUrl'] -and $manifest.updateUrl -and -not $fileBaseUrl) {
            $u = [string]$manifest.updateUrl
            $slash = $u.LastIndexOf('/')
            if ($slash -gt 0) { $fileBaseUrl = $u.Substring(0, $slash + 1) }
        }
    } catch {
        [void]$manifestErrors.Add('更新地址未返回有效清单')
    }
}
if (-not $manifest -and $errors.Count -eq 0) {
    $errors.Add('拉取/解析清单失败：已尝试 ' + $manifestUrls.Count + ' 个更新地址。' + (($manifestErrors -join '；'))) | Out-Null
    if ($manifestUrls.Count -gt 0) { $manifestUrl = [string]$manifestUrls[0] }
}

# Preserve the same customization policy as normal synchronization.
$stateFiles=@();$previousHashes=@{};$preserved=New-Object 'System.Collections.Generic.List[string]'
$preservedChanged=New-Object System.Collections.Generic.List[object]
$restoreTargets=New-Object System.Collections.Generic.List[object]
$restoredToServer=New-Object System.Collections.Generic.List[string]
$fixProblems=[bool]$Fix
$statePath=Join-Path $instanceRoot '.portable-sync-state.json'
if (Test-Path -LiteralPath $statePath -PathType Leaf) {
    try { $repairState=Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json;$stateFiles=@($repairState.files) } catch { [void]$warnings.Add('同步状态无法读取；不会覆盖符合保留规则的本地修改。') }
}
foreach($entry in $stateFiles){if($entry.path -and $entry.sha1){$previousHashes[(Normalize-RelKey ([string]$entry.path))]=[string]$entry.sha1}}
if ($Fix -and $manifest) {
    $probePaths=New-Object 'System.Collections.Generic.List[string]'
    foreach($entry in (@($manifest.files)+@($stateFiles))) {
        if (-not $entry.path) { continue }
        $rel=[string]$entry.path
        if ($rel -match '\.(bat|command)$') { continue }
        [void]$probePaths.Add((Join-Safe $instanceRoot $rel))
    }
    Assert-RepairPathsAvailable $probePaths
}

# Java：需求随清单里的 Minecraft 版本变化，不把某个私服版本写死在公版逻辑里。
$minecraftVersion = if ($manifest -and $manifest.PSObject.Properties['minecraftVersion']) { [string]$manifest.minecraftVersion } else { '' }
$requiredJavaMajor = Get-RequiredJavaMajor $minecraftVersion
$java = Find-JavaInfo $requiredJavaMajor
if (-not $java.found) {
    if ($requiredJavaMajor -gt 0) {
        $warnings.Add(("未检测到 Java。Minecraft {0} 需要 Java {1}；请优先使用启动器自动下载的 Java。" -f $minecraftVersion, $requiredJavaMajor)) | Out-Null
    } else {
        $warnings.Add('未检测到 Java，且清单未提供可识别的 Minecraft 版本，无法自动判断所需主版本。') | Out-Null
    }
} elseif (-not $java.compatible) {
    $warnings.Add(("Java 版本不匹配：{0}（Minecraft {1} 需要 Java {2}）" -f $java.version, $minecraftVersion, $requiredJavaMajor)) | Out-Null
}

# 对照清单（默认只查 mods/** 与 _updater/**；Deep 查全部非保留类）
if ($manifest -and $manifest.files) {
    $files = @($manifest.files)
    foreach ($f in $files) {
        if (-not $f.path -or -not $f.sha1) { continue }
        $rel = [string]$f.path
        $key = Normalize-Rel $rel
        $isMod = $key -like 'mods/*'
        $isUpdater = $key -like '_updater/*'
        if (-not $Deep -and -not $isMod -and -not $isUpdater) { continue }
        # 跳过玩家个人向
        $forceSync=Test-RepairGlob $key (Get-RepairProperty $manifest 'forceSyncGlobs' @())
        if ($key -match '^(saves/|screenshots/)' -or (-not $forceSync -and $key -match '^(options\.txt|servers\.dat)$')) { continue }

        $checked++
        $full = Join-Safe $instanceRoot $rel
        $expected = ([string]$f.sha1).ToLowerInvariant()
        # 支持把整套更新器作为实例根目录下的子文件夹保存。
        # 此时 manifest 中的 _updater/* 不在实例根目录，但当前诊断脚本旁边已有同一文件；
        # 只把它作为校验候选，不复制、不移动，也不改变 InstanceDir 的边界。
        if ($isUpdater -and -not (Test-Path -LiteralPath $full -PathType Leaf)) {
            $bundledUpdaterFile = Join-Path $PSScriptRoot ([IO.Path]::GetFileName($key))
            if (Test-Path -LiteralPath $bundledUpdaterFile -PathType Leaf) { $full = $bundledUpdaterFile }
        }
        $exists=Test-Path -LiteralPath $full -PathType Leaf
        $previous=[string]$previousHashes[(Normalize-RelKey $key)]
        $preserveCustom=[bool](Get-RepairProperty $manifest 'preservePlayerCustomizations' $true)
        if ($preserveCustom -and -not $forceSync) {
            if (-not $exists -and $previous -and (Test-RepairGlob $key (Get-RepairProperty $manifest 'preserveLocalDeletionGlobs' @()))) {
                [void]$preserved.Add($rel);continue
            }
            if ($exists -and (Test-RepairGlob $key (Get-RepairProperty $manifest 'preserveLocalChangeGlobs' @()))) {
                $local=Get-Sha1 $full
                if ($local -ne $expected -and (-not $previous -or $local -ne $previous)) {
                    [void]$preserved.Add($rel)
                    [void]$preservedChanged.Add([pscustomobject]@{ path = $rel; sha1 = $expected })
                    continue
                }
            }
        }
        if (-not (Test-Path -LiteralPath $full -PathType Leaf)) {
            $missing.Add([pscustomobject]@{ path = $rel; sha1 = $expected; size = $f.size }) | Out-Null
            continue
        }
        $actual = Get-Sha1 $full
        if ($actual -ne $expected) {
            $mismatch.Add([pscustomobject]@{ path = $rel; expected = $expected; actual = $actual }) | Out-Null
        } else {
            $okCount++
        }
    }
}

# 本地 mods 多余（相对清单）
$extraMods = @()
$modsDir = Join-Path $instanceRoot 'mods'
if ((Test-Path $modsDir) -and $manifest -and $manifest.files) {
    $manifestMods = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    foreach ($f in @($manifest.files)) {
        $r = Normalize-Rel ([string]$f.path)
        if ($r -like 'mods/*') { [void]$manifestMods.Add($r) }
    }
    Get-ChildItem $modsDir -File -Filter '*.jar' -EA SilentlyContinue | ForEach-Object {
        $rel = 'mods/' + $_.Name
        # 允许中文名前缀 jar：用相对路径
        $rel2 = Normalize-Rel (('mods/' + $_.Name))
        $found = $false
        foreach ($m in $manifestMods) {
            if ([IO.Path]::GetFileName($m) -eq $_.Name) { $found = $true; break }
        }
        if (-not $found) { $extraMods += $_.Name }
    }
}

# Interactive questions (one-click BAT): fix problems, then restore kept personal edits.
if ($Interactive -and $manifest) {
    if ($missing.Count + $mismatch.Count -gt 0) {
        Write-Host ''
        Write-Host ('检查完成：{0} 个文件缺失，{1} 个文件损坏或版本不对。' -f $missing.Count, $mismatch.Count) -ForegroundColor Yellow
        foreach ($m in (@($missing) + @($mismatch) | Select-Object -First 8)) { Write-Host ('  · ' + $m.path) }
        if (-not $fileBaseUrl) { Write-Host '更新源暂时连不上，现在无法重新下载；请稍后再运行一次。' -ForegroundColor Yellow }
        elseif (Read-YesNo '是否现在重新下载这些文件？原文件会先备份。') { $fixProblems = $true }
    } else {
        Write-Host ''
        Write-Host '检查完成：模组、配置和更新器文件都和服务器一致。' -ForegroundColor Green
    }
    if ($preservedChanged.Count -gt 0 -and $fileBaseUrl) {
        Write-Host ''
        Write-Host ('有 {0} 个文件保留了你的个人修改，和服务器版本不同：' -f $preservedChanged.Count)
        foreach ($p in ($preservedChanged | Select-Object -First 10)) { Write-Host ('  · ' + $p.path) }
        Write-Host '如果某个模组出现异常，可以把这些文件恢复为服务器版本；不确定时选跳过。'
        if (Read-YesNo '是否把它们恢复为服务器版本？你的版本会先备份到 _repair 目录。') { foreach ($p in $preservedChanged) { [void]$restoreTargets.Add($p) } }
    }
}
if ($RestorePreserved) { foreach ($p in $preservedChanged) { [void]$restoreTargets.Add($p) } }

# Fix
if ($fileBaseUrl -and (($fixProblems -and ($missing.Count -gt 0 -or $mismatch.Count -gt 0)) -or $restoreTargets.Count -gt 0)) {
    $fileByPath = @{}
    if ($manifest -and $manifest.files) {
        foreach ($f in @($manifest.files)) {
            if ($f.path) { $fileByPath[(Normalize-Rel ([string]$f.path))] = $f }
        }
    }
    $toFix = @()
    if ($fixProblems) {
        foreach ($m in $missing) { $toFix += [pscustomobject]@{ path = $m.path; sha1 = $m.sha1; restore = $false } }
        foreach ($m in $mismatch) { $toFix += [pscustomobject]@{ path = $m.path; sha1 = $m.expected; restore = $false } }
    }
    foreach ($m in $restoreTargets) { $toFix += [pscustomobject]@{ path = $m.path; sha1 = $m.sha1; restore = $true } }
    if (-not $Fix) { Assert-RepairPathsAvailable @($toFix | ForEach-Object { Join-Safe $instanceRoot ([string]$_.path) }) }
    foreach ($item in $toFix) {
        try {
            $rel = [string]$item.path
            $dest = Join-Safe $instanceRoot $rel
            if (Test-Path $dest) {
                $bak = Join-Safe (Join-Path $outDir 'backup') $rel
                New-Item -ItemType Directory -Force (Split-Path $bak) | Out-Null
                Copy-Item $dest $bak -Force
            }
            $src = $fileByPath[(Normalize-Rel $rel)]
            Download-RepairEntry -File $src -FileBaseUrl $fileBaseUrl -Dest $dest -ExpectedSha1 $item.sha1 -Rel $rel
            if ($item.restore) { $restoredToServer.Add($rel) | Out-Null } else { $fixed.Add($rel) | Out-Null }
        } catch {
            $errors.Add((Format-RepairError $_ ([string]$item.path))) | Out-Null
        }
    }
}

# Recheck actual repaired bytes before reporting remaining problems.
if ($fixProblems) {
    foreach($item in @($missing.ToArray())) {
        if ((Get-Sha1 (Join-Safe $instanceRoot ([string]$item.path))) -eq $item.sha1) { [void]$missing.Remove($item);$okCount++ }
    }
    foreach($item in @($mismatch.ToArray())) {
        if ((Get-Sha1 (Join-Safe $instanceRoot ([string]$item.path))) -eq $item.expected) { [void]$mismatch.Remove($item);$okCount++ }
    }
}

# 评分
$issueN = $missing.Count + $mismatch.Count + $errors.Count
$level = '绿'
if ($issueN -gt 0 -or $warnings.Count -gt 0) { $level = '黄' }
if ($missing.Count -gt 5 -or $mismatch.Count -gt 5 -or $errors.Count -gt 0) { $level = '红' }
if (-not $serverReachable -and $manifestUrl -notlike 'file:*') { if ($level -eq '绿') { $level = '黄' } }

$sb = New-Object Text.StringBuilder
[void]$sb.AppendLine('【客户端自助修复】')
[void]$sb.AppendLine(('诊断编号：{0}' -f $diagId))
[void]$sb.AppendLine(('实例：{0}' -f $instanceRoot))
if ($packVersion) { [void]$sb.AppendLine(('清单版本：{0}' -f $packVersion)) }
[void]$sb.AppendLine(('更新源：{0}' -f $(if ($serverReachable) { '可达' } elseif ($manifestUrl -like 'file:*') { '本地清单' } else { '不可达/未知' })))
if ($java.found) {
    $javaStatus = if ($requiredJavaMajor -le 0) { '清单版本未知，未判定' } elseif ($java.compatible) { 'OK' } else { "需 Java $requiredJavaMajor" }
    [void]$sb.AppendLine(('Java：{0}（主版本 {1}）{2}' -f $java.version, $java.major, $javaStatus))
} else {
    [void]$sb.AppendLine('Java：未检测到')
}
[void]$sb.AppendLine(('检查文件：{0} · 正常 {1} · 缺失 {2} · 哈希不符 {3}' -f $checked, $okCount, $missing.Count, $mismatch.Count))
if ($preserved.Count -gt 0) { [void]$sb.AppendLine(('按清单保留个人修改或删除：{0} 个文件' -f $preserved.Count)) }
if ($extraMods.Count -gt 0) {
    [void]$sb.AppendLine(('本地多余模组 jar：{0} 个（未强制删除）' -f $extraMods.Count))
    $show = $extraMods | Select-Object -First 8
    foreach ($e in $show) { [void]$sb.AppendLine(('  · {0}' -f $e)) }
}
if ($missing.Count -gt 0) {
    [void]$sb.AppendLine('缺失（最多列 12）：')
    foreach ($m in ($missing | Select-Object -First 12)) { [void]$sb.AppendLine(('  · {0}' -f $m.path)) }
}
if ($mismatch.Count -gt 0) {
    [void]$sb.AppendLine('损坏/版本不对（最多列 12）：')
    foreach ($m in ($mismatch | Select-Object -First 12)) { [void]$sb.AppendLine(('  · {0}' -f $m.path)) }
}
if ($fixed.Count -gt 0) {
    [void]$sb.AppendLine(('已自动修补：{0} 个文件' -f $fixed.Count))
}
if ($restoredToServer.Count -gt 0) {
    [void]$sb.AppendLine(('已恢复为服务器版本：{0} 个文件（你的版本已备份到报告目录 backup）' -f $restoredToServer.Count))
}
foreach ($e in $errors) { [void]$sb.AppendLine(('错误：' + $e)) }
foreach ($w in $warnings) { [void]$sb.AppendLine(('提示：' + $w)) }
[void]$sb.AppendLine(('风险灯：{0}' -f $level))
if (-not $fixProblems -and ($missing.Count -gt 0 -or $mismatch.Count -gt 0)) {
    [void]$sb.AppendLine('建议：再双击「一键客户端自助修复.bat」并选择修复，或双击「更新mod-Windows端.bat」完整同步。')
}
[void]$sb.AppendLine(('报告目录：_repair\{0}' -f $stamp))
[void]$sb.AppendLine('把诊断编号发给管理员可加快协助。')
$qqText = $sb.ToString().TrimEnd()

$meta = [ordered]@{
    diagId = $diagId
    instanceRoot = $instanceRoot
    packVersion = $packVersion
    minecraftVersion = $minecraftVersion
    requiredJavaMajor = $requiredJavaMajor
    manifestUrl = Get-RepairMaskedUrl $manifestUrl
    preserved = @($preserved.ToArray())
    serverReachable = $serverReachable
    java = $java
    checked = $checked
    okCount = $okCount
    missing = @($missing | ForEach-Object { $_.path })
    mismatch = @($mismatch | ForEach-Object { $_.path })
    extraMods = @($extraMods)
    fixed = @($fixed)
    restoredToServer = @($restoredToServer)
    level = $level
    fixMode = [bool]$fixProblems
    generatedAt = (Get-Date).ToString('o')
}
New-Item -ItemType Directory -Path $outDir -Force | Out-Null
$enc = New-Object Text.UTF8Encoding $true
[IO.File]::WriteAllText((Join-Path $outDir 'report.txt'), $qqText, $enc)
[IO.File]::WriteAllText((Join-Path $outDir 'report-qq.txt'), $qqText, $enc)
[IO.File]::WriteAllText((Join-Path $outDir 'meta.json'), ($meta | ConvertTo-Json -Depth 6), [Text.UTF8Encoding]::new($false))
# latest shortcut
$latest = Join-Path $instanceRoot '_repair\latest'
New-Item -ItemType Directory -Path $latest -Force | Out-Null
Copy-Item (Join-Path $outDir 'report.txt') (Join-Path $latest 'report.txt') -Force
Copy-Item (Join-Path $outDir 'meta.json') (Join-Path $latest 'meta.json') -Force

if ($QqSummary) { Write-Output $qqText }
elseif (-not $Quiet) {
    Write-Host $qqText
    Write-Host ''
    Write-Host ("完整报告: " + (Join-Path $outDir 'report.txt')) -ForegroundColor Cyan
}

# Bring back files the last update replaced (configs and the like; never mods).
if ($Interactive -or $RestoreLastUpdate) {
    $lastBackup = Get-LastUpdateBackup $instanceRoot
    $restorable = @(Get-RestorableBackupFiles $lastBackup $instanceRoot)
    if ($restorable.Count -gt 0) {
        $doRestore = [bool]$RestoreLastUpdate
        if (-not $doRestore) {
            Write-Host ''
            Write-Host ('上次更新（{0}）替换了 {1} 个配置等文件（不含模组）：' -f $lastBackup.Name, $restorable.Count)
            foreach ($item in ($restorable | Select-Object -First 8)) { Write-Host ('  · ' + $item.Rel) }
            Write-Host '一般不需要恢复，只在想找回被更新覆盖的个人配置时使用。服务器要求统一的文件，下次更新时仍会更新。'
            $doRestore = Read-YesNo '是否把这些文件恢复为更新前的版本？当前版本会先备份。'
        }
        if ($doRestore) {
            Assert-RepairPathsAvailable @($restorable | ForEach-Object { $_.Destination })
            $restoredCount = 0
            foreach ($item in $restorable) {
                $tmp = Join-Path (Split-Path -Parent $item.Destination) ('.repair-' + [guid]::NewGuid().ToString('N') + '.tmp')
                try {
                    $bak = Join-Safe (Join-Path $outDir 'backup') $item.Rel
                    New-Item -ItemType Directory -Force (Split-Path $bak) | Out-Null
                    Copy-Item -LiteralPath $item.Destination -Destination $bak -Force
                    Copy-Item -LiteralPath $item.Source -Destination $tmp -Force
                    Install-RepairFile -Temporary $tmp -Destination $item.Destination
                    $restoredCount++
                } catch { Write-Host (Format-RepairError $_ $item.Rel) -ForegroundColor Red }
                finally { if ([IO.File]::Exists($tmp)) { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue } }
            }
            Write-Host ('已恢复 {0} 个文件；当前版本备份在 {1}' -f $restoredCount, (Join-Path $outDir 'backup')) -ForegroundColor Green
        }
    } elseif ($RestoreLastUpdate) {
        Write-Host '没有找到可以恢复的上次更新备份。'
    }
}

if (-not $NoPause -and -not $QqSummary -and -not $Quiet) {
    try { Write-Host '按 Enter 关闭...'; [void][Console]::ReadLine() } catch { }
}

if ($level -eq '红' -or $errors.Count -gt 0) { exit 2 }
if ($level -eq '黄') { exit 1 }
exit 0

} catch {
    Write-Host (Format-RepairError $_) -ForegroundColor Red
    exit 2
} finally { if ($repairLock) { $repairLock.Dispose() } }

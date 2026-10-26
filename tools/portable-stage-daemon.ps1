<#
    portable-stage-daemon.ps1 —— 客户端后台"暂存 daemon"

    目的：把整合包更新的下载耗时，从"下次启动前干等"提前到"玩家在线正在游戏时后台悄悄拉"。
          管理员在线发布更新后，本进程后台把变更文件下到 .portable-staging/，玩家下次重启游戏时
          由主更新器按最新清单验证缓存，再执行备份、安装与对账。

    设计铁律（务必保持）：
      1) 只写 .portable-staging/，绝不改运行中被 JVM 锁住的 mods/*.jar 等文件——热覆盖在 Windows 上必失败。
      2) player-update-generic.ps1 仍是唯一权威对账器。暂存只是"预下载"，即使暂存过期/残缺也无害：
         旧 Promote 入口仅提示，不落位或清理。Watch 只改暂存区，与主同步共用实例锁。
      3) 只在 manifest 文本变化时才做文件 diff；idle 轮询近乎零开销（一次 GET + 字符串比较）。
      4) 生命周期自管：游戏由 PCL/HMCL 独立进程启动，本 daemon 靠"检测 java 进程 + 最大时长 + 单例锁"自退，
         不残留后台进程。
      5) Windows PowerShell 5.1 使用 UTF-8 BOM 识别中文；CRLF 或 LF 均可。
#>
param(
    [string]$InstanceDir = ".",
    [string]$ManifestUrl = "",
    [ValidateSet("Watch", "Promote")]
    [string]$Mode = "Watch",
    [int]$IntervalSeconds = 30,
    [int]$MaxRuntimeMinutes = 720,
    [int]$GraceMinutes = 15,
    [int]$IdleExitChecks = 3,
    [switch]$Toast
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch {}
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false); $OutputEncoding = [Console]::OutputEncoding } catch {}

function Resolve-FullPath { param([string]$Path) return [System.IO.Path]::GetFullPath($Path) }
function Normalize-Rel { param([string]$Rel) return ([string]$Rel).Replace('\', '/').TrimStart('/') }

function Test-RelativePathSafe {
    param([string]$Rel)
    $n = Normalize-Rel $Rel
    if ([string]::IsNullOrWhiteSpace($n)) { return $false }
    if ($n.StartsWith('/') -or $n -match '^[A-Za-z]:') { return $false }
    foreach ($part in $n.Split('/')) { if ($part -eq '..' -or $part -eq '.') { return $false } }
    return $true
}

function Join-SafeRel {
    param([string]$Root, [string]$Rel)
    if (-not (Test-RelativePathSafe -Rel $Rel)) { throw "Unsafe path: $Rel" }
    return Join-Path $Root ((Normalize-Rel $Rel).Replace('/', '\'))
}

function Get-Sha1 {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return "" }
    $hash = [System.Security.Cryptography.SHA1]::Create()
    $stream = $null
    try {
        $stream = [System.IO.File]::OpenRead($Path)
        return ([System.BitConverter]::ToString($hash.ComputeHash($stream))).Replace('-', '').ToLowerInvariant()
    } finally {
        if ($stream) { $stream.Dispose() }
        $hash.Dispose()
    }
}

function Test-GlobMatch {
    param([string]$Rel, $Globs)
    $n = Normalize-Rel $Rel
    foreach ($g in @($Globs)) {
        $gg = Normalize-Rel ([string]$g)
        if ([string]::IsNullOrWhiteSpace($gg)) { continue }
        if ($n -like $gg) { return $true }
    }
    return $false
}

function Read-UpdateUrls {
    param([string]$Root, [string]$Override)
    $urls = New-Object 'System.Collections.Generic.List[string]'
    $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    $add = {
        param([string]$Value)
        $v = ([string]$Value).Trim()
        if (-not [string]::IsNullOrWhiteSpace($v) -and -not $v.StartsWith('#') -and $seen.Add($v)) { [void]$urls.Add($v) }
    }
    if (-not [string]::IsNullOrWhiteSpace($Override)) {
        & $add $Override
        return @($urls.ToArray())
    }
    # TFCR-update-url.txt 是旧周目玩家包留下的文件名，老玩家实例里还有——请勿删除。
    $primary = @('UPDATE-URL.txt', 'PORTABLE-UPDATE-URL.txt', 'TFCR-update-url.txt', '_updater\UPDATE-URL.txt', '_updater\PORTABLE-UPDATE-URL.txt')
    foreach ($rel in $primary) {
        $p = Join-Path $Root $rel
        if (-not (Test-Path -LiteralPath $p -PathType Leaf)) { continue }
        try {
            foreach ($line in ((Get-Content -LiteralPath $p -Encoding UTF8 -ErrorAction Stop) -split "`r?`n")) { & $add $line }
        } catch {}
    }
    return @($urls.ToArray())
}

function Read-UpdateUrl {
    param([string]$Root, [string]$Override)
    $urls = @(Read-UpdateUrls -Root $Root -Override $Override)
    if ($urls.Count -gt 0) { return [string]$urls[0] }
    return ""
}

# URL_RULES_V1: keep identical to player-update-generic.ps1 (tests/fixtures/url-order-cases.json).
function Test-LocalOrPrivateHost {
    param([string]$HostName)
    $h = ([string]$HostName).Trim().Trim('[', ']').ToLowerInvariant().TrimEnd('.')
    if ([string]::IsNullOrWhiteSpace($h) -or $h -eq 'localhost' -or $h.EndsWith('.local')) { return $true }
    if ($h -notmatch '^\d{1,3}(\.\d{1,3}){3}$' -and -not $h.Contains(':')) { return $false }
    $ip = $null
    if (-not [Net.IPAddress]::TryParse(($h -split '%')[0], [ref]$ip)) { return $false }
    if ($ip.IsIPv4MappedToIPv6) { $ip = $ip.MapToIPv4() }
    $b = $ip.GetAddressBytes()
    if ($ip.AddressFamily -eq [Net.Sockets.AddressFamily]::InterNetwork) {
        return ($b[0] -eq 10 -or $b[0] -eq 127 -or ($b[0] -eq 172 -and $b[1] -ge 16 -and $b[1] -le 31) -or ($b[0] -eq 192 -and $b[1] -eq 168) -or ($b[0] -eq 169 -and $b[1] -eq 254))
    }
    return ($ip.Equals([Net.IPAddress]::IPv6Loopback) -or (($b[0] -band 0xFE) -eq 0xFC) -or ($b[0] -eq 0xFE -and ($b[1] -band 0xC0) -eq 0x80))
}

function Test-PrivateUpdateUrl {
    param([string]$Url)
    try { return (Test-LocalOrPrivateHost ([Uri]$Url).Host) } catch { return $false }
}

function Test-UsableUpdateUrl {
    param([string]$Url)
    $uri = $null
    if (-not [Uri]::TryCreate(([string]$Url).Trim(), [UriKind]::Absolute, [ref]$uri)) { return $false }
    return ($uri.Scheme -in @('http', 'https') -and -not [string]::IsNullOrEmpty($uri.Host))
}

function Get-OrderedUpdateUrls {
    # Same order as the main updater: the first public URL,
    # then listed LAN URLs in file order. LastGood cannot reorder.
    param([string[]]$Urls, [string]$LastGood = '')
    $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    $out = New-Object System.Collections.Generic.List[string]
    $usable = @($Urls | ForEach-Object { ([string]$_).Trim() } | Where-Object { (Test-UsableUpdateUrl $_) })
    $public = @($usable | Where-Object { -not (Test-PrivateUpdateUrl $_) })
    foreach ($u in (@($public | Select-Object -First 1) + @($usable | Where-Object { Test-PrivateUpdateUrl $_ }))) {
        if ($seen.Add($u)) { [void]$out.Add($u) }
    }
    return @($out.ToArray())
}

function Test-ManifestUrl {
    param([string]$Url, [int]$TimeoutSec = 8)
    return ($null -ne (Get-ManifestProbe -Url $Url -TimeoutSec $TimeoutSec))
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
$script:SyncUserAgent = "SCEX-updater/$($script:UpdaterVersion) (stage-powershell $($PSVersionTable.PSVersion.Major).$($PSVersionTable.PSVersion.Minor); windows)"

function Get-ManifestProbe {
    # Probe once and require a JSON manifest; the first watch round reuses this download.
    param([string]$Url, [int]$TimeoutSec = 8)
    try {
        $trusted = Get-CachedManifest -Url $Url -Root $script:TrustRoot -TimeoutSec $TimeoutSec -UserAgent $script:SyncUserAgent
        $text = [Text.Encoding]::UTF8.GetString([byte[]]$trusted.Raw).TrimStart([char]0xFEFF)
        $manifest = $text | ConvertFrom-Json
        if ($null -eq $manifest -or $null -eq $manifest.files) { return $null }
        return [pscustomobject]@{ Text = $text; LastModified = $null }
    } catch {
        return $null
    }
}

function Join-UrlPath {
    param([string]$BaseUrl, [string]$Rel)
    $parts = (Normalize-Rel $Rel).Split('/') | ForEach-Object { [Uri]::EscapeDataString($_) }
    return $BaseUrl + (($parts) -join '/')
}

function Write-Utf8NoBom {
    param([string]$Path, [string]$Value)
    New-Item -ItemType Directory -Force (Split-Path -Parent $Path) | Out-Null
    [System.IO.File]::WriteAllText($Path, $Value, (New-Object System.Text.UTF8Encoding($false)))
}

function Write-Log {
    param([string]$Message)
    $line = ("[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $Message)
    Write-Host $line
    try { Add-Content -LiteralPath $script:LogPath -Value ($line + "`r`n") -Encoding UTF8 -ErrorAction SilentlyContinue } catch {}
}

function Test-OfficialHttpsUrl {
    param([string]$Url)
    if ([string]::IsNullOrWhiteSpace($Url)) { return $false }
    try { $uri = [Uri]$Url } catch { return $false }
    if (-not $uri.IsAbsoluteUri) { return $false }
    if ($uri.Scheme -ne 'https') { return $false }
    $h = [string]$uri.Host
    if ([string]::IsNullOrWhiteSpace($h)) { return $false }
    if ($h -eq 'localhost' -or $h -eq '127.0.0.1' -or $h -eq '::1') { return $false }
    return $true
}

function Get-OfficialFileUrls {
    param($File)
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

function Get-DownloadFailReason {
    param($Exception)
    $msg = [string]$Exception.Message
    if ($msg -match 'SHA1') { return '哈希不符' }
    if ($msg -match 'too slow|太慢') { return '太慢' }
    if ($msg -match 'timed out|Timeout|超时') { return '超时' }
    if ($msg -match '\(404\)') { return '404' }
    if ($msg -match '\(403\)') { return '403' }
    return '连接失败'
}

$script:StageProxyUrl = $null
$script:StageProxyResolved = $false

function Get-StageProxyUrl {
    if ($script:StageProxyResolved) { return [string]$script:StageProxyUrl }
    $script:StageProxyResolved = $true
    $script:StageProxyUrl = ''
    $flag = ([string][Environment]::GetEnvironmentVariable('PORTABLE_SYNC_NOPROXY')).Trim().ToLowerInvariant()
    if ($flag -in @('1', 'true', 'yes', 'on')) { return '' }
    foreach ($key in @('PORTABLE_SYNC_PROXY', 'HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy')) {
        $raw = ([string][Environment]::GetEnvironmentVariable($key)).Trim()
        if ([string]::IsNullOrWhiteSpace($raw)) { continue }
        if ($raw.StartsWith('socks', [StringComparison]::OrdinalIgnoreCase)) { continue }
        if ($raw -notmatch '^[A-Za-z][A-Za-z0-9+.-]*://') { $raw = 'http://' + $raw }
        $script:StageProxyUrl = $raw
        return $script:StageProxyUrl
    }
    try {
        $ie = Get-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -ErrorAction Stop
        if ([int]$ie.ProxyEnable -eq 1 -and -not [string]::IsNullOrWhiteSpace([string]$ie.ProxyServer)) {
            $server = [string]$ie.ProxyServer
            if ($server -notmatch '=') { $script:StageProxyUrl = $(if ($server -match '://') { $server } else { 'http://' + $server }) }
        }
    } catch {}
    return [string]$script:StageProxyUrl
}

function Test-StageShouldUseProxy {
    param([string]$Url)
    $proxy = Get-StageProxyUrl
    if ([string]::IsNullOrWhiteSpace($proxy)) { return $false }
    try { $h = ([Uri]$Url).Host.ToLowerInvariant() } catch { return $false }
    if ($h -in @('localhost', '127.0.0.1', '::1')) { return $false }
    if ($h.StartsWith('192.168.') -or $h.StartsWith('10.')) { return $false }
    return $true
}

function Save-UrlToFile {
    param([Parameter(Mandatory = $true)][string]$Url, [Parameter(Mandatory = $true)][string]$Path, [int]$TimeoutSec = 120, [switch]$AllowProxy, [int]$MinBytesAfterTimeout = 0)
    $request = [System.Net.HttpWebRequest]::Create($Url)
    if ($AllowProxy) {
        $proxy = Get-StageProxyUrl
        if ($proxy) { $request.Proxy = New-Object System.Net.WebProxy($proxy) } else { $request.Proxy = $null }
    } else {
        $request.Proxy = $null
    }
    $request.Timeout = [Math]::Max(1000, $TimeoutSec * 1000)
    $request.ReadWriteTimeout = [Math]::Max(1000, $TimeoutSec * 1000)
    $request.KeepAlive = $false
    try { $request.ServicePoint.Expect100Continue = $false } catch {}
    $request.UserAgent = $script:SyncUserAgent
    $response = $null
    $inputStream = $null
    $outputStream = $null
    try {
        $response = $request.GetResponse()
        $inputStream = $response.GetResponseStream()
        $outputStream = [System.IO.File]::Open($Path, [System.IO.FileMode]::Create, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
        $buffer = New-Object byte[] 65536
        $got = [long]0
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        $nextCheck = [double]$TimeoutSec
        $minRate = 0
        if ($MinBytesAfterTimeout -gt 0) { $minRate = 128KB }
        while (($n = $inputStream.Read($buffer, 0, $buffer.Length)) -gt 0) {
            $outputStream.Write($buffer, 0, $n)
            $got += $n
            if ($nextCheck -le 0) { continue }
            $elapsed = $sw.Elapsed.TotalSeconds
            if ($elapsed -lt $nextCheck) { continue }
            if ($minRate -gt 0 -and $got -lt ($minRate * $elapsed)) { throw 'official source too slow' }
            if ($MinBytesAfterTimeout -gt 0 -and $got -lt $MinBytesAfterTimeout) { throw 'official source too slow' }
            $nextCheck = $elapsed + [Math]::Max(1, $TimeoutSec)
        }
    } catch {
        try { $request.Abort() } catch {}
        throw
    } finally {
        if ($outputStream) { $outputStream.Dispose() }
        if ($inputStream) { $inputStream.Dispose() }
        if ($response) { $response.Dispose() }
    }
}


function Enter-SyncLock {
    param([string]$Root)
    try { return [IO.File]::Open((Join-Path $Root '.portable-sync.lock'), [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None) }
    catch {
        $e = $_.Exception
        while ($e) {
            if (($e.HResult -band 0xffff) -in @(32,33)) { return $null }
            $e = $e.InnerException
        }
        throw
    }
}

# --- 解析实例目录与暂存区 -------------------------------------------------
$instanceRoot = Resolve-FullPath $InstanceDir
$script:TrustRoot = $instanceRoot
$stageRoot = Join-Path $instanceRoot '.portable-staging'
$readyPath = Join-Path $stageRoot 'READY.json'
$noticePath = Join-Path $instanceRoot '更新已就绪.txt'
$lockPath = Join-Path $stageRoot 'daemon.lock'
$script:LogPath = Join-Path $stageRoot 'daemon.log'

# ==========================================================================
# Promote remains a no-op for old launchers. The main updater verifies and installs cache.
# ==========================================================================
if ($Mode -eq 'Promote') {
    Write-Host '[暂存] 缓存将由主更新器按最新清单校验后安装；此步骤不改动客户端或缓存。'
    return
}

# ==========================================================================
# Watch 模式：后台轮询 + 增量暂存
# ==========================================================================
# 自身最小化控制台（运行时 ShowWindow，非命令行 -WindowStyle Hidden——后者会命中火绒
# PS.NetLoader 启发式害得拉起它的 bat 被删。控制面板同款手法，已验证安全）。
try {
    if (-not ([System.Management.Automation.PSTypeName]'PortableWin32').Type) {
        Add-Type -Namespace '' -Name 'PortableWin32' -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("kernel32.dll")] public static extern System.IntPtr GetConsoleWindow();
[System.Runtime.InteropServices.DllImport("user32.dll")] public static extern bool ShowWindow(System.IntPtr hWnd, int nCmdShow);
[System.Runtime.InteropServices.DllImport("user32.dll")] public static extern bool FlashWindow(System.IntPtr hWnd, bool bInvert);
'@ -ErrorAction SilentlyContinue
    }
    $hwnd = [PortableWin32]::GetConsoleWindow()
    if ($hwnd -ne [System.IntPtr]::Zero) { [void][PortableWin32]::ShowWindow($hwnd, 6) }  # 6 = SW_MINIMIZE
} catch {}
# WATCH_BANNER_V1: tell the player what this extra window is before they wonder.
try { $Host.UI.RawUI.WindowTitle = '整合包后台预下载（可以关闭，不影响游戏）' } catch {}
Write-Host '============================================================' -ForegroundColor DarkCyan
Write-Host '  这是整合包后台预下载窗口：玩游戏时悄悄下载管理员新发布的更新，' -ForegroundColor DarkCyan
Write-Host '  下次启动时就不用再等。可以直接关闭，不影响游戏；下次运行更新器会重新开启。' -ForegroundColor DarkCyan
Write-Host '  游戏退出后它会自己结束。' -ForegroundColor DarkCyan
Write-Host '============================================================' -ForegroundColor DarkCyan

New-Item -ItemType Directory -Force $stageRoot | Out-Null

# --- 单例锁：同一实例只允许一个 daemon 在跑 ------------------------------
if (Test-Path -LiteralPath $lockPath -PathType Leaf) {
    $oldPid = 0
    try { $oldPid = [int]((Get-Content -LiteralPath $lockPath -Raw -ErrorAction SilentlyContinue).Trim()) } catch { $oldPid = 0 }
    if ($oldPid -gt 0) {
        $alive = $null
        try { $alive = Get-Process -Id $oldPid -ErrorAction SilentlyContinue } catch { $alive = $null }
        if ($alive) { Write-Host "[暂存] 已有 daemon 在运行（PID $oldPid），本次退出。"; return }
    }
}
Set-Content -LiteralPath $lockPath -Value ([string]$PID) -Encoding ASCII

$manifestUrls = @(Read-UpdateUrls -Root $instanceRoot -Override $ManifestUrl)
if ($manifestUrls.Count -eq 0) {
    Write-Log "未找到 UPDATE-URL，暂存 daemon 退出。"
    Remove-Item -LiteralPath $lockPath -Force -ErrorAction SilentlyContinue
    return
}
$lastGood = ''
$statePath = Join-Path $instanceRoot '.portable-sync-state.json'
if (Test-Path -LiteralPath $statePath -PathType Leaf) {
    try {
        $st = Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($st.lastManifestUrl) { $lastGood = [string]$st.lastManifestUrl }
        elseif ($st.manifestUrl) { $lastGood = [string]$st.manifestUrl }
    } catch {}
}
$manifestUrls = @(Get-OrderedUpdateUrls -Urls $manifestUrls -LastGood $lastGood)
$manifestUrl = ""
$selectedIndex = -1
$probe = $null
for ($i = 0; $i -lt $manifestUrls.Count; $i++) {
    $probe = Get-ManifestProbe -Url ([string]$manifestUrls[$i]) -TimeoutSec 8
    if ($null -ne $probe) {
        $manifestUrl = [string]$manifestUrls[$i]
        $selectedIndex = $i
        break
    }
}
if ([string]::IsNullOrWhiteSpace($manifestUrl)) {
    Write-Log "更新源暂时不可达，暂存 daemon 本轮不启动；下次玩家同步会重试。"
    Remove-Item -LiteralPath $lockPath -Force -ErrorAction SilentlyContinue
    return
}
if ($selectedIndex -gt 0) { Write-Log "已切换到备用更新地址。" }
$baseUrl = $manifestUrl.Substring(0, $manifestUrl.LastIndexOf('/') + 1)
Write-Log ("暂存 daemon 启动 (PID $PID)，间隔 ${IntervalSeconds}s，最长 ${MaxRuntimeMinutes} 分钟。")

# 本地 sha1 缓存：key = "rel|size|mtimeTicks"，避免每轮重算数百 MB 的哈希。
$script:LocalShaCache = @{}
function Get-LocalSha1Cached {
    param([string]$Path, $Record = $null)
    if (-not [IO.File]::Exists($Path)) { return "" }
    $fi = New-Object IO.FileInfo $Path
    $key = ("{0}|{1}|{2}" -f $Path.ToLowerInvariant(), $fi.Length, $fi.LastWriteTimeUtc.Ticks)
    if ($script:LocalShaCache.ContainsKey($key)) { return $script:LocalShaCache[$key] }
    # DAEMON_VERIFIED_V1: the sync that started this watcher just proved most files;
    # trust its size+mtime record instead of re-reading ~1 GB right at game launch.
    $h = $null
    if ($Record) {
        $recorded = ''; $recordedSize = [int64]-1; $recordedMtime = [int64]-1
        try { $recorded = ([string]$Record.sha1).ToLowerInvariant(); $recordedSize = [int64]$Record.size; $recordedMtime = [int64]$Record.mtime } catch {}
        # Same acceptance as player-update-generic.ps1: true Unix seconds (truncated or
        # rounded) and the rounded local-epoch value older PowerShell versions wrote.
        $seconds = ($fi.LastWriteTimeUtc - [DateTime]::new(1970, 1, 1, 0, 0, 0, [DateTimeKind]::Utc)).TotalSeconds
        $legacySeconds = $fi.LastWriteTimeUtc.Subtract([datetime]'1970-01-01Z').TotalSeconds
        $mtimeMatches = $recordedMtime -in @([int64][Math]::Floor($seconds), [int64][Math]::Round($seconds, [MidpointRounding]::ToEven), [int64][Math]::Round($legacySeconds, [MidpointRounding]::ToEven))
        if ($recorded -match '^[0-9a-f]{40}$' -and $recordedSize -eq $fi.Length -and $mtimeMatches) { $h = $recorded }
    }
    if (-not $h) { $h = Get-Sha1 -Path $Path }
    $script:LocalShaCache[$key] = $h
    return $h
}

# 条件请求拉清单：带上一版的 Last-Modified，服务器没变就回 304（几乎零流量），
# 这样才能把轮询降到 30s 也不心疼。更新服基于 SimpleHTTPRequestHandler，支持 If-Modified-Since。
$script:LastModified = $null
function Get-ManifestIfChanged {
    # MANIFEST_CACHE_V1: an unchanged manifest costs one 304 and returns the cached bytes;
    # the watch loop then skips it because the text equals the last round.
    param([string]$Url)
    $trusted = Get-CachedManifest -Url $Url -Root $script:TrustRoot -TimeoutSec 30 -UserAgent $script:SyncUserAgent
    return [pscustomobject]@{ Changed = $true; Text = [Text.Encoding]::UTF8.GetString([byte[]]$trusted.Raw).TrimStart([char]0xFEFF) }
}

function Install-VerifiedFile {
    param([string]$Temporary,[string]$Destination)
    # Temp is on the destination volume. Never delete the old file to make room.
    if ([IO.Directory]::Exists($Destination)) { throw '更新目标是目录，请检查客户端文件路径。' }
    $oldAttributes = $null
    if ([IO.File]::Exists($Destination)) {
        $oldAttributes = [IO.File]::GetAttributes($Destination)
        if ($oldAttributes -band [IO.FileAttributes]::ReparsePoint) { throw '拒绝替换链接目标。' }
    }
    try {
        if ($null -ne $oldAttributes -and ($oldAttributes -band [IO.FileAttributes]::ReadOnly)) {
            [IO.File]::SetAttributes($Destination, ($oldAttributes -band (-bnot [IO.FileAttributes]::ReadOnly)))
        }
        for ($attempt=0; $attempt -lt 3; $attempt++) {
            try {
                if ([IO.File]::Exists($Destination)) { [IO.File]::Replace($Temporary,$Destination,[NullString]::Value) }
                else { [IO.File]::Move($Temporary,$Destination) }
                return
            } catch { if ($attempt -eq 2) { throw }; Start-Sleep -Milliseconds 250 }
        }
    } catch {
        if ($null -ne $oldAttributes -and [IO.File]::Exists($Destination)) { [IO.File]::SetAttributes($Destination,$oldAttributes) }
        throw ('文件替换失败，旧文件已保留。请完全退出 Minecraft、关闭占用该资源包的程序后重试。' + $_.Exception.Message)
    }
}


function Save-StagedFile {
    param([string]$Url, [string]$Destination, [string]$ExpectedSha1, [switch]$AllowProxy, [int]$TimeoutSec = 120, [int]$MinBytesAfterTimeout = 0)
    New-Item -ItemType Directory -Force (Split-Path -Parent $Destination) | Out-Null
    $tmp = Join-Path (Split-Path -Parent $Destination) (".portable-stage-" + [System.Guid]::NewGuid().ToString('N') + ".tmp")
    try {
        Save-UrlToFile -Url $Url -Path $tmp -TimeoutSec $TimeoutSec -AllowProxy:$AllowProxy -MinBytesAfterTimeout $MinBytesAfterTimeout
        $actual = Get-Sha1 -Path $tmp
        if ($actual -ne $ExpectedSha1.ToLowerInvariant()) { throw "SHA1 mismatch expected=$ExpectedSha1 actual=$actual" }
        Install-VerifiedFile -Temporary $tmp -Destination $Destination
    } finally {
        if (Test-Path -LiteralPath $tmp -PathType Leaf) { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue }
    }
}

function Test-CountsAgainstSource {
    # Hash mismatches and HTTP 4xx (except 408/429) are file-specific, not host failures.
    param($ErrorRecord)
    $ex = $ErrorRecord.Exception
    if ([string]$ex.Message -match 'SHA1 mismatch') { return $false }
    while ($null -ne $ex) {
        if ($ex -is [System.Net.WebException] -and $null -ne $ex.Response) {
            $status = 0
            try { $status = [int]$ex.Response.StatusCode } catch {}
            return -not ($status -ge 400 -and $status -lt 500 -and $status -notin @(408, 429))
        }
        $ex = $ex.InnerException
    }
    return $true
}

# OFFICIAL_BREAKER_V1 for one watch round, same rule as the main updater: a host
# failing for 3 consecutive files is skipped for the rest of the round; a success
# resets it. Reset at the start of every round.
$script:OfficialFailureLimit = 3
$script:OfficialFailures = @{}
$script:OfficialTripped = @{}

function Save-StagedEntry {
    param($File, [string]$BaseUrl, [string]$Destination, [string]$ExpectedSha1, [string]$Rel)
    foreach ($url in @(Get-OfficialFileUrls $File)) {
        $officialHost = ([Uri]$url).Host.ToLowerInvariant()
        if ($script:OfficialTripped.ContainsKey($officialHost)) { continue }
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        $useProxy = Test-StageShouldUseProxy $url
        try {
            Save-StagedFile -Url $url -Destination $Destination -ExpectedSha1 $ExpectedSha1 -TimeoutSec 8 -AllowProxy:$useProxy -MinBytesAfterTimeout (1MB)
            $script:OfficialFailures[$officialHost] = 0
            return 'official'
        } catch {
            Write-Log ("[预下载] 官方源不可用（{0:N1}s，{1}），改走更新服务：{2}" -f $sw.Elapsed.TotalSeconds, (Get-DownloadFailReason $_.Exception), $Rel)
            if (Test-CountsAgainstSource $_) {
                $script:OfficialFailures[$officialHost] = 1 + [int]$script:OfficialFailures[$officialHost]
                if ($script:OfficialFailures[$officialHost] -ge $script:OfficialFailureLimit) {
                    $script:OfficialTripped[$officialHost] = $true
                    Write-Log ("[预下载] 官方源 {0} 连续 {1} 个文件下载失败，本轮不再尝试，其余文件直接走更新服务。" -f $officialHost, $script:OfficialFailureLimit)
                }
            }
        }
    }
    Save-StagedFile -Url (Join-UrlPath -BaseUrl $BaseUrl -Rel $Rel) -Destination $Destination -ExpectedSha1 $ExpectedSha1
    return 'home'
}

function Test-GameRunning {
    # 严格：只认命令行精确含本实例路径的进程 = 本实例的游戏在跑。
    # 这样守 A 实例的 daemon 绝不会被主客户端/别的实例/无关 java 带偏（每实例各管各的，语义干净）。
    # 主客户端的 --gameDir 是它自己的版本目录、不含本实例路径，故它在跑不会误判成本实例在跑。
    # 仅当"完全读不到任何 java 命令行"（权限）时才保守当作在跑，避免误退。
    param([string]$Root)
    try {
        $procs = Get-CimInstance Win32_Process -Filter "Name='javaw.exe' OR Name='java.exe'" -ErrorAction SilentlyContinue
    } catch { return $true }
    if (-not $procs) { return $false }
    $needle = $Root.ToLowerInvariant()
    $sawCmdline = $false
    foreach ($p in $procs) {
        $cl = [string]$p.CommandLine
        if ([string]::IsNullOrWhiteSpace($cl)) { continue }
        $sawCmdline = $true
        if ($cl.ToLowerInvariant().Contains($needle)) { return $true }
    }
    if (-not $sawCmdline) { return $true }
    return $false
}

function Show-Toast {
    param([string]$Title, [string]$Text)
    if (-not $Toast) { return }
    try {
        $null = [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
        $tmpl = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
        $texts = $tmpl.GetElementsByTagName('text')
        $texts.Item(0).AppendChild($tmpl.CreateTextNode($Title)) | Out-Null
        $texts.Item(1).AppendChild($tmpl.CreateTextNode($Text)) | Out-Null
        $toast = [Windows.UI.Notifications.ToastNotification]::new($tmpl)
        [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('便携整合包同步').Show($toast)
    } catch {
        # 系统不支持 toast 时静默；READY.json + 更新已就绪.txt + 下面的窗口提醒才是主通道。
    }
}

function Show-ReadyNotice {
    # 主通道（可靠、不抢焦点）：把最小化的小黑窗还原（不激活）+ 任务栏闪烁 + 大字横幅 + 改标题。
    # 玩家 alt-tab 或瞄一眼任务栏就能看到，全程不打断正在玩的游戏。toast 只作锦上添花。
    param([string]$Text)
    try {
        $hwnd = [PortableWin32]::GetConsoleWindow()
        if ($hwnd -ne [System.IntPtr]::Zero) {
            [void][PortableWin32]::ShowWindow($hwnd, 4)  # 4 = SW_SHOWNOACTIVATE：显示但不抢焦点
            for ($i = 0; $i -lt 8; $i++) { [void][PortableWin32]::FlashWindow($hwnd, $true); Start-Sleep -Milliseconds 200 }
        }
        try { $Host.UI.RawUI.WindowTitle = '★ 整合包更新已就绪 — 重开游戏即可生效 ★' } catch {}
    } catch {}
    Write-Host ''
    Write-Host '============================================================' -ForegroundColor Yellow
    Write-Host ('  ' + $Text) -ForegroundColor Yellow
    Write-Host '  退出游戏后重开「更新mod-Windows端.bat」即可用上新内容' -ForegroundColor Cyan
    Write-Host '============================================================' -ForegroundColor Yellow
    Write-Host ''
    Show-Toast -Title '整合包更新已就绪' -Text $Text
}

$startTime = Get-Date
$lastManifestText = ""
$pendingManifestText = $probe.Text
if ($probe.LastModified) { $script:LastModified = $probe.LastModified }
$idleGameChecks = 0
$stagedTotal = 0
$firstIteration = $true

try {
    while ($true) {
        # 启动后先等 3s 立刻探一次（不让玩家干等一个轮询周期）；此后每 IntervalSeconds 一轮。
        if ($firstIteration) { $firstIteration = $false; Start-Sleep -Seconds 3 } else { Start-Sleep -Seconds $IntervalSeconds }

        $elapsedMin = ((Get-Date) - $startTime).TotalMinutes
        if ($elapsedMin -ge $MaxRuntimeMinutes) { Write-Log "达到最长运行时间，退出。"; break }

        # 生命周期：过了宽限期后，若连续多次检测不到本实例的游戏进程，判定玩家已退出，收工。
        if ($elapsedMin -ge $GraceMinutes) {
            if (Test-GameRunning -Root $instanceRoot) { $idleGameChecks = 0 }
            else {
                $idleGameChecks++
                if ($idleGameChecks -ge $IdleExitChecks) { Write-Log "检测到游戏已退出，暂存 daemon 收工。"; break }
            }
        }

        $syncLock = $null
        try {
        $syncLock = Enter-SyncLock -Root $instanceRoot
        if ($null -eq $syncLock) { Write-Log '主更新器正在同步，跳过本轮预下载。'; continue }
        # 条件请求拉清单：304（未变化）或失败都跳过本轮，几乎零负担。
        # 首轮直接使用启动探测时已下载的清单，不再重复下载。
        $mres = $null
        if ($null -ne $pendingManifestText) {
            $mres = [pscustomobject]@{ Changed = $true; Text = $pendingManifestText }
            $pendingManifestText = $null
        } else {
            try { $mres = Get-ManifestIfChanged -Url $manifestUrl } catch { Write-Log ("清单获取失败：" + $_.Exception.Message); continue }
        }
        if (-not $mres.Changed) { continue }
        $manifestText = [string]$mres.Text
        if ([string]::IsNullOrWhiteSpace($manifestText)) { continue }
        # 二次保险：内容和上一版逐字节相同就跳过（防服务器不给 Last-Modified 时的空转）。
        if ($manifestText -eq $lastManifestText) { continue }

        $manifest = $null
        try { $manifest = $manifestText | ConvertFrom-Json } catch { Write-Log "清单解析失败，跳过本轮。"; continue }
        if (-not $manifest.files) { $lastManifestText = $manifestText; continue }
        $lastManifestText = $manifestText

        $preserveChangeGlobs = @()
        if ($manifest.PSObject.Properties['preserveLocalChangeGlobs']) { $preserveChangeGlobs = @($manifest.preserveLocalChangeGlobs) }
        $preserveDeletionGlobs = @()
        if ($manifest.PSObject.Properties['preserveLocalDeletionGlobs']) { $preserveDeletionGlobs = @($manifest.preserveLocalDeletionGlobs) }
        $forceSyncGlobs = @()
        if ($manifest.PSObject.Properties['forceSyncGlobs']) { $forceSyncGlobs = @($manifest.forceSyncGlobs) }
        $preservePlayerCustomizations = $true
        if ($manifest.PSObject.Properties['preservePlayerCustomizations']) { $preservePlayerCustomizations = [bool]$manifest.preservePlayerCustomizations }
        # 基线 = 上次同步的清单哈希：本地文件与基线不符即玩家改过，不预下载（与 Promote 同一防线）。
        $baseline = @{}
        $verifiedRecords = @{}
        $syncStatePath = Join-Path $instanceRoot '.portable-sync-state.json'
        if (Test-Path -LiteralPath $syncStatePath -PathType Leaf) {
            try {
                $syncState = Get-Content -LiteralPath $syncStatePath -Raw -Encoding UTF8 | ConvertFrom-Json
                foreach ($item in @($syncState.files)) {
                    if ($item -and $item.path -and $item.sha1) { $baseline[(Normalize-Rel ([string]$item.path)).ToLowerInvariant()] = ([string]$item.sha1).ToLowerInvariant() }
                }
                if ($syncState.PSObject.Properties['verified'] -and $syncState.verified) {
                    foreach ($prop in $syncState.verified.PSObject.Properties) { $verifiedRecords[([string]$prop.Name).ToLowerInvariant()] = $prop.Value }
                }
            } catch {}
        }
        $script:OfficialFailures = @{}
        $script:OfficialTripped = @{}

        Write-Log "检测到新清单，开始比对并后台预下载……"
        $newStaged = 0
        $stagedList = New-Object System.Collections.Generic.List[string]
        foreach ($file in @($manifest.files)) {
            $rel = Normalize-Rel ([string]$file.path)
            if (-not (Test-RelativePathSafe -Rel $rel)) { continue }
            if ($rel -in @('server-manifest.json', 'update-log.txt')) { continue }
            $expected = ([string]$file.sha1).ToLowerInvariant()
            if ([string]::IsNullOrWhiteSpace($expected)) { continue }
            # 玩家会本地改动/保留的文件（options、模组配置、资源包、光影、存档等）不预下载，避免打架。
            # forceSyncGlobs 例外：管理员点名强推的文件照常预下载。
            $isForceSync = Test-GlobMatch -Rel $rel -Globs $forceSyncGlobs
            if (-not $isForceSync -and (Test-GlobMatch -Rel $rel -Globs $preserveChangeGlobs)) { continue }

            $localPath = Join-SafeRel -Root $instanceRoot -Rel $rel
            $localHash = Get-LocalSha1Cached -Path $localPath -Record $verifiedRecords[$rel.ToLowerInvariant()]
            if ($localHash -eq $expected) { continue }  # 本地已是最新，无需暂存
            $baseHash = if ($baseline.ContainsKey($rel.ToLowerInvariant())) { [string]$baseline[$rel.ToLowerInvariant()] } else { '' }
            # 与 player-update-generic.ps1 保持同一语义：已在上次清单中、
            # 当前被玩家删除、且命中删除保留规则的文件不预下载。
            # 若旧 daemon 已留下暂存件，也在这里丢弃，避免下一次落位复活。
            if ([string]::IsNullOrWhiteSpace($localHash) -and -not [string]::IsNullOrWhiteSpace($baseHash) -and
                $preservePlayerCustomizations -and -not $isForceSync -and (Test-GlobMatch -Rel $rel -Globs $preserveDeletionGlobs)) {
                $stagePath = Join-SafeRel -Root $stageRoot -Rel $rel
                if (Test-Path -LiteralPath $stagePath -PathType Leaf) {
                    Remove-Item -LiteralPath $stagePath -Force -ErrorAction SilentlyContinue
                    Write-Log ("[预下载] 保留玩家删除，丢弃旧暂存：" + $rel)
                }
                continue
            }
            if (-not $isForceSync -and $localHash) {
                # 本地存在但与基线不符（或没有基线记录）= 玩家改过：不预下载，交给权威同步器按规则裁决。
                if (-not $baseHash -or $localHash -ne $baseHash) { continue }
            }

            $stagePath = Join-SafeRel -Root $stageRoot -Rel $rel
            if ((Test-Path -LiteralPath $stagePath -PathType Leaf) -and ((Get-Sha1 -Path $stagePath) -eq $expected)) { continue }  # 已暂存

            try {
                $source = Save-StagedEntry -File $file -BaseUrl $baseUrl -Destination $stagePath -ExpectedSha1 $expected -Rel $rel
                if ($source -eq 'official') { Write-Log ("[预下载/官方源] " + $rel) } else { Write-Log ("[预下载] " + $rel) }
                $newStaged++
                [void]$stagedList.Add($rel)
            } catch {
                Write-Log ("[预下载失败] " + $rel + "；" + $_.Exception.Message)
            }
        }

        if ($newStaged -gt 0) {
            $stagedTotal += $newStaged
            $version = ''
            if ($manifest.PSObject.Properties['version']) { $version = [string]$manifest.version }
            $ready = [ordered]@{
                version = $version
                stagedAt = (Get-Date).ToString('s')
                count = $stagedTotal
                lastBatch = @($stagedList)
            }
            Write-Utf8NoBom -Path $readyPath -Value (($ready | ConvertTo-Json -Depth 6) + "`r`n")
            Write-Utf8NoBom -Path $noticePath -Value ("整合包有更新，已在后台下载完成 $stagedTotal 个文件。`r`n重启游戏即可自动生效（无需再等下载）。`r`n版本：$version`r`n" )
            Write-Log ("本轮预下载完成 $newStaged 个，累计 $stagedTotal 个；已写就绪标记。")
            Show-ReadyNotice -Text "整合包更新已在后台下载完成（$stagedTotal 个文件）"
        } else {
            Write-Log "清单有变化，但无需预下载的新文件（可能只是元数据/被保留项）。"
        }
        } catch {
            # 兜住本轮任何未预期异常：记进日志、继续下一轮，绝不让 daemon 崩掉或在小黑窗里闪红。
            Write-Log ("本轮异常已跳过：" + $_.Exception.Message)
        } finally { if ($syncLock) { $syncLock.Dispose() } }
    }
} finally {
    if (([string](Get-Content -LiteralPath $lockPath -Raw -ErrorAction SilentlyContinue)).Trim() -eq [string]$PID) { Remove-Item -LiteralPath $lockPath -Force -ErrorAction SilentlyContinue }
    Write-Log "暂存 daemon 已退出。"
}

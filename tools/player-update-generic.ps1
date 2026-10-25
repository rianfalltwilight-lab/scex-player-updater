param(
    [string]$InstanceDir = ".",
    [string]$ManifestUrl = "",
    [switch]$NoPause
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
# PS 5.1 默认不启用 TLS 1.2；更新源切 HTTPS 后没有这行会直接握手失败。对 HTTP 无影响。
try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch {}
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding

function Resolve-FullPath { param([Parameter(Mandatory = $true)][string]$Path) return [System.IO.Path]::GetFullPath($Path) }
# 注意 '\' 与 '\\'：PowerShell 单引号不转义，String.Replace 是字面替换而非正则。
# 这里必须是单反斜杠——写成 '\\' 只会替换「连续两个反斜杠」，单反斜杠路径根本不会被规范化，
# 于是 Test-RelativePathSafe 按 '/' 切分时看不到 '..' 段，形如 ..\..\x 的路径会绕过安全检查。
function Normalize-Rel { param([string]$Rel) return ([string]$Rel).Replace('\', '/').TrimStart('/') }
function Normalize-RelKey { param([string]$Rel) return (Normalize-Rel $Rel).ToLowerInvariant() }

function Test-RelativePathSafe {
    param([Parameter(Mandatory=$true)][string]$Rel)
    if ([string]::IsNullOrWhiteSpace($Rel) -or [IO.Path]::IsPathRooted($Rel) -or $Rel -match '[:<>"|?*]') { return $false }
    foreach ($part in $Rel.Replace('\','/').Split('/')) {
        if (-not $part -or $part -in @('.','..') -or $part.EndsWith('.') -or $part.EndsWith(' ')) { return $false }
    }
    return $true
}

function Join-SafeRelativePath {
    param([Parameter(Mandatory=$true)][string]$Root,[Parameter(Mandatory=$true)][string]$Rel)
    if (-not (Test-RelativePathSafe $Rel)) { throw "[路径错误] 无效更新路径：$Rel" }
    $target = [IO.Path]::GetFullPath((Join-Path $Root $Rel.Replace('/','\')))
    Assert-SafeAbsolutePath -Path $target -Boundary $Root
    if ($script:SyncRoot -and $target.StartsWith($script:SyncRoot.TrimEnd('\')+'\',[StringComparison]::OrdinalIgnoreCase)) { Assert-SafeAbsolutePath -Path $target -Boundary $script:SyncRoot }
    return $target
}

function Get-Sha1 {
    param([Parameter(Mandatory = $true)][string]$Path)
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

function Write-Utf8NoBom {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Value)
    Assert-SafeAbsolutePath -Path $Path
    New-Item -ItemType Directory -Force (Split-Path -Parent $Path) | Out-Null
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Value, $encoding)
}

function Read-UpdateUrls {
    param([string]$Root, [string]$Override)
    $rawValues = New-Object System.Collections.Generic.List[string]
    if (-not [string]::IsNullOrWhiteSpace($Override)) {
        [void]$rawValues.Add($Override)
    } else {
        # TFCR-update-url.txt 是旧周目玩家包留下的文件名，老玩家实例里还有——请勿删除。
        $primary = @('UPDATE-URL.txt', 'PORTABLE-UPDATE-URL.txt', 'TFCR-update-url.txt', '_updater\UPDATE-URL.txt', '_updater\PORTABLE-UPDATE-URL.txt')
        foreach ($rel in $primary) {
            $path = Join-Path $Root $rel
            if (Test-Path -LiteralPath $path -PathType Leaf) {
                try { [void]$rawValues.Add((Get-Content -LiteralPath $path -Raw -Encoding UTF8)) } catch { }
            }
        }
    }
    $urls = New-Object System.Collections.Generic.List[string]
    $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    foreach ($raw in $rawValues) {
        foreach ($line in ([string]$raw -split "`r?`n")) {
            $value = ([string]$line).Trim()
            if ([string]::IsNullOrWhiteSpace($value) -or $value.StartsWith('#')) { continue }
            if ($seen.Add($value)) { [void]$urls.Add($value) }
        }
    }
    if ($urls.Count -eq 0) { throw '找不到 UPDATE-URL.txt。请确认玩家包没有被拆散，或使用 -ManifestUrl 手动指定 server-manifest.json 地址。' }
    return @($urls)
}

function Read-UpdateUrl {
    param([string]$Root, [string]$Override)
    return (Read-UpdateUrls -Root $Root -Override $Override | Select-Object -First 1)
}

function ConvertTo-FileMap {
    param($Files)
    $map = @{}
    foreach ($file in @($Files)) {
        if ($file -and $file.path) { $map[(Normalize-RelKey ([string]$file.path))] = [string]$file.sha1 }
    }
    return $map
}

function Test-GlobMatch {
    param([Parameter(Mandatory = $true)][string]$Rel, $Globs)
    $normalized = Normalize-Rel $Rel
    foreach ($glob in @($Globs)) {
        $g = Normalize-Rel ([string]$glob)
        if ([string]::IsNullOrWhiteSpace($g)) { continue }
        if ($normalized.Equals($g, [System.StringComparison]::OrdinalIgnoreCase)) { return $true }
        if ($normalized -like $g) { return $true }
    }
    return $false
}

function Join-UrlPath {
    param([Parameter(Mandatory = $true)][string]$BaseUrl, [Parameter(Mandatory = $true)][string]$Rel)
    $parts = (Normalize-Rel $Rel).Split('/') | ForEach-Object { [Uri]::EscapeDataString($_) }
    return $BaseUrl + (($parts) -join '/')
}

function Get-MaskedUrl {
    param([string]$Url)
    try {
        $builder = New-Object UriBuilder $Url
        $builder.UserName=''; $builder.Password=''; $builder.Query=''; $builder.Fragment=''
        return [regex]::Replace($builder.Uri.AbsoluteUri, '(?<=^[A-Za-z][A-Za-z0-9+.-]*://[^/]+/)[^/]+(?=/)', '***')
    } catch { return '[地址已隐藏]' }
}

function Test-ProtectedRel {
    param([string]$Rel)
    $first = (Normalize-Rel $Rel).Split('/')[0]
    return ($first -like '.portable-*' -or $first -like '_disabled_*' -or $first -eq '_repair')
}

function Backup-FileIfExists {
    param([Parameter(Mandatory=$true)][string]$Path,[Parameter(Mandatory=$true)][string]$Rel,[Parameter(Mandatory=$true)][string]$BackupRoot)
    if (-not [IO.File]::Exists($Path)) { return }
    Assert-SafeAbsolutePath -Path $Path
    $backupPath = Join-SafeRelativePath -Root $BackupRoot -Rel $Rel
    if ([IO.File]::Exists($backupPath)) { return }
    New-Item -ItemType Directory -Force (Split-Path -Parent $backupPath) | Out-Null
    [IO.File]::Copy($Path,$backupPath,$false)
}

$script:ProxyUrl = $null
$script:ProxyResolved = $false
$script:OfficialMinBytes = 1MB
$script:OfficialMinRateBps = 128KB
$script:PrivateProbeTimeoutSec = 2
$script:PublicProbeTimeoutSec = 8

function Normalize-ProxyUrl {
    param([string]$Raw)
    $raw = ([string]$Raw).Trim()
    if ([string]::IsNullOrWhiteSpace($raw)) { return '' }
    if ($raw.StartsWith('socks', [StringComparison]::OrdinalIgnoreCase)) { return '' }
    if ($raw -notmatch '^[A-Za-z][A-Za-z0-9+.-]*://') { $raw = 'http://' + $raw }
    try {
        $uri = [Uri]$raw
        if ($uri.Scheme -notin @('http', 'https')) { return '' }
        if ([string]::IsNullOrWhiteSpace($uri.Host)) { return '' }
        return $raw
    } catch { return '' }
}

function ConvertFrom-WinProxyServer {
    param([string]$Server)
    $server = ([string]$Server).Trim()
    if ([string]::IsNullOrWhiteSpace($server)) { return '' }
    if ($server -match '=') {
        $map = @{}
        foreach ($item in $server.Split(';')) {
            if ($item -notmatch '=') { continue }
            $pair = $item.Split('=', 2)
            $map[$pair[0].Trim().ToLowerInvariant()] = $pair[1].Trim()
        }
        foreach ($key in @('https', 'http')) {
            if ($map.ContainsKey($key) -and -not [string]::IsNullOrWhiteSpace($map[$key])) {
                $parsed = Normalize-ProxyUrl $map[$key]
                if ($parsed) { return $parsed }
            }
        }
        return ''
    }
    return (Normalize-ProxyUrl $server)
}

function Get-SystemProxyUrl {
    if ($script:ProxyResolved) { return [string]$script:ProxyUrl }
    $script:ProxyResolved = $true
    $script:ProxyUrl = ''
    $flag = ([string][Environment]::GetEnvironmentVariable('PORTABLE_SYNC_NOPROXY')).Trim().ToLowerInvariant()
    if ($flag -in @('1', 'true', 'yes', 'on')) { return '' }
    $override = Normalize-ProxyUrl ([Environment]::GetEnvironmentVariable('PORTABLE_SYNC_PROXY'))
    if ($override) { $script:ProxyUrl = $override; return $script:ProxyUrl }
    foreach ($key in @('HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy', 'ALL_PROXY', 'all_proxy')) {
        $parsed = Normalize-ProxyUrl ([Environment]::GetEnvironmentVariable($key))
        if ($parsed) { $script:ProxyUrl = $parsed; return $script:ProxyUrl }
    }
    try {
        $ie = Get-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -ErrorAction Stop
        if ([int]$ie.ProxyEnable -eq 1 -and -not [string]::IsNullOrWhiteSpace([string]$ie.ProxyServer)) {
            $parsed = ConvertFrom-WinProxyServer ([string]$ie.ProxyServer)
            if ($parsed) { $script:ProxyUrl = $parsed; return $script:ProxyUrl }
        }
    } catch {}
    return ''
}

function Get-ProxyDescription {
    param([string]$Url)
    if ([string]::IsNullOrWhiteSpace($Url)) { $Url = Get-SystemProxyUrl }
    if ([string]::IsNullOrWhiteSpace($Url)) { return '' }
    try {
        $uri = [Uri]$Url
        if ($uri.Port -gt 0) { return ($uri.Host + ':' + $uri.Port) }
        return $uri.Host
    } catch { return $Url }
}

function Test-LocalOrPrivateHost {
    param([string]$HostName)
    $h = ([string]$HostName).Trim().Trim('[', ']').ToLowerInvariant()
    if ([string]::IsNullOrWhiteSpace($h)) { return $true }
    if ($h -in @('localhost', '127.0.0.1', '::1')) { return $true }
    $parts = $h.Split('.')
    if ($parts.Count -eq 4) {
        $nums = @()
        foreach ($p in $parts) {
            $n = 0
            if (-not [int]::TryParse($p, [ref]$n)) { return $false }
            $nums += $n
        }
        $a = $nums[0]; $b = $nums[1]
        if ($a -eq 127 -or $a -eq 10) { return $true }
        if ($a -eq 192 -and $b -eq 168) { return $true }
        if ($a -eq 172 -and $b -ge 16 -and $b -le 31) { return $true }
    }
    return $false
}

function Test-UrlBypassesProxy {
    param([string]$Url)
    try { $hostName = ([Uri]$Url).Host } catch { return $true }
    if (Test-LocalOrPrivateHost $hostName) { return $true }
    $raw = [Environment]::GetEnvironmentVariable('NO_PROXY')
    if ([string]::IsNullOrWhiteSpace($raw)) { $raw = [Environment]::GetEnvironmentVariable('no_proxy') }
    foreach ($item in @(([string]$raw).Split(','))) {
        $rule = $item.Trim().ToLowerInvariant()
        if ([string]::IsNullOrWhiteSpace($rule)) { continue }
        if ($rule -eq '*') { return $true }
        $h = $hostName.ToLowerInvariant()
        if ($rule.StartsWith('.')) {
            if ($h.EndsWith($rule) -or $h -eq $rule.Substring(1)) { return $true }
        } elseif ($h -eq $rule -or $h.EndsWith('.' + $rule)) { return $true }
    }
    return $false
}

function Test-ShouldUseProxyForOfficial {
    param([string]$Url)
    $proxy = Get-SystemProxyUrl
    if ([string]::IsNullOrWhiteSpace($proxy)) { return $false }
    if (Test-UrlBypassesProxy $Url) { return $false }
    return $true
}

function Get-ProbeTimeoutSec {
    param([string]$Url)
    try { $hostName = ([Uri]$Url).Host } catch { return $script:PublicProbeTimeoutSec }
    if (Test-LocalOrPrivateHost $hostName) { return $script:PrivateProbeTimeoutSec }
    return $script:PublicProbeTimeoutSec
}

function Test-PrivateUpdateUrl {
    param([string]$Url)
    try { return (Test-LocalOrPrivateHost ([Uri]$Url).Host) } catch { return $false }
}

function Get-OrderedManifestUrls {
    param([string[]]$Urls, [string]$LastGood = '')
    $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    $out = New-Object System.Collections.Generic.List[string]
    $add = {
        param([string]$Value)
        $v = ([string]$Value).Trim()
        if (-not [string]::IsNullOrWhiteSpace($v) -and $seen.Add($v)) { [void]$out.Add($v) }
    }
    $public = @($Urls | Where-Object { -not (Test-PrivateUpdateUrl $_) })
    $candidates = if ($public.Count -gt 0) { $public } else { @($Urls) }
    if ($candidates -contains $LastGood -and -not (Test-PrivateUpdateUrl $LastGood)) { & $add $LastGood }
    foreach ($u in $candidates) { & $add $u }
    if (-not [string]::IsNullOrWhiteSpace($LastGood) -and -not (Test-PrivateUpdateUrl $LastGood)) { & $add $LastGood }
    foreach ($u in @($Urls | Where-Object { Test-PrivateUpdateUrl $_ })) { & $add $u }
    return @($out)
}

function Get-FileVerifyRecord {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Sha1)
    $item = Get-Item -LiteralPath $Path
    return [ordered]@{ sha1 = $Sha1.ToLowerInvariant(); size = [int64]$item.Length; mtime = [int64][double]$item.LastWriteTimeUtc.Subtract([datetime]'1970-01-01Z').TotalSeconds }
}

function Test-LooksLikeCurrentFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Expected,
        $ManifestSize,
        [string]$PreviousHash,
        $Verified
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    if ([string]::IsNullOrWhiteSpace($Expected)) { return $false }
    $item = Get-Item -LiteralPath $Path
    $size = [int64]$item.Length
    if ($null -ne $ManifestSize -and [string]$ManifestSize -ne '' -and [string]$ManifestSize -ne '0') {
        try { if ($size -ne [int64]$ManifestSize) { return $false } } catch {}
    }
    $expected = $Expected.ToLowerInvariant()
    $previous = ([string]$PreviousHash).ToLowerInvariant()
    $vSha = ''
    $vSize = [int64]-1
    $vMtime = [int64]-1
    if ($Verified) {
        try { $vSha = ([string]$Verified.sha1).ToLowerInvariant() } catch {}
        try { $vSize = [int64]$Verified.size } catch {}
        try { $vMtime = [int64]$Verified.mtime } catch {}
    }
    $mtime = [int64][double]$item.LastWriteTimeUtc.Subtract([datetime]'1970-01-01Z').TotalSeconds
    if ($previous -eq $expected) {
        if ($vSha -eq $expected) {
            if ($vSize -eq $size -and $vMtime -eq $mtime) { return $true }
            return ((Get-Sha1 -Path $Path) -eq $expected)
        }
        return ((Get-Sha1 -Path $Path) -eq $expected)
    }
    if ($vSha -eq $expected -and $vSize -eq $size -and $vMtime -eq $mtime) { return $true }
    return ((Get-Sha1 -Path $Path) -eq $expected)
}

function New-DirectHttpRequest {
    param([Parameter(Mandatory = $true)][string]$Url, [int]$TimeoutSec = 30, [switch]$AllowProxy)
    # 家宽更新源走非标准端口，系统代理（Clash/安全软件）可能把请求丢进黑洞，必须直连。
    # 官方 CDN 有代理时走代理（国内直连经常能通但很慢）；走代理时不要改写成 IPv4，
    # 否则 HTTPS CONNECT / Clash 分流对不上域名。
    $target = $Url
    $hostHeader = $null
    if (-not $AllowProxy) {
        try {
            $uri = [Uri]$Url
            if ($uri.Scheme -eq 'http' -and $uri.HostNameType -eq [UriHostNameType]::Dns) {
                $ipv4 = [Net.Dns]::GetHostAddresses($uri.Host) |
                    Where-Object { $_.AddressFamily -eq 'InterNetwork' } |
                    Select-Object -First 1
                if ($ipv4) {
                    $b = New-Object UriBuilder $uri
                    $b.Host = $ipv4.IPAddressToString
                    $target = $b.Uri.AbsoluteUri
                    $hostHeader = $uri.Host
                }
            }
        } catch {}
    }
    $request = [System.Net.HttpWebRequest]::Create($target)
    if ($hostHeader) { $request.Host = $hostHeader }
    if ($AllowProxy) {
        $proxy = Get-SystemProxyUrl
        if ($proxy) { $request.Proxy = New-Object System.Net.WebProxy($proxy) }
    } else {
        $request.Proxy = $null
    }
    # PowerShell 5.1 同源默认仅 2 条连接；连接上限须覆盖最多 8 个下载 worker，避免排队耗尽请求超时。
    $request.ServicePoint.ConnectionLimit = [Math]::Max(8, $request.ServicePoint.ConnectionLimit)
    $request.Timeout = [Math]::Max(1000, $TimeoutSec * 1000)
    $request.ReadWriteTimeout = [Math]::Max(1000, $TimeoutSec * 1000)
    $request.KeepAlive = $false
    try { $request.ServicePoint.Expect100Continue = $false } catch {}
    $request.UserAgent = 'portable-server-kit-sync/1.0'
    try { $request.CachePolicy = New-Object System.Net.Cache.RequestCachePolicy([System.Net.Cache.RequestCacheLevel]::NoCacheNoStore) } catch {}
    return $request
}

function Get-DownloadFailReason {
    param($Exception)
    $msg = [string]$Exception.Message
    if ($msg -match 'SHA1|校验失败') { return '哈希不符' }
    if ($msg -match 'too slow|太慢|过低') { return '太慢' }
    if ($msg -match 'timed out|Timeout|超时') { return '超时' }
    if ($msg -match '\(404\)|HTTP 404') { return '404' }
    if ($msg -match '\(403\)|HTTP 403') { return '403' }
    if ($msg -match '无法解析|name or service|DNS') { return 'DNS' }
    return '连接失败'
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
    $urls = New-Object 'System.Collections.Generic.List[string]'
    $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    $raw = @()
    if ($File -and $File.PSObject.Properties['downloads']) { $raw += @($File.downloads) }
    if ($File -and $File.PSObject.Properties['url']) { $raw += [string]$File.url }
    foreach ($value in $raw) {
        $url = ([string]$value).Trim()
        if ((Test-OfficialHttpsUrl $url) -and $seen.Add($url)) { [void]$urls.Add($url) }
    }
    return @($urls.ToArray())
}

function Read-UrlUtf8Text {
    # 更新源重启/隧道瞬断会短暂 502/503，重试几次再放弃。
    param([Parameter(Mandatory = $true)][string]$Url, [int]$TimeoutSec = 30, [int]$Retries = 2)
    for ($attempt = 0; $attempt -le $Retries; $attempt++) {
        $request = $null
        $response = $null
        $reader = $null
        try {
            $request = New-DirectHttpRequest -Url $Url -TimeoutSec $TimeoutSec
            $response = $request.GetResponse()
            $reader = New-Object System.IO.StreamReader($response.GetResponseStream(), [System.Text.Encoding]::UTF8)
            return ($reader.ReadToEnd()).TrimStart([char]0xFEFF)
        } catch {
            if ($attempt -ge $Retries) { throw }
            $wait = if ($TimeoutSec -le 2) { 0 } else { 3 }
            if ($wait -gt 0) {
                Write-Host ("[同步] 清单获取失败（第 " + ($attempt + 1) + " 次）：" + $_.Exception.Message + "；${wait} 秒后重试...")
                Start-Sleep -Seconds $wait
            }
        } finally {
            if ($reader) { $reader.Dispose() }
            if ($response) { $response.Dispose() }
        }
    }
}

function Copy-DownloadStream {
    param($InputStream, $OutputStream, [int]$TimeoutSec = 60, [int]$MinBytesAfterTimeout = 0, [int]$MinRateBps = 0)
    $buffer = New-Object byte[] 65536
    $got = [long]0
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $nextCheck = [double]$TimeoutSec
    while (($n = $InputStream.Read($buffer, 0, $buffer.Length)) -gt 0) {
        $OutputStream.Write($buffer, 0, $n)
        $got += $n
        if ($nextCheck -le 0) { continue }
        $elapsed = $sw.Elapsed.TotalSeconds
        if ($elapsed -lt $nextCheck) { continue }
        if ($MinRateBps -gt 0 -and $got -lt ($MinRateBps * $elapsed)) { throw 'official source too slow' }
        if ($MinBytesAfterTimeout -gt 0 -and $got -lt $MinBytesAfterTimeout) { throw 'official source too slow' }
        $nextCheck = $elapsed + [Math]::Max(1, $TimeoutSec)
    }
}

function Save-OfficialViaProxy {
    # HttpWebRequest + 系统代理时 Timeout 经常不生效。先用 HttpClient 拿响应头（8 秒取消），
    # 再按字节流读 body：8 秒内字节太少就放弃，避免国内直连细水长流。
    param([Parameter(Mandatory = $true)][string]$Url, [Parameter(Mandatory = $true)][string]$Path, [int]$TimeoutSec = 8, [string]$ProxyUrl, [int]$MinBytesAfterTimeout = 0, [int]$MinRateBps = 0)
    Add-Type -AssemblyName System.Net.Http -ErrorAction SilentlyContinue
    $handler = New-Object System.Net.Http.HttpClientHandler
    $handler.UseProxy = $true
    $handler.Proxy = New-Object System.Net.WebProxy($ProxyUrl)
    $handler.AllowAutoRedirect = $true
    $client = New-Object System.Net.Http.HttpClient($handler)
    $client.Timeout = [TimeSpan]::FromMinutes(10)
    $client.DefaultRequestHeaders.TryAddWithoutValidation('User-Agent', 'portable-server-kit-sync/1.0') | Out-Null
    $cts = New-Object System.Threading.CancellationTokenSource
    $cts.CancelAfter([Math]::Max(1000, $TimeoutSec * 1000))
    $response = $null
    $inputStream = $null
    $outputStream = $null
    try {
        $response = $client.GetAsync($Url, [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead, $cts.Token).GetAwaiter().GetResult()
        if (-not $response.IsSuccessStatusCode) {
            throw ("HTTP {0} from official proxy download" -f [int]$response.StatusCode)
        }
        $inputStream = $response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
        $outputStream = [System.IO.File]::Open($Path, [System.IO.FileMode]::Create, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
        Copy-DownloadStream -InputStream $inputStream -OutputStream $outputStream -TimeoutSec $TimeoutSec -MinBytesAfterTimeout $MinBytesAfterTimeout -MinRateBps $MinRateBps
    } finally {
        if ($outputStream) { $outputStream.Dispose() }
        if ($inputStream) { $inputStream.Dispose() }
        if ($response) { $response.Dispose() }
        $cts.Dispose()
        $client.Dispose()
        $handler.Dispose()
    }
}

function Save-UrlToFile {
    param([Parameter(Mandatory=$true)][string]$Url,[Parameter(Mandatory=$true)][string]$Path,[int]$TimeoutSec=60,[switch]$AllowProxy,[int]$MinBytesAfterTimeout=0,[int]$MinRateBps=0,[switch]$Resume)
    if ($AllowProxy) {
        $proxy = Get-SystemProxyUrl
        if ($proxy) { Save-OfficialViaProxy -Url $Url -Path $Path -TimeoutSec $TimeoutSec -ProxyUrl $proxy -MinBytesAfterTimeout $MinBytesAfterTimeout -MinRateBps $MinRateBps; return }
    }
    $offset = if ($Resume -and [IO.File]::Exists($Path)) { (Get-Item -LiteralPath $Path).Length } else { [long]0 }
    $request = New-DirectHttpRequest -Url $Url -TimeoutSec $TimeoutSec -AllowProxy:$AllowProxy
    if ($offset -gt 0) { $request.AddRange([long]$offset) }
    $response=$null; $inputStream=$null; $outputStream=$null
    try {
        try { $response=$request.GetResponse() }
        catch [Net.WebException] {
            if ($offset -gt 0 -and $_.Exception.Response -and [int]$_.Exception.Response.StatusCode -eq 416) {
                $_.Exception.Response.Close()
                [IO.File]::Delete($Path)
                Save-UrlToFile -Url $Url -Path $Path -TimeoutSec $TimeoutSec -AllowProxy:$AllowProxy -MinBytesAfterTimeout $MinBytesAfterTimeout -MinRateBps $MinRateBps
                return
            }
            throw
        }
        $append = $false
        if ([int]$response.StatusCode -eq 206) {
            $range=[string]$response.Headers['Content-Range']
            if ($range -notmatch '^bytes (\d+)-(\d+)/(\d+)$' -or [long]$Matches[1] -ne $offset -or [long]$Matches[2] -lt $offset -or [long]$Matches[3] -le [long]$Matches[2]) { throw '[下载失败] 更新源返回无效续传范围，请稍后重试。' }
            $append = $offset -gt 0
        } elseif ([int]$response.StatusCode -ne 200) { throw '[下载失败] 更新源返回了无法处理的下载状态。' }
        $mode = if ($append) { [IO.FileMode]::Append } else { [IO.FileMode]::Create }
        $inputStream=$response.GetResponseStream()
        $outputStream=[IO.File]::Open($Path,$mode,[IO.FileAccess]::Write,[IO.FileShare]::None)
        $before=$outputStream.Length
        Copy-DownloadStream -InputStream $inputStream -OutputStream $outputStream -TimeoutSec $TimeoutSec -MinBytesAfterTimeout $MinBytesAfterTimeout -MinRateBps $MinRateBps
        if ($response.ContentLength -ge 0 -and ($outputStream.Length-$before) -ne $response.ContentLength) { throw '[下载失败] 下载连接提前中断，已保留断点，下次可继续。' }
    } catch { try { $request.Abort() } catch {}; throw }
    finally { if ($outputStream) {$outputStream.Dispose()}; if ($inputStream) {$inputStream.Dispose()}; if ($response) {$response.Dispose()} }
}

# FILE_LOCK_PREFLIGHT_V1: report local file errors before any synchronization writes.
function Format-UpdateError {
    param($ErrorRecord, [string]$Phase = '更新', [string]$Path = '')
    $ex = if ($ErrorRecord -is [Management.Automation.ErrorRecord]) { $ErrorRecord.Exception } else { $ErrorRecord }
    $code = 0; $kind = '未知错误'; $http = 0; $timeout = $false; $slow = $false
    while ($null -ne $ex) {
        $message = [string]$ex.Message
        if ($message -match '(?s)(\[(?:文件占用|权限不足|空间不足|下载失败|校验失败|替换失败|预检失败|更新失败|获取清单失败|正在更新|状态错误|清单错误|路径错误)\].*)') { return $Matches[1] }
        $kind = $ex.GetType().Name
        if ($message -match 'too slow|太慢|过低') { $slow = $true }
        $native = $ex.HResult -band 65535
        if ($native -in @(5,32,33,39,112)) { $code = $native }
        if ($ex -is [Net.WebException]) {
            $timeout = $ex.Status -eq [Net.WebExceptionStatus]::Timeout
            if ($ex.Response -is [Net.HttpWebResponse]) { $http = [int]$ex.Response.StatusCode }
        }
        if ($ex -is [TimeoutException]) { $timeout = $true }
        $ex = $ex.InnerException
    }
    $target = if ($Path) { "文件：$Path。" } else { '' }
    if ($code -in @(32,33)) { return "[文件占用] $target 文件正被其他程序使用，原文件已保留。请完全退出 Minecraft（不是返回标题界面），关闭打开此文件的程序，再重新运行更新器。" }
    if ($code -eq 5) { return "[权限不足] $target 无法访问或写入此位置。请检查文件夹权限及安全软件拦截，再重试。" }
    if ($code -in @(39,112)) { return "[空间不足] $target 目标磁盘空间不足，请腾出空间后重试。" }
    if ($Phase -eq '替换') { return "[替换失败] $target 未完成文件替换，请检查文件权限、磁盘和占用情况后重试。（错误类型：$kind）" }
    if ($Phase -eq '校验') { return "[校验失败] $target 下载内容与清单不一致，未安装该文件。请重新运行更新器。" }
    if ($Phase -in @('下载','获取清单')) {
        $reason = if ($http) { "更新源返回 HTTP $http" } elseif ($timeout) { '连接更新源超时' } elseif ($slow) { '下载速度持续过低' } else { '无法取得更新内容' }
        return "[${Phase}失败] $target $reason。请检查网络或稍后重试。（错误类型：$kind）"
    }
    return "[${Phase}失败] $target 操作未完成，请将本窗口截图交给管理员。（错误类型：$kind）"
}

function Test-LocalUpdateError {
    param($ErrorRecord)
    $message = Format-UpdateError -ErrorRecord $ErrorRecord
    return $message -match '^\[(文件占用|权限不足|空间不足|替换失败|预检失败|路径错误|状态错误)\]'
}

function Assert-UpdateFilesAvailable {
    param([string]$Root, $Files)
    $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    foreach ($file in @($Files)) {
        $rel = Normalize-Rel ([string]$file.path)
        if (-not (Test-RelativePathSafe $rel)) { throw '[预检失败] 更新清单含无效文件路径，请联系管理员。' }
        # A running batch entry may keep itself open; preserve its existing optional policy.
        if ((Test-OptionalHelperFile $rel) -or -not $seen.Add($rel)) { continue }
        $path = Join-SafeRelativePath -Root $Root -Rel $rel
        if (-not [IO.File]::Exists($path)) { continue }
        if ([IO.File]::GetAttributes($path) -band [IO.FileAttributes]::ReparsePoint) { throw "[预检失败] 文件是链接，无法安全更新：$rel" }
        $handle = $null
        try {
            # Opening for read with no sharing changes neither data nor attributes.
            $handle = [IO.File]::Open($path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::None)
        } catch { throw (Format-UpdateError -ErrorRecord $_ -Phase '预检' -Path $rel) }
        finally { if ($handle) { $handle.Dispose() } }
    }
}

function Install-VerifiedFile {
    param([string]$Temporary,[string]$Destination)
    Assert-SafeAbsolutePath -Path $Destination
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
                if ([IO.File]::Exists($Destination)) { [IO.File]::Replace($Temporary,$Destination,[System.Management.Automation.Language.NullString]::Value) }
                else { [IO.File]::Move($Temporary,$Destination) }
                return
            } catch { if ($attempt -eq 2) { throw }; Start-Sleep -Milliseconds 250 }
        }
    } catch {
        if ($null -ne $oldAttributes -and [IO.File]::Exists($Destination)) { [IO.File]::SetAttributes($Destination,$oldAttributes) }
        throw (Format-UpdateError -ErrorRecord $_ -Phase '替换' -Path $Destination)
    }
}


function Get-BusyRetryDelay {
    # Returns @{Status; Seconds} for HTTP 429/500/502/503/504, otherwise $null.
    param($ErrorRecord, [int]$Attempt)
    if ($Attempt -ge 5) { return $null }
    $ex = if ($ErrorRecord -is [System.Management.Automation.ErrorRecord]) { $ErrorRecord.Exception } else { $ErrorRecord }
    $response = $null
    while ($null -ne $ex) {
        if ($ex -is [System.Net.WebException] -and $null -ne $ex.Response) { $response = $ex.Response; break }
        $ex = $ex.InnerException
    }
    if ($null -eq $response) { return $null }
    $status = 0
    try { $status = [int]$response.StatusCode } catch { return $null }
    if ($status -notin @(429, 500, 502, 503, 504)) { return $null }
    $seconds = [Math]::Min(30.0, 1.5 * [Math]::Pow(2, $Attempt))
    try {
        $retryAfter = [string]$response.Headers['Retry-After']
        if ($retryAfter -match '^\s*\d+\s*$') { $seconds = [Math]::Min(30.0, [Math]::Max($seconds, [double]$retryAfter)) }
    } catch {}
    $seconds += (Get-Random -Minimum 0.0 -Maximum ($seconds * 0.25))
    return [pscustomobject]@{ Status = $status; Seconds = $seconds }
}

function Download-ManifestFile {
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [Parameter(Mandatory = $true)][string]$Destination,
        [Parameter(Mandatory = $true)][string]$ExpectedSha1,
        [switch]$AllowProxy,
        [int]$TimeoutSec = 60,
        [int]$Retries = 1,
        [int]$MinBytesAfterTimeout = 0,
        [int]$MinRateBps = 0,
        [switch]$NoBackoff
    )
    Assert-SafeAbsolutePath -Path $Destination
    New-Item -ItemType Directory -Force (Split-Path -Parent $Destination) | Out-Null
    $tmp = Get-PartialDownloadPath -Destination $Destination -Sha1 $ExpectedSha1
    $identity = [IO.Path]::GetFileName($tmp).Substring(56)
    foreach ($stale in @(Get-ChildItem -LiteralPath (Split-Path -Parent $Destination) -Filter ('.portable-part-*' + $identity) -File -Force)) {
        # SHORT_PATH_V1: compare names; Get-ChildItem reports long names even for an 8.3 short directory path.
        if ($stale.Name -ne [IO.Path]::GetFileName($tmp)) { Assert-SafeAbsolutePath -Path $stale.FullName; [IO.File]::Delete($stale.FullName) }
    }
    if ([IO.File]::Exists($tmp) -and (Get-Sha1 $tmp) -eq $ExpectedSha1) { Install-VerifiedFile -Temporary $tmp -Destination $Destination; return }
    $phase = '下载'
    try {
        $attempt = 0
        $busyAttempt = 0
        while ($true) {
            try {
                Save-UrlToFile -Url $Url -Path $tmp -TimeoutSec $TimeoutSec -AllowProxy:$AllowProxy -MinBytesAfterTimeout $MinBytesAfterTimeout -MinRateBps $MinRateBps -Resume
                break
            } catch {
                # BUSY_BACKOFF_V1: 429/5xx means "busy", not "broken"; wait and retry.
                $busyDelay = if ($NoBackoff) { $null } else { Get-BusyRetryDelay -ErrorRecord $_ -Attempt $busyAttempt }
                if ($null -ne $busyDelay) {
                    $busyAttempt++
                    Write-Host ("[同步] 更新服务繁忙（HTTP {0}），{1:N1} 秒后重试：{2}" -f $busyDelay.Status, $busyDelay.Seconds, (Get-MaskedUrl $Url))
                    Start-Sleep -Milliseconds ([int]($busyDelay.Seconds * 1000))
                    continue
                }
                $attempt++
                if ($attempt -gt $Retries) { throw }
                Start-Sleep -Seconds 2
            }
        }
        $phase = '校验'
        $actual = Get-Sha1 -Path $tmp
        if ($actual -ne $ExpectedSha1.ToLowerInvariant()) { [IO.File]::Delete($tmp); throw "SHA1 mismatch for $(Get-MaskedUrl $Url). expected=$ExpectedSha1 actual=$actual" }
        $phase = '替换'
        Install-VerifiedFile -Temporary $tmp -Destination $Destination
    } catch { throw (Format-UpdateError -ErrorRecord $_ -Phase $phase -Path $Destination) }
    # Verified partials survive local lock errors; incomplete downloads resume next run.
}

# HYBRID_DOWNLOAD_V1: alternate manifests are byte sources, never policy overrides.
function Initialize-HybridSources {
    if ($null -ne $script:HybridSources) { return }
    $script:HybridSources = @()
    $reference = @{}
    foreach ($r in $script:HybridManifest.files) { $reference[[string]$r.path] = ([string]$r.sha1).ToLowerInvariant() + ':' + [string]$r.size }
    $seen = @{}
    foreach ($url in (@($script:HybridManifestUrl) + @($script:HybridManifest.mirrorManifestUrls | Select-Object -First 3))) {
        if ($seen.ContainsKey([string]$url)) { continue }; $seen[[string]$url] = $true
        try {
            $uri = [Uri]$url
            if ($uri.Scheme -notin @('http','https') -or -not $uri.Host -or $uri.UserInfo -or $uri.Fragment) { continue }
            $m = if ($url -eq $script:HybridManifestUrl) { $script:HybridManifest } else { (Read-UrlUtf8Text -Url $url -TimeoutSec 4 -Retries 0) | ConvertFrom-Json }
            if ([string]$m.packId -cne [string]$script:HybridManifest.packId -or [string]$m.version -cne [string]$script:HybridManifest.version -or @($m.files).Count -ne $reference.Count) { throw 'Different mirror release' }
            $rows = @{}
            foreach ($r in $m.files) {
                $rel = [string]$r.path
                if ($rows.ContainsKey($rel) -or -not $reference.ContainsKey($rel) -or $reference[$rel] -cne (([string]$r.sha1).ToLowerInvariant() + ':' + [string]$r.size)) { throw 'Different mirror inventory' }
                $rows[$rel] = $r
            }
            $source = [pscustomobject]@{ Base = $url.Substring(0,$url.LastIndexOf('/')+1); Rows = $rows; Score = 0.0; Failed = $false }
            $sample = $m.files | Sort-Object { [int64]$_.size } -Descending | Select-Object -First 1
            $request = $null; $response = $null; $stream = $null
            $sw = [Diagnostics.Stopwatch]::StartNew()
            try {
                $request = New-DirectHttpRequest -Url (Get-HybridFileUrl $source ([string]$sample.path)) -TimeoutSec 3
                $request.AddRange(0,131071)
                $response = $request.GetResponse(); $stream = $response.GetResponseStream()
                $buf = New-Object byte[] 131072; $got = 0
                while ($got -lt $buf.Length) { $n=$stream.Read($buf,$got,$buf.Length-$got); if ($n -le 0) { break }; $got += $n; if ($sw.Elapsed.TotalSeconds -ge 3) { break } }
                $source.Score = $got / [Math]::Max($sw.Elapsed.TotalSeconds,0.001)
            } catch {} finally { if ($stream) { $stream.Dispose() }; if ($response) { $response.Dispose() }; if ($request) { $request.Abort() } }
            $script:HybridSources += $source
        } catch { Write-Host '[混合源] 已忽略不可用或文件清单不一致的备用源。' }
    }
    $script:HybridSources = @($script:HybridSources | Sort-Object Score -Descending)
    if ($script:HybridSources.Count) { Write-Host ('[混合源] 本机采样优先选择 ' + ([Uri]$script:HybridSources[0].Base).Host + '；失败或持续低速会自动换源。') }
}
function Get-HybridFileUrl {
    param($Source,[string]$Rel)
    $row = $Source.Rows[$Rel]; $remote = $Rel
    if ($row.PSObject.Properties['downloadPath']) { $remote = [string]$row.downloadPath }
    if ($remote -cne $Rel -and $remote -cnotmatch '^blobs/[0-9a-f]{64}$') { throw 'Invalid mirror downloadPath' }
    return Join-UrlPath -BaseUrl $Source.Base -Rel $remote
}
function Download-HybridEntry {
    param($File,[string]$Destination,[string]$ExpectedSha1,[string]$Rel)
    Initialize-HybridSources
    $last = $null
    foreach ($source in $script:HybridSources) {
        if ($source.Failed) { continue }
        try {
            $rate = if ($script:HybridSources.Count -gt 1) { 65536 } else { 0 }
            Download-ManifestFile -Url (Get-HybridFileUrl $source $Rel) -Destination $Destination -ExpectedSha1 $ExpectedSha1 -TimeoutSec 180 -Retries 0 -MinRateBps $rate
            return 'home'
        } catch { if (Test-LocalUpdateError $_) { throw }; $last = $_; Write-Host '[混合源] 下载失败、低速或校验不符，正在切换备用源。' }
    }
    if ($last) { throw $last }
    throw '本轮所有更新源均不可用，请稍后重试。'
}


function Download-ManifestEntry {
    param($File, [Parameter(Mandatory = $true)][string]$BaseUrl, [Parameter(Mandatory = $true)][string]$Destination, [Parameter(Mandatory = $true)][string]$ExpectedSha1, [Parameter(Mandatory = $true)][string]$Rel)
    if (Install-StagedEntry -Destination $Destination -ExpectedSha1 $ExpectedSha1 -Rel $Rel -File $File) { return 'staged' }
    # Downloads are hash-matched at publication and verified again before install.
    # Failure of one URL must not disable CurseForge or Modrinth for other files.
    foreach ($url in @(Get-OfficialFileUrls $File)) {
        $attempts = @($false)
        if (Test-ShouldUseProxyForOfficial $url) { $attempts += $true }
        foreach ($useProxy in $attempts) {
            try {
                Download-ManifestFile -Url $url -Destination $Destination -ExpectedSha1 $ExpectedSha1 -TimeoutSec 8 -Retries 0 -AllowProxy:$useProxy -NoBackoff -MinBytesAfterTimeout $script:OfficialMinBytes -MinRateBps $script:OfficialMinRateBps
                return 'official'
            } catch {
                if (Test-LocalUpdateError $_) { throw }
                Write-Host ("[同步] {0} 不可用（{1}），继续尝试下一下载源：{2}" -f ([Uri]$url).Host, (Get-DownloadFailReason $_.Exception), $Rel)
            }
        }
    }
    if ($script:HybridManifest) { return Download-HybridEntry -File $File -Destination $Destination -ExpectedSha1 $ExpectedSha1 -Rel $Rel }
    $homeTimeout = 180
    try {
        if ($File -and $File.PSObject.Properties['size'] -and [int64]$File.size -gt 0) {
            $homeTimeout = [Math]::Max(120, [Math]::Min(300, [int]([int64]$File.size / 131072) + 60))
        }
    } catch { $homeTimeout = 180 }
    $remote = $Rel
    if ($File -and $File.PSObject.Properties['downloadPath']) {
        $remote = [string]$File.downloadPath
        if ($remote -cne $Rel -and $remote -cnotmatch '^blobs/[0-9a-f]{64}$') { throw 'Invalid cloud downloadPath' }
    }
    Download-ManifestFile -Url (Join-UrlPath -BaseUrl $BaseUrl -Rel $remote) -Destination $Destination -ExpectedSha1 $ExpectedSha1 -TimeoutSec $homeTimeout
    return 'home'
}

function Test-OptionalHelperFile {
    param([Parameter(Mandatory = $true)][string]$Rel)
    $normalized = Normalize-Rel $Rel
    return ($normalized -in @('_updater/Windows-sync.bat', '更新mod-Windows端.bat', '更新mod-Mac端.command', '更新mod.bat', '启动游戏-Windows端.bat', 'Windows-sync.bat', '_updater/macOS-sync.command', '启动游戏-Mac端.command', 'macOS-sync.command', '_updater/portable-stage-daemon.ps1', '_updater/portable-stage-daemon.py'))
}

function Write-SyncWarning {
    param([Parameter(Mandatory = $true)][string]$Message)
    Write-Host ("[warn] " + $Message)
}
function Get-PropertyValue {
    param($Object, [string]$Name, $Default = $null)
    if ($Object -and $Object.PSObject.Properties[$Name]) { return $Object.PSObject.Properties[$Name].Value }
    return $Default
}

function Get-ArrayValue {
    param($Object, [string]$Name, [object[]]$Default = @())
    if ($Object -and $Object.PSObject.Properties[$Name]) { return @($Object.PSObject.Properties[$Name].Value) }
    return $Default
}

function Get-ExistingFileHashIndex {
    param([string]$Root)
    $map = @{}
    foreach ($dir in @('mods','config','defaultconfigs','kubejs','scripts','resourcepacks','shaderpacks','data')) {
        $full = Join-Path $Root $dir
        if (-not [IO.Directory]::Exists($full)) { continue }
        if ((Get-Item -LiteralPath $full -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { continue }
        foreach ($file in @(Get-SafeClientFiles -Root $full)) {
            $rel = Normalize-Rel ($file.FullName.Substring($Root.Length).TrimStart('\','/'))
            if ($rel -like 'mods/.connector/*' -or $rel -like '*/.connector/*') { continue }
            $digest = Get-Sha1 -Path $file.FullName
            if (-not $digest) { continue }
            if (-not $map.ContainsKey($digest)) { $map[$digest] = New-Object 'System.Collections.Generic.List[string]' }
            [void]$map[$digest].Add($file.FullName)
        }
    }
    return $map
}

function Get-AdoptSource {
    # 挑一个本地已有、SHA1 相符的文件来满足 $Target，省去重新下载。返回 Path+Mode：
    #   move = 一份"多余副本"，可以安全地移动消费掉；
    #   copy = 唯一可用的同哈希文件本身也是清单要保留的目标，复制而不是偷走它。
    # 顺手剔除已失效（被前一次接管移走）的索引项，避免指向不存在的路径。
    param($Index, [string]$Expected, [string]$TargetFull, $ManifestTargets)
    if (-not $Index.ContainsKey($Expected)) { return $null }
    $alive = New-Object System.Collections.Generic.List[string]
    foreach ($p in $Index[$Expected]) { if (Test-Path -LiteralPath $p -PathType Leaf) { [void]$alive.Add($p) } }
    $Index[$Expected] = $alive
    $spare = $null
    $twin = $null
    foreach ($p in $alive) {
        $pf = [System.IO.Path]::GetFullPath($p)
        if ($pf -ieq $TargetFull) { continue }
        if ($ManifestTargets.Contains($pf)) { if (-not $twin) { $twin = $p } }
        elseif (-not $spare) { $spare = $p }
    }
    if ($spare) {
        $rest = New-Object System.Collections.Generic.List[string]
        foreach ($p in $alive) { if ($p -ne $spare) { [void]$rest.Add($p) } }
        $Index[$Expected] = $rest
        return [pscustomobject]@{ Path = $spare; Mode = 'move' }
    }
    if ($twin) { return [pscustomobject]@{ Path = $twin; Mode = 'copy' } }
    return $null
}

function Set-OptionLine {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Key, [Parameter(Mandatory = $true)][string]$Value)
    $lines = New-Object 'System.Collections.Generic.List[string]'
    $found = $false
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8 -ErrorAction SilentlyContinue) {
            if ($line -match '^([^:]+):') {
                $k = $Matches[1]
                if ($k -eq $Key) {
                    $lines.Add("${Key}:${Value}")
                    $found = $true
                    continue
                }
            }
            $lines.Add($line)
        }
    }
    if (-not $found) { $lines.Add("${Key}:${Value}") }
    Write-Utf8NoBom -Path $Path -Value (($lines -join "`n") + "`n")
}

function Apply-OptionDefaults {
    param([string]$Root, $Manifest, [string]$BackupRoot)
    $optionsConfig = Get-PropertyValue -Object $Manifest -Name 'playerOptions' -Default $null
    if (-not $optionsConfig) { return 0 }
    $defaults = Get-PropertyValue -Object $optionsConfig -Name 'defaults' -Default $null
    if (-not $defaults) { return 0 }
    $applyMode = [string](Get-PropertyValue -Object $optionsConfig -Name 'apply' -Default 'missing')
    $targets = Get-ArrayValue -Object $optionsConfig -Name 'targets' -Default @('options.txt')
    $changed = 0
    foreach ($targetRel in $targets) {
        if (-not (Test-RelativePathSafe -Rel ([string]$targetRel))) { continue }
        if ($script:PreservedDeletionKeys -and $script:PreservedDeletionKeys.Contains((Normalize-RelKey ([string]$targetRel)))) { continue }
        $path = Join-SafeRelativePath -Root $Root -Rel ([string]$targetRel)
        $exists = Test-Path -LiteralPath $path -PathType Leaf
        if ($applyMode -eq 'missing' -and $exists) { continue }

        $currentValues = @{}
        if ($exists) {
            foreach ($line in Get-Content -LiteralPath $path -Encoding UTF8 -ErrorAction SilentlyContinue) {
                if ($line -match '^([^:]+):(.*)$') { $currentValues[$Matches[1]] = $Matches[2] }
            }
        }
        $pending = New-Object System.Collections.Generic.List[object]
        foreach ($prop in $defaults.PSObject.Properties) {
            $key = [string]$prop.Name
            $value = [string]$prop.Value
            if (-not $currentValues.ContainsKey($key) -or [string]$currentValues[$key] -ne $value) {
                [void]$pending.Add($prop)
            }
        }
        if ($pending.Count -eq 0) { continue }
        if ($exists) { Backup-FileIfExists -Path $path -Rel ([string]$targetRel) -BackupRoot $BackupRoot }
        foreach ($prop in $pending) {
            Set-OptionLine -Path $path -Key ([string]$prop.Name) -Value ([string]$prop.Value)
            $changed++
        }
    }
    if ($changed -gt 0) { Write-Host "[修复] 已写入安全默认选项：$changed 项" }
    return $changed
}

function Ensure-RequiredResourcePacks {
    param([string]$Root, $Manifest, [string]$BackupRoot)
    $optionsConfig = Get-PropertyValue -Object $Manifest -Name 'playerOptions' -Default $null
    if (-not $optionsConfig) { return 0 }
    $requested = @(Get-ArrayValue -Object $optionsConfig -Name 'requiredResourcePacks' -Default @())
    if ($requested.Count -eq 0) { return 0 }

    $required = New-Object 'System.Collections.Generic.List[string]'
    foreach ($item in $requested) {
        $packId = ([string]$item).Trim()
        if ([string]::IsNullOrWhiteSpace($packId) -or $required.Contains($packId)) { continue }
        if ($packId.StartsWith('file/')) {
            $packRel = 'resourcepacks/' + $packId.Substring(5)
            if (-not (Test-RelativePathSafe -Rel $packRel)) {
                Write-Host "[警告] 忽略不安全的必选资源包：$packId" -ForegroundColor Yellow
                continue
            }
            $packPath = Join-SafeRelativePath -Root $Root -Rel $packRel
            if (-not (Test-Path -LiteralPath $packPath -PathType Leaf)) {
                Write-Host "[警告] 必选资源包尚未同步，暂不启用：$packId" -ForegroundColor Yellow
                continue
            }
        }
        [void]$required.Add($packId)
    }
    if ($required.Count -eq 0) { return 0 }

    $changed = 0
    $targets = @(Get-ArrayValue -Object $optionsConfig -Name 'targets' -Default @('options.txt'))
    foreach ($targetRel in $targets) {
        $rel = [string]$targetRel
        if (-not (Test-RelativePathSafe -Rel $rel)) { continue }
        if ($script:PreservedDeletionKeys -and $script:PreservedDeletionKeys.Contains((Normalize-RelKey $rel))) { continue }
        $path = Join-SafeRelativePath -Root $Root -Rel $rel
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { continue }

        $rawValue = $null
        $language = $null
        foreach ($line in Get-Content -LiteralPath $path -Encoding UTF8 -ErrorAction SilentlyContinue) {
            if ($line -match '^resourcePacks:(.*)$') { $rawValue = $Matches[1] }
            elseif ($line -match '^lang:(.*)$') { $language = $Matches[1].Trim() }
        }
        if ([string]::IsNullOrWhiteSpace($language)) {
            Write-Host "[警告] $rel 未找到 lang:zh_cn；Relics 中文需要把游戏语言设为简体中文。未自动修改玩家语言。" -ForegroundColor Yellow
        }
        elseif ($language -ne 'zh_cn') {
            Write-Host "[警告] $rel 当前语言为 $language；Relics 中文需要 lang:zh_cn。未自动修改玩家语言。" -ForegroundColor Yellow
        }
        try {
            $current = if ($null -eq $rawValue) { @('vanilla') } else { @($rawValue | ConvertFrom-Json -ErrorAction Stop) }
        }
        catch {
            Write-Host "[警告] 无法解析 $rel 中的 resourcePacks，已保留原值。" -ForegroundColor Yellow
            continue
        }
        if (@($current | Where-Object { $_ -isnot [string] }).Count -gt 0) {
            Write-Host "[警告] $rel 中的 resourcePacks 不是字符串列表，已保留原值。" -ForegroundColor Yellow
            continue
        }

        $desired = New-Object 'System.Collections.Generic.List[string]'
        foreach ($value in $current) { if (-not $required.Contains([string]$value)) { [void]$desired.Add([string]$value) } }
        foreach ($value in $required) { [void]$desired.Add($value) }
        # Opt-in NeoForge baseline: preserve existing pack order and only add a missing ID.
        if ($optionsConfig.ensureModResourcesBaseline -eq $true -and -not $desired.Contains('mod_resources')) {
            $vanillaIndex = $desired.IndexOf('vanilla')
            $insertIndex = if ($vanillaIndex -ge 0) { $vanillaIndex + 1 } else { 0 }
            $desired.Insert($insertIndex, 'mod_resources')
        }
        $currentJson = ConvertTo-Json -InputObject @($current) -Compress
        $desiredJson = ConvertTo-Json -InputObject @($desired) -Compress
        if ($currentJson -eq $desiredJson) {
            Write-Host "[检查] 服务器汉化包已启用且处于最高优先级：$rel" -ForegroundColor Green
            continue
        }

        Backup-FileIfExists -Path $path -Rel $rel -BackupRoot $BackupRoot
        Set-OptionLine -Path $path -Key 'resourcePacks' -Value $desiredJson
        $changed++
        Write-Host "[修复] 已启用服务器汉化包并保留其他资源包：$rel" -ForegroundColor Green
    }
    return $changed
}

function Write-U16BE { param([System.IO.BinaryWriter]$Writer, [int]$Value) $Writer.Write([byte](($Value -shr 8) -band 0xff)); $Writer.Write([byte]($Value -band 0xff)) }
function Write-I32BE { param([System.IO.BinaryWriter]$Writer, [int]$Value) $Writer.Write([byte](($Value -shr 24) -band 0xff)); $Writer.Write([byte](($Value -shr 16) -band 0xff)); $Writer.Write([byte](($Value -shr 8) -band 0xff)); $Writer.Write([byte]($Value -band 0xff)) }
function Write-NbtStringPayload { param([System.IO.BinaryWriter]$Writer, [string]$Value) $bytes = [System.Text.Encoding]::UTF8.GetBytes($Value); Write-U16BE -Writer $Writer -Value $bytes.Length; $Writer.Write($bytes) }
function Write-NamedTagHeader { param([System.IO.BinaryWriter]$Writer, [byte]$Type, [string]$Name) $Writer.Write($Type); Write-NbtStringPayload -Writer $Writer -Value $Name }

function Write-ServersDat {
    param([string]$Path, [string]$Name, [string]$Ip)
    New-Item -ItemType Directory -Force (Split-Path -Parent $Path) | Out-Null
    $ms = New-Object System.IO.MemoryStream
    $writer = New-Object System.IO.BinaryWriter($ms)
    try {
        Write-NamedTagHeader -Writer $writer -Type 10 -Name ""
        Write-NamedTagHeader -Writer $writer -Type 9 -Name "servers"
        $writer.Write([byte]10)
        Write-I32BE -Writer $writer -Value 1
        Write-NamedTagHeader -Writer $writer -Type 8 -Name "name"
        Write-NbtStringPayload -Writer $writer -Value $Name
        Write-NamedTagHeader -Writer $writer -Type 8 -Name "ip"
        Write-NbtStringPayload -Writer $writer -Value $Ip
        Write-NamedTagHeader -Writer $writer -Type 1 -Name "acceptTextures"
        $writer.Write([byte]1)
        $writer.Write([byte]0)
        $writer.Write([byte]0)
        [System.IO.File]::WriteAllBytes($Path, $ms.ToArray())
    } finally { $writer.Dispose(); $ms.Dispose() }
}

function Show-LauncherHints {
    param([string]$Root)
    $launcherRoot = Join-Path $Root '_launchers'
    if (-not (Test-Path -LiteralPath $launcherRoot -PathType Container)) { return }
    $items = @(
        @{ Name = 'PCL'; Path = Join-Path $launcherRoot 'PCL.exe' },
        @{ Name = 'PCL 最近启动脚本'; Path = Join-Path $launcherRoot 'PCL\LatestLaunch.bat' },
        @{ Name = 'HMCL'; Path = Join-Path $launcherRoot 'HMCL.jar' }
    )
    $found = @($items | Where-Object { Test-Path -LiteralPath $_.Path -PathType Leaf })
    if ($found.Count -le 0) { return }
    Write-Host '[启动器] 已同步启动器文件：'
    foreach ($item in $found) { Write-Host ("[启动器] {0}: {1}" -f $item.Name, $item.Path) }
}
function Get-LauncherInstanceName {
    param([string]$Root, $Manifest)
    $launcher = Get-PropertyValue -Object $Manifest -Name 'launcher' -Default $null
    $pcl = Get-PropertyValue -Object $Manifest -Name 'pcl' -Default $null
    $name = [string](Get-PropertyValue -Object $launcher -Name 'instanceName' -Default '')
    if ([string]::IsNullOrWhiteSpace($name)) { $name = [string](Get-PropertyValue -Object $pcl -Name 'instanceName' -Default '') }
    if (-not [string]::IsNullOrWhiteSpace($name)) { return $name }
    $json = Get-ChildItem -LiteralPath $Root -File -Filter '*.json' -ErrorAction SilentlyContinue | Where-Object { $_.Name -notin @('launcher_profiles.json', '.portable-sync-state.json') } | Sort-Object Length -Descending | Select-Object -First 1
    if ($json) { return [System.IO.Path]::GetFileNameWithoutExtension($json.Name) }
    return (Split-Path -Leaf $Root)
}

function Get-MinecraftHomeForInstance {
    param([string]$Root)
    $parent = Split-Path -Parent $Root
    if ([string]::IsNullOrWhiteSpace($parent)) { return $null }
    $grandparent = Split-Path -Parent $parent
    if ([string]::IsNullOrWhiteSpace($grandparent)) { return $null }
    if (((Split-Path -Leaf $parent) -ieq 'versions') -and ((Split-Path -Leaf $grandparent) -ieq '.minecraft')) {
        return (Split-Path -Parent $grandparent)
    }
    return $null
}

function Ensure-NeoForgeClientExtra {
    param([string]$Root, $Manifest, [string]$BackupRoot)
    # 仅把当前实例已经安装的纯资源 client-extra 加到 classpath，供 CSL 读取 version.json。
    $loader = Get-PropertyValue -Object $Manifest -Name 'loader' -Default $null
    if (([string](Get-PropertyValue -Object $loader -Name 'type' -Default '')).Trim() -ine 'neoforge') { return $false }
    try {
        $launcherHome = Get-MinecraftHomeForInstance -Root $Root
        if (-not $launcherHome) { throw '当前目录不是 .minecraft/versions/<实例>，无法定位客户端库' }
        $mc = ([string](Get-PropertyValue -Object $Manifest -Name 'minecraftVersion' -Default '')).Trim()
        if ($mc -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]*$') { throw '清单缺少有效 Minecraft 版本' }
        $preferred = Join-Path $Root ((Split-Path -Leaf $Root) + '.json')
        $paths = if (Test-Path -LiteralPath $preferred -PathType Leaf) { @((Get-Item -LiteralPath $preferred)) } else { @(Get-ChildItem -LiteralPath $Root -File -Filter '*.json' | Sort-Object Name) }
        $candidates = @()
        foreach ($file in $paths) {
            try { $data = Get-Content -LiteralPath $file.FullName -Raw -Encoding UTF8 | ConvertFrom-Json } catch { continue }
            $id = Get-PropertyValue -Object $data -Name 'id' -Default $null
            if ($id -is [string] -and $id -and ((Get-PropertyValue -Object $data -Name 'mainClass' -Default '') -or (Get-PropertyValue -Object $data -Name 'inheritsFrom' -Default ''))) {
                $candidates += @{ Path = $file.FullName; Name = $file.Name; Data = $data }
            }
        }
        if ($candidates.Count -ne 1) { throw '找不到唯一的当前实例版本 JSON' }
        $selected = $candidates[0]
        $data = $selected.Data
        $libraries = @()
        if ($data.PSObject.Properties['libraries']) { $libraries = $data.libraries }
        if ($null -eq $libraries -or $libraries -isnot [System.Array]) { throw '当前实例 libraries 格式不适用' }
        $arguments = Get-PropertyValue -Object $data -Name 'arguments' -Default $null
        $gameArgs = @()
        if ($arguments -and $arguments.PSObject.Properties['game']) { $gameArgs = $arguments.game }
        if ($null -eq $gameArgs -or $gameArgs -isnot [System.Array]) { throw '当前实例游戏参数格式不适用' }
        $values = @{}
        for ($i = 0; $i -lt ($gameArgs.Count - 1); $i++) {
            if ($gameArgs[$i] -is [string] -and $gameArgs[$i + 1] -is [string] -and $gameArgs[$i] -in @('--fml.neoForgeVersion', '--fml.mcVersion', '--fml.neoFormVersion', '--fml.mcpVersion')) {
                if (-not $values.ContainsKey($gameArgs[$i])) { $values[$gameArgs[$i]] = $gameArgs[$i + 1] }
            }
        }
        $hasNeoForge = -not [string]::IsNullOrWhiteSpace([string]$values['--fml.neoForgeVersion'])
        foreach ($library in $libraries) {
            if ([string](Get-PropertyValue -Object $library -Name 'name' -Default '') -clike 'net.neoforged:neoforge:*') { $hasNeoForge = $true }
        }
        if (-not $hasNeoForge) { throw '当前实例版本 JSON 没有 NeoForge 标记，已跳过' }
        $declaredMc = [string]$values['--fml.mcVersion']
        $parentVersion = [string](Get-PropertyValue -Object $data -Name 'inheritsFrom' -Default '')
        if (($declaredMc -and $declaredMc -cne $mc) -or ($parentVersion -match '^\d+\.\d+(?:\.\d+)?$' -and $parentVersion -cne $mc)) { throw '当前实例 Minecraft 版本与更新清单不一致' }
        $neoform = [string]$values['--fml.neoFormVersion']
        if (-not $neoform) { $neoform = [string]$values['--fml.mcpVersion'] }
        if ($neoform -and $neoform -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]*$') { throw '当前实例 NeoForm 版本格式不适用' }
        $minecraftDir = Join-Path $launcherHome '.minecraft'
        $clientDir = Join-Path $minecraftDir 'libraries\net\minecraft\client'
        $jars = @()
        if ($neoform) {
            $coordinateVersion = "$mc-$neoform"
            $jarPath = Join-Path (Join-Path $clientDir $coordinateVersion) ("client-$coordinateVersion-extra.jar")
            if (Test-Path -LiteralPath $jarPath -PathType Leaf) { $jars = @((Get-Item -LiteralPath $jarPath)) }
        } elseif (Test-Path -LiteralPath $clientDir -PathType Container) {
            foreach ($dir in @(Get-ChildItem -LiteralPath $clientDir -Directory | Where-Object { $_.Name.StartsWith("$mc-", [StringComparison]::Ordinal) })) {
                $jarPath = Join-Path $dir.FullName ("client-" + $dir.Name + '-extra.jar')
                if (Test-Path -LiteralPath $jarPath -PathType Leaf) { $jars += Get-Item -LiteralPath $jarPath }
            }
        }
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $valid = @()
        $invalid = @()
        foreach ($jar in $jars) {
            $archive = $null
            $reader = $null
            try {
                $archive = [IO.Compression.ZipFile]::OpenRead($jar.FullName)
                if (@($archive.Entries | Where-Object { $_.FullName.EndsWith('.class', [StringComparison]::OrdinalIgnoreCase) }).Count -gt 0) { $invalid += 'client-extra 包含 class，拒绝加入普通 classpath'; continue }
                $entries = @($archive.Entries | Where-Object { $_.FullName -ceq 'version.json' })
                if ($entries.Count -ne 1) { $invalid += 'client-extra 缺少唯一 version.json'; continue }
                $reader = New-Object IO.StreamReader($entries[0].Open())
                $metadata = $reader.ReadToEnd() | ConvertFrom-Json
                if ([string](Get-PropertyValue -Object $metadata -Name 'id' -Default '') -cne $mc) { $invalid += 'client-extra 内 Minecraft 版本与更新清单不一致'; continue }
                $valid += $jar
            } catch { $invalid += 'client-extra 无法读取' } finally {
                if ($reader) { $reader.Dispose() }
                if ($archive) { $archive.Dispose() }
            }
        }
        if ($valid.Count -ne 1) {
            if ($valid.Count -gt 1) { throw '同一 Minecraft 版本存在多个 client-extra，且实例未指定 NeoForm 版本' }
            if ($invalid.Count -gt 0) { throw $invalid[0] }
            throw '缺少当前实例已安装的 client-extra.jar，请先用启动器补全游戏库后重新更新'
        }
        $jar = $valid[0]
        $coordinateVersion = $jar.Directory.Name
        $name = "net.minecraft:client:${coordinateVersion}:extra"
        $relative = "net/minecraft/client/$coordinateVersion/client-$coordinateVersion-extra.jar"
        foreach ($library in $libraries) {
            $existing = [string](Get-PropertyValue -Object $library -Name 'name' -Default '')
            if ($existing -ceq $name) {
                $downloads = Get-PropertyValue -Object $library -Name 'downloads' -Default $null
                $artifact = Get-PropertyValue -Object $downloads -Name 'artifact' -Default $null
                $ordinary = ($null -eq $downloads) -or ($null -ne $artifact -and ([string](Get-PropertyValue -Object $artifact -Name 'path' -Default $relative) -ceq $relative))
                if ($ordinary -and -not (Get-PropertyValue -Object $library -Name 'rules' -Default $null) -and -not (Get-PropertyValue -Object $library -Name 'natives' -Default $null) -and -not (Get-PropertyValue -Object $library -Name 'extract' -Default $null)) {
                    Write-Host '[启动修复] 当前实例已包含 client-extra 普通库，无需修改。'
                    return $false
                }
                throw 'client-extra 已有特殊规则或分类器配置，请检查启动器版本配置'
            }
            if ($existing -clike 'net.minecraft:client:*:extra') { throw '当前实例已声明其他 client-extra 版本，未覆盖玩家配置' }
        }
        $entry = [ordered]@{ name = $name; downloads = [ordered]@{ artifact = [ordered]@{ path = $relative; sha1 = (Get-Sha1 -Path $jar.FullName); size = $jar.Length } } }
        $data | Add-Member -NotePropertyName 'libraries' -NotePropertyValue (@($libraries) + @($entry)) -Force
        $encoded = ($data | ConvertTo-Json -Depth 100) + "`r`n"
        Backup-FileIfExists -Path $selected.Path -Rel $selected.Name -BackupRoot $BackupRoot
        $temporary = $selected.Path + '.' + [Guid]::NewGuid().ToString('N') + '.tmp'
        try {
            Write-Utf8NoBom -Path $temporary -Value $encoded
            [IO.File]::Replace($temporary, $selected.Path, [System.Management.Automation.Language.NullString]::Value)
        } finally {
            if (Test-Path -LiteralPath $temporary -PathType Leaf) { Remove-Item -LiteralPath $temporary -Force }
        }
        Write-Host '[启动修复] 已补齐当前实例 client-extra 资源库。请完全退出游戏并重新打开启动器，再启动游戏。'
        return $true
    } catch {
        Write-Host ("[启动修复未完成] {0}；当前实例版本 JSON 未改。" -f $_.Exception.Message) -ForegroundColor Yellow
        return $false
    }
}


function Write-PclGlobalConfig {
    param([string]$PclHome, [string]$MinecraftDirValue, [string]$InstanceName, [string]$BackupRoot)
    # 温和写入：PCL.ini 缺失才创建；Setup.ini 只维护 LaunchFolderSelect 一行，
    # 保留玩家自己的其他 PCL 设置，避免每次同步重置启动器个人配置。
    $pclIni = Join-Path $PclHome 'PCL.ini'
    if (-not (Test-Path -LiteralPath $pclIni -PathType Leaf)) {
        $cardName = $InstanceName.Replace(':', '-')
        Write-Utf8NoBom -Path $pclIni -Value ("InstanceCache:{0}`r`nVersion:{1}`r`nCardKey1:2`r`nCardValue1:2:{1}:1:`r`nCardCount:1`r`n" -f ([Math]::Abs($PclHome.GetHashCode())), $cardName)
    }

    $pclDir = Join-Path $PclHome 'PCL'
    New-Item -ItemType Directory -Force $pclDir | Out-Null
    $setup = Join-Path $pclDir 'Setup.ini'
    $desired = ("LaunchFolderSelect:{0}" -f $MinecraftDirValue)
    if (Test-Path -LiteralPath $setup -PathType Leaf) {
        $lines = @(Get-Content -LiteralPath $setup -Encoding UTF8 -ErrorAction SilentlyContinue)
        if ($lines -contains $desired) { return }
        Backup-FileIfExists -Path $setup -Rel 'pcl-home/PCL/Setup.ini' -BackupRoot $BackupRoot
        $kept = @($lines | Where-Object { $_ -notmatch '^LaunchFolderSelect:' })
        $out = @($desired) + $kept
        Write-Utf8NoBom -Path $setup -Value (($out -join "`r`n") + "`r`n")
    } else {
        Write-Utf8NoBom -Path $setup -Value ($desired + "`r`n")
    }
}

function Copy-LauncherFileToRoot {
    param([string]$Root, [string]$Relative, [string]$BackupRoot)
    $source = Join-SafeRelativePath -Root (Join-Path $Root '_launchers') -Rel $Relative
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { return $null }
    $dest = Join-SafeRelativePath -Root $Root -Rel $Relative
    # 启动器（HMCL / PCL 等）会自我升级：玩家点了“升级”后，本地版本比包内自带的更新。
    # 只做「首次落位」——已存在就保留玩家自己的版本，绝不用包内旧版覆盖回去，否则每次同步
    # 都把玩家升级好的启动器打回原形（与 .py 端一致，2026-07-11 实锤）。
    if (Test-Path -LiteralPath $dest -PathType Leaf) { return $dest }
    New-Item -ItemType Directory -Force (Split-Path -Parent $dest) | Out-Null
    Copy-Item -LiteralPath $source -Destination $dest -Force
    return $dest
}

function Apply-LauncherProfile {
    param([string]$Root, $Manifest, [string]$BackupRoot)
    $launcherRoot = Join-Path $Root '_launchers'
    if (-not (Test-Path -LiteralPath $launcherRoot -PathType Container)) { return }

    $instanceName = Get-LauncherInstanceName -Root $Root -Manifest $Manifest
    $packName = [string](Get-PropertyValue -Object $Manifest -Name 'packName' -Default $instanceName)
    if ([string]::IsNullOrWhiteSpace($packName)) { $packName = $instanceName }
    $mcVersion = [string](Get-PropertyValue -Object $Manifest -Name 'minecraftVersion' -Default '')
    $loader = Get-PropertyValue -Object $Manifest -Name 'loader' -Default $null
    $loaderType = [string](Get-PropertyValue -Object $loader -Name 'type' -Default '')
    $loaderVersion = [string](Get-PropertyValue -Object $loader -Name 'version' -Default '')

    foreach ($rel in @('PCL.exe', 'Plain Craft Launcher.exe', 'SakuraLauncher.exe', 'HMCL.jar')) {
        [void](Copy-LauncherFileToRoot -Root $Root -Relative $rel -BackupRoot $BackupRoot)
    }

    $mcHome = Get-MinecraftHomeForInstance -Root $Root
    if ($mcHome) {
        foreach ($rel in @('PCL.exe', 'Plain Craft Launcher.exe', 'SakuraLauncher.exe', 'HMCL.jar')) {
            $source = Join-SafeRelativePath -Root $launcherRoot -Rel $rel
            if (Test-Path -LiteralPath $source -PathType Leaf) {
                $dest = Join-Path $mcHome $rel
                # 同上：启动器自升级后不回退，缺失才落位。
                if (Test-Path -LiteralPath $dest -PathType Leaf) { continue }
                Copy-Item -LiteralPath $source -Destination $dest -Force
            }
        }
    }

    $profilePath = Join-Path $Root 'launcher_profiles.json'
    Backup-FileIfExists -Path $profilePath -Rel 'launcher_profiles.json' -BackupRoot $BackupRoot
    $profile = [ordered]@{
        profiles = [ordered]@{
            Portable = [ordered]@{
                icon = 'Furnace'
                name = $packName
                lastVersionId = $instanceName
                type = 'custom'
                lastUsed = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss.0000Z")
            }
        }
        selectedProfile = 'Portable'
        clientToken = '23323323323323323323323323323333'
    }
    Write-Utf8NoBom -Path $profilePath -Value (($profile | ConvertTo-Json -Depth 8) + "`r`n")

    $pclIni = Join-Path $Root 'PCL.ini'
    Backup-FileIfExists -Path $pclIni -Rel 'PCL.ini' -BackupRoot $BackupRoot
    $cardName = $instanceName.Replace(':', '-')
    Write-Utf8NoBom -Path $pclIni -Value ("InstanceCache:{0}`r`nVersion:{1}`r`nCardKey1:2`r`nCardValue1:2:{1}:1:`r`nCardCount:1`r`n" -f ([Math]::Abs($Root.GetHashCode())), $cardName)

    $pclDir = Join-Path $Root 'PCL'
    New-Item -ItemType Directory -Force $pclDir | Out-Null
    $setup = Join-Path $pclDir 'Setup.ini'
    Backup-FileIfExists -Path $setup -Rel 'PCL/Setup.ini' -BackupRoot $BackupRoot
    $info = if ($loaderType -and $loaderVersion) { "正式版 $mcVersion, $($loaderType.Substring(0,1).ToUpper() + $loaderType.Substring(1)) $loaderVersion" } else { "正式版 $mcVersion" }
    $lines = New-Object 'System.Collections.Generic.List[string]'
    $lines.Add('State:9')
    if ($mcVersion) { $lines.Add("VersionVanillaName:$mcVersion") }
    $lines.Add('VersionArgumentIndieV2:True')
    $lines.Add("Info:$info")
    $lines.Add('VersionLiteLoader:False')
    if ($loaderType -ieq 'forge' -and $loaderVersion) { $lines.Add("VersionForge:$loaderVersion") }
    if ($loaderType -ieq 'fabric' -and $loaderVersion) { $lines.Add("VersionFabric:$loaderVersion") }
    $lines.Add('Logo:pack://application:,,,/Plain Craft Launcher 2;component/Images/Blocks/Anvil.png')
    $lines.Add("LaunchFolderSelect:$Root")
    Write-Utf8NoBom -Path $setup -Value (($lines -join "`r`n") + "`r`n")

    if ($mcHome) {
        $profileHome = Join-Path (Join-Path $mcHome '.minecraft') 'launcher_profiles.json'
        Backup-FileIfExists -Path $profileHome -Rel 'pcl-home/.minecraft/launcher_profiles.json' -BackupRoot $BackupRoot
        Write-Utf8NoBom -Path $profileHome -Value (($profile | ConvertTo-Json -Depth 8) + "`r`n")
        Write-PclGlobalConfig -PclHome $mcHome -MinecraftDirValue '$.minecraft\' -InstanceName $instanceName -BackupRoot $BackupRoot
    }

    # Mirror launcher config for older wrappers that still start PCL/HMCL from _launchers.
    foreach ($rel in @('launcher_profiles.json', 'PCL.ini', 'PCL/Setup.ini')) {
        $src = Join-SafeRelativePath -Root $Root -Rel $rel
        $dst = Join-SafeRelativePath -Root $launcherRoot -Rel $rel
        if (Test-Path -LiteralPath $src -PathType Leaf) {
            if (Test-Path -LiteralPath $dst -PathType Leaf) { Backup-FileIfExists -Path $dst -Rel ("_launchers/" + $rel) -BackupRoot $BackupRoot }
            New-Item -ItemType Directory -Force (Split-Path -Parent $dst) | Out-Null
            Copy-Item -LiteralPath $src -Destination $dst -Force
        }
    }
    Write-Host ("[启动器] 已写入本地实例配置：{0}" -f $instanceName)
}
function Apply-ServerList {
    param([string]$Root, $Manifest, [string]$BackupRoot)
    $serverList = Get-PropertyValue -Object $Manifest -Name 'serverList' -Default $null
    if (-not $serverList -or -not [bool](Get-PropertyValue -Object $serverList -Name 'enabled' -Default $false)) { return }
    $target = [string](Get-PropertyValue -Object $serverList -Name 'target' -Default 'servers.dat')
    $name = [string](Get-PropertyValue -Object $serverList -Name 'name' -Default (Get-PropertyValue -Object $Manifest -Name 'serverName' -Default 'Minecraft Server'))
    $address = [string](Get-PropertyValue -Object $serverList -Name 'address' -Default (Get-PropertyValue -Object $Manifest -Name 'serverAddress' -Default ''))
    $writeIfMissingOnly = [bool](Get-PropertyValue -Object $serverList -Name 'writeIfMissingOnly' -Default $true)
    if ([string]::IsNullOrWhiteSpace($address) -or -not (Test-RelativePathSafe -Rel $target)) { return }
    if ($script:PreservedDeletionKeys -and $script:PreservedDeletionKeys.Contains((Normalize-RelKey $target))) { return }
    $path = Join-SafeRelativePath -Root $Root -Rel $target
    if ($writeIfMissingOnly) {
        if (Test-Path -LiteralPath $path -PathType Leaf) { return }
        if (-not $script:InitialSync) { return }
    }
    Backup-FileIfExists -Path $path -Rel $target -BackupRoot $BackupRoot
    Write-ServersDat -Path $path -Name $name -Ip $address
    Write-Host "[修复] 已写入服务器列表：$name / $address"
}

function Move-ToDisabledDir {
    param([string]$Path, [string]$DisabledDir)
    New-Item -ItemType Directory -Force $DisabledDir | Out-Null
    $item = Get-Item -LiteralPath $Path
    $dest = Join-Path $DisabledDir $item.Name
    if (Test-Path -LiteralPath $dest) { $dest = Join-Path $DisabledDir ("{0}.{1}{2}" -f $item.BaseName, (Get-Date -Format 'yyyyMMddHHmmss'), $item.Extension) }
    Move-Item -LiteralPath $Path -Destination $dest -Force
    return $dest
}

function Remove-ConnectorRuntimeCache {
    param([string]$Root)
    $cache = Join-SafeRelativePath -Root $Root -Rel 'mods/.connector'
    if (Test-Path -LiteralPath $cache -PathType Container) {
        Remove-Item -LiteralPath $cache -Recurse -Force -ErrorAction SilentlyContinue
        Write-Host '[修复] 已清理 Connector 运行缓存：mods/.connector'
    }
}

function Disable-LauncherRepairIndex {
    param([string[]]$Roots)
    foreach ($root in $Roots) {
        if ([string]::IsNullOrWhiteSpace($root) -or -not (Test-Path -LiteralPath $root -PathType Container)) { continue }
        $index = Join-Path $root 'modrinth.index.json'
        if (-not (Test-Path -LiteralPath $index -PathType Leaf)) { continue }
        $dest = Move-ToDisabledDir -Path $index -DisabledDir (Join-Path $root '_disabled_launcher_repair')
        Write-Host "[修复] 已禁用启动器修包索引：$dest"
    }
}

function Get-JarModId {
    param([string]$Path)
    try {
        Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction SilentlyContinue
        $zip = [System.IO.Compression.ZipFile]::OpenRead($Path)
        try {
            foreach ($name in @('fabric.mod.json', 'quilt.mod.json')) {
                $entry = $zip.GetEntry($name)
                if ($entry) {
                    $reader = New-Object System.IO.StreamReader($entry.Open(), [System.Text.Encoding]::UTF8, $true)
                    try { $json = $reader.ReadToEnd() | ConvertFrom-Json } finally { $reader.Dispose() }
                    if ($json.id) { return ([string]$json.id).ToLowerInvariant() }
                    if ($json.quilt_loader -and $json.quilt_loader.id) { return ([string]$json.quilt_loader.id).ToLowerInvariant() }
                }
            }
            foreach ($name in @('META-INF/mods.toml', 'META-INF/neoforge.mods.toml')) {
                $entry = $zip.GetEntry($name)
                if ($entry) {
                    $reader = New-Object System.IO.StreamReader($entry.Open(), [System.Text.Encoding]::UTF8, $true)
                    try { $text = $reader.ReadToEnd() } finally { $reader.Dispose() }
                    $m = [regex]::Match($text, '(?m)^\s*modId\s*=\s*["'']([^"'']+)["'']')
                    if ($m.Success) { return $m.Groups[1].Value.ToLowerInvariant() }
                }
            }
            $mcmod = $zip.GetEntry('mcmod.info')
            if ($mcmod) {
                $reader = New-Object System.IO.StreamReader($mcmod.Open(), [System.Text.Encoding]::UTF8, $true)
                try { $json = $reader.ReadToEnd() | ConvertFrom-Json } finally { $reader.Dispose() }
                if ($json -is [array] -and $json.Count -gt 0 -and $json[0].modid) { return ([string]$json[0].modid).ToLowerInvariant() }
            }
        } finally { $zip.Dispose() }
    } catch { return $null }
    return $null
}

function Disable-DuplicateMods {
    param([string]$Root, $Manifest)
    $mods = Join-Path $Root 'mods'
    if (-not (Test-Path -LiteralPath $mods -PathType Container)) { return }
    $managedPaths = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
    $managedSha1 = @{}
    foreach ($item in @($Manifest.files)) {
        $rel = Normalize-Rel ([string]$item.path)
        if ($rel -like 'mods/*.jar') {
            [void]$managedPaths.Add($rel)
            $managedSha1[[string]$item.sha1] = $rel
        }
    }
    $disabled = Join-Path $Root '_disabled_mod_duplicates'
    $moved = 0
    Get-ChildItem -LiteralPath $mods -File -Filter '*.jar' -ErrorAction SilentlyContinue | ForEach-Object {
        $rel = $_.FullName.Substring($Root.Length).TrimStart('\', '/') -replace '\\', '/'
        if ($managedPaths.Contains($rel)) { return }
        $digest = Get-Sha1 -Path $_.FullName
        if ($managedSha1.ContainsKey($digest)) {
            $dest = Move-ToDisabledDir -Path $_.FullName -DisabledDir $disabled
            Write-Host "[修复] 已按 SHA1 归档旧重复 mod：$rel -> $dest"
            $script:DuplicateMoved++
        }
    }
    $groups = @{}
    Get-ChildItem -LiteralPath $mods -File -Filter '*.jar' -ErrorAction SilentlyContinue | ForEach-Object {
        $modId = Get-JarModId -Path $_.FullName
        if ([string]::IsNullOrWhiteSpace($modId)) { return }
        if (-not $groups.ContainsKey($modId)) { $groups[$modId] = New-Object System.Collections.ArrayList }
        [void]$groups[$modId].Add($_)
    }
    foreach ($modId in $groups.Keys) {
        $items = @($groups[$modId])
        if ($items.Count -le 1) { continue }
        $keep = $items | Sort-Object -Descending @{Expression={ $rel=$_.FullName.Substring($Root.Length).TrimStart('\','/').Replace('\','/'); if($managedPaths.Contains($rel)){2}else{0} }}, LastWriteTime | Select-Object -First 1
        foreach ($jar in $items) {
            if ($jar.FullName -eq $keep.FullName) { continue }
            $dest = Move-ToDisabledDir -Path $jar.FullName -DisabledDir $disabled
            Write-Host "[修复] 已归档重复 modId=$modId；保留 $($keep.Name)，移动 $($jar.Name)"
            $script:DuplicateMoved++
        }
    }
    if ($script:DuplicateMoved -gt $moved) { Write-Host "[修复] 重复 mod 清理完成，移动 $($script:DuplicateMoved - $moved) 个" }
}

function Assert-SafeAbsolutePath {
    param([string]$Path, [string]$Boundary = '')
    $full = [IO.Path]::GetFullPath($Path)
    if (-not $Boundary) { $Boundary = $script:SyncRoot }
    if (-not $Boundary) { $Boundary = [IO.Path]::GetDirectoryName($full) }
    $root = [IO.Path]::GetFullPath($Boundary).TrimEnd('\')
    if ($full -ne $root -and -not $full.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)) { throw '[路径错误] 更新目标超出允许的目录。' }
    $cursor = $full
    while ($cursor -and $cursor.Length -ge $root.Length) {
        if (Test-Path -LiteralPath $cursor) {
            if ((Get-Item -LiteralPath $cursor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "[路径错误] 更新路径经过目录链接或文件链接，请检查：$cursor" }
        }
        if ($cursor -eq $root) { break }
        $cursor = [IO.Path]::GetDirectoryName($cursor)
    }
}

function Enter-SyncLock {
    param([string]$Root)
    $path = Join-SafeRelativePath -Root $Root -Rel '.portable-sync.lock'
    try { return [IO.File]::Open($path, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None) }
    catch { throw '[正在更新] 此实例已有更新或后台缓存任务正在运行，请等待其完成后再试。' }
}

function Exit-SyncLock { param($Handle) if ($Handle) { $Handle.Dispose() } }

function Write-AtomicUtf8 {
    param([string]$Path, [string]$Value)
    Assert-SafeAbsolutePath -Path $Path
    $temp = $Path + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
    try {
        [IO.File]::WriteAllText($temp, $Value, [Text.UTF8Encoding]::new($false))
        Install-VerifiedFile -Temporary $temp -Destination $Path
    } finally { if ([IO.File]::Exists($temp)) { [IO.File]::Delete($temp) } }
}

function Test-SyncStateData {
    param($Value)
    if (-not $Value -or -not ($Value.files -is [Array])) { return $false }
    foreach ($file in $Value.files) {
        if (-not $file -or -not $file.path -or -not (Test-RelativePathSafe ([string]$file.path)) -or [string]$file.sha1 -notmatch '^[a-fA-F0-9]{40}$') { return $false }
    }
    return $true
}

function Read-ValidSyncState {
    param([string]$Path)
    $previous = Join-Path (Split-Path -Parent $Path) '.portable-sync-state.prev.json'
    if (-not [IO.File]::Exists($Path) -and -not [IO.File]::Exists($previous)) { return $null }
    foreach ($candidate in @($Path, $previous)) {
        if (-not [IO.File]::Exists($candidate)) { continue }
        Assert-SafeAbsolutePath -Path $candidate
        try {
            $data = [IO.File]::ReadAllText($candidate, [Text.Encoding]::UTF8) | ConvertFrom-Json
            if (-not (Test-SyncStateData $data)) { throw 'invalid state' }
            if ($candidate -ne $Path) { Write-Host '[恢复] 当前同步状态损坏或缺失，已读取上一份有效状态。' }
            return $data
        } catch { }
    }
    throw '[状态错误] 同步状态损坏且没有有效备份。已停止更新以保护个人修改，请将日志交给管理员。'
}

function Save-SyncState {
    param([string]$Path, $Value)
    $previous = Join-Path (Split-Path -Parent $Path) '.portable-sync-state.prev.json'
    $old = $null
    if ([IO.File]::Exists($Path)) {
        try { $raw = [IO.File]::ReadAllText($Path, [Text.Encoding]::UTF8); $old = $raw | ConvertFrom-Json; if (-not (Test-SyncStateData $old)) { $old = $null } } catch { $old = $null }
    }
    if ($old) { Write-AtomicUtf8 -Path $previous -Value $raw }
    elseif (-not [IO.File]::Exists($Path) -and -not [IO.File]::Exists($previous)) { Write-AtomicUtf8 -Path $previous -Value (($Value | ConvertTo-Json -Depth 12) + "`n") }
    Write-AtomicUtf8 -Path $Path -Value (($Value | ConvertTo-Json -Depth 12) + "`n")
}

function Assert-ManifestFiles {
    param([string]$Root, $Files, $DeleteGlobs=@())
    $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    foreach ($file in @($Files)) {
        $rel = [string]$file.path
        if (-not (Test-RelativePathSafe $rel)) { throw '[清单错误] 清单包含无效文件路径。' }
        $path = Join-SafeRelativePath -Root $Root -Rel $rel
        if (-not $seen.Add([IO.Path]::GetFullPath($path))) { throw "[清单错误] 清单包含重复文件路径：$rel" }
        if ([string]$file.sha1 -notmatch '^[a-fA-F0-9]{40}$') { throw "[清单错误] 文件校验值无效：$rel" }
        if (Test-ProtectedRel $rel) { throw "[清单错误] 清单不能覆盖更新器运行记录：$rel" }
        if (Test-GlobMatch -Rel $rel -Globs $DeleteGlobs) { throw "[清单错误] 同一路径同时要求安装和删除：$rel" }
    }
}

function Initialize-SyncJournal {
    param([string]$Root, $Manifest, [hashtable]$PreviousMap, [hashtable]$PreviousRelByKey)
    $script:JournalPath = Join-SafeRelativePath -Root $Root -Rel '.portable-sync-pending.json'
    $pack = [string](Get-PropertyValue $Manifest 'packId' (Get-PropertyValue $Manifest 'pack' ''))
    $script:Journal = @{ format=1; packId=$pack; version=[string]$Manifest.version; applied=@{} }
    if (-not [IO.File]::Exists($script:JournalPath)) { return }
    try { $old = [IO.File]::ReadAllText($script:JournalPath, [Text.Encoding]::UTF8) | ConvertFrom-Json } catch { throw '[状态错误] 未完成更新的记录损坏，请将日志交给管理员。' }
    if (-not $old -or $old.format -ne 1 -or [string]$old.packId -cne $pack -or -not $old.PSObject.Properties['applied']) { throw '[状态错误] 未完成更新记录不属于当前整合包或格式无效。' }
    foreach ($entry in $old.applied.PSObject.Properties) {
        $rel = [string]$entry.Value.path; $hash = [string]$entry.Value.sha1
        if (-not (Test-RelativePathSafe $rel) -or $hash -notmatch '^[a-fA-F0-9]{40}$') { throw '[状态错误] 未完成更新记录包含无效路径或校验值。' }
        $target = Join-SafeRelativePath -Root $Root -Rel $rel
        if ([IO.File]::Exists($target) -and (Get-Sha1 $target) -eq $hash) {
            $key = Normalize-RelKey $rel
            $PreviousMap[$key] = $hash; $PreviousRelByKey[$key] = $rel
            $script:Journal.applied[$key] = @{path=$rel;sha1=$hash}
        }
    }
    Write-Host '[恢复] 已核对上次未完成更新的文件，将继续同步并保留真正的个人修改。'
}

function Add-SyncIntent {
    param([string]$Rel, [string]$Sha1, [switch]$Save)
    if (-not $script:Journal) { return }
    $script:Journal.applied[(Normalize-RelKey $Rel)] = @{path=$Rel;sha1=$Sha1}
    if ($Save) { Save-SyncJournal }
}

function Save-SyncJournal {
    if ($script:Journal) { Write-AtomicUtf8 -Path $script:JournalPath -Value (($script:Journal | ConvertTo-Json -Depth 8) + "`n") }
}

function Write-Host {
    [CmdletBinding()]
    param([Parameter(Position=0,ValueFromRemainingArguments=$true)][object[]]$Object,
          [object]$Separator=' ', [ConsoleColor]$ForegroundColor, [ConsoleColor]$BackgroundColor, [switch]$NoNewline)
    $text = ($Object | ForEach-Object { [string]$_ }) -join $Separator
    $safe = [regex]::Replace($text, 'https?://[^\s；，)]+', { param($m) Get-MaskedUrl $m.Value })
    $arguments = @{Object=$safe;NoNewline=$NoNewline}
    if ($PSBoundParameters.ContainsKey('ForegroundColor')) { $arguments.ForegroundColor=$ForegroundColor }
    if ($PSBoundParameters.ContainsKey('BackgroundColor')) { $arguments.BackgroundColor=$BackgroundColor }
    Microsoft.PowerShell.Utility\Write-Host @arguments
    if ($script:SyncLogPath) {
        try { [IO.File]::AppendAllText($script:SyncLogPath, $safe + $(if ($NoNewline) { '' } else { "`n" }), [Text.UTF8Encoding]::new($false)) } catch { }
    }
}

function Start-SyncLog {
    param([string]$Root)
    $directory = Join-SafeRelativePath -Root $Root -Rel '.portable-sync-logs'
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
    $script:SyncLogPath = Join-SafeRelativePath -Root $Root -Rel '.portable-sync-logs/latest.log'
    if ([IO.File]::Exists($script:SyncLogPath)) { [IO.File]::Copy($script:SyncLogPath, (Join-SafeRelativePath -Root $Root -Rel '.portable-sync-logs/previous.log'), $true) }
    [IO.File]::WriteAllText($script:SyncLogPath, ('[诊断] 开始同步 ' + (Get-Date -Format o) + "`n"), [Text.UTF8Encoding]::new($false))
    Write-Host "[诊断] 日志：$script:SyncLogPath"
}

function Get-PartialDownloadPath {
    param([string]$Destination, [string]$Sha1)
    $hasher = [Security.Cryptography.SHA256]::Create()
    try { $id = ([BitConverter]::ToString($hasher.ComputeHash([Text.Encoding]::UTF8.GetBytes([IO.Path]::GetFileName($Destination))))).Replace('-','').ToLowerInvariant().Substring(0,16) } finally { $hasher.Dispose() }
    return Join-Path ([IO.Path]::GetDirectoryName($Destination)) ('.portable-part-' + $Sha1.ToLowerInvariant() + '-' + $id + '.tmp')
}

function Get-SafeClientFiles {
    param([string]$Root)
    $queue = New-Object 'System.Collections.Generic.Queue[string]'
    $queue.Enqueue($Root)
    while ($queue.Count) {
        $directory = $queue.Dequeue()
        foreach ($item in @(Get-ChildItem -LiteralPath $directory -Force -ErrorAction SilentlyContinue)) {
            $rel = Normalize-Rel ($item.FullName.Substring($Root.Length).TrimStart('\','/'))
            if (Test-ProtectedRel ($rel + $(if ($item.PSIsContainer) { '/' } else { '' }))) { continue }
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { continue }
            if ($item.PSIsContainer) { $queue.Enqueue($item.FullName) } else { $item }
        }
    }
}

function Get-ForceDeleteTargets {
    param([string]$Root, $Patterns)
    $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    $wildcards = New-Object 'System.Collections.Generic.List[string]'
    foreach ($pattern in @($Patterns)) {
        $rel = Normalize-Rel ([string]$pattern)
        if (-not $rel) { continue }
        if (-not (Test-RelativePathSafe ($rel.Replace('*','x').Replace('?','x')))) { throw '[清单错误] 删除规则包含无效路径。' }
        if (Test-ProtectedRel $rel) { continue }
        if ($rel -match '[*?]') { [void]$wildcards.Add($rel); continue }
        $path = Join-SafeRelativePath -Root $Root -Rel $rel
        if ([IO.File]::Exists($path) -and $seen.Add($path)) { Get-Item -LiteralPath $path -Force }
    }
    if ($wildcards.Count) {
        foreach ($item in @(Get-SafeClientFiles -Root $Root)) {
            $rel = Normalize-Rel ($item.FullName.Substring($Root.Length).TrimStart('\','/'))
            if ((Test-GlobMatch $rel $wildcards) -and $seen.Add($item.FullName)) { $item }
        }
    }
}

function Install-StagedEntry {
    param([string]$Destination,[string]$ExpectedSha1,[string]$Rel,$File)
    if (-not $script:SyncRoot) { return $false }
    $cached = Join-SafeRelativePath -Root $script:SyncRoot -Rel ('.portable-staging/' + $Rel)
    if (-not [IO.File]::Exists($cached)) { return $false }
    if ((Get-Sha1 $cached) -ne $ExpectedSha1) { return $false }
    if ($File.PSObject.Properties['size'] -and (Get-Item -LiteralPath $cached).Length -ne [int64]$File.size) { return $false }
    New-Item -ItemType Directory -Force (Split-Path -Parent $Destination) | Out-Null
    $temp = Get-PartialDownloadPath -Destination $Destination -Sha1 $ExpectedSha1
    Copy-Item -LiteralPath $cached -Destination $temp -Force
    if ((Get-Sha1 $temp) -ne $ExpectedSha1) { [IO.File]::Delete($temp); return $false }
    Install-VerifiedFile -Temporary $temp -Destination $Destination
    return $true
}

$syncLock=$null
$script:SyncLogPath=$null
try {
$instanceRoot = Resolve-FullPath $InstanceDir
New-Item -ItemType Directory -Force $instanceRoot | Out-Null
$script:SyncRoot=$instanceRoot.TrimEnd('\')
$syncLock=Enter-SyncLock -Root $instanceRoot
Start-SyncLog -Root $instanceRoot
Set-Location -LiteralPath $instanceRoot

# 老系统/未开启强加密的 .NET 默认拿不到 TLS 1.2，https 更新源会直接失败；入口 bat 已设，
# 这里再兜一层，保证直接跑本脚本（不经 bat）也能连 https。
try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072 } catch { }

$manifestUrls = @(Read-UpdateUrls -Root $instanceRoot -Override $ManifestUrl)
$statePath = Join-Path $instanceRoot '.portable-sync-state.json'
$state = Read-ValidSyncState -Path $statePath
$lastGood = ''
if ([string]::IsNullOrWhiteSpace($ManifestUrl) -and $state) {
    $lastGood = [string](Get-PropertyValue -Object $state -Name 'lastManifestUrl' -Default (Get-PropertyValue -Object $state -Name 'manifestUrl' -Default ''))
}
if ([string]::IsNullOrWhiteSpace($ManifestUrl)) {
    $manifestUrls = @(Get-OrderedManifestUrls -Urls $manifestUrls -LastGood $lastGood)
}
Write-Host ("[同步] 清单候选：" + (($manifestUrls | ForEach-Object { Get-MaskedUrl $_ }) -join '；'))
$detectedProxy = Get-SystemProxyUrl
if ($detectedProxy) {
    Write-Host ("[同步] 已检测到系统代理 {0}，官方源先直连，受阻后可使用现有代理；更新服务直连。" -f (Get-ProxyDescription $detectedProxy))
} else {
    Write-Host '[同步] 未检测到系统代理，官方源直连（太慢会在约 8 秒内回落更新服务）。'
}
$manifest = $null
$manifestText = $null
$selectedManifestUrl = $null
$lastManifestError = $null
foreach ($candidate in $manifestUrls) {
    $probeTimeout = Get-ProbeTimeoutSec -Url $candidate
    $retries = if ($probeTimeout -le 2) { 0 } else { 1 }
    try {
        $candidateText = Read-UrlUtf8Text -Url $candidate -TimeoutSec $probeTimeout -Retries $retries
        $candidateManifest = $candidateText | ConvertFrom-Json
        if (-not $candidateManifest.files) { throw 'manifest 缺少 files。' }
        $manifestText = $candidateText
        $manifest = $candidateManifest
        $selectedManifestUrl = $candidate
        break
    } catch {
        $lastManifestError = $_.Exception
    }
}

# 完整包通常自带清单；更新源短暂不可达时，用它继续做本地哈希对账，
# 已有文件可以完成同步，缺失文件等更新源恢复后再补下。
if (-not $manifest) {
    $localManifestPath = Join-Path $instanceRoot 'server-manifest.json'
    if (Test-Path -LiteralPath $localManifestPath -PathType Leaf) {
        try {
            $localManifest = Get-Content -LiteralPath $localManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($localManifest.files) {
                $manifest = $localManifest
                $selectedManifestUrl = $manifestUrls[0]
                Write-Host '[降级] 更新源暂时不可达，改用包内 server-manifest.json 做本地对账；缺失文件仍需更新源恢复后补下。' -ForegroundColor Yellow
            }
        } catch { $manifest = $null }
    }
}
if (-not $manifest) {
    throw (Format-UpdateError -ErrorRecord $lastManifestError -Phase '获取清单')
}
$manifestUrl = [string]$selectedManifestUrl
if (-not [string]::IsNullOrWhiteSpace($lastGood) -and $manifestUrl -eq $lastGood) {
    Write-Host "[同步] 沿用上次可用更新地址：$(Get-MaskedUrl $manifestUrl)"
} elseif ($manifestUrl -ne [string]$manifestUrls[0]) {
    Write-Host "[同步] 已切换到可用备用地址：$(Get-MaskedUrl $manifestUrl)"
}
if (-not $manifest.files) { throw 'manifest 缺少 files。' }
Assert-ManifestFiles -Root $instanceRoot -Files $manifest.files -DeleteGlobs @(Get-ArrayValue -Object $manifest -Name 'forceDeleteGlobs' -Default @())
Write-Host ('[版本] ' + [string]$manifest.version)

$slash = $manifestUrl.LastIndexOf('/')
if ($slash -lt 0) { throw "manifest URL 格式异常：$manifestUrl" }
$baseUrl = $manifestUrl.Substring(0, $slash + 1)
$script:HybridManifest = $null
$script:HybridSources = $null
if ($manifest.PSObject.Properties['mirrorManifestUrls'] -and @($manifest.mirrorManifestUrls).Count) { $script:HybridManifest = $manifest; $script:HybridManifestUrl = $manifestUrl }
$script:InitialSync = -not $state -or -not (Get-PropertyValue -Object $state -Name 'files' -Default $null)
$previousVersion = [string](Get-PropertyValue -Object $state -Name 'version' -Default '')
$currentVersion = [string](Get-PropertyValue -Object $manifest -Name 'version' -Default '')
$versionChanged = $previousVersion -ne $currentVersion
$previousMap = ConvertTo-FileMap -Files (Get-PropertyValue -Object $state -Name 'files' -Default @())
# key 是小写化的，删除旧文件时要还原状态文件里的原始大小写（大小写敏感卷才找得到文件）。
$previousRelByKey = @{}
foreach ($item in @(Get-PropertyValue -Object $state -Name 'files' -Default @())) {
    if ($item -and $item.path) { $previousRelByKey[(Normalize-RelKey ([string]$item.path))] = Normalize-Rel ([string]$item.path) }
}
Initialize-SyncJournal -Root $instanceRoot -Manifest $manifest -PreviousMap $previousMap -PreviousRelByKey $previousRelByKey
$newMap = ConvertTo-FileMap -Files $manifest.files
$verified = @{}
$rawVerified = Get-PropertyValue -Object $state -Name 'verified' -Default $null
if ($rawVerified) {
    foreach ($prop in $rawVerified.PSObject.Properties) {
        try { $verified[(Normalize-RelKey $prop.Name)] = $prop.Value } catch {}
    }
}
$preserveChangeGlobs = Get-ArrayValue -Object $manifest -Name 'preserveLocalChangeGlobs' -Default @()
$preserveDeletionGlobs = Get-ArrayValue -Object $manifest -Name 'preserveLocalDeletionGlobs' -Default @()
$forceDeleteGlobs = Get-ArrayValue -Object $manifest -Name 'forceDeleteGlobs' -Default @()
# 强制同步：匹配的文件无视保留规则、一律以服务端为准（覆盖前照常备份）。
# 服务端确需推送玩家常改的配置时，临时把路径加进 forceSyncGlobs 发布一版即可。
$forceSyncGlobs = Get-ArrayValue -Object $manifest -Name 'forceSyncGlobs' -Default @()
$additiveDirs = Get-ArrayValue -Object $manifest -Name 'additiveDirs' -Default @('shaderpacks', 'resourcepacks', 'schematics', 'saves', 'screenshots')
$cleanup = Get-PropertyValue -Object $manifest -Name 'cleanup' -Default $null
$preservePlayerCustomizations = [bool](Get-PropertyValue -Object $manifest -Name 'preservePlayerCustomizations' -Default $true)
$adoptExistingFiles = [bool](Get-PropertyValue -Object $manifest -Name 'adoptExistingFiles' -Default $true)
$stamp = (Get-Date -Format 'yyyyMMdd-HHmmss-fff') + '-' + [guid]::NewGuid().ToString('N').Substring(0,8)
$backupRoot = Join-Path $instanceRoot (".portable-sync-backups\$stamp")

Write-Host '[预检] 检查现有受管文件是否被占用...'
$forceDeleteTargets = @(Get-ForceDeleteTargets -Root $instanceRoot -Patterns $forceDeleteGlobs)
$checkFiles = @($manifest.files) + @($previousRelByKey.Values | ForEach-Object { [pscustomobject]@{path=$_} })
foreach ($deleteTarget in $forceDeleteTargets) { $checkFiles += [pscustomobject]@{path=(Normalize-Rel $deleteTarget.FullName.Substring($instanceRoot.Length).TrimStart('\','/'))} }
$optionConfig = Get-PropertyValue $manifest 'playerOptions' $null
if ($optionConfig) { foreach ($rel in @(Get-PropertyValue $optionConfig 'targets' @('options.txt'))) { $checkFiles += [pscustomobject]@{path=$rel} } }
Assert-UpdateFilesAvailable -Root $instanceRoot -Files $checkFiles
if ([bool](Get-PropertyValue -Object $cleanup -Name 'removeConnectorCache' -Default $true)) { Remove-ConnectorRuntimeCache -Root $instanceRoot }
if ([bool](Get-PropertyValue -Object $cleanup -Name 'disableLauncherRepairIndex' -Default $true)) { Disable-LauncherRepairIndex -Roots @($PSScriptRoot, $instanceRoot) }

# 接管索引惰性构建：日常同步通常没有缺失文件，这时不必对 mods/config/resourcepacks 等
# 目录做全量 SHA1 扫描（大包一次要几十秒）。只在真的遇到缺失文件需要接管时才建一次。
$script:AdoptIndexCache = $null
function Get-AdoptIndex {
    if ($null -eq $script:AdoptIndexCache) {
        Write-Host '[同步] 有文件缺失，正在建立本地文件哈希索引（仅本次需要）...'
        $script:AdoptIndexCache = Get-ExistingFileHashIndex -Root $instanceRoot
    }
    return $script:AdoptIndexCache
}
# 清单里所有目标的绝对路径集合：接管时据此区分"多余副本"（可移动）与"清单要保留的孪生文件"（只能复制）。
$manifestTargets = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
foreach ($file in @($manifest.files)) {
    $mr = Normalize-Rel ([string]$file.path)
    if (Test-RelativePathSafe -Rel $mr) { [void]$manifestTargets.Add([System.IO.Path]::GetFullPath((Join-SafeRelativePath -Root $instanceRoot -Rel $mr))) }
}
$downloaded = 0
$adopted = 0
$skipped = 0
$preserved = 0
$removed = 0
$updatedPaths = New-Object 'System.Collections.Generic.List[string]'
$adoptedPaths = New-Object 'System.Collections.Generic.List[string]'
$forcedAppliedPaths = New-Object 'System.Collections.Generic.List[string]'
$script:PreservedDeletionKeys = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
# 保留了玩家修改、但服务端同一文件本次其实也有更新的清单：结尾集中提醒一次。
$script:PreservedConflicts = New-Object 'System.Collections.Generic.List[string]'

$downloadJobs = New-Object 'System.Collections.Generic.List[object]'
foreach ($file in @($manifest.files)) {
    $rel = Normalize-Rel ([string]$file.path)
    $relKey = Normalize-RelKey $rel
    if (-not (Test-RelativePathSafe -Rel $rel)) { throw "Unsafe manifest path: $rel" }
    $target = Join-SafeRelativePath -Root $instanceRoot -Rel $rel
    $expected = ([string]$file.sha1).ToLowerInvariant()
    $exists = Test-Path -LiteralPath $target -PathType Leaf
    $hadPrevious = $previousMap.ContainsKey($relKey)
    $previousHash = if ($hadPrevious) { [string]$previousMap[$relKey] } else { "" }
    $manifestSize = $null
    if ($file.PSObject.Properties['size']) { $manifestSize = $file.size }
    $verifiedEntry = $null
    if ($verified.ContainsKey($relKey)) { $verifiedEntry = $verified[$relKey] }
    if ($exists -and (Test-LooksLikeCurrentFile -Path $target -Expected $expected -ManifestSize $manifestSize -PreviousHash $previousHash -Verified $verifiedEntry)) {
        $verified[$relKey] = Get-FileVerifyRecord -Path $target -Sha1 $expected
        $skipped++
        continue
    }
    $currentHash = if ($exists) { Get-Sha1 -Path $target } else { "" }
    if ($exists -and $currentHash -eq $expected) {
        $verified[$relKey] = Get-FileVerifyRecord -Path $target -Sha1 $expected
        $skipped++
        continue
    }
    $forceSync = Test-GlobMatch -Rel $rel -Globs $forceSyncGlobs
    if (-not $exists -and $hadPrevious -and $preservePlayerCustomizations -and -not $forceSync -and (Test-GlobMatch -Rel $rel -Globs $preserveDeletionGlobs)) {
        Write-Host "[保留玩家删除] $rel"
        [void]$script:PreservedDeletionKeys.Add($relKey)
        $preserved++
        continue
    }
    if ($exists -and $preservePlayerCustomizations -and -not $forceSync -and (Test-GlobMatch -Rel $rel -Globs $preserveChangeGlobs)) {
        # 三方对比：current==previousHash 说明玩家没改过，照常走下面的更新；
        # 改过（或没有基线）就保留。若服务端同一文件本次也变了，记下来结尾集中提醒。
        if ([string]::IsNullOrWhiteSpace($previousHash) -or $currentHash -ne $previousHash) {
            if ($hadPrevious -and $expected -ne $previousHash) { [void]$script:PreservedConflicts.Add($rel) }
            Write-Host "[保留玩家修改] $rel"
            $preserved++
            continue
        }
    }
    if (-not $exists -and $adoptExistingFiles) {
        $targetFull = [System.IO.Path]::GetFullPath($target)
        $adopt = Get-AdoptSource -Index (Get-AdoptIndex) -Expected $expected -TargetFull $targetFull -ManifestTargets $manifestTargets
        if ($adopt) {
            Add-SyncIntent -Rel $rel -Sha1 $expected -Save
            Assert-SafeAbsolutePath -Path $adopt.Path
            New-Item -ItemType Directory -Force (Split-Path -Parent $target) | Out-Null
            if ($adopt.Mode -eq 'move') {
                # 同哈希接管通常是官方改名/换名。虽然内容不变，旧路径仍属于玩家本地
                # 将发生的变更，必须先按原相对路径备份，才能满足可完整回滚的安全约束。
                $rootFull = [System.IO.Path]::GetFullPath($instanceRoot).TrimEnd('\') + '\'
                $adoptFull = [System.IO.Path]::GetFullPath([string]$adopt.Path)
                if (-not $adoptFull.StartsWith($rootFull, [System.StringComparison]::OrdinalIgnoreCase)) {
                    throw "接管来源越出实例目录：$adoptFull"
                }
                $adoptRel = Normalize-Rel $adoptFull.Substring($rootFull.Length)
                Backup-FileIfExists -Path $adopt.Path -Rel $adoptRel -BackupRoot $backupRoot
                Move-Item -LiteralPath $adopt.Path -Destination $target -Force
            }
            else { Copy-Item -LiteralPath $adopt.Path -Destination $target -Force }
            if ((Get-Sha1 -Path $target) -eq $expected) {
                $verified[$relKey] = Get-FileVerifyRecord -Path $target -Sha1 $expected
                Write-Host "[接管本地文件] $rel"
                $adopted++
                [void]$adoptedPaths.Add($rel)
                continue
            }
            # 接管后校验不符（极罕见）：不 continue，落到下面正常下载覆盖。
        }
    }
    if ($exists) { Backup-FileIfExists -Path $target -Rel $rel -BackupRoot $backupRoot }
    Add-SyncIntent -Rel $rel -Sha1 $expected
    [void]$downloadJobs.Add([pscustomobject]@{ File=$file; Target=$target; Expected=$expected; Rel=$rel; Key=$relKey; Force=$forceSync })
}

if ($downloadJobs.Count) {
    Save-SyncJournal
    $workers = [Math]::Max(1, [Math]::Min(8, [int](Get-PropertyValue -Object $manifest -Name 'downloadWorkers' -Default 4)))
    $workers = [Math]::Min($workers, $downloadJobs.Count)
    Write-Host ("[同步] 需要更新 {0} 个文件（并行 {1}）。" -f $downloadJobs.Count, $workers)
    # Native runspaces also work in Windows PowerShell 5.1; no Python install needed.
    $ast = [Management.Automation.Language.Parser]::ParseFile($PSCommandPath, [ref]$null, [ref]$null)
    $defs = $ast.FindAll({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst]}, $false)
    $workerCode = ($defs | ForEach-Object { $_.Extent.Text }) -join "`n"
    $workerCode += @'

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$script:ProxyResolved = $false
$script:ProxyUrl = $null
$script:OfficialMinBytes = 1MB
$script:OfficialMinRateBps = 128KB
$script:HybridManifest = $null
$script:HybridSources = $null
try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072 } catch {}
try {
    $script:HybridManifest = $args[2]
    $script:HybridManifestUrl = $args[3]
    $script:SyncRoot = $args[4]
    $job = $args[0]
    $source = Download-ManifestEntry -File $job.File -BaseUrl $args[1] -Destination $job.Target -ExpectedSha1 $job.Expected -Rel $job.Rel
    [pscustomobject]@{ Job=$job; Source=$source; Record=(Get-FileVerifyRecord -Path $job.Target -Sha1 $job.Expected); Error=$null }
} catch { [pscustomobject]@{ Job=$args[0]; Source=$null; Record=$null; Error=$_.Exception.Message } }
'@
    $pool = [RunspaceFactory]::CreateRunspacePool(1, $workers)
    $pool.Open()
    $pending = New-Object 'System.Collections.Generic.List[object]'
    $next = 0
    $hardError = $null
    try {
        while ($next -lt $downloadJobs.Count -or $pending.Count) {
            while ($next -lt $downloadJobs.Count -and $pending.Count -lt $workers) {
                $ps = [PowerShell]::Create()
                $ps.RunspacePool = $pool
                [void]$ps.AddScript($workerCode).AddArgument($downloadJobs[$next]).AddArgument($baseUrl).AddArgument($script:HybridManifest).AddArgument($script:HybridManifestUrl).AddArgument($instanceRoot)
                [void]$pending.Add([pscustomobject]@{ Shell=$ps; Handle=$ps.BeginInvoke(); Job=$downloadJobs[$next] })
                $next++
            }
            foreach ($task in @($pending.ToArray())) {
                if (-not $task.Handle.IsCompleted) { continue }
                try {
                    $results = @($task.Shell.EndInvoke($task.Handle))
                    $result = $results | Where-Object { $_.PSObject.Properties['Record'] } | Select-Object -Last 1
                    if (-not $result) { throw '下载任务未返回结果。' }
                    $job = $task.Job
                    if ($result.Error) { throw $result.Error }
                    $verified[$job.Key] = $result.Record
                    $downloaded++
                    [void]$updatedPaths.Add($job.Rel)
                    if ($job.Force) { [void]$forcedAppliedPaths.Add($job.Rel) }
                    $label = if ($result.Source -eq 'official') { '[官方源]' } elseif ($result.Source -eq 'staged') { '[缓存]' } else { '[更新]' }
                    Write-Host "$label $($job.Rel)"
                } catch {
                    if (Test-OptionalHelperFile -Rel $task.Job.Rel) {
                        Write-SyncWarning "辅助脚本未更新：$($task.Job.Rel)"
                        $preserved++
                    } elseif (-not $hardError) { $hardError = Format-UpdateError -ErrorRecord $_ -Path $task.Job.Rel }
                } finally { $task.Shell.Dispose(); [void]$pending.Remove($task) }
            }
            if ($pending.Count) { Start-Sleep -Milliseconds 100 }
        }
    } finally { foreach ($task in $pending) { $task.Shell.Dispose() }; $pool.Dispose() }
    if ($hardError) { throw $hardError }
}

foreach ($oldKey in @($previousMap.Keys)) {
    if ($newMap.ContainsKey($oldKey)) { continue }
    $oldRel = if ($previousRelByKey.ContainsKey($oldKey)) { $previousRelByKey[$oldKey] } else { $oldKey }
    if (-not (Test-RelativePathSafe -Rel $oldRel)) { continue }
    $target = Join-SafeRelativePath -Root $instanceRoot -Rel $oldRel
    if (-not (Test-Path -LiteralPath $target -PathType Leaf)) { continue }
    # tacz 等大件：清单撤下也不删玩家已装的。
    # mods/** 也在删除保留里，但官方换版本（旧 jar 名离开清单）仍要清掉未改过的旧文件，否则会双模组。
    $keepByDeletionGlob = $preservePlayerCustomizations -and (Test-GlobMatch -Rel $oldRel -Globs $preserveDeletionGlobs)
    $isPackMod = $oldRel -like 'mods/*'
    if ($keepByDeletionGlob -and -not $isPackMod) {
        Write-Host "[保留本地额外文件] $oldRel"
        $preserved++
        continue
    }
    $currentHash = Get-Sha1 -Path $target
    if ($currentHash -ne $previousMap[$oldKey]) { Write-Host "[保留本地额外文件] $oldRel"; $preserved++; continue }
    Backup-FileIfExists -Path $target -Rel $oldRel -BackupRoot $backupRoot
    Remove-Item -LiteralPath $target -Force
    Write-Host "[删除旧文件] $oldRel"
    $removed++
}

foreach ($match in $forceDeleteTargets) {
    $rel = Normalize-Rel $match.FullName.Substring($instanceRoot.Length).TrimStart('\','/')
    if (-not [IO.File]::Exists($match.FullName)) { continue }
    Assert-SafeAbsolutePath -Path $match.FullName
    Backup-FileIfExists -Path $match.FullName -Rel $rel -BackupRoot $backupRoot
    Remove-Item -LiteralPath $match.FullName -Force
    Write-Host "[强制删除] $rel"
    $removed++
}

foreach ($dir in $additiveDirs) {
    $rel = Normalize-Rel ([string]$dir)
    if ([string]::IsNullOrWhiteSpace($rel) -or -not (Test-RelativePathSafe -Rel $rel)) { continue }
    New-Item -ItemType Directory -Force (Join-SafeRelativePath -Root $instanceRoot -Rel $rel) | Out-Null
}

[void](Apply-OptionDefaults -Root $instanceRoot -Manifest $manifest -BackupRoot $backupRoot)
$requiredPackChanges = Ensure-RequiredResourcePacks -Root $instanceRoot -Manifest $manifest -BackupRoot $backupRoot
if ($requiredPackChanges -gt 0) {
    Write-Host "[重要] 已修改资源包顺序。请完全退出 Minecraft 后重新启动客户端；游戏运行中不会自动采用外部 options.txt 变更。" -ForegroundColor Yellow
}
Apply-ServerList -Root $instanceRoot -Manifest $manifest -BackupRoot $backupRoot
Apply-LauncherProfile -Root $instanceRoot -Manifest $manifest -BackupRoot $backupRoot
[void](Ensure-NeoForgeClientExtra -Root $instanceRoot -Manifest $manifest -BackupRoot $backupRoot)
Show-LauncherHints -Root $instanceRoot
if ([bool](Get-PropertyValue -Object $cleanup -Name 'removeConnectorCache' -Default $true)) { Remove-ConnectorRuntimeCache -Root $instanceRoot }
if ([bool](Get-PropertyValue -Object $cleanup -Name 'disableDuplicateMods' -Default $true)) {
    $extras = @()
    $modsDir = Join-Path $instanceRoot 'mods'
    if (Test-Path -LiteralPath $modsDir -PathType Container) {
        $managedModPaths = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
        foreach ($item in @($manifest.files)) {
            $mr = Normalize-Rel ([string]$item.path)
            if ($mr -like 'mods/*.jar') { [void]$managedModPaths.Add($mr) }
        }
        $extras = @(Get-ChildItem -LiteralPath $modsDir -File -Filter '*.jar' -ErrorAction SilentlyContinue | Where-Object {
            $rel = $_.FullName.Substring($instanceRoot.Length).TrimStart('\', '/') -replace '\\', '/'
            -not $managedModPaths.Contains($rel)
        })
    }
    if ($extras.Count -gt 0 -or $downloaded -gt 0 -or $adopted -gt 0 -or $removed -gt 0 -or $script:InitialSync) {
        $script:DuplicateMoved = 0
        Disable-DuplicateMods -Root $instanceRoot -Manifest $manifest
    }
}
if ([bool](Get-PropertyValue -Object $cleanup -Name 'disableLauncherRepairIndex' -Default $true)) { Disable-LauncherRepairIndex -Roots @($PSScriptRoot, $instanceRoot) }

$verifiedOut = [ordered]@{}
foreach ($key in @($verified.Keys)) {
    if ($newMap.ContainsKey($key)) { $verifiedOut[$key] = $verified[$key] }
}
$stateOut = [ordered]@{
    format = 3
    packId = Get-PropertyValue -Object $manifest -Name 'packId' -Default (Get-PropertyValue -Object $manifest -Name 'pack' -Default '')
    packName = Get-PropertyValue -Object $manifest -Name 'packName' -Default (Get-PropertyValue -Object $manifest -Name 'name' -Default '')
    version = Get-PropertyValue -Object $manifest -Name 'version' -Default ''
    manifestUrl = $manifestUrl
    lastManifestUrl = $manifestUrl
    skipOfficial = $false
    syncedAt = (Get-Date).ToString('s')
    files = $manifest.files
    verified = $verifiedOut
}
Save-SyncState -Path $statePath -Value $stateOut
if ([IO.File]::Exists($script:JournalPath)) { [IO.File]::Delete($script:JournalPath) }
$configApplied = @(@($updatedPaths) + @($adoptedPaths) | Where-Object { $_ -like 'config/*' -or $_ -like 'defaultconfigs/*' })
$forcedAppliedSet = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
foreach ($rel in $forcedAppliedPaths) { [void]$forcedAppliedSet.Add($rel) }
Write-Host "[同步] 完成。更新=$downloaded 配置变更=$($configApplied.Count) 接管=$adopted 跳过=$skipped 保留=$preserved 删除=$removed 备份=$backupRoot"
if ($configApplied.Count -gt 0) {
    Write-Host "[配置已应用] 共 $($configApplied.Count) 项："
    foreach ($rel in $configApplied) {
        $suffix = if ($forcedAppliedSet.Contains($rel)) { '（强制同步；原文件如存在已备份）' } else { '' }
        Write-Host ("  - " + $rel + $suffix)
    }
}
$releaseNotes = @(Get-ArrayValue -Object $manifest -Name 'releaseNotes' -Default @() | ForEach-Object { ([string]$_).Trim() } | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
if ($releaseNotes.Count -gt 0 -and ($versionChanged -or $downloaded -gt 0 -or $adopted -gt 0 -or $removed -gt 0 -or $script:InitialSync)) {
    Write-Host ("[本版说明] " + $(if ($currentVersion) { $currentVersion } else { '当前版本' }))
    foreach ($note in $releaseNotes) { Write-Host ("  - " + $note) }
}
if ($script:PreservedConflicts.Count -gt 0) {
    Write-Host ("[提示] 以下 " + $script:PreservedConflicts.Count + " 个文件保留了你的本地修改，但服务端本次也更新了它们（未应用服务端版）：")
    foreach ($r in $script:PreservedConflicts) { Write-Host ("  - " + $r) }
    Write-Host "[提示] 如相关模组出现异常，删除对应文件后重新同步，即可取回服务端最新版本。"
}

if (-not $NoPause) {
    Write-Host '按回车关闭。'
    [void][Console]::ReadLine()
}

} catch {
    Write-Host (Format-UpdateError -ErrorRecord $_) -ForegroundColor Red
    Write-Host '[同步] 更新未完成，请处理上述问题后重新运行；成功前不要启动游戏。' -ForegroundColor Yellow
    if (-not $NoPause) { Write-Host '按回车关闭。'; [void][Console]::ReadLine() }
    exit 1
} finally { Exit-SyncLock -Handle $syncLock }

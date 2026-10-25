<##
    portable-bootstrap-refresh.ps1 —— Windows 启动器自刷新

    只负责一件事：在真正的同步器启动前，把 _updater 下的工具安全刷新到本地。
    这里不使用 Get-FileHash（部分 PowerShell 5.1 以 -File 运行时无法自动加载该命令），
    也不把网络异常按每个文件重复打印；先探测一次清单，成功后再批量下载。
    主更新器仍会再次校验清单和 SHA1，这个脚本不能绕过同步器的安全策略。
##>
param(
    [string]$InstanceDir = ".",
    [string]$ScriptDir = ".",
    [string]$ManifestUrl = ""
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch {}
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false); $OutputEncoding = [Console]::OutputEncoding } catch {}

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


function Stop-PreviousWatch {
    param([string]$Root)
    $record = Join-Path $Root '.portable-staging\daemon.lock'
    if (-not (Test-Path -LiteralPath $record -PathType Leaf)) { return }
    $oldPid = 0
    [void][int]::TryParse(([IO.File]::ReadAllText($record)).Trim(), [ref]$oldPid)
    $process = if ($oldPid -gt 0) { Get-CimInstance Win32_Process -Filter ("ProcessId=$oldPid") -ErrorAction Stop }
    if ($process) {
        $line = [string]$process.CommandLine
        # SHORT_PATH_V1: compare normalized paths. GetFullPath expands 8.3 short names,
        # so the watch may have been started with a different spelling of the same folder.
        $sameInstance = $false
        $wanted = ([IO.Path]::GetFullPath($Root)).TrimEnd('\', '/')
        if ($line -match '(?i)(?:--instance-dir|-InstanceDir)\s+(?:"([^"]+)"|(\S+))') {
            $given = if ($Matches[1]) { $Matches[1] } else { $Matches[2] }
            try { $sameInstance = ([IO.Path]::GetFullPath($given)).TrimEnd('\', '/') -ieq $wanted } catch { $sameInstance = $false }
        }
        if ($line -match '(?i)portable-stage-daemon\.(?:ps1|py)(?:"|\s|$)' -and $sameInstance -and $line -match '(?i)(?:--mode|-Mode)\s+"?watch(?:"|\s|$)') {
            Stop-Process -Id $oldPid -Force -ErrorAction Stop
            Wait-Process -Id $oldPid -Timeout 5 -ErrorAction SilentlyContinue
            if (Get-Process -Id $oldPid -ErrorAction SilentlyContinue) { throw '旧后台预下载仍在运行，请关闭后重试。' }
            Write-Host '[更新器] 已停止本实例的旧后台预下载。'
        }
    }
    # A missing process or a reused PID is a stale record, never a reason to kill another process.
    if (Test-Path -LiteralPath $record -PathType Leaf) {
        if (([IO.File]::ReadAllText($record)).Trim() -eq [string]$oldPid) { Remove-Item -LiteralPath $record -Force }
    }
}

function Read-UpdateUrls {
    param([string]$Root, [string]$ToolsRoot, [string]$Override)
    $urls = New-Object 'System.Collections.Generic.List[string]'
    $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    $add = {
        param([string]$Value)
        $v = ([string]$Value).Trim()
        if ([string]::IsNullOrWhiteSpace($v) -or $v.StartsWith('#')) { return }
        # 7caiyun CDN is retired: never refresh updater scripts from it.
        try { $h = ([Uri]$v).Host.ToLowerInvariant().TrimEnd('.'); if ($h -eq '7caiyun.com' -or $h.EndsWith('.7caiyun.com')) { return } } catch {}
        if ($seen.Add($v)) { [void]$urls.Add($v) }
    }
    if (-not [string]::IsNullOrWhiteSpace($Override)) {
        foreach ($line in ($Override -split '[\r\n\x1f]+')) { & $add $line }
        return @($urls.ToArray())
    }
    $relative = @(
        'UPDATE-URL.txt', 'PORTABLE-UPDATE-URL.txt', 'TFCR-update-url.txt', 'UPDATE-URL-LAN.txt'
    )
    # ROOT_SLIM_V1: since 2.27.23 the URL files live in _updater; older packs keep them in the root.
    foreach ($base in @($Root, $ToolsRoot, (Join-Path $Root '_updater'))) {
        foreach ($rel in $relative) {
            $path = Join-Path $base $rel
            if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { continue }
            try {
                foreach ($line in (Get-Content -LiteralPath $path -Encoding UTF8 -ErrorAction Stop)) { & $add $line }
            } catch {}
        }
    }
    return @($urls.ToArray())
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

function Test-RetiredCloudUrl {
    param([string]$Url)
    try {
        $hostName = ([Uri]$Url).Host.ToLowerInvariant().TrimEnd('.')
        return ($hostName -eq '7caiyun.com' -or $hostName.EndsWith('.7caiyun.com'))
    } catch { return $false }
}

function Test-UsableUpdateUrl {
    param([string]$Url)
    $uri = $null
    if (-not [Uri]::TryCreate(([string]$Url).Trim(), [UriKind]::Absolute, [ref]$uri)) { return $false }
    return ($uri.Scheme -in @('http', 'https') -and -not [string]::IsNullOrEmpty($uri.Host))
}

function Get-OrderedUpdateUrls {
    # Drop retired hosts, the first public URL only, then listed LAN URLs in file order.
    param([string[]]$Urls, [string]$LastGood = '')
    $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    $out = New-Object System.Collections.Generic.List[string]
    $usable = @($Urls | ForEach-Object { ([string]$_).Trim() } | Where-Object { (Test-UsableUpdateUrl $_) -and -not (Test-RetiredCloudUrl $_) })
    $public = @($usable | Where-Object { -not (Test-PrivateUpdateUrl $_) })
    # LAN_FALLBACK_V1: LAN URLs listed in UPDATE-URL go last, for routers without NAT hairpin.
    foreach ($u in (@($public | Select-Object -First 1) + @($usable | Where-Object { Test-PrivateUpdateUrl $_ }))) {
        if ($seen.Add($u)) { [void]$out.Add($u) }
    }
    return @($out.ToArray())
}

function Save-DirectFile {
    param([Parameter(Mandatory = $true)][string]$Url, [Parameter(Mandatory = $true)][string]$Path, [int]$TimeoutSec = 8)
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
    $request = [System.Net.HttpWebRequest]::Create($target)
    if ($hostHeader) { $request.Host = $hostHeader }
    $request.Proxy = $null
    $request.Method = 'GET'
    $request.Timeout = [Math]::Max(1000, $TimeoutSec * 1000)
    $request.ReadWriteTimeout = [Math]::Max(1000, $TimeoutSec * 1000)
    $request.KeepAlive = $false
    $request.UserAgent = $script:SyncUserAgent
    $response = $null
    $inputStream = $null
    $outputStream = $null
    try {
        $response = $request.GetResponse()
        $inputStream = $response.GetResponseStream()
        $outputStream = [System.IO.File]::Open($Path, [System.IO.FileMode]::Create, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
        $inputStream.CopyTo($outputStream)
    } finally {
        if ($outputStream) { $outputStream.Dispose() }
        if ($inputStream) { $inputStream.Dispose() }
        if ($response) { $response.Dispose() }
        try { $request.Abort() } catch {}
    }
}

# MANIFEST_TRUST_BEGIN: identical in player-update-generic.ps1, portable-bootstrap-refresh.ps1,
# portable-stage-daemon.ps1, player-self-repair.ps1 and portable-windows-repair.ps1;
# tests/test_manifest_trust.py compares the copies (rules as in player-update-generic.py).
# MANIFEST_SIGNATURE_V1: server-manifest.json.sig is an RSA PKCS#1 v1.5 SHA-256 signature by a
# trusted key. After a sync has verified one, the state says manifestSignature = required and
# unsigned or wrongly signed manifests are refused. MANIFEST_CACHE_V1: the last verified
# manifest and signature are kept with their ETags, so an unchanged manifest costs two 304s.
$script:UpdaterVersion = '2.27.24'
$script:TrustedManifestKeys = @{ 'd95f8fce25c000bb' = '36G6hVExT3FYPdPFSYf/TTxIqZ0Q8ybtxLqtR8KXgZ8yc2CNEJA+kW27ZO8vzQB7wrjKG7H58r0bkyHQpFRlcU0JygsWTypUzdCeNioTe5PP9vixwORTM37E4O4Dr+/IaLfxUQK+VsWhVMV30Wn2RWBrjLsVg5d9WeK0HC/Sm/VR2NJfCH8d9PLMFRptpp3z3mAaATBdyIMFh6+wlv241rayh/hLTSj6ZGtTcedlMrbY4lNR/b9VV0C/abhvQxU32qKOgLJEHmSxHh2YdzreW9lMBvzQOPztftnZjr1uCqm1+3H0B53iqHmqwvNf5MpQiajBK5bbm/tkXM/AVwuIe5z9kNDAWgDYr7FxiwlX5Glg5C3i4KQJK5eQn8X8m4GW22e/lfYEggvfPiNNYIn9k2C7itxI3W8jxe8GBuiNKiTuDkzOjw0RoOYfOYW3IGvVqy+MRt51bEp3XoVgw/DyeTi+RDQHR56n4Pkr78ISuVWOQKqHO4R7j4jSIM9maEM5' }
$script:ManifestCacheDir = '.portable-manifest-cache'

function Test-ManifestSignature {
    # Returns the id of the trusted key that signed Data; throws '[签名校验失败] <reason>'.
    param([byte[]]$Data, [byte[]]$SignatureFile)
    try {
        $info = [Text.Encoding]::UTF8.GetString($SignatureFile).TrimStart([char]0xFEFF) | ConvertFrom-Json
        $keyId = [string]$info.keyId
        $signature = [Convert]::FromBase64String([string]$info.signature)
        $algorithm = [string]$info.algorithm
    } catch { throw '[签名校验失败] 签名文件格式错误' }
    if ($algorithm -ne 'RSA-PKCS1-v1_5-SHA256') { throw '[签名校验失败] 签名算法不受支持' }
    if (-not $keyId -or -not $script:TrustedManifestKeys.ContainsKey($keyId)) { throw '[签名校验失败] 签名密钥不受信任' }
    $parameters = New-Object Security.Cryptography.RSAParameters
    $parameters.Modulus = [Convert]::FromBase64String([string]$script:TrustedManifestKeys[$keyId])
    $parameters.Exponent = [byte[]](1, 0, 1)
    $rsa = New-Object Security.Cryptography.RSACryptoServiceProvider
    $valid = $false
    try { $rsa.ImportParameters($parameters); $valid = $rsa.VerifyData($Data, 'SHA256', $signature) } catch { $valid = $false } finally { $rsa.Dispose() }
    if (-not $valid) { throw '[签名校验失败] 签名与清单不符' }
    return $keyId
}

function Test-ManifestSignatureRequired {
    param([string]$Root)
    try {
        $state = [IO.File]::ReadAllText((Join-Path $Root '.portable-sync-state.json'), [Text.Encoding]::UTF8) | ConvertFrom-Json
        return ([string]$state.manifestSignature -eq 'required')
    } catch { return $false }
}

function Invoke-TrustRequest {
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
            # 304 and 404 are answers; anything else keeps the original WebException for the caller.
            if ($web -and $web.Response -and ([int]$web.Response.StatusCode) -in @(304, 404)) { $response = $web.Response } else { throw }
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

function Read-ManifestTrustCache {
    param([string]$Root)
    $folder = Get-ManifestCacheFolder $Root
    if (-not $folder) { return $null }
    try {
        $meta = [IO.File]::ReadAllText((Join-Path $folder 'meta.json'), [Text.Encoding]::UTF8) | ConvertFrom-Json
        $manifest = [IO.File]::ReadAllBytes((Join-Path $folder 'server-manifest.json'))
        $signature = [IO.File]::ReadAllBytes((Join-Path $folder 'server-manifest.json.sig'))
        $sha = [Security.Cryptography.SHA256]::Create()
        try {
            $manifestSha = ([BitConverter]::ToString($sha.ComputeHash($manifest))).Replace('-', '').ToLowerInvariant()
            $signatureSha = ([BitConverter]::ToString($sha.ComputeHash($signature))).Replace('-', '').ToLowerInvariant()
        } finally { $sha.Dispose() }
        if ($manifestSha -ne [string]$meta.manifestSha256 -or $signatureSha -ne [string]$meta.signatureSha256) { return $null }
        return @{ Manifest = $manifest; ManifestETag = [string]$meta.manifestEtag; Signature = $signature; SignatureETag = [string]$meta.signatureEtag }
    } catch { return $null }
}

function Write-ManifestTrustCache {
    param([string]$Root, [byte[]]$Manifest, [string]$ManifestETag, [byte[]]$Signature, [string]$SignatureETag)
    $folder = Get-ManifestCacheFolder $Root
    if (-not $folder) { return }
    try {
        [void][IO.Directory]::CreateDirectory($folder)
        $sha = [Security.Cryptography.SHA256]::Create()
        try {
            $meta = [ordered]@{ manifestEtag = $ManifestETag; manifestSha256 = ([BitConverter]::ToString($sha.ComputeHash($Manifest))).Replace('-', '').ToLowerInvariant()
                signatureEtag = $SignatureETag; signatureSha256 = ([BitConverter]::ToString($sha.ComputeHash($Signature))).Replace('-', '').ToLowerInvariant() }
        } finally { $sha.Dispose() }
        $files = @(@('server-manifest.json', $Manifest), @('server-manifest.json.sig', $Signature), @('meta.json', [Text.Encoding]::UTF8.GetBytes(($meta | ConvertTo-Json -Compress))))
        foreach ($file in $files) {
            $target = Join-Path $folder $file[0]
            $temporary = $target + '.' + $PID + '.tmp'
            [IO.File]::WriteAllBytes($temporary, [byte[]]$file[1])
            if ([IO.File]::Exists($target)) { [IO.File]::Replace($temporary, $target, [System.Management.Automation.Language.NullString]::Value) } else { [IO.File]::Move($temporary, $target) }
        }
    } catch { }  # the cache only saves bandwidth
}

function Get-TrustedManifest {
    # Returns [pscustomobject]@{ Raw = [byte[]]; KeyId = id or $null }. Throws '[签名校验失败] ...'
    # for a missing or wrong signature, and any other exception when the source is unreachable.
    param([string]$Url, [string]$Root = '', [int]$TimeoutSec = 20, [string]$UserAgent = 'SCEX-updater', [switch]$NoCache)
    $required = [bool]($Root -and (Test-ManifestSignatureRequired $Root))
    $cache = if ($Root -and -not $NoCache) { Read-ManifestTrustCache $Root } else { $null }
    for ($attempt = 1; $attempt -le 2; $attempt++) {
        $reply = Invoke-TrustRequest -Url $Url -ETag $(if ($cache) { $cache.ManifestETag } else { '' }) -TimeoutSec $TimeoutSec -UserAgent $UserAgent
        if ($reply.Status -eq 304 -and $cache) { $raw = $cache.Manifest; $rawTag = $cache.ManifestETag }
        elseif ($reply.Status -eq 200) { $raw = $reply.Body; $rawTag = $reply.ETag }
        else { throw ('HTTP ' + $reply.Status) }
        $reply = Invoke-TrustRequest -Url ($Url + '.sig') -ETag $(if ($cache) { $cache.SignatureETag } else { '' }) -TimeoutSec $TimeoutSec -UserAgent $UserAgent
        if ($reply.Status -eq 304 -and $cache) { $sig = $cache.Signature; $sigTag = $cache.SignatureETag }
        elseif ($reply.Status -eq 200) { $sig = $reply.Body; $sigTag = $reply.ETag }
        elseif ($reply.Status -eq 404) { $sig = $null }
        else { throw ('HTTP ' + $reply.Status) }
        if ($null -eq $sig) {
            if ($required) { throw '[签名校验失败] 更新清单缺少签名' }
            return [pscustomobject]@{ Raw = $raw; KeyId = $null }
        }
        try { $keyId = Test-ManifestSignature -Data $raw -SignatureFile $sig }
        catch {
            $failure = $_
            $named = $null
            try { $named = [string]([Text.Encoding]::UTF8.GetString($sig).TrimStart([char]0xFEFF) | ConvertFrom-Json).manifestSha256 } catch { }
            $sha = [Security.Cryptography.SHA256]::Create()
            try { $actual = ([BitConverter]::ToString($sha.ComputeHash($raw))).Replace('-', '').ToLowerInvariant() } finally { $sha.Dispose() }
            if ($attempt -eq 1 -and $named -and $named -ne $actual) { $cache = $null; Start-Sleep -Milliseconds 1500; continue }
            throw $failure
        }
        $fromCache = $cache -and [object]::ReferenceEquals($raw, $cache.Manifest) -and [object]::ReferenceEquals($sig, $cache.Signature)
        if ($Root -and -not $NoCache -and -not $fromCache) { Write-ManifestTrustCache -Root $Root -Manifest $raw -ManifestETag $rawTag -Signature $sig -SignatureETag $sigTag }
        return [pscustomobject]@{ Raw = $raw; KeyId = $keyId }
    }
    throw '[签名校验失败] 签名与清单不符'
}
# MANIFEST_TRUST_END
$script:SyncUserAgent = "SCEX-updater/$($script:UpdaterVersion) (bootstrap $($PSVersionTable.PSVersion.Major).$($PSVersionTable.PSVersion.Minor); windows)"

function Read-ManifestUrl {
    # MANIFEST_SIGNATURE_V1: helper scripts are refreshed only from a verified manifest.
    param([string]$Url, [int]$TimeoutSec = 8, [string]$Root = '')
    try {
        $trusted = Get-TrustedManifest -Url $Url -Root $Root -TimeoutSec $TimeoutSec -UserAgent $script:SyncUserAgent
        $manifest = [Text.Encoding]::UTF8.GetString([byte[]]$trusted.Raw).TrimStart([char]0xFEFF) | ConvertFrom-Json
        if ($null -ne $manifest -and $null -ne $manifest.files) { return $manifest }
        return $null
    } catch {
        if ([string]$_.Exception.Message -like '*签名校验失败*') { $script:ManifestTrustFailed = $true }
        return $null
    }
}

function Assert-PowerShellSyntax {
    param([string]$Path)
    $tokens = $null
    $errors = $null
    [System.Management.Automation.Language.Parser]::ParseFile($Path, [ref]$tokens, [ref]$errors) | Out-Null
    if ($errors.Count -gt 0) { throw 'PowerShell syntax check failed' }
}

function Install-RefreshedFile {
    param([string]$Source, [string]$Destination, [string]$Name)
    if ((Get-Sha1 -Path $Source) -eq (Get-Sha1 -Path $Destination)) { return $false }
    if ($Name.EndsWith('.ps1', [StringComparison]::OrdinalIgnoreCase)) { Assert-PowerShellSyntax -Path $Source }
    $swap = Join-Path (Split-Path -Parent $Destination) ('.' + [IO.Path]::GetFileName($Destination) + '.portable-new-' + [guid]::NewGuid().ToString('N'))
    try {
        Copy-Item -LiteralPath $Source -Destination $swap -Force
        Move-Item -LiteralPath $swap -Destination $Destination -Force
        return $true
    } finally {
        if (Test-Path -LiteralPath $swap -PathType Leaf) { Remove-Item -LiteralPath $swap -Force -ErrorAction SilentlyContinue }
    }
}

$instanceRoot = [IO.Path]::GetFullPath($InstanceDir)
$toolsRoot = [IO.Path]::GetFullPath($ScriptDir)
try { Stop-PreviousWatch -Root $instanceRoot } catch { Write-Host '[更新器] 无法安全停止旧后台预下载，请关闭该实例的更新器后重试。'; exit 1 }
$urls = @(Read-UpdateUrls -Root $instanceRoot -ToolsRoot $toolsRoot -Override $ManifestUrl)
if ($urls.Count -eq 0) {
    Write-Host '[更新器] 未找到更新地址，跳过自刷新。'
    exit 0
}
$lastGood = ''
$statePath = Join-Path $instanceRoot '.portable-sync-state.json'
if (Test-Path -LiteralPath $statePath -PathType Leaf) {
    try {
        $state = Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($state.lastManifestUrl) { $lastGood = [string]$state.lastManifestUrl }
        elseif ($state.manifestUrl) { $lastGood = [string]$state.manifestUrl }
    } catch {}
}
$urls = @(Get-OrderedUpdateUrls -Urls $urls -LastGood $lastGood)
$selected = ''
$selectedManifest = $null
$selectedIndex = -1
for ($i = 0; $i -lt $urls.Count; $i++) {
    $probe = Read-ManifestUrl -Url ([string]$urls[$i]) -TimeoutSec 8 -Root $instanceRoot
    if ($null -ne $probe) {
        $selectedManifest = $probe
        $selected = [string]$urls[$i]
        $selectedIndex = $i
        break
    }
}
if ([string]::IsNullOrWhiteSpace($selected)) {
    if ($script:ManifestTrustFailed) { Write-Host '[更新器] 更新清单签名校验没有通过，跳过自刷新；主同步器会再次校验。' }
    else { Write-Host '[更新器] 更新源暂时不可达，跳过自刷新；本地同步器会继续尝试主线。' }
    exit 0
}
if ($selectedIndex -gt 0) { Write-Host '[更新器] 已切换到备用更新地址。' }
$base = $selected.Substring(0, $selected.LastIndexOf('/'))
$names = @('player-update-generic.ps1', 'player-update-generic.py', 'portable-stage-daemon.ps1', 'portable-stage-daemon.py', 'portable-bootstrap-refresh.ps1', 'macOS-sync.command', 'Windows-sync.bat')
# One pass over the manifest instead of a Where-Object scan per helper.
$rowsByPath = @{}
foreach ($row in @($selectedManifest.files)) {
    $key = ([string]$row.path).Replace('\', '/').ToLowerInvariant()
    if (-not $rowsByPath.ContainsKey($key)) { $rowsByPath[$key] = New-Object System.Collections.Generic.List[object] }
    [void]$rowsByPath[$key].Add($row)
}
foreach ($name in $names) {
    $entries = @()
    $key = ('_updater/' + $name).ToLowerInvariant()
    if ($rowsByPath.ContainsKey($key)) { $entries = @($rowsByPath[$key].ToArray()) }
    if ($entries.Count -ne 1 -or ([string]$entries[0].sha1) -notmatch '^[0-9a-fA-F]{40}$' -or ([string]$entries[0].size) -notmatch '^\d+$') {
        Write-Host "[更新器] 清单未提供有效校验信息，保留本地 $name。"
        continue
    }
    $expectedHash = ([string]$entries[0].sha1).ToLowerInvariant()
    $expectedSize = [long]$entries[0].size
    $url = $base + '/_updater/' + [Uri]::EscapeDataString($name) + '?t=' + [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    $destination = Join-Path $toolsRoot $name
    # SELF_REFRESH_LOCAL_FIRST_V1: a local copy matching the manifest needs no download.
    if ([IO.File]::Exists($destination) -and (Get-Item -LiteralPath $destination).Length -eq $expectedSize -and (Get-Sha1 -Path $destination) -eq $expectedHash) {
        Write-Host "[更新器] 已是最新 $name"
        continue
    }
    $temp = Join-Path ([IO.Path]::GetTempPath()) ('portable-refresh-' + [guid]::NewGuid().ToString('N') + '.tmp')
    $done = $false
    try {
        for ($attempt = 1; $attempt -le 2 -and -not $done; $attempt++) {
            try {
                Save-DirectFile -Url $url -Path $temp -TimeoutSec 8
                if ((Get-Item -LiteralPath $temp).Length -ne $expectedSize -or (Get-Sha1 -Path $temp) -ne $expectedHash) { throw '清单大小或 SHA1 校验失败' }
                $done = $true
            } catch {
                if ($attempt -eq 2) { Write-Host ("[更新器] 跳过自刷新 $name（更新源暂时不可达或文件校验失败）。") }
                else { Start-Sleep -Seconds 1 }
            }
        }
        if ($done) {
            try {
                if (Install-RefreshedFile -Source $temp -Destination $destination -Name $name) { Write-Host "[更新器] 已刷新 $name" }
                else { Write-Host "[更新器] 已是最新 $name" }
            } catch {
                Write-Host ("[更新器] 跳过自刷新 $name（文件校验失败）。")
            }
        }
    } finally {
        if (Test-Path -LiteralPath $temp -PathType Leaf) { Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue }
    }
}
exit 0

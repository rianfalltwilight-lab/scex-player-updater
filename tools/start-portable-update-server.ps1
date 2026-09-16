param(
    [string]$ConfigPath = '.\tools\portable-pack.json',
    [string]$Bind = '127.0.0.1',
    [string]$Python = 'python'
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $Root
function Resolve-LocalPath([string]$Path) {
    if ([IO.Path]::IsPathRooted($Path)) { return [IO.Path]::GetFullPath($Path) }
    return [IO.Path]::GetFullPath((Join-Path $Root $Path))
}
$config = Get-Content -LiteralPath (Resolve-LocalPath $ConfigPath) -Raw -Encoding UTF8 | ConvertFrom-Json
$publish = Resolve-LocalPath ([string]$config.publishDir)
$rootPrefix = [IO.Path]::GetFullPath($Root).TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
if (-not $publish.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'publishDir must be a subdirectory of this updater repository.'
}
if (-not (Test-Path -LiteralPath (Join-Path $publish 'server-manifest.json') -PathType Leaf)) {
    throw 'Publish first: powershell -File tools\portable-publish.ps1'
}
$tokenFile = [string]$config.update.tokenFile
if ([string]::IsNullOrWhiteSpace($tokenFile)) { $tokenFile = '.update-server-token' }
$token = (Get-Content -LiteralPath (Resolve-LocalPath $tokenFile) -Raw -Encoding ASCII).Trim()
if ($token -notmatch '^[-A-Za-z0-9_]{24,80}$') { throw 'Invalid update token file.' }
$port = [int]$config.update.port
if ($port -lt 1 -or $port -gt 65535) { throw 'update.port must be between 1 and 65535.' }
$serverArgs = @((Join-Path $PSScriptRoot 'secure-update-server.py'), '--directory', $publish,
    '--port', $port, '--bind', $Bind, '--token', $token)
$cert = [string]$config.update.certFile
$key = [string]$config.update.keyFile
if ($cert) {
    $cert = Resolve-LocalPath $cert
    if (-not (Test-Path -LiteralPath $cert -PathType Leaf)) { throw 'Configured TLS certificate does not exist.' }
    $serverArgs += @('--certfile', $cert)
    if ($key) {
        $key = Resolve-LocalPath $key
        if (-not (Test-Path -LiteralPath $key -PathType Leaf)) { throw 'Configured TLS private key does not exist.' }
        $serverArgs += @('--keyfile', $key)
    }
}
Write-Host "Serving player updates on ${Bind}:$port. Press Ctrl+C to stop."
Write-Host 'The player URL is in modpack-public\portable\UPDATE-URL.txt (or your configured publishDir).'
& $Python @serverArgs
exit $LASTEXITCODE


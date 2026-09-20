param(
    [string]$Source = '',
    [string]$ConfigPath = ''
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($Source)) { $Source = Join-Path $root 'modpack-public\portable' }
if ([string]::IsNullOrWhiteSpace($ConfigPath)) { $ConfigPath = Join-Path $PSScriptRoot 'cloud.local.json' }
$config = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
foreach ($name in @('pythonExe', 'credentialPath', 'backupRoot')) {
    if (-not $config.PSObject.Properties[$name] -or [string]::IsNullOrWhiteSpace($config.$name)) {
        throw "Cloud configuration lacks $name"
    }
}
$credential = Import-Clixml -LiteralPath $config.credentialPath
$oldKey = $env:SCEX_CLOUD_ACCESS_KEY
$oldSecret = $env:SCEX_CLOUD_SECRET_KEY
try {
    $env:SCEX_CLOUD_ACCESS_KEY = $credential.UserName
    $env:SCEX_CLOUD_SECRET_KEY = $credential.GetNetworkCredential().Password
    $receipt = Join-Path $root ('logs\cloud\publish-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.json')
    & $config.pythonExe (Join-Path $PSScriptRoot 'cloud_publish.py') `
        --source $Source --config $ConfigPath --apply --receipt $receipt `
        --bridge-source $Source --backup-root $config.backupRoot --lock-root $root
    if ($LASTEXITCODE -ne 0) { throw 'Cloud publication failed. Check cloud state before retrying.' }
    Write-Host '[Cloud] Published, read back, and updated the original migration bridge.'
} finally {
    $env:SCEX_CLOUD_ACCESS_KEY = $oldKey
    $env:SCEX_CLOUD_SECRET_KEY = $oldSecret
}

param(
    [string]$Source = '',
    [string]$ConfigPath = '',
    [switch]$StageOnly,
    [string]$ReleasePlanDirectory = '',
    [string]$ActivateStaged = '',
    [string]$CheckStaged = '',
    [string]$ReceiptPath = ''
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
if (([int][bool]$StageOnly + [int][bool]$ActivateStaged + [int][bool]$CheckStaged) -gt 1) {
    throw 'StageOnly, ActivateStaged and CheckStaged are mutually exclusive.'
}
if ($ReleasePlanDirectory -and -not ($StageOnly -or $CheckStaged)) {
    throw 'ReleasePlanDirectory requires StageOnly or CheckStaged.'
}
if ($StageOnly -and [string]::IsNullOrWhiteSpace($ReceiptPath)) {
    throw 'StageOnly requires an explicit ReceiptPath for later activation.'
}
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
    $receipt = $ReceiptPath
    if (-not $receipt) {
        $label = if ($CheckStaged) { 'check' } else { 'publish' }
        $receipt = Join-Path $root ('logs\cloud\' + $label + '-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.json')
    }
    $cloudArguments = @('--source', $Source, '--config', $ConfigPath, '--receipt', $receipt, '--lock-root', $root)
    if ($StageOnly) { $cloudArguments += @('--apply', '--stage-only') }
    elseif ($CheckStaged) { $cloudArguments += @('--check-staged', $CheckStaged) }
    else {
        $cloudArguments += @('--apply', '--bridge-source', $Source, '--backup-root', $config.backupRoot)
        if ($ActivateStaged) { $cloudArguments += @('--activate-staged', $ActivateStaged) }
    }
    if ($ReleasePlanDirectory) { $cloudArguments += @('--release-plan', $ReleasePlanDirectory) }
    & $config.pythonExe (Join-Path $PSScriptRoot 'cloud_publish.py') @cloudArguments
    if ($LASTEXITCODE -ne 0) { throw 'Cloud publication failed. Check cloud state before retrying.' }
    if ($StageOnly) { Write-Host '[Cloud] Candidate staged and verified; live manifests and bootstrap unchanged.' }
    elseif ($CheckStaged) { Write-Host '[Cloud] Staged candidate preflight passed; no cloud writes.' }
    else { Write-Host '[Cloud] Published, read back, and updated the original migration bridge.' }
} finally {
    $env:SCEX_CLOUD_ACCESS_KEY = $oldKey
    $env:SCEX_CLOUD_SECRET_KEY = $oldSecret
}

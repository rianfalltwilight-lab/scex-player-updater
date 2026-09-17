@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
set "PORTABLE_ENTRY_FILE=%~f0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop';$s=[IO.File]::ReadAllText($env:PORTABLE_ENTRY_FILE,[Text.Encoding]::UTF8);try { & ([ScriptBlock]::Create(($s -split '(?m)^# PORTABLE_ENTRY_SCRIPT\r?$')[1])); exit $LASTEXITCODE } catch { Write-Host $_.Exception.Message; if($env:PORTABLE_ENTRY_NO_PAUSE -ne '1'){Read-Host 'Press Enter to close'}; exit 2 }" & call exit /b %%errorlevel%%
# PORTABLE_ENTRY_SCRIPT
$ErrorActionPreference = 'Stop'
function Assert-Unlinked([string]$Path) {
    $item = Get-Item -LiteralPath $Path -Force
    while ($item) {
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Linked client paths are not supported: $Path" }
        $item = if ($item -is [IO.FileInfo]) { $item.Directory } else { $item.Parent }
    }
}
function Test-Client([string]$Path) {
    if (!(Test-Path -LiteralPath $Path -PathType Container)) { return $false }
    if ((Test-Path -LiteralPath (Join-Path $Path 'server.properties')) -or (Test-Path -LiteralPath (Join-Path $Path 'eula.txt'))) { return $false }
    return ((Test-Path -LiteralPath (Join-Path $Path '_updater\Windows-sync.bat') -PathType Leaf) -and
        (Test-Path -LiteralPath (Join-Path $Path '_updater\player-update-generic.ps1') -PathType Leaf) -and
        ((Test-Path -LiteralPath (Join-Path $Path 'UPDATE-URL.txt') -PathType Leaf) -or
         (Test-Path -LiteralPath (Join-Path $Path 'PORTABLE-UPDATE-URL.txt') -PathType Leaf)))
}
$base = Split-Path -Parent $env:PORTABLE_ENTRY_FILE
$candidates = New-Object 'System.Collections.Generic.List[string]'
function Add-Candidate([string]$Path) {
    if (Test-Client $Path) {
        Assert-Unlinked $Path
        $p = (Get-Item -LiteralPath $Path).FullName
        if (!$candidates.Contains($p)) { $candidates.Add($p) }
    }
}
if ($env:PORTABLE_INSTANCE_DIR) {
    $explicit = $env:PORTABLE_INSTANCE_DIR
    if (![IO.Path]::IsPathRooted($explicit)) { $explicit = Join-Path $base $explicit }
    if (!(Test-Client $explicit)) { throw 'PORTABLE_INSTANCE_DIR is not a complete updater client instance.' }
    Add-Candidate $explicit
} else {
    # Bounded launcher layouts. Never search the entire disk or launcher accounts.
    Add-Candidate $base
    if ((Split-Path -Leaf $base) -ieq '_updater') { Add-Candidate (Split-Path -Parent $base) }
    foreach ($name in @('.minecraft','minecraft','client','main-client')) { Add-Candidate (Join-Path $base $name) }
    foreach ($name in @('.minecraft\versions','versions','instances')) {
        $container = Join-Path $base $name
        if (Test-Path -LiteralPath $container -PathType Container) {
            Assert-Unlinked $container
            foreach ($child in @(Get-ChildItem -LiteralPath $container -Directory -Force | Sort-Object Name)) {
                Assert-Unlinked $child.FullName
                Add-Candidate $child.FullName
                Add-Candidate (Join-Path $child.FullName '.minecraft')
                Add-Candidate (Join-Path $child.FullName 'minecraft')
            }
        }
    }
}
if ($candidates.Count -eq 0) {
    throw 'No update-enabled client found. Put this BAT beside the launcher or inside the instance. Keep _updater and UPDATE-URL.txt inside that instance. For a custom layout, set PORTABLE_INSTANCE_DIR.'
}
if ($env:PORTABLE_ENTRY_DETECT_ONLY -eq '1') {
    ConvertTo-Json -InputObject @($candidates.ToArray()) -Compress
    $global:LASTEXITCODE = 0
    return
}
$selected = 0
if ($candidates.Count -gt 1) {
    Write-Host 'Multiple client instances found. Choose ONE to update:'
    for ($i=0; $i -lt $candidates.Count; $i++) { Write-Host ('{0}. {1}' -f ($i+1), $candidates[$i]) }
    $number = 0
    $answer = Read-Host 'Number (Enter to cancel)'
    if (![int]::TryParse($answer, [ref]$number) -or $number -lt 1 -or $number -gt $candidates.Count) { throw 'Cancelled; no client was changed.' }
    $selected = $number - 1
}
$instance = $candidates[$selected]
$engine = Join-Path $instance '_updater\Windows-sync.bat'
Assert-Unlinked $engine
Assert-Unlinked (Join-Path $instance '_updater\player-update-generic.ps1')
Write-Host ('Client instance: ' + $instance)
# Do not inherit an old bootstrap directory from a parent updater process.
$env:PORTABLE_SYNC_BOOTSTRAPPED = $null
$env:PORTABLE_SYNC_HOME = $null
$env:PORTABLE_INSTANCE_DIR = $instance
& $engine
$result = $LASTEXITCODE
$global:LASTEXITCODE = $result

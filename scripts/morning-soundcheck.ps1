<#
Run after stopping rehearsal recording and saving the Ableton set.
Creates a new results folder and a calibrated set beside the saved source.
Use -Latest only when the latest take on every track belongs to the same rehearsal.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Profile,
    [string]$Recordings,
    [string]$RecordedOn = (Get-Date -Format 'yyyy-MM-dd'),
    [switch]$Latest,
    [ValidateRange(10, 300)][double]$Seconds = 90,
    [ValidateRange(0, 86400)][double]$Start = 0
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw 'Create .venv and install this project with its soundcheck extra first. See docs/soundcheck.md.'
}
$profilePath = (Resolve-Path -LiteralPath $Profile).Path
$profileDirectory = Split-Path -Parent $profilePath
$configuration = Get-Content -LiteralPath $profilePath -Raw | ConvertFrom-Json
$sourcePath = [string]$configuration.source_set
if (-not [System.IO.Path]::IsPathRooted($sourcePath)) {
    $sourcePath = Join-Path $profileDirectory $sourcePath
}
$sourcePath = (Resolve-Path -LiteralPath $sourcePath).Path
$sourceDirectory = Split-Path -Parent $sourcePath
if (-not $Recordings) {
    $Recordings = Join-Path $sourceDirectory 'Samples\Recorded'
}
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
$resultsPath = Join-Path $profileDirectory "runs\$stamp"
$setName = [System.IO.Path]::GetFileNameWithoutExtension($sourcePath)
$calibratedPath = Join-Path $sourceDirectory "$setName - Soundcheck $stamp.als"
$arguments = @('-m', 'wcfi_tools.cli', 'soundcheck', 'calibrate', $profilePath,
    '--recordings', $Recordings, '--recorded-on', $RecordedOn,
    '--seconds', $Seconds.ToString([System.Globalization.CultureInfo]::InvariantCulture),
    '--start', $Start.ToString([System.Globalization.CultureInfo]::InvariantCulture),
    '--output', $resultsPath, '--write-set', $calibratedPath)
if ($Latest) { $arguments += '--latest' }
& $pythonPath @arguments
if ($LASTEXITCODE -ne 0) {
    throw "Soundcheck needs attention. Inspect the results in $resultsPath before retrying."
}
Write-Output "Review report: $(Join-Path $resultsPath 'report.html')"
Write-Output "Then audition in Ableton: $calibratedPath"

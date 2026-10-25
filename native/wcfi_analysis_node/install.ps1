param([string]$Destination = (Join-Path $env:CommonProgramFiles 'VST3\WCFI'))
$ErrorActionPreference = 'Stop'
$source = Join-Path $PSScriptRoot 'build\VST3\Release\WCFI Analysis Node.vst3'
if (-not (Test-Path -LiteralPath "$source\Contents\Resources\moduleinfo.json")) {
    throw 'Build and validate the plugin with build.ps1 first.'
}
$target = Join-Path ([IO.Path]::GetFullPath($Destination)) 'WCFI Analysis Node.vst3'
if (Test-Path -LiteralPath $target) {
    throw "Already installed at $target. Close DAWs and remove that exact plugin bundle before replacing it."
}
New-Item -ItemType Directory -Force -Path $Destination | Out-Null
Copy-Item -LiteralPath $source -Destination $target -Recurse
Write-Output "Installed: $target"
Write-Output 'Rescan VST3 plugins in Ableton. No ASIO registration or JACK is needed.'

# Make this checkout's existing virtual-environment CLI available in PowerShell.
# No administrator rights, package downloads or DAW controls are involved.
[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$BinDirectory = (Join-Path $env:USERPROFILE '.local\bin'),
    [switch]$NoPathUpdate
)

$ErrorActionPreference = 'Stop'
$checkoutRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$cliExecutable = Join-Path $checkoutRoot '.venv\Scripts\wcfi.exe'
if (-not (Test-Path -LiteralPath $cliExecutable -PathType Leaf)) {
    throw "The checkout CLI is missing. Install the project in $checkoutRoot\.venv first."
}
$launcherDirectory = [IO.Path]::GetFullPath($BinDirectory)
$launcherPath = Join-Path $launcherDirectory 'wcfi.cmd'
# cmd.exe expands percent variables even inside quoted paths. Fail rather than
# generate an ambiguous launcher for unusual checkout names.
if ($cliExecutable -match '[%"\r\n]') {
    throw 'The checkout path contains characters unsupported by the Windows launcher.'
}
$marker = 'rem Installed by wcfi-tools scripts/install-cli.ps1'
$launcherContent = "@echo off`r`n$marker`r`n`"$cliExecutable`" %*`r`nexit /b %errorlevel%`r`n"
if (Test-Path -LiteralPath $launcherPath) {
    $existing = [IO.File]::ReadAllText($launcherPath)
    if (-not $existing.Contains($marker)) {
        throw "An unrelated launcher exists at $launcherPath; it was not overwritten."
    }
}
if ($PSCmdlet.ShouldProcess($launcherPath, 'Install the wcfi checkout launcher')) {
    [IO.Directory]::CreateDirectory($launcherDirectory) | Out-Null
    [IO.File]::WriteAllText($launcherPath, $launcherContent, [Text.Encoding]::ASCII)
}
if (-not $NoPathUpdate) {
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    $existingDirectories = @($userPath -split ';' | Where-Object { $_ } | ForEach-Object {
        [Environment]::ExpandEnvironmentVariables($_).TrimEnd('\')
    })
    if ($existingDirectories -notcontains $launcherDirectory.TrimEnd('\')) {
        $updatedPath = if ([string]::IsNullOrWhiteSpace($userPath)) {
            $launcherDirectory
        } else {
            $userPath.TrimEnd(';') + ';' + $launcherDirectory
        }
        if ($PSCmdlet.ShouldProcess('Current user PATH', "Append $launcherDirectory")) {
            [Environment]::SetEnvironmentVariable('Path', $updatedPath, 'User')
        }
    }
    if (-not $WhatIfPreference -and ($env:Path -split ';') -notcontains $launcherDirectory) {
        $env:Path = "$launcherDirectory;$env:Path"
    }
}
if (-not $WhatIfPreference) {
    Write-Host "Launcher: $launcherPath"
    if (-not $NoPathUpdate) {
        Write-Host 'User PATH configured. Restart an existing terminal to pick up the change.'
    }
    Write-Host 'Try: wcfi audio emulator launch'
}

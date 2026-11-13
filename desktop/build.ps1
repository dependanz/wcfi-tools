param([switch]$Debug, [ValidateRange(1,16)][int]$Jobs = 2, [switch]$UseLld)
$ErrorActionPreference = 'Stop'
$repository = Split-Path -Parent $PSScriptRoot

# Use an existing Rust installation, or the workspace-local bootstrap if present.
if (-not (Get-Command cargo.exe -ErrorAction SilentlyContinue)) {
    $bootstrap = Join-Path $repository '_work/emulator-desktop-2026-10-20'
    $localCargo = Join-Path $bootstrap 'cargo/bin/cargo.exe'
    if (Test-Path -LiteralPath $localCargo) {
        $env:CARGO_HOME = Join-Path $bootstrap 'cargo'
        $env:RUSTUP_HOME = Join-Path $bootstrap 'rustup'
        $env:PATH = (Join-Path $env:CARGO_HOME 'bin') + ';' + $env:PATH
    } else {
        throw 'Install stable Rust for x86_64-pc-windows-msvc: https://rustup.rs/'
    }
}
if (-not (Get-Command npm.cmd -ErrorAction SilentlyContinue)) {
    throw 'Install Node.js LTS: https://nodejs.org/'
}
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio/Installer/vswhere.exe'
if (-not (Test-Path -LiteralPath $vswhere)) {
    throw 'Install Microsoft C++ Build Tools with Desktop development with C++ and a Windows SDK.'
}
$installation = & $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $installation) { throw 'Microsoft Visual C++ x64 build tools are missing.' }
$developerCommand = Join-Path $installation 'Common7/Tools/VsDevCmd.bat'
if (-not (Test-Path -LiteralPath $developerCommand)) { throw 'Visual Studio developer environment was not found.' }
# Import the vendor's build environment; all file operations remain in PowerShell.
$environment = & $env:ComSpec /d /c "call `"$developerCommand`" -no_logo -arch=x64 -host_arch=x64 && set"
if ($LASTEXITCODE -ne 0) { throw 'Could not initialize the Microsoft compiler environment.' }
foreach ($line in $environment) {
    if ($line -match '^([A-Za-z_][A-Za-z0-9_()]*)=(.*)$') {
        [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], 'Process')
    }
}
if (-not (Get-Command link.exe -ErrorAction SilentlyContinue) -or -not (Get-Command rc.exe -ErrorAction SilentlyContinue)) {
    throw 'The C++ linker or Windows SDK resource compiler is missing. Repair Desktop development with C++.'
}
$env:CARGO_BUILD_JOBS = "$Jobs"
if ($UseLld) {
    # COFF linker bundled with Rust; still uses the installed MSVC/Windows SDK libraries.
    $sysroot = & rustc.exe --print sysroot
    if ($LASTEXITCODE -ne 0) { throw 'Could not locate the Rust toolchain.' }
    $lld = Join-Path $sysroot 'lib/rustlib/x86_64-pc-windows-msvc/bin/rust-lld.exe'
    if (-not (Test-Path -LiteralPath $lld)) { throw 'The Rust COFF linker was not found.' }
    $env:CARGO_TARGET_X86_64_PC_WINDOWS_MSVC_LINKER = $lld
    $env:RUSTFLAGS = "$env:RUSTFLAGS -C linker-flavor=lld-link".Trim()
}
Push-Location $PSScriptRoot
try {
    if (-not (Test-Path -LiteralPath 'node_modules')) {
        & npm.cmd ci --no-audit --no-fund
        if ($LASTEXITCODE -ne 0) { throw 'Frontend dependency installation failed.' }
    }
    & npm.cmd run build
    if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
    $tauriArguments = @('exec', '--', 'tauri', 'build', '--no-bundle')
    if ($Debug) { $tauriArguments += '--debug' }
    & npm.cmd @tauriArguments
    if ($LASTEXITCODE -ne 0) { throw 'Tauri build failed.' }
} finally { Pop-Location }
$mode = if ($Debug) { 'debug' } else { 'release' }
Write-Output "Built desktop/src-tauri/target/$mode/wcfi-audio.exe. Run: wcfi audio emulator launch"

param([switch]$Fetch, [string]$Lld)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$sdk = Join-Path $root 'deps\vst3sdk'
$pin = '3cdf9ca5d1f5b1b21e0a86832aa4abe55607bd96'
if (-not (Test-Path -LiteralPath "$sdk\CMakeLists.txt")) {
    if (-not $Fetch) { throw 'SDK missing. Run build.ps1 -Fetch to download the pinned official SDK.' }
    New-Item -ItemType Directory -Force -Path (Split-Path $sdk) | Out-Null
    git clone --branch v3.8.1_build_84 --depth 1 https://github.com/steinbergmedia/vst3sdk.git $sdk
    if ($LASTEXITCODE) { throw 'SDK download failed' }
}
$actual = git -C $sdk rev-parse HEAD
if ($actual -ne $pin) { throw "Unexpected VST3 SDK revision: $actual" }
if ($Fetch) {
    git -C $sdk submodule update --init --depth 1 base cmake pluginterfaces public.sdk
    if ($LASTEXITCODE) { throw 'SDK submodule download failed' }
}
$vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
$vs = & $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $vs) { throw 'Install Visual Studio Build Tools with Desktop development with C++' }
$dev = Join-Path $vs 'Common7\Tools\VsDevCmd.bat'
$environment = & $env:ComSpec /d /c "call `"$dev`" -no_logo -arch=x64 -host_arch=x64 && set"
foreach ($line in $environment) {
    if ($line -match '^([A-Za-z_][A-Za-z0-9_()]*)=(.*)$') {
        [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], 'Process')
    }
}
$cmake = Join-Path $vs 'Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe'
$ninja = Join-Path $vs 'Common7\IDE\CommonExtensions\Microsoft\CMake\Ninja\ninja.exe'
$extra = @()
if ($Lld) {
    $resolvedLinker = (Resolve-Path -LiteralPath $Lld).Path
    New-Item -ItemType Directory -Force -Path "$root\build" | Out-Null
    Copy-Item -LiteralPath $resolvedLinker -Destination "$root\build\lld-link.exe" -Force
    $extra += "-DCMAKE_LINKER=$root\build\lld-link.exe"
}
& $cmake -S $root -B "$root\build" -G Ninja "-DCMAKE_MAKE_PROGRAM=$ninja" -DCMAKE_BUILD_TYPE=Release @extra
if ($LASTEXITCODE) { throw 'CMake configuration failed' }
& $cmake --build "$root\build" --target WcfiAnalysisNode WcfiAnalysisBridge node_tests
if ($LASTEXITCODE) { throw 'Native build or SDK validation failed' }
& (Join-Path (Split-Path $cmake) 'ctest.exe') --test-dir "$root\build" --output-on-failure
if ($LASTEXITCODE) { throw 'Analysis Node tests failed' }

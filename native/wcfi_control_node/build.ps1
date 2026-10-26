param(
    [string]$Lld = (Join-Path $PSScriptRoot '..\wcfi_analysis_node\build\lld-link.exe'),
    [string]$BuildDirectory = (Join-Path $PSScriptRoot 'build-red-single-mic')
)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$sdk = Join-Path $root '..\wcfi_analysis_node\deps\vst3sdk'
if (-not (Test-Path -LiteralPath "$sdk\CMakeLists.txt")) { throw 'Existing pinned SDK is required; this script never downloads or installs.' }
$actual = git -C $sdk rev-parse HEAD
if ($actual -ne '3cdf9ca5d1f5b1b21e0a86832aa4abe55607bd96') { throw "Unexpected SDK revision: $actual" }
$taskProcess = [System.Diagnostics.Process]::GetCurrentProcess()
$taskProcess.PriorityClass = 'Idle'
$taskAffinity = $taskProcess.ProcessorAffinity.ToInt64()
$taskProcess.ProcessorAffinity = [IntPtr]($taskAffinity -band (-$taskAffinity))
$env:CMAKE_BUILD_PARALLEL_LEVEL = '1'
$vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
$vs = & $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $vs) { throw 'Existing Visual Studio C++ Build Tools are required' }
$dev = Join-Path $vs 'Common7\Tools\VsDevCmd.bat'
$environment = & $env:ComSpec /d /c "call `"$dev`" -no_logo -arch=x64 -host_arch=x64 && set"
foreach ($line in $environment) {
    if ($line -match '^([A-Za-z_][A-Za-z0-9_()]*)=(.*)$') {
        [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], 'Process')
    }
}
$cmake = Join-Path $vs 'Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe'
$ninja = Join-Path $vs 'Common7\IDE\CommonExtensions\Microsoft\CMake\Ninja\ninja.exe'
$resolvedLinker = (Resolve-Path -LiteralPath $Lld).Path
& $cmake -S $root -B $BuildDirectory -G Ninja "-DCMAKE_MAKE_PROGRAM=$ninja" -DCMAKE_BUILD_TYPE=Release "-DCMAKE_LINKER=$resolvedLinker"
if ($LASTEXITCODE) { throw 'CMake configure failed' }
& $cmake --build $BuildDirectory --target WcfiControlNode WcfiControlBridge WcfiMicBridge control_tests single_mic_tests control_ui_preview control_ui_tests --parallel 1
if ($LASTEXITCODE) { throw 'Native build or SDK validator failed' }
& (Join-Path (Split-Path $cmake) 'ctest.exe') --test-dir $BuildDirectory --output-on-failure
if ($LASTEXITCODE) { throw 'Control Node tests failed' }

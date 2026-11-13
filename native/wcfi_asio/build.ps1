param([Parameter(Mandatory=$true)][string]$Zig, [switch]$Test, [string]$OutputDirectory)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$zigPath = (Resolve-Path -LiteralPath $Zig).Path
$outDir = if ($OutputDirectory) { [IO.Path]::GetFullPath($OutputDirectory) } else { Join-Path $root 'build' }
New-Item -ItemType Directory -Force $outDir | Out-Null
$env:ZIG_GLOBAL_CACHE_DIR = Join-Path $outDir 'zig-cache'
$env:ZIG_LOCAL_CACHE_DIR = $env:ZIG_GLOBAL_CACHE_DIR
$env:TEMP = Join-Path $outDir 'temp'
$env:TMP = $env:TEMP
New-Item -ItemType Directory -Force $env:TEMP | Out-Null
$common = @('cc', '-target', 'x86_64-windows-gnu', '-std=c11', '-O2', '-g',
    '-Wall', '-Wextra', '-Werror', '-Wno-dll-attribute-on-redeclaration')
& $zigPath @common '-shared' (Join-Path $root 'driver.c') '-o' (Join-Path $outDir 'WcfiAsio.dll') '-lole32' '-luuid' '-ladvapi32' '-luser32'
if ($LASTEXITCODE -ne 0) { throw 'ASIO DLL build failed' }
& $zigPath @common '-DWCFI_TEST_NAMESPACE' '-shared' (Join-Path $root 'driver.c') '-o' (Join-Path $outDir 'WcfiAsioTest.dll') '-lole32' '-luuid' '-ladvapi32' '-luser32'
if ($LASTEXITCODE -ne 0) { throw 'Isolated ASIO test DLL build failed' }
& $zigPath @common '-municode' (Join-Path $root 'test_host.c') '-o' (Join-Path $outDir 'wcfi-asio-test.exe') '-lole32' '-luuid' '-ladvapi32'
if ($LASTEXITCODE -ne 0) { throw 'ASIO test host build failed' }
if ($Test) {
    & (Join-Path $outDir 'wcfi-asio-test.exe') (Join-Path $outDir 'WcfiAsioTest.dll')
    if ($LASTEXITCODE -ne 0) { throw 'ASIO integration test failed' }
}

param([switch]$Uninstall, [switch]$Elevated)
$ErrorActionPreference = 'Stop'
if (-not [Environment]::Is64BitProcess) { throw 'Use 64-bit PowerShell.' }
$dll = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot 'build\WcfiAsio.dll')).Path
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    if ($Elevated) { throw 'Windows did not grant administrator access.' }
    $elevationArgs = @('-NoProfile', '-File', ('"' + $PSCommandPath + '"'), '-Elevated')
    if ($Uninstall) { $elevationArgs += '-Uninstall' }
    Write-Output 'Requesting Windows administrator access. Approve the UAC prompt to continue.'
    try {
        $elevatedProcess = Start-Process -FilePath "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -ArgumentList $elevationArgs -Verb RunAs -WindowStyle Hidden -PassThru -Wait
    } catch {
        throw "Windows administrator access was canceled or unavailable: $($_.Exception.Message)"
    }
    if ($elevatedProcess.ExitCode -ne 0) {
        throw 'Elevated registration failed. Run this script from Administrator PowerShell to see the detailed error.'
    }
    if ($Uninstall) { Write-Output 'Unregistered WCFI Emulator.' }
    else { Write-Output 'Registered WCFI Emulator. Keep the DLL at its current path.' }
    return
}
$argsList = @('/s')
if ($Uninstall) { $argsList += '/u' }
$argsList += ('"' + $dll + '"')
$process = Start-Process -FilePath "$env:SystemRoot\System32\regsvr32.exe" -ArgumentList $argsList -WindowStyle Hidden -PassThru -Wait
if ($process.ExitCode -ne 0) { throw "ASIO registration failed (regsvr32 exit $($process.ExitCode)). No other driver's registration is replaced." }
if ($Uninstall) { Write-Output 'Unregistered WCFI Emulator.' }
else { Write-Output 'Registered WCFI Emulator. Keep the DLL at its current path.' }

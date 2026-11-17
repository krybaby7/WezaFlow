$ErrorActionPreference = 'Stop'

function Get-ImagePath($Path) {
    # Resolve the file through junctions even when process metadata reports
    # the dated installation path used before the stable launcher was added.
    $file = [IO.File]::Open($Path, 'Open', 'Read', 'ReadWrite,Delete')
    try {
        $resolved = New-Object Text.StringBuilder 32768
        $length = [WezzaLaunchPath]::GetFinalPathNameByHandle($file.SafeFileHandle, $resolved, $resolved.Capacity, 0)
        if ($length -eq 0 -or $length -ge $resolved.Capacity) {
            throw "Could not resolve the Wezza executable path: $Path"
        }
        return $resolved.ToString()
    } finally {
        $file.Dispose()
    }
}

$trialRoot = $PSScriptRoot
$trialProject = Join-Path $trialRoot 'project'
$trialExecutable = Join-Path $trialRoot 'Wezza.exe'
$logs = Join-Path $trialRoot 'logs'
$trialPython = 'C:\Users\adami\projects\Hobby\Tier 1\WEZZA\.venv\Scripts'

try {
    [IO.Directory]::CreateDirectory($logs) | Out-Null
    try {
        $launchLock = [IO.File]::Open((Join-Path $logs 'launcher.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
    } catch [IO.IOException] {
        $win32Code = $_.Exception.GetBaseException().HResult -band 0xffff
        if ($win32Code -in 32, 33) { return }
        throw
    }
    try {
        if (-not (Test-Path -LiteralPath $trialExecutable -PathType Leaf)) {
            throw 'Wezza.exe is missing. The package has not been fully assembled.'
        }
        if (-not ('WezzaLaunchPath' -as [type])) {
            Add-Type @"
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;

public static class WezzaLaunchPath {
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
    public static extern uint GetFinalPathNameByHandle(
        SafeFileHandle file, StringBuilder path, int length, int flags);
}
"@
        }
        $image = Get-ImagePath $trialExecutable
        foreach ($existing in Get-Process -Name Wezza -ErrorAction SilentlyContinue) {
            $path = $existing.Path
            if ($path -and (Get-ImagePath $path) -eq $image) { return }
        }
        if (-not (Test-Path -LiteralPath (Join-Path $trialPython 'python.exe') -PathType Leaf)) {
            throw 'The existing WEZZA Python environment is missing.'
        }

        $env:LOCALFLOW_PROJECT_ROOT = $trialProject
        $env:PATH = "$trialPython;$env:PATH"
        $env:WEBVIEW2_USER_DATA_FOLDER = Join-Path $trialRoot 'webview2'
        $env:HF_HUB_OFFLINE = '1'
        $env:TRANSFORMERS_OFFLINE = '1'
        $trialStamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
        Start-Process -FilePath $trialExecutable -WorkingDirectory $trialProject -WindowStyle Normal `
            -RedirectStandardOutput (Join-Path $logs "desktop-$trialStamp.stdout.log") `
            -RedirectStandardError (Join-Path $logs "desktop-$trialStamp.stderr.log")
    } finally {
        $launchLock.Dispose()
    }
} catch {
    $message = "$(Get-Date -Format o) $($_.Exception.Message)"
    Add-Content -LiteralPath (Join-Path $logs 'launcher.log') -Value $message -ErrorAction SilentlyContinue
    throw
}

param([switch]$DevelopmentRelease)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

if (-not $DevelopmentRelease) {
    $install = Join-Path $root "artifacts\WezzaFlow"
    $launcher = Join-Path $install "Launch Wezza.vbs"
    if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
        throw "WezzaFlow launcher not found: $launcher. For a checkout build, use -DevelopmentRelease or npm run tauri -- dev."
    }
    Start-Process -FilePath $launcher -WorkingDirectory $install
    return
}

$tauriExe = Join-Path $root "src-tauri\target\release\localflow.exe"
if (-not (Test-Path -LiteralPath $tauriExe -PathType Leaf)) {
    throw "Development release executable not found: $tauriExe. Build it with npm run tauri -- build, or run in development with npm run tauri -- dev."
}
Start-Process -FilePath $tauriExe -WorkingDirectory $root -WindowStyle Hidden

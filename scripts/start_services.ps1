$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$tauriExe = Join-Path $root "src-tauri\target\release\localflow.exe"

if (-not (Test-Path -LiteralPath $tauriExe -PathType Leaf)) {
    throw "LocalFlow desktop executable not found: $tauriExe. Build it with npm run tauri -- build, or run in development with npm run tauri -- dev."
}

Start-Process -FilePath $tauriExe `
    -WorkingDirectory $root `
    -WindowStyle Hidden

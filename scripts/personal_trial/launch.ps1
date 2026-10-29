$ErrorActionPreference = 'Stop'
try {
    $trialRoot = $PSScriptRoot
    $trialProject = Join-Path $trialRoot 'project'
    $trialExecutable = Join-Path $trialRoot 'Wezza.exe'
    $trialPython = 'C:\Users\adami\projects\Hobby\Tier 1\WEZZA\.venv\Scripts'
    if (-not (Test-Path -LiteralPath $trialExecutable -PathType Leaf)) { throw 'Wezza.exe is missing. The trial package has not been fully assembled.' }
    if (-not (Test-Path -LiteralPath (Join-Path $trialPython 'python.exe') -PathType Leaf)) { throw 'The existing WEZZA Python environment is missing.' }
    $env:LOCALFLOW_PROJECT_ROOT = $trialProject
    $env:PATH = "$trialPython;$env:PATH"
    $env:WEBVIEW2_USER_DATA_FOLDER = Join-Path $trialRoot 'webview2'
    $env:HF_HUB_OFFLINE = '1'
    $env:TRANSFORMERS_OFFLINE = '1'
    $trialStamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
    $trialProcess = Start-Process -FilePath $trialExecutable -WorkingDirectory $trialProject -WindowStyle Normal -PassThru -RedirectStandardOutput (Join-Path $trialRoot "logs\desktop-$trialStamp.stdout.log") -RedirectStandardError (Join-Path $trialRoot "logs\desktop-$trialStamp.stderr.log")
    # Retain the process handle before waiting so Windows PowerShell can read
    # the exit status even when startup fails immediately.
    $null = $trialProcess.Handle
    $trialProcess.WaitForExit()
    if ($trialProcess.ExitCode -ne 0) { throw "Wezza exited with code $($trialProcess.ExitCode). See the package logs folder and project\artifacts\logs." }
} catch {
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show($_.Exception.Message, 'Wezza trial could not start') | Out-Null
}

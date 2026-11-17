import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / 'scripts' / 'start_services.ps1'
POWERSHELL = shutil.which('pwsh') or shutil.which('powershell')


@unittest.skipUnless(POWERSHELL, 'PowerShell is required')
class DesktopLauncherTests(unittest.TestCase):
    def run_launcher(self, built, *, development=False, installed=False):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / 'scripts' / 'start_services.ps1'
            script.parent.mkdir()
            shutil.copyfile(LAUNCHER, script)
            exe = root / 'src-tauri' / 'target' / 'release' / 'localflow.exe'
            exe.parent.mkdir(parents=True)
            if built:
                exe.write_bytes(b'dummy executable')
            package_launcher = root / 'artifacts' / 'WezzaFlow' / 'Launch Wezza.vbs'
            if installed:
                package_launcher.parent.mkdir(parents=True)
                package_launcher.touch()
            python = root / '.venv' / 'Scripts' / 'python.exe'
            python.parent.mkdir(parents=True)
            python.write_bytes(b'dummy executable')
            pid = root / 'artifacts' / 'logs' / 'desktop-python-api.pid'
            pid.parent.mkdir(parents=True)
            pid.write_text('12345', encoding='utf-8')
            config = root / 'config.json'
            config.write_text('sentinel config', encoding='utf-8')
            calls = root / 'calls.jsonl'
            wrapper = root / 'wrapper.ps1'
            wrapper.write_text(r'''
param($Launcher, $Calls, $Mode)
function Start-Process {
    param($FilePath, $WorkingDirectory, $WindowStyle, $ArgumentList, [switch]$PassThru)
    @{ FilePath = $FilePath; WorkingDirectory = $WorkingDirectory; WindowStyle = $WindowStyle; ArgumentList = $ArgumentList } | ConvertTo-Json -Compress | Add-Content -LiteralPath $Calls
    if ($PassThru) { return @{ Id = 99999 } }
}
function Stop-Process { throw 'Unexpected Stop-Process' }
function taskkill { throw 'Unexpected taskkill' }
function Get-Process { throw 'Unexpected Get-Process' }
function Start-Sleep { throw 'Unexpected Start-Sleep' }
try { if ($Mode -eq 'dev') { & $Launcher -DevelopmentRelease } else { & $Launcher } } catch { [Console]::Error.WriteLine($_.Exception.Message); exit 1 }
''', encoding='utf-8')
            result = subprocess.run([POWERSHELL, '-NoProfile', '-NonInteractive', '-File', str(wrapper), str(script), str(calls), 'dev' if development else 'deployed'], capture_output=True, text=True, env=os.environ.copy(), timeout=10)
            recorded = [json.loads(line) for line in calls.read_text(encoding='utf-8-sig').splitlines()] if calls.exists() else []
            self.assertEqual(pid.read_text(encoding='utf-8'), '12345')
            self.assertEqual(config.read_text(encoding='utf-8'), 'sentinel config')
            return result, recorded, str(root), str(exe)

    def test_launches_built_desktop_once(self):
        result, calls, root, exe = self.run_launcher(True, development=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['FilePath'], exe)
        self.assertEqual(calls[0]['WorkingDirectory'], root)
        self.assertEqual(calls[0]['WindowStyle'], 'Hidden')
        self.assertFalse(calls[0]['ArgumentList'])

    def test_missing_build_fails_with_instructions_and_no_launch(self):
        result, calls, _, _ = self.run_launcher(False, development=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('npm run tauri -- build', result.stderr)
        self.assertIn('npm run tauri -- dev', result.stderr)
        self.assertEqual(calls, [])

    def test_default_launches_canonical_package_even_when_development_build_exists(self):
        result, calls, root, _ = self.run_launcher(True, installed=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        package = Path(root) / 'artifacts' / 'WezzaFlow'
        self.assertEqual(len(calls), 1)
        self.assertEqual(Path(calls[0]['FilePath']), package / 'Launch Wezza.vbs')
        self.assertEqual(Path(calls[0]['WorkingDirectory']), package)

    def test_default_missing_package_does_not_launch_development_build(self):
        result, calls, _, _ = self.run_launcher(True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('WezzaFlow', result.stderr)
        self.assertEqual(calls, [])

    @unittest.skipUnless(os.name == "nt", "Windows process and junction paths")
    def test_packaged_launcher_uses_one_process_through_a_junction(self):
        powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dated = root / "dated trial"
            (dated / "project").mkdir(parents=True)
            (dated / "logs").mkdir()
            source = (ROOT / "scripts" / "personal_trial" / "launch.ps1").read_text(encoding="utf-8")
            source = source.replace(
                r"$trialPython = 'C:\Users\adami\projects\Hobby\Tier 1\WEZZA\.venv\Scripts'",
                "$trialPython = $trialRoot",
            )
            (dated / "launch.ps1").write_text(source, encoding="utf-8")
            for name in ("Wezza.exe", "python.exe"):
                (dated / name).touch()
            canonical = root / "WezzaFlow"
            wrapper = root / "wrapper.ps1"
            wrapper.write_text(r"""
param($Canonical, $Dated)
$ErrorActionPreference = 'Stop'
New-Item -ItemType Junction -Path $Canonical -Target $Dated | Out-Null
$global:launches = 0
$global:runningImage = $null
function Get-Process {
    param($Name)
    if ($global:runningImage) { [pscustomobject]@{ Path = $global:runningImage } }
}
function Start-Process {
    param($FilePath, $WorkingDirectory, $WindowStyle, $RedirectStandardOutput, $RedirectStandardError)
    if ($WorkingDirectory -ne (Join-Path $Canonical 'project')) {
        throw "Incorrect working directory: $WorkingDirectory"
    }
    $global:launches++
    $global:runningImage = Join-Path $Dated 'Wezza.exe'
}
$launchScript = Join-Path $Canonical 'launch.ps1'
$lockPath = Join-Path $Dated 'logs\launcher.lock'
$lock = [IO.File]::Open($lockPath, 'OpenOrCreate', 'ReadWrite', 'None')
try {
    & $launchScript
    if ($global:launches -ne 0) { throw 'Launcher spawned during another launch' }
} finally {
    $lock.Dispose()
}
& $launchScript
& $launchScript
if ($global:launches -ne 1) { throw "Expected one start, got $global:launches" }
""", encoding="utf-8")
            result = subprocess.run(
                [str(powershell), "-NoProfile", "-NonInteractive", "-File",
                 str(wrapper), str(canonical), str(dated)],
                capture_output=True, text=True, timeout=12,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

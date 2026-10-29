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
    def run_launcher(self, built):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / 'scripts' / 'start_services.ps1'
            script.parent.mkdir()
            shutil.copyfile(LAUNCHER, script)
            exe = root / 'src-tauri' / 'target' / 'release' / 'localflow.exe'
            exe.parent.mkdir(parents=True)
            if built:
                exe.write_bytes(b'dummy executable')
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
param($Launcher, $Calls)
function Start-Process {
    param($FilePath, $WorkingDirectory, $WindowStyle, $ArgumentList, [switch]$PassThru)
    @{ FilePath = $FilePath; WorkingDirectory = $WorkingDirectory; WindowStyle = $WindowStyle; ArgumentList = $ArgumentList } | ConvertTo-Json -Compress | Add-Content -LiteralPath $Calls
    if ($PassThru) { return @{ Id = 99999 } }
}
function Stop-Process { throw 'Unexpected Stop-Process' }
function taskkill { throw 'Unexpected taskkill' }
function Get-Process { throw 'Unexpected Get-Process' }
function Start-Sleep { throw 'Unexpected Start-Sleep' }
try { & $Launcher } catch { [Console]::Error.WriteLine($_.Exception.Message); exit 1 }
''', encoding='utf-8')
            result = subprocess.run([POWERSHELL, '-NoProfile', '-NonInteractive', '-File', str(wrapper), str(script), str(calls)], capture_output=True, text=True, env=os.environ.copy(), timeout=10)
            recorded = [json.loads(line) for line in calls.read_text(encoding='utf-8-sig').splitlines()] if calls.exists() else []
            self.assertEqual(pid.read_text(encoding='utf-8'), '12345')
            self.assertEqual(config.read_text(encoding='utf-8'), 'sentinel config')
            return result, recorded, str(root), str(exe)

    def test_launches_built_desktop_once(self):
        result, calls, root, exe = self.run_launcher(True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['FilePath'], exe)
        self.assertEqual(calls[0]['WorkingDirectory'], root)
        self.assertEqual(calls[0]['WindowStyle'], 'Hidden')
        self.assertFalse(calls[0]['ArgumentList'])

    def test_missing_build_fails_with_instructions_and_no_launch(self):
        result, calls, _, _ = self.run_launcher(False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('npm run tauri -- build', result.stderr)
        self.assertIn('npm run tauri -- dev', result.stderr)
        self.assertEqual(calls, [])

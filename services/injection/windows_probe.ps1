param([switch]$FixtureTarget)
$ErrorActionPreference = 'Stop'
try {
    Add-Type -AssemblyName UIAutomationClient
    Add-Type -AssemblyName UIAutomationTypes
    . (Join-Path $PSScriptRoot 'windows_probe_uia.ps1')
    Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class ProbeNative {
 [StructLayout(LayoutKind.Sequential)] public struct RECT { public int l,t,r,b; }
 [StructLayout(LayoutKind.Sequential)] public struct GUI { public uint size, flags; public IntPtr active,focus,capture,menu,move,caret; public RECT rect; }
 [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
 [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
 [DllImport("user32.dll")] public static extern bool GetGUIThreadInfo(uint id, ref GUI info);
 [DllImport("kernel32.dll")] public static extern IntPtr OpenProcess(uint access, bool inherit, uint pid);
 [DllImport("kernel32.dll")] public static extern bool GetProcessTimes(IntPtr h, out long creation, out long exit, out long kernel, out long user);
 [DllImport("kernel32.dll")] public static extern bool CloseHandle(IntPtr h);
}
"@
    $result = @{}
    $fixture = $null
    if ($FixtureTarget) {
        Add-Type -AssemblyName System.Windows.Forms
        $fixture = New-Object System.Windows.Forms.TextBox
        $fixture.Text = 'Synthetic fixture only'
        $hwnd = $fixture.Handle
    } else { $hwnd = [ProbeNative]::GetForegroundWindow() }
    if (-not $FixtureTarget) {
    $result.foreground_hwnd = $hwnd.ToInt64()
    [uint32]$processId = 0
    $threadId = [ProbeNative]::GetWindowThreadProcessId($hwnd, [ref]$processId)
    $result.foreground_pid = $processId
    $gui = New-Object ProbeNative+GUI
    $gui.size = [Runtime.InteropServices.Marshal]::SizeOf($gui)
    if ([ProbeNative]::GetGUIThreadInfo($threadId, [ref]$gui)) { $result.focus_hwnd = $gui.focus.ToInt64() }
    $handle = [ProbeNative]::OpenProcess(0x1000, $false, $processId)
    if ($handle -ne [IntPtr]::Zero) {
        try {
            [long]$creation = 0; [long]$exit = 0; [long]$kernel = 0; [long]$user = 0
            if ([ProbeNative]::GetProcessTimes($handle, [ref]$creation, [ref]$exit, [ref]$kernel, [ref]$user)) { $result.process_creation_identity = $creation }
        } finally { [void][ProbeNative]::CloseHandle($handle) }
    }
    }
    try {
        if ($FixtureTarget) { $element = [System.Windows.Automation.AutomationElement]::FromHandle($hwnd) }
        else { $element = [System.Windows.Automation.AutomationElement]::FocusedElement }
        if ($null -ne $element) {
            # Never access names, titles, AutomationId, URLs or ValuePattern.Value.
            try { $result.uia_pid = $element.GetCurrentPropertyValue([System.Windows.Automation.AutomationElement]::ProcessIdProperty, $true) } catch {}
            try { $result.uia_native_hwnd = $element.GetCurrentPropertyValue([System.Windows.Automation.AutomationElement]::NativeWindowHandleProperty, $true) } catch {}
            try { $result.runtime_id = @($element.GetRuntimeId()) } catch {}
            try { $result.control_type = $element.Current.ControlType.Id } catch {}
            $text = $null; $value = $null; $text2 = $null
            try { $result.text_pattern = $element.TryGetCurrentPattern([System.Windows.Automation.TextPattern]::Pattern, [ref]$text) } catch {}
            try { $result.value_pattern = $element.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$value) } catch {}
            try { $result.text_pattern2 = $element.TryGetCurrentPattern([System.Windows.Automation.TextPattern2]::Pattern, [ref]$text2) } catch {}
            Read-ProbeTextObservation $element $text $result
            $ancestor = $element
            $count = 0
            while ($count -lt 8) {
                try { $ancestor = [System.Windows.Automation.TreeWalker]::ControlViewWalker.GetParent($ancestor) } catch { break }
                if ($null -eq $ancestor) { break }
                $count++
            }
            $result.ancestors_inspected = $count
        }
    } catch {}
    if (-not $FixtureTarget) {
        try {
            $after = [ProbeNative]::GetForegroundWindow()
            $result.foreground_hwnd_after = $after.ToInt64()
            [uint32]$afterPid = 0
            $afterThread = [ProbeNative]::GetWindowThreadProcessId($after, [ref]$afterPid)
            $result.foreground_pid_after = $afterPid
            $afterGui = New-Object ProbeNative+GUI
            $afterGui.size = [Runtime.InteropServices.Marshal]::SizeOf($afterGui)
            if ([ProbeNative]::GetGUIThreadInfo($afterThread, [ref]$afterGui)) { $result.focus_hwnd_after = $afterGui.focus.ToInt64() }
            $afterElement = [System.Windows.Automation.AutomationElement]::FocusedElement
            if ($null -ne $afterElement) { $result.runtime_id_after = @($afterElement.GetRuntimeId()) }
        } catch {}
    }
    if ($null -ne $fixture) { $fixture.Dispose() }
    $result | ConvertTo-Json -Compress -Depth 4
} catch { exit 1 }

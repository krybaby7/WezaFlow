param([string]$Mode, [string]$Module)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
. $Module
$script:textCalls = 0
$script:selectionCalls = 0
$script:nativeLimit = $null
$element = New-Object PSObject
$element | Add-Member NoteProperty Mode $Mode
$element | Add-Member ScriptMethod GetCurrentPropertyValue {
    param($property, $ignoreDefault)
    if ($ignoreDefault -ne $true) { throw 'default values forbidden' }
    if ($this.Mode -eq 'inaccessible') { throw 'PRIVATE exception' }
    if ($this.Mode -eq 'unknown') { return [System.Windows.Automation.AutomationElement]::NotSupported }
    if ($this.Mode -eq 'null') { return $null }
    return ($this.Mode -eq 'password')
}
$range = New-Object PSObject
$range | Add-Member ScriptMethod GetText { param($limit) $script:textCalls++; $script:nativeLimit = $limit; return 'synthetic' }
$text = New-Object PSObject
$text | Add-Member NoteProperty DocumentRange $range
$text | Add-Member ScriptMethod GetSelection { $script:selectionCalls++; return @() }
$result = @{}
Read-ProbeTextObservation $element $text $result
if ($Mode -eq 'readable') {
    if ($script:textCalls -ne 1 -or $script:selectionCalls -ne 1 -or $script:nativeLimit -ne 2000) { throw 'bounded observation assertion' }
} elseif ($script:textCalls -ne 0 -or $script:selectionCalls -ne 0) { throw 'suppression assertion' }
$result | ConvertTo-Json -Compress

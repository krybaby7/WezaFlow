# Shared by the native diagnostic and synthetic provider tests. No UIA defaults.
function Read-ProbeTextObservation($element, $text, $result) {
    try {
        $password = $element.GetCurrentPropertyValue([System.Windows.Automation.AutomationElement]::IsPasswordProperty, $true)
        if ($password -is [bool]) { $result.password = $password }
    } catch {}
    try {
        $readOnly = $element.GetCurrentPropertyValue([System.Windows.Automation.ValuePattern]::IsReadOnlyProperty, $true)
        if ($readOnly -is [bool]) { $result.read_only = $readOnly }
    } catch {}
    # Missing/NotSupported/null/error is unknown. Only an actual Boolean false permits observations.
    if ($result.ContainsKey('password') -and $result.password -is [bool] -and $result.password -eq $false -and $null -ne $text) {
        try {
            $observed = $text.DocumentRange.GetText(2000)
            $result.inspected_characters = $observed.Length
            $observed = $null
        } catch {}
        try {
            $selection = $text.GetSelection()
            $result.selection_observable = $true
            $result.selection_count = $selection.Length
            $selection = $null
        } catch {}
    }
}

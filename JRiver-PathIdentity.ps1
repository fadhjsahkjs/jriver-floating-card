# Pure path comparison helpers; sourcing this file does not contact JRiver.
function ConvertTo-JRiverComparablePath {
    param([Parameter(Mandatory)][string]$Filename, [string]$InstallDrive = '')
    $value = $Filename.Replace('/', '\')
    $prefix = '(Install Drive):'
    if ($value.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        if ($InstallDrive -notmatch '^[A-Za-z]:$') { throw 'Cannot resolve the JRiver installation drive.' }
        $value = $InstallDrive + $value.Substring($prefix.Length)
    }
    if ($value -notmatch '^[A-Za-z]:\\' -and $value -notmatch '^\\\\[^\\]+\\[^\\]+') {
        throw 'JRiver returned a non-absolute audio file path.'
    }
    return [IO.Path]::GetFullPath($value)
}

function Test-JRiverFilenameIdentity {
    param([Parameter(Mandatory)][string]$Expected, [Parameter(Mandatory)][string]$Actual,
          [string]$InstallDrive = '')
    if ([string]::Equals($Expected, $Actual, [StringComparison]::OrdinalIgnoreCase)) { return $true }
    $left = ConvertTo-JRiverComparablePath -Filename $Expected -InstallDrive $InstallDrive
    $right = ConvertTo-JRiverComparablePath -Filename $Actual -InstallDrive $InstallDrive
    return [string]::Equals($left, $right, [StringComparison]::OrdinalIgnoreCase)
}

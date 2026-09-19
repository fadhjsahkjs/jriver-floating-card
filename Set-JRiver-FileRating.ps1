[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateRange(0,2147483647)][int]$FileKey,
    [Parameter(Mandatory)][ValidateRange(0,5)][int]$Rating,
    [Parameter(Mandatory)][string]$ExpectedFilename,
    [ValidateRange(-1,5)][int]$ExpectedRating = -1
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
try {
    # Attach only to an already running JRiver. Never launch another library instance.
    $jriverApp = [Runtime.InteropServices.Marshal]::GetActiveObject('MediaJukebox Application')
    $jriverFile = $jriverApp.GetFileByKey($FileKey)
    if ($null -eq $jriverFile -or $jriverFile.GetKey() -ne $FileKey) { throw 'JRiver track identity mismatch.' }
    $get = [Reflection.BindingFlags]::GetProperty
    $filename = [string]$jriverFile.GetType().InvokeMember('Filename',$get,$null,$jriverFile,@())
    if (-not [string]::Equals($filename,$ExpectedFilename,[StringComparison]::OrdinalIgnoreCase)) {
        throw 'JRiver library identity mismatch. Rating was not changed.'
    }
    $before = [int]$jriverFile.GetType().InvokeMember('Rating',$get,$null,$jriverFile,@())
    if ($ExpectedRating -ge 0 -and $before -ne $ExpectedRating) {
        throw 'The rating has changed elsewhere. Refresh before trying again.'
    }
    $jriverFile.GetType().InvokeMember('Rating',[Reflection.BindingFlags]::SetProperty,$null,$jriverFile,@($Rating)) | Out-Null
    $after = [int]$jriverApp.GetFileByKey($FileKey).GetType().InvokeMember('Rating',$get,$null,$jriverApp.GetFileByKey($FileKey),@())
    if ($after -ne $Rating) { throw 'JRiver did not retain the requested rating.' }
    @{ key=$FileKey; before=$before; rating=$after } | ConvertTo-Json -Compress
} catch {
    @{ error=$_.Exception.Message } | ConvertTo-Json -Compress
    exit 1
}

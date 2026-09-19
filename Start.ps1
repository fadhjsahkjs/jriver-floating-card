$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
if (!(Test-Path -LiteralPath $python)) { throw 'Run the README installation commands first to create .venv.' }
Start-Process -FilePath $python -ArgumentList ('"' + (Join-Path $PSScriptRoot 'floating_player.pyw') + '"') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden

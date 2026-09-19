"""Exercise Windows PowerShell 5.1 path guards without any COM or rating writes."""
import base64
import json
import os
from pathlib import Path
import subprocess
import unittest


@unittest.skipUnless(os.name == 'nt', 'JRiver COM rating path is Windows-only')
class RatingPathTests(unittest.TestCase):
    def test_install_drive_and_absolute_recording_identity(self):
        helper = str(Path(__file__).with_name('JRiver-PathIdentity.ps1')).replace("'", "''")
        script = r'''
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
. 'HELPER'
$checks = @(
    (Test-JRiverFilenameIdentity '(Install Drive):\CloudMusic\Song.m4a' 'C:\CloudMusic\Song.m4a' 'C:'),
    (Test-JRiverFilenameIdentity '(Install Drive):\\CloudMusic\\Song.m4a' 'C:/CloudMusic/Song.m4a' 'C:'),
    (Test-JRiverFilenameIdentity '(Install Drive):\CloudMusic\Song.m4a' 'D:\CloudMusic\Song.m4a' 'D:'),
    (-not (Test-JRiverFilenameIdentity '(Install Drive):\CloudMusic\Song.m4a' 'D:\CloudMusic\Song.m4a' 'C:')),
    (-not (Test-JRiverFilenameIdentity '(Install Drive):\CloudMusic\Song.m4a' 'C:\CloudMusic\Live.m4a' 'C:')),
    (Test-JRiverFilenameIdentity '\\nas\music\Song.flac' '//nas/music/Song.flac'),
    (-not (Test-JRiverFilenameIdentity '\\nas\music\Song.flac' '//other/music/Song.flac'))
)
$missingDriveRejected = $false
try { ConvertTo-JRiverComparablePath '(Install Drive):\CloudMusic\Song.m4a' | Out-Null } catch { $missingDriveRejected = $true }
$relativeRejected = $false
try { ConvertTo-JRiverComparablePath 'C:CloudMusic\Song.m4a' | Out-Null } catch { $relativeRejected = $true }
@{checks=$checks; missing_drive_rejected=$missingDriveRejected;relative_rejected=$relativeRejected} | ConvertTo-Json -Compress
'''.replace('HELPER', helper)
        powershell = Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
        done = subprocess.run([str(powershell), '-NoProfile', '-NonInteractive', '-EncodedCommand',
                               base64.b64encode(script.encode('utf-16le')).decode()],
                              capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(done.returncode, 0, done.stderr.decode('utf-8', errors='replace'))
        result = json.loads(done.stdout.decode('utf-8-sig'))
        self.assertEqual(result['checks'], [True]*7)
        self.assertTrue(result['missing_drive_rejected'])
        self.assertTrue(result['relative_rejected'])


if __name__ == '__main__':unittest.main()

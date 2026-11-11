# Launch the Jarvis browser helper (127.0.0.1:47822) with the venv's base python.
#   .\start_helper.ps1              start the helper (Ctrl+C to stop)
#   .\start_helper.ps1 -CopyToken   copy the shared token to the clipboard (to paste into the extension options) and exit
param([switch]$CopyToken)

# Base interpreter from the venv's pyvenv.cfg (Smart App Control blocks .venv\Scripts\python.exe).
$cfg = Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) '.venv\pyvenv.cfg'
$home_ = ((Get-Content $cfg | Where-Object { $_ -match '^home\s*=' }) -replace '^home\s*=\s*', '').Trim()
$py = Join-Path $home_ 'python.exe'
if (-not (Test-Path $py)) { Write-Error "Base Python not found: $py"; exit 1 }
$helper = Join-Path $PSScriptRoot 'jarvis_browser_helper.py'

if ($CopyToken) {
    $tok = (& $py $helper --print-token).Trim()
    Set-Clipboard -Value $tok
    Write-Host "Token copied to clipboard ($($tok.Length) chars). Paste it in the extension options page."
    exit 0
}

& $py $helper

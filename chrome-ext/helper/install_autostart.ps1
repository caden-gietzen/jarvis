# Start the Jarvis browser helper automatically at login (per-user, no admin needed).
#   .\install_autostart.ps1              create "Jarvis Browser Helper.lnk" in the Startup folder (shell:startup)
#   .\install_autostart.ps1 -Uninstall   remove that shortcut
# The shortcut runs start_helper.ps1 in a hidden PowerShell window. It does not start the helper now.
param([switch]$Uninstall)

$startup = [Environment]::GetFolderPath('Startup')
$lnk = Join-Path $startup 'Jarvis Browser Helper.lnk'

if ($Uninstall) {
    if (Test-Path $lnk) { Remove-Item $lnk -Force; Write-Host "Removed $lnk" }
    else { Write-Host "Nothing to remove ($lnk does not exist)." }
    exit 0
}

$script = Join-Path $PSScriptRoot 'start_helper.ps1'
if (-not (Test-Path $script)) { Write-Error "Not found: $script"; exit 1 }
$ps = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'

$sh = New-Object -ComObject WScript.Shell
$s = $sh.CreateShortcut($lnk)
$s.TargetPath = $ps
$s.Arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`""
$s.WorkingDirectory = $PSScriptRoot
$s.WindowStyle = 7   # minimized
$s.Description = 'Jarvis browser helper (127.0.0.1:47822)'
$s.Save()
Write-Host "Created $lnk"
Write-Host "The helper will start at your next login. Remove it with: install_autostart.ps1 -Uninstall"

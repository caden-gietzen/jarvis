# Tile two app windows side by side on one monitor.
# Usage: powershell -File tile.ps1 [-Left chrome] [-Right code] [-Monitor extended|primary]
param(
    [string]$Left = 'chrome',
    [string]$Right = 'code',
    [ValidateSet('extended', 'primary')][string]$Monitor = 'extended'
)
Add-Type @'
using System; using System.Runtime.InteropServices;
public class W {
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
  [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr a, int x, int y, int w, int ht, uint f);
}
'@
[W]::SetProcessDPIAware() | Out-Null
Add-Type -AssemblyName System.Windows.Forms
$screens = [System.Windows.Forms.Screen]::AllScreens
if ($Monitor -eq 'extended') { $s = $screens | Where-Object { -not $_.Primary } | Select-Object -First 1 }
if (-not $s) { $s = $screens | Where-Object { $_.Primary } | Select-Object -First 1 }
$a = $s.WorkingArea
$half = [int]($a.Width / 2)
function Place($name, $x) {
    $p = Get-Process $name -ErrorAction SilentlyContinue | Where-Object MainWindowHandle -ne 0 | Select-Object -First 1
    if (-not $p) { Write-Output "No window found for '$name'"; return }
    [W]::ShowWindow($p.MainWindowHandle, 9) | Out-Null   # restore if maximized/minimized
    [W]::SetWindowPos($p.MainWindowHandle, [IntPtr]::Zero, $x, $a.Y, $half, $a.Height, 0x0040) | Out-Null
    Write-Output "Placed $name ($($p.MainWindowTitle))"
}
Place $Left $a.X
Place $Right ($a.X + $half)

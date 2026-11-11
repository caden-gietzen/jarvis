# Restart Jarvis: open a new window running Jarvis (--resume), then close the old one.
# Usage: restart_jarvis.ps1 -OldHwnd <hwnd> -OldPid <old jarvis pid> -OldShellPid <hosting shell pid>
# Everything is appended to logs\restart.log. Parameters default to the real behaviour;
# scripts\test_restart.py substitutes a fake command/match/log.
param(
    [Int64]$OldHwnd = 0,
    [int]$OldPid = 0,
    [int]$OldShellPid = 0,
    [string]$Command = '.\jarvis.cmd run --resume',
    # Command line of the Jarvis python: "python.exe bootstrap.py run ..." (jarvis.cmd) or "... -m jarvis run ..." (old style).
    [string]$Match = '(jarvis|bootstrap\.py"?)\s+run',
    [string]$LogFile = '',
    [string]$WorkDir = '',
    [int]$TimeoutSec = 30
)

$repo = Split-Path -Parent $PSScriptRoot
if (-not $WorkDir) { $WorkDir = $repo }
if (-not $LogFile) { $LogFile = Join-Path $repo 'logs\restart.log' }
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $LogFile) | Out-Null

function Log($msg) {
    $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff'
    Add-Content -Path $LogFile -Value "$ts [ps $PID] $msg" -Encoding UTF8
}

Add-Type -Namespace Win -Name Native -MemberDefinition @'
[DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h, uint msg, IntPtr w, IntPtr l);
[DllImport("user32.dll")] public static extern bool IsWindow(IntPtr h);
[DllImport("kernel32.dll")] public static extern IntPtr GetConsoleWindow();
[DllImport("kernel32.dll")] public static extern IntPtr OpenProcess(uint access, bool inherit, int pid);
[DllImport("kernel32.dll")] public static extern bool TerminateProcess(IntPtr h, uint code);
[DllImport("kernel32.dll")] public static extern bool CloseHandle(IntPtr h);
'@

# Terminate with exit code 0 so Windows Terminal's default closeOnExit=graceful closes the tab.
function Stop-Clean([int]$p) {
    $h = [Win.Native]::OpenProcess(0x0001, $false, $p)   # PROCESS_TERMINATE
    if ($h -eq [IntPtr]::Zero) { Log "  OpenProcess($p) failed (gone or denied)"; return }
    $ok = [Win.Native]::TerminateProcess($h, 0)
    [Win.Native]::CloseHandle($h) | Out-Null
    Log "  terminated pid $p exit=0 ok=$ok"
}

function Get-Descendants([int]$root, $all) {
    $out = New-Object System.Collections.ArrayList
    $queue = New-Object System.Collections.Queue
    $queue.Enqueue($root)
    while ($queue.Count -gt 0) {
        $cur = $queue.Dequeue()
        foreach ($c in ($all | Where-Object { $_.ParentProcessId -eq $cur -and $_.ProcessId -ne $cur })) {
            if (-not $out.Contains([int]$c.ProcessId)) { [void]$out.Add([int]$c.ProcessId); $queue.Enqueue([int]$c.ProcessId) }
        }
    }
    return $out.ToArray()
}

try {
    $own = [Win.Native]::GetConsoleWindow().ToInt64()
    Log "=== restart script start: OldHwnd=$OldHwnd OldPid=$OldPid OldShellPid=$OldShellPid Command='$Command' Match='$Match' WorkDir='$WorkDir' Timeout=$TimeoutSec ownPid=$PID ownConsoleHwnd=$own"

    # The old tree: shell + its descendants + the old pid (and its ancestors up to the shell).
    $all = @(Get-CimInstance Win32_Process)
    $oldTree = New-Object System.Collections.Generic.HashSet[int]
    if ($OldShellPid -ne 0) {
        [void]$oldTree.Add($OldShellPid)
        foreach ($d in @(Get-Descendants $OldShellPid $all)) { [void]$oldTree.Add([int]$d) }
    }
    if ($OldPid -ne 0) {
        [void]$oldTree.Add($OldPid)
        $cur = $OldPid
        while ($true) {
            $p = $all | Where-Object { $_.ProcessId -eq $cur } | Select-Object -First 1
            if (-not $p -or $p.ParentProcessId -eq 0) { break }
            $cur = [int]$p.ParentProcessId
            if ($cur -eq $OldShellPid -or -not ($all | Where-Object { $_.ProcessId -eq $cur })) { break }
            [void]$oldTree.Add($cur)
        }
    }
    Log "old tree pids: $(($oldTree | Sort-Object) -join ',')"
    if ($oldTree.Contains([int]$PID)) { Log "ABORT: this script is inside the old tree; refusing to continue"; exit 3 }

    # 1. Start the new Jarvis in its own PowerShell window.
    $new = Start-Process powershell -WorkingDirectory $WorkDir -PassThru -ArgumentList '-NoExit', '-Command', $Command
    Log "started new window: powershell pid=$($new.Id) command='$Command'"

    # 2. Wait for a new Jarvis process (matching command line, not part of the old tree).
    $found = $null
    for ($i = 0; $i -lt $TimeoutSec -and -not $found; $i++) {
        Start-Sleep -Seconds 1
        $found = Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
            Where-Object { $_.CommandLine -match $Match -and -not $oldTree.Contains([int]$_.ProcessId) } |
            Select-Object -First 1
    }
    if (-not $found) {
        Log "RESULT: new Jarvis not detected after ${TimeoutSec}s; leaving old window alone. exit 1"
        exit 1
    }
    Log "new Jarvis detected: pid=$($found.ProcessId) parent=$($found.ParentProcessId) cmd=$($found.CommandLine)"

    # 3. Gentle attempt: WM_CLOSE to the old console window.
    if ($OldHwnd -ne 0 -and $OldHwnd -ne $own) {
        $isWin = [Win.Native]::IsWindow([IntPtr]$OldHwnd)
        Log "IsWindow($OldHwnd)=$isWin"
        if ($isWin) {
            $pm = [Win.Native]::PostMessage([IntPtr]$OldHwnd, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero)
            Log "PostMessage WM_CLOSE -> $pm"
            Start-Sleep -Seconds 2
        }
    } else { Log "skipping WM_CLOSE (hwnd=$OldHwnd own=$own)" }

    # 4. Terminate the old shell's process tree (children first, shell last), exit code 0.
    $all = @(Get-CimInstance Win32_Process)
    $victims = @()
    if ($OldShellPid -ne 0 -and ($all | Where-Object { $_.ProcessId -eq $OldShellPid })) {
        $victims = @(Get-Descendants $OldShellPid $all) + $OldShellPid
    } elseif ($OldPid -ne 0 -and ($all | Where-Object { $_.ProcessId -eq $OldPid })) {
        $victims = @(Get-Descendants $OldPid $all) + $OldPid
    }
    # Safety: never kill ourselves, the new window, or the new Jarvis. Leaf first.
    $victims = @($victims | Where-Object { $_ -ne $PID -and $_ -ne $new.Id -and $_ -ne [int]$found.ProcessId })
    [array]::Reverse($victims)
    # Reverse puts the shell first; keep shell last by moving it to the end.
    if ($OldShellPid -ne 0) { $victims = @($victims | Where-Object { $_ -ne $OldShellPid }) + @($victims | Where-Object { $_ -eq $OldShellPid }) }
    Log "terminating old tree: $($victims -join ',')"
    foreach ($v in $victims) { Stop-Clean $v }
    if ($OldPid -ne 0 -and (Get-Process -Id $OldPid -ErrorAction SilentlyContinue)) {
        Log "old pid $OldPid still alive; taskkill /T /F"
        Log ((taskkill /T /F /PID $OldPid) -join ' ')
    }
    Start-Sleep -Seconds 1
    $left = @($victims | Where-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue })
    Log "RESULT: done. old pids still alive: $($left -join ',')"
} catch {
    Log "ERROR: $($_ | Out-String)"
    exit 2
}

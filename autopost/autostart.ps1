# Aion Space autostart: the daily post preparation on this PC + its Windows Task Scheduler entries.
#
#   .\autostart.ps1 install      create/update the scheduled tasks (wakes the PC from sleep)
#   .\autostart.ps1 uninstall    remove the tasks
#   .\autostart.ps1 status       task state, next run, last result, tail of the last log
#   .\autostart.ps1 run          run the chain right now (what the task does)
#   .\autostart.ps1 test         send a test message to the Telegram bot
#
# The chain (aion_daily.py): Claude (claude -p) searches fresh topics -> posts -> the Telegram bot with photos.
# It is safe to start twice a day: when today's posts are already sent, the second start does nothing.
# Settings: settings.json next to this file. Source is kept ASCII-only on purpose (Windows PowerShell 5.1).

param(
    [Parameter(Position = 0)][ValidateSet('install', 'uninstall', 'status', 'run', 'test')][string]$Action = 'status',
    [switch]$NoSleep,   # run: never put the PC to sleep afterwards
    [switch]$Force      # run: send even if today's posts were already sent
)

$ErrorActionPreference = 'Stop'
$TaskName     = 'Aion Space Daily'
$FallbackName = 'Aion Space Fallback'
$Here   = $PSScriptRoot
$Script = Join-Path $Here 'aion_daily.py'
$LogDir = Join-Path $Here 'logs'

function Get-Settings {
    Get-Content -Raw -Encoding UTF8 (Join-Path $Here 'settings.json') | ConvertFrom-Json
}

function Get-Python($s) {
    if ($s.python) { return $s.python }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $cmd) { throw 'python not found in PATH: set "python" in settings.json' }
    return $cmd.Source
}

Add-Type -Namespace AionNative -Name Idle -MemberDefinition @'
[StructLayout(LayoutKind.Sequential)] public struct LASTINPUTINFO { public uint cbSize; public uint dwTime; }
[DllImport("user32.dll")] public static extern bool GetLastInputInfo(ref LASTINPUTINFO plii);
public static double IdleMinutes() {
    LASTINPUTINFO i = new LASTINPUTINFO(); i.cbSize = (uint)Marshal.SizeOf(typeof(LASTINPUTINFO));
    if (!GetLastInputInfo(ref i)) return 0;
    return (double)((uint)Environment.TickCount - i.dwTime) / 60000.0;
}
'@

# After the chain: put the PC back to sleep, but only if nobody has used it for a while.
function Invoke-SleepIfIdle($s) {
    if ($NoSleep -or -not $s.sleep_after) { return }
    $idle = [AionNative.Idle]::IdleMinutes()
    if ($idle -lt [double]$s.sleep_only_if_idle_minutes) {
        Write-Host ('not sleeping: the PC is in use (idle {0:N1} min)' -f $idle)
        return
    }
    Write-Host ('going to sleep (idle {0:N1} min)' -f $idle)
    Add-Type -AssemblyName System.Windows.Forms
    Start-Sleep -Seconds 5
    [void][System.Windows.Forms.Application]::SetSuspendState([System.Windows.Forms.PowerState]::Suspend, $false, $false)
}

function Invoke-Run {
    $s  = Get-Settings
    $py = Get-Python $s
    $env:PYTHONIOENCODING = 'utf-8'
    $extra = @()
    if ($Force) { $extra += '--force' }
    & $py $Script run @extra
    $rc = $LASTEXITCODE
    # 10 = already sent today, 11 = another run is in progress: nothing was done, do not touch the sleep state
    if ($rc -ne 10 -and $rc -ne 11) { Invoke-SleepIfIdle $s }
    return $rc
}

function Install-Task {
    $s = Get-Settings
    $py = Get-Python $s
    $psExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $arg = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" run' -f (Join-Path $Here 'autostart.ps1')
    $runAt = [datetime]::ParseExact($s.run_time, 'HH:mm', $null)
    $hours = if ($s.task_hours) { [int]$s.task_hours } else { 2 }
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

    # main task: at run_time, wakes the PC from sleep
    $action  = New-ScheduledTaskAction -Execute $psExe -Argument $arg -WorkingDirectory $Here
    $trigger = New-ScheduledTaskTrigger -Daily -At $runAt
    $set = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Hours $hours) -MultipleInstances IgnoreNew
    if ($s.wake_pc) { $set.WakeToRun = $true }
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $set -Principal $principal `
        -Description 'Aion Space: morning posts -> Telegram bot' -Force | Out-Null
    Write-Host "task '$TaskName' registered: daily at $($s.run_time), wake PC: $($s.wake_pc), python: $py"

    # fallback task: a few minutes later, only if the PC is on; does nothing when the posts are already sent
    $after = if ($s.fallback_after_minutes) { [int]$s.fallback_after_minutes } else { 15 }
    $fTrigger = New-ScheduledTaskTrigger -Daily -At ($runAt.AddMinutes($after))
    $fSet = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Hours $hours) -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $FallbackName -Action $action -Trigger $fTrigger -Settings $fSet -Principal $principal `
        -Description 'Aion Space: second start if the first one was missed' -Force | Out-Null
    Write-Host ("task '$FallbackName' registered: daily at {0:HH:mm}" -f $runAt.AddMinutes($after))

    if ($s.wake_pc) {
        # "Allow wake timers": 1 = enabled (the default 'important only' would ignore the task's wake request)
        $q = powercfg /setacvalueindex SCHEME_CURRENT SUB_SLEEP BD3B718A-0680-4D9D-8AB2-E1D2B4AC806D 1 2>&1
        if ($LASTEXITCODE -eq 0) {
            powercfg /setactive SCHEME_CURRENT | Out-Null
            Write-Host 'wake timers enabled on AC power (current power plan)'
        } else {
            Write-Warning ("could not enable wake timers: {0}`nRun this once in an elevated PowerShell:`n  powercfg /setacvalueindex SCHEME_CURRENT SUB_SLEEP BD3B718A-0680-4D9D-8AB2-E1D2B4AC806D 1; powercfg /setactive SCHEME_CURRENT" -f ($q -join ' '))
        }
    }
}

function Show-Status {
    foreach ($n in @($TaskName, $FallbackName)) {
        $t = Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue
        if (-not $t) { Write-Host "task '$n' is not installed: run  .\autostart.ps1 install"; continue }
        $i = Get-ScheduledTaskInfo -TaskName $n
        "{0}: {1}, next {2}, last {3}, result {4}" -f $n, $t.State, $i.NextRunTime, $i.LastRunTime, $i.LastTaskResult
    }
    $last = Get-ChildItem $LogDir -Filter 'daily_*.log' -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($last) { "--- tail of $($last.Name)"; Get-Content -Tail 15 -Encoding UTF8 $last.FullName }
}

switch ($Action) {
    'install'   { Install-Task }
    'uninstall' {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $FallbackName -Confirm:$false -ErrorAction SilentlyContinue
        Write-Host 'tasks removed'
    }
    'status'    { Show-Status }
    'run'       { exit (Invoke-Run) }
    'test'      {
        $s = Get-Settings
        $env:PYTHONIOENCODING = 'utf-8'
        & (Get-Python $s) $Script test-telegram
        exit $LASTEXITCODE
    }
}

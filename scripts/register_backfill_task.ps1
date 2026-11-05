# Registers the daily bus_daily backfill in Windows Task Scheduler.
#   powershell -ExecutionPolicy Bypass -File scripts\register_backfill_task.ps1
# Remove it with: Unregister-ScheduledTask -TaskName JejuBusBackfill -Confirm:$false

$root = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path (Split-Path -Parent (Get-Command python).Source) 'pythonw.exe'

$action = New-ScheduledTaskAction -Execute $pythonw -Argument 'scripts\backfill_bus.py' -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Daily -At '2026-11-06 09:00'
$trigger.EndBoundary = '2026-11-12T00:00:00'

# StartWhenAvailable: a laptop that was off at 09:00 starts the run when it comes back on.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 15) `
    -DeleteExpiredTaskAfter (New-TimeSpan -Days 1)

Register-ScheduledTask -TaskName 'JejuBusBackfill' -Action $action -Trigger $trigger -Settings $settings -Force `
    -Description 'bus_daily backfill for the Jeju agent. See scripts/backfill_bus.py' | Out-Null
Get-ScheduledTaskInfo -TaskName 'JejuBusBackfill' | Select-Object TaskName, NextRunTime

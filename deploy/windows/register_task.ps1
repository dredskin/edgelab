# Registers the daily EdgeLab task in Windows Task Scheduler for the current user.
#   powershell -ExecutionPolicy Bypass -File deploy\windows\register_task.ps1 [-Time 18:30] [-Execute]
# Times are local (Eastern, after the US close). The task runs only while you are logged on, and a run
# missed while the PC was off starts when it next can.
param([string]$Time = "18:30", [switch]$Execute)

$missing = @("EDGELAB_MAIL_TO", "EDGELAB_MAIL_FROM", "EDGELAB_SMTP_USER", "EDGELAB_SMTP_PASSWORD") |
    Where-Object { -not [Environment]::GetEnvironmentVariable($_, "User") }
if ($missing) { throw "Set these User environment variables first (names only): $($missing -join ', ')" }

$script = Join-Path $PSScriptRoot "run_daily.ps1"
$arg = "-NoProfile -ExecutionPolicy Bypass -File `"$script`"" + $(if ($Execute) { " -Execute" } else { "" })
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arg -WorkingDirectory (Resolve-Path (Join-Path $PSScriptRoot "..\.."))
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "EdgeLab-Daily" -Action $action -Trigger $trigger -Settings $settings -Description "EdgeLab paper-trading session and e-mail report" -Force | Out-Null
Write-Output "Registered EdgeLab-Daily at $Time (local). Test it now:  Start-ScheduledTask -TaskName EdgeLab-Daily"

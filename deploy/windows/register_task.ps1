# Registers the daily EdgeLab task in Windows Task Scheduler.
#   powershell -ExecutionPolicy Bypass -File deploy\windows\register_task.ps1 [-Time 18:30] [-Publish] [-Execute] [-Background] [-Wake]
#
#   -Background  run whether or not you are logged on. Uses Windows' "S4U" logon, so your Windows password is
#                NOT stored. Needs an ADMINISTRATOR PowerShell window to register. The task can reach the internet
#                but not Windows-authenticated network shares (EdgeLab needs none).
#   -Wake        wake the PC from sleep to run (a PC that is shut down cannot be woken).
#   -Publish     publish the hosted dashboard after a clean session.   -Execute  send PAPER orders.
# Times are local. A run missed while the PC was off or asleep starts when it next can.
param([string]$Time = "18:30", [switch]$Execute, [switch]$Publish, [switch]$Background, [switch]$Wake)

$missing = @("EDGELAB_MAIL_TO", "EDGELAB_MAIL_FROM", "EDGELAB_SMTP_USER", "EDGELAB_SMTP_PASSWORD") |
    Where-Object { -not [Environment]::GetEnvironmentVariable($_, "User") }
if ($missing) { throw "Set these User environment variables first (names only): $($missing -join ', ')" }

if ($Background) {
    $admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $admin) { throw "-Background needs an administrator PowerShell window (right-click PowerShell > Run as administrator)." }
}

$repo = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$script = Join-Path $PSScriptRoot "run_daily.ps1"
$arg = "-NoProfile -ExecutionPolicy Bypass -File `"$script`"" + $(if ($Execute) { " -Execute" } else { "" }) + $(if ($Publish) { " -Publish" } else { "" })
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arg -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2) -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -WakeToRun:$Wake
$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$logon = if ($Background) { "S4U" } else { "Interactive" }
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType $logon -RunLevel Limited

Register-ScheduledTask -TaskName "EdgeLab-Daily" -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description "EdgeLab paper-trading session and e-mail report" -Force | Out-Null
$mode = if ($Background) { "whether or not you are logged on" } else { "only while you are logged on" }
Write-Output "Registered EdgeLab-Daily at $Time (local), running $mode$(if ($Wake) { ', waking the PC from sleep' })."
Write-Output "Test it now:  Start-ScheduledTask -TaskName EdgeLab-Daily   (then read logs\daily-<today>.log)"

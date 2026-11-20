# Keeps IB Gateway running with the automatic PAPER login (IBC). Run once, in a normal PowerShell window:
#   powershell -ExecutionPolicy Bypass -File deploy\windows\register_ibgateway_task.ps1
# The task checks at Windows logon, every day at 18:00 (before EdgeLab's 18:30 run) and on Sundays at 09:00
# (after IB's weekly reset), and starts Gateway only if it is not already answering on port 4002.
# Gateway is a window on your desktop, so this task runs only while you are logged on.
$repo = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$python = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "No virtual environment at $python" }
$log = Join-Path $repo "logs\ibgateway.log"
$cmd = "-NoProfile -ExecutionPolicy Bypass -Command `"New-Item -ItemType Directory -Force '$repo\logs' | Out-Null; " +
       "'=== ' + (Get-Date -Format s) | Add-Content '$log'; & '$python' -X utf8 -m edgelab.cli ib-gateway start *>> '$log'`""
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $cmd -WorkingDirectory $repo
$triggers = @(
    (New-ScheduledTaskTrigger -AtLogOn -User ([Security.Principal.WindowsIdentity]::GetCurrent().Name)),
    (New-ScheduledTaskTrigger -Daily -At "18:00"),
    (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At "09:00")
)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName "EdgeLab-IBGateway" -Action $action -Trigger $triggers -Settings $settings -Principal $principal `
    -Description "Start IB Gateway with the saved paper login if it is not running" -Force | Out-Null
Write-Output "Registered EdgeLab-IBGateway (at logon, daily 18:00, Sunday 09:00). Test it now:  Start-ScheduledTask -TaskName EdgeLab-IBGateway"

# Runs one EdgeLab paper-trading session, then e-mails the report.
# Called by Task Scheduler (see register_task.ps1). Safe to run by hand:  powershell -File run_daily.ps1
# Orders go to the broker's PAPER account only when you pass -Execute. Live trading is not supported.
param([switch]$Execute)

$ErrorActionPreference = "Continue"
$repo = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$python = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "No virtual environment at $python. See deploy\windows\README.md step 2." }

$logDir = Join-Path $repo "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir ("daily-" + (Get-Date -Format "yyyy-MM-dd") + ".log")

Set-Location $repo
$cmdArgs = @("-X", "utf8", "-m", "edgelab.cli", "daily")
if ($Execute) { $cmdArgs += "--execute" }

"=== $(Get-Date -Format s) daily run ($(if ($Execute) {'PAPER orders'} else {'research only, no orders'})) ===" | Tee-Object -FilePath $log -Append
& $python @cmdArgs 2>&1 | Tee-Object -FilePath $log -Append
$code = $LASTEXITCODE

"=== $(Get-Date -Format s) e-mail report (run exit code $code) ===" | Tee-Object -FilePath $log -Append
& $python -X utf8 -m edgelab.cli email-report --run-exit $code 2>&1 | Tee-Object -FilePath $log -Append
if ($LASTEXITCODE -ne 0) { "e-mail step failed; the report is still in the log and the database" | Tee-Object -FilePath $log -Append }
exit $code

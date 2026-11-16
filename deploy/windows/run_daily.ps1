# Runs one EdgeLab paper-trading session, then e-mails the report.
# Called by Task Scheduler (see register_task.ps1). Safe to run by hand:  powershell -File run_daily.ps1
# Orders go to the broker's PAPER account only when you pass -Execute. Live trading is not supported.
param([switch]$Execute, [switch]$Publish)

$ErrorActionPreference = "Continue"

# A background (S4U) task may start without your User environment variables. Load them from the registry so
# the mail, Cloudflare and API settings are always visible; PATH is merged rather than replaced.
foreach ($entry in [Environment]::GetEnvironmentVariables("User").GetEnumerator()) {
    if ($entry.Key -eq "Path") {
        $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + $entry.Value
    } elseif (-not (Get-Item "env:$($entry.Key)" -ErrorAction SilentlyContinue)) {
        Set-Item -Path "env:$($entry.Key)" -Value $entry.Value
    }
}
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
# Publish the hosted snapshot only after a clean session (never publish a failed run's partial data)
if ($Publish -and $code -eq 0) {
    "=== $(Get-Date -Format s) publish hosted snapshot ===" | Tee-Object -FilePath $log -Append
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "publish_site.ps1") 2>&1 | Tee-Object -FilePath $log -Append
    if ($LASTEXITCODE -ne 0) { "publish step failed; see the messages above" | Tee-Object -FilePath $log -Append }
}
exit $code

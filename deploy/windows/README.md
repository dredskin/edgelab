# EdgeLab daily run on Windows

Paper trading only. The e-mail uses the same Gmail account and app-password method as TWIS.

## 1. Get the code
Clone or download `dredskin/edgelab` to a folder, for example `C:\Projects\edgelab`.

## 2. Install Python packages (PowerShell, from the repo folder)
    py -3.12 -m venv .venv
    .venv\Scripts\python.exe -m pip install -e ".[live]"

## 3. Set the e-mail settings (User environment variables, never in a file)
Create a Gmail **app password** for the sender account (Google Account > Security > App passwords). Then, in
PowerShell (replace the values, and press Enter on each line):

    [Environment]::SetEnvironmentVariable("EDGELAB_MAIL_TO", "jvbramwell@gmail.com", "User")
    [Environment]::SetEnvironmentVariable("EDGELAB_MAIL_FROM", "<sender Gmail address>", "User")
    [Environment]::SetEnvironmentVariable("EDGELAB_SMTP_USER", "<sender Gmail address>", "User")
    [Environment]::SetEnvironmentVariable("EDGELAB_SMTP_PASSWORD", "<app password>", "User")

Open a new PowerShell window afterwards so the variables load.

## 4. Stocks to cover and the SEC contact
- Replace the placeholder `AAPL` in `universe.txt` with 20 to 50 small caps to start.
- In `config.yaml`, set `sec.user_agent` to your name and a real contact address, as the SEC requires.
- Optional: `edgelab backfill --days 365` once, so the "first insider purchase" flags have history.

## 5. Test by hand, then schedule
    powershell -ExecutionPolicy Bypass -File deploy\windows\run_daily.ps1
    .venv\Scripts\python.exe -m edgelab.cli email-report --dry-run     # shows the e-mail without sending
    powershell -ExecutionPolicy Bypass -File deploy\windows\register_task.ps1 -Time 18:30
    Start-ScheduledTask -TaskName EdgeLab-Daily                        # run it once now

Add `-Execute` to both scripts only when you want orders sent to the broker's **paper** account. The broker
settings and the paper-only checks are in `config.yaml`.

Logs for each day are in `logs\daily-YYYY-MM-DD.log`. Nothing in this folder is committed (see `.gitignore`).

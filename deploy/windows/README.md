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

    [Environment]::SetEnvironmentVariable("EDGELAB_MAIL_TO", "<your report address>", "User")
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

## 6. Hosted copy on Cloudflare Pages behind Cloudflare Access (optional)

Your computer sends a read-only snapshot out to Cloudflare. Nothing listens for connections on your PC, and the
database is never uploaded. The hosted address is fixed (`https://<project>.pages.dev`), so no domain is needed.

**Order matters:** create the Access login first, so the site is never open to the internet.

1. **Create the Pages project** (no content yet). In PowerShell, from the repo folder, with `CLOUDFLARE_API_TOKEN`
   and `CLOUDFLARE_ACCOUNT_ID` set as User variables:

       npx --yes wrangler@4 pages project create edgelab-dashboard --production-branch main

2. **Put Access in front of it.** Cloudflare dashboard > Zero Trust > Access > Applications > Add an application >
   Self-hosted. Application domain: `edgelab-dashboard.pages.dev`. Add an **Allow** policy that lists only your own
   login email. Use one-time PIN login (or another identity provider you already use). Save.

3. **Set the hosted address** as a User variable: `EDGELAB_PAGES_URL = https://edgelab-dashboard.pages.dev`

4. **Publish once by hand** and read the last line. It must say the site is protected:

       powershell -ExecutionPolicy Bypass -File deploy\windows\publish_site.ps1

   If it says the site is reachable without a login, delete the deployment in the Pages dashboard and fix step 2.
   The script refuses to call a public site safe.

5. **Schedule it:** `register_task.ps1 -Time 18:30 -Publish`. Publishing happens only after a clean daily session.

What the hosted copy shows: the same summary, signals, trades, orders, log and data-source health as the local
dashboard, as of the last publish. It is paper trading data only. Anyone who could read it could see your paper
positions, so keep the Access policy limited to you.

Revoking access later: remove the Allow policy in Access, or delete the Pages project.

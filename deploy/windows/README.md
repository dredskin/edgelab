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
    powershell -ExecutionPolicy Bypass -File deploy\windows\register_task.ps1 -Time 18:30 -Publish -Background -Wake
    Start-ScheduledTask -TaskName EdgeLab-Daily                        # run it once now

`-Background` runs the task whether or not you are logged on (register it from an **administrator** PowerShell
window; your Windows password is not stored). `-Wake` wakes the PC from sleep for the run; a PC that is shut down
cannot run it. Without `-Background` the task runs only while you are logged on.

Add `-Execute` to both scripts only when you want orders sent to the broker's **paper** account. The broker
settings and the paper-only checks are in `config.yaml`.

Logs for each day are in `logs\daily-YYYY-MM-DD.log`. Nothing in this folder is committed (see `.gitignore`).

## 6. Hosted copy on Cloudflare Pages behind Cloudflare Access (optional)

Your computer sends a read-only snapshot out to Cloudflare. Nothing listens for connections on your PC, and the
database is never uploaded. The hosted address is fixed (`https://<project>.pages.dev`), so no domain is needed.

**Order matters:** create the Access login first, so the site is never open to the internet. The Access
application needs one destination `*` on `edgelab-dashboard.pages.dev`; remove any destination on the bare
hostname, since Cloudflare rejects it here.

1. **Create the Pages project** (no content yet). In PowerShell, from the repo folder, with `CLOUDFLARE_API_TOKEN`
   and `CLOUDFLARE_ACCOUNT_ID` set as User variables:

       npx --yes wrangler@4 pages project create edgelab-dashboard --production-branch main

2. **Put Access in front of it.** Cloudflare dashboard > Zero Trust > Access > Applications > Add an application >
   Self-hosted. Application domain: `edgelab-dashboard.pages.dev`. Add an **Allow** policy that lists only your own
   login email. Use one-time PIN login (or another identity provider you already use). Save.

3. **Where things go.** The real dashboard is published to `https://preview.edgelab-dashboard.pages.dev`, which Access covers through its `*` destination. The bare `https://edgelab-dashboard.pages.dev` cannot be put behind Access here, so every publish first puts an empty placeholder page there. (`EDGELAB_PAGES_URL` is no longer used.)

4. **Publish once by hand** and read the last line. It must say the site is protected:

       powershell -ExecutionPolicy Bypass -File deploy\windows\publish_site.ps1

   If it says the site is reachable without a login, delete the deployment in the Pages dashboard and fix step 2.
   Check the branch address (`preview.edgelab-dashboard.pages.dev`) in a private window as well.
   The script refuses to call a public site safe.

5. **Schedule it:** `register_task.ps1 -Time 18:30 -Publish`. Publishing happens only after a clean daily session.

What the hosted copy shows: the same summary, signals, trades, orders, log and data-source health as the local
dashboard, as of the last publish. It is paper trading data only. Anyone who could read it could see your paper
positions, so keep the Access policy limited to you.

Revoking access later: remove the Allow policy in Access, or delete the Pages project.

## 7. Paper orders through Interactive Brokers

EdgeLab sends orders only to an IB **paper** account. It refuses to connect unless BOTH the API port is a paper port
(7497 for TWS, 4002 for IB Gateway) AND the account id starts with `DU`.

1. **Install the IB connector** (once, from the repo folder):

       .venv\Scripts\python.exe -m pip install -e ".[live,ibkr]"

2. **Turn on the API in TWS or IB Gateway**, logged in to your **paper** account:
   Configure > Settings > API > Settings:
   - tick **Enable ActiveX and Socket Clients**
   - **Socket port**: 7497 (TWS paper) or 4002 (Gateway paper)
   - leave **Read-Only API** unticked (orders need it off)
   - tick **Allow connections from localhost only**
   If you use IB Gateway, change `broker.ibkr.port` in `config.yaml` to `4002`.

3. **Base currency**: set `broker.ibkr.base_currency` in `config.yaml` to your paper account's base currency
   (shown in TWS Account window, usually USD or CAD).

4. **Check the connection** with TWS/Gateway running:

       .venv\Scripts\python.exe -m edgelab.cli broker

   It must print an account id starting with `DU` and `status paper`.

5. **Turn on orders**: register the task again with `-Execute` (administrator window if you use `-Background`):

       powershell -ExecutionPolicy Bypass -File deploy\windows\register_task.ps1 -Time 18:30 -Publish -Background -Wake -Execute

**TWS or IB Gateway must be running and logged in at 6:30 pm.** IB logs it off once a day: in its settings, set the
daily auto-restart (Lock and Exit / Auto restart) to a time that is not around 6:30 pm. If it is not running, the day
is still researched and published, no orders are sent, and the e-mail says **BROKER UNAVAILABLE**.

Orders are market orders sized by the paper book (quarter-Kelly, 5% cap, monthly loss cap and drawdown rules).
EdgeLab only ever closes positions it opened itself.

## 8. Automatic IB Gateway login (IBC, paper only)

IBC fills in Gateway's login for you. Your IB **paper** username and password are kept in Windows Credential
Manager, not in any file. When Gateway starts, a temporary settings file readable only by your Windows account is
written and deleted as soon as Gateway answers (at most 3 minutes). Trading mode is always paper, port 4002.

IBC was retired by its author on 1 Sept 2026; its last release (3.24.2) supports Gateway 10.48 and later. If a
future Gateway update breaks it, you will see BROKER UNAVAILABLE in the e-mail and can log in by hand meanwhile.

1. **Install IBC**: download `IBCWin-3.24.2.zip` from https://github.com/IbcAlpha/IBC/releases/latest and unzip it
   to `C:\IBC` (so that `C:\IBC\scripts\StartIBC.bat` exists).
2. **Use the offline (stable) Gateway** installed in `C:\Jts` (the self-updating installer is not supported by IBC).
3. **Update the Python packages**: `.venv\Scripts\python.exe -m pip install -e ".[live,ibkr]"`
4. **Save your PAPER login** (prompts; the password is not shown):

       .venv\Scripts\python.exe -m edgelab.cli ib-gateway save-login

5. **Close Gateway if it is open**, then test: `.venv\Scripts\python.exe -m edgelab.cli ib-gateway start`
   Gateway should open and log in by itself within a few minutes, then `edgelab broker` shows the DU account.
6. **Keep it running automatically**: `powershell -ExecutionPolicy Bypass -File deploy\windows\register_ibgateway_task.ps1`

To remove the saved login: Windows Credential Manager > Windows Credentials > `edgelab-ibkr-paper` > Remove.
Log of each start: `logs\ibgateway.log`; IBC's own log: `C:\IBC\Logs`.

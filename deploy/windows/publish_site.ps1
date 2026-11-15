# Publishes the read-only dashboard snapshot to Cloudflare Pages, then checks Cloudflare Access.
# Runs only when you call it (or the scheduled task was registered with -Publish). Never runs on a Git push.
# Needs these User environment variables (names only): CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID,
# EDGELAB_PAGES_URL (for example https://edgelab-dashboard.pages.dev), and optionally EDGELAB_PAGES_PROJECT.
$ErrorActionPreference = "Stop"
$repo = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$python = Join-Path $repo ".venv\Scripts\python.exe"
Set-Location $repo

foreach ($name in "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "EDGELAB_PAGES_URL") {
    if (-not [Environment]::GetEnvironmentVariable($name, "User") -and -not (Get-Item "env:$name" -ErrorAction SilentlyContinue)) {
        throw "Missing environment variable $name (see deploy\windows\README.md)"
    }
}
$project = if ($env:EDGELAB_PAGES_PROJECT) { $env:EDGELAB_PAGES_PROJECT } else { "edgelab-dashboard" }

# 1. Build site\ from the database (read-only; the database file is never copied)
& $python -X utf8 -m edgelab.cli snapshot
if ($LASTEXITCODE -ne 0) { throw "snapshot export failed" }

# 2. Upload (outbound only). The production branch is "main".
npx --yes wrangler@4 pages deploy site --project-name=$project --branch=main --commit-dirty=true
if ($LASTEXITCODE -ne 0) { throw "Wrangler upload failed" }

# 3. The site must be behind Cloudflare Access. If it is not, say so loudly.
# An empty URL is a configuration problem, not a finding about the site, so it gets its own message.
$url = $env:EDGELAB_PAGES_URL
if (-not $url) {
    Write-Output "The upload finished, but EDGELAB_PAGES_URL is not set in THIS window, so Access could not be checked. Open a new PowerShell window and run the script again. Until the check passes, treat the site as unverified: open the address in a private browser window and confirm it asks you to log in."
    exit 3
}
& $python -X utf8 -m edgelab.cli verify-access $url
if ($LASTEXITCODE -eq 1) {
    Write-Output "The uploaded copy is reachable WITHOUT a login. Remove it now: Cloudflare dashboard > Workers & Pages > $project > Deployments, delete the latest one, and create the Access application for this hostname before publishing again."
    exit 2
}
if ($LASTEXITCODE -ne 0) {
    Write-Output "Access could not be verified (see the message above). The site may be fine; check it in a private browser window before trusting it."
    exit 3
}
Write-Output "Published and protected by Cloudflare Access: $url"

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
& $python -X utf8 -m edgelab.cli verify-access $env:EDGELAB_PAGES_URL
if ($LASTEXITCODE -ne 0) {
    Write-Output "The uploaded copy is reachable WITHOUT a login. Remove it now: Cloudflare dashboard > Workers & Pages > $project > Deployments, delete the latest one, and create the Access application for this hostname before publishing again."
    exit 2
}
Write-Output "Published and protected by Cloudflare Access: $env:EDGELAB_PAGES_URL"

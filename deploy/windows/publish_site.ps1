# Publishes the read-only dashboard snapshot to Cloudflare Pages.
#   powershell -ExecutionPolicy Bypass -File deploy\windows\publish_site.ps1               # the real snapshot
#   powershell -ExecutionPolicy Bypass -File deploy\windows\publish_site.ps1 -Placeholder  # blank test page only
#
# How the addresses are kept safe:
#   * The bare address https://<project>.pages.dev cannot be put behind Cloudflare Access in this account, so it
#     only ever gets an empty placeholder page (production branch "main"), deployed first on every run.
#   * The real snapshot goes to the "preview" branch: https://preview.<project>.pages.dev, which Access covers
#     through its * destination.
#   * Afterwards the bare address must hold no data, and the preview address must redirect to the Access login.
# Runs only when you call it (or the scheduled task was registered with -Publish). Never runs on a Git push.
# Needs User environment variables (names only): CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID.
# Optional: EDGELAB_PAGES_PROJECT (default edgelab-dashboard), EDGELAB_PAGES_BRANCH (default preview).
param([switch]$Placeholder)

$ErrorActionPreference = "Stop"
$repo = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$python = Join-Path $repo ".venv\Scripts\python.exe"
Set-Location $repo

foreach ($name in "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID") {
    if (-not (Get-Item "env:$name" -ErrorAction SilentlyContinue)) {
        throw "Environment variable $name is not visible in THIS window. Set it (see deploy\windows\README.md), then open a new PowerShell window. Nothing was uploaded."
    }
}
$project = if ($env:EDGELAB_PAGES_PROJECT) { $env:EDGELAB_PAGES_PROJECT } else { "edgelab-dashboard" }
$branch = if ($env:EDGELAB_PAGES_BRANCH) { $env:EDGELAB_PAGES_BRANCH } else { "preview" }
if ($branch -eq "main") { throw "Refusing to publish data to the production branch 'main': the bare address cannot be put behind Access." }
$bare = "https://$project.pages.dev"
$private = "https://$branch.$project.pages.dev"

# 1. Placeholder (no data) for the bare address. Built fresh every run.
$holder = Join-Path $repo "site-placeholder"
Remove-Item -Recurse -Force $holder -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $holder | Out-Null
Set-Content -Path (Join-Path $holder "index.html") -Value "<!DOCTYPE html><html><body><p>EdgeLab placeholder: no data.</p></body></html>" -Encoding utf8
npx --yes wrangler@4 pages deploy site-placeholder --project-name=$project --branch=main --commit-dirty=true
if ($LASTEXITCODE -ne 0) { throw "Placeholder upload to the bare address failed; the real snapshot was NOT uploaded." }

# 2. The content for the private branch: the placeholder again, or the real snapshot.
if ($Placeholder) {
    $folder = "site-placeholder"
} else {
    & $python -X utf8 -m edgelab.cli snapshot
    if ($LASTEXITCODE -ne 0) { throw "snapshot export failed" }
    $folder = "site"
}
# Wrangler writes straight to this window: it refuses to run when its output is piped.
npx --yes wrangler@4 pages deploy $folder --project-name=$project --branch=$branch --commit-dirty=true
if ($LASTEXITCODE -ne 0) { throw "Wrangler upload failed" }

# 3. Checks. Give Cloudflare a few seconds to switch over to the new deployments.
Start-Sleep -Seconds 10
$problems = @()
& $python -X utf8 -m edgelab.cli verify-empty $bare
if ($LASTEXITCODE -ne 0) { $problems += "$bare holds data (it must show only the placeholder)" }
& $python -X utf8 -m edgelab.cli verify-access $private
if ($LASTEXITCODE -ne 0) { $problems += "$private is not confirmed behind the Access login" }

if ($problems.Count -gt 0) {
    Write-Output ""
    Write-Output "CHECK FAILED:"
    $problems | ForEach-Object { Write-Output "  - $_" }
    Write-Output "Open each address in a NEW private browser window. If either shows EdgeLab data without a login, delete the edgelab-dashboard project in Cloudflare (Workers & Pages > edgelab-dashboard > Settings) and say so."
    exit 2
}
Write-Output ""
Write-Output "OK: $bare shows only the placeholder; $private is behind the Cloudflare Access login."

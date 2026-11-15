# Publishes the read-only dashboard snapshot to a NON-production Cloudflare Pages branch (default "preview"),
# then checks Cloudflare Access on every address Wrangler reports. The bare project address stays empty, so
# nothing is ever served there. Access covers *.<project>.pages.dev, which includes the branch address.
#   powershell -ExecutionPolicy Bypass -File deploy\windows\publish_site.ps1               # the real snapshot
#   powershell -ExecutionPolicy Bypass -File deploy\windows\publish_site.ps1 -Placeholder  # blank test page, no data
# Runs only when you call it (or the scheduled task was registered with -Publish). Never runs on a Git push.
# Needs these User environment variables (names only): CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID,
# EDGELAB_PAGES_URL (for example https://edgelab-dashboard.pages.dev), and optionally EDGELAB_PAGES_PROJECT.
param([switch]$Placeholder)

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
$branch = if ($env:EDGELAB_PAGES_BRANCH) { $env:EDGELAB_PAGES_BRANCH } else { "preview" }
if ($branch -eq "main") { throw "Refusing to publish to the production branch 'main': the bare project address cannot be put behind Access. Use a branch such as 'preview'." }
$url = $env:EDGELAB_PAGES_URL
if (-not $url) {
    Write-Output "EDGELAB_PAGES_URL is not set in THIS window. Open a new PowerShell window and run the script again. Nothing was uploaded."
    exit 3
}

# 1. Build the upload folder. The placeholder contains no data at all.
if ($Placeholder) {
    $site = Join-Path $repo "site-placeholder"
    Remove-Item -Recurse -Force $site -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Path $site | Out-Null
    Set-Content -Path (Join-Path $site "index.html") -Value "<!DOCTYPE html><html><body><p>EdgeLab placeholder: no data.</p></body></html>" -Encoding utf8
    $folder = "site-placeholder"
} else {
    & $python -X utf8 -m edgelab.cli snapshot
    if ($LASTEXITCODE -ne 0) { throw "snapshot export failed" }
    $folder = "site"
}

# 2. Upload (outbound only) to the non-production branch. Wrangler writes straight to this window: it refuses to run
#    when its output is piped, so nothing is captured here.
npx --yes wrangler@4 pages deploy $folder --project-name=$project --branch=$branch --commit-dirty=true
if ($LASTEXITCODE -ne 0) { throw "Wrangler upload failed" }

# 3. The branch address and the configured address must both sit behind Cloudflare Access.
#    (Per-deployment addresses like https://<hash>.<project>.pages.dev fall under the same * destination.)
$addresses = @($url, "https://$branch.$project.pages.dev") | Sort-Object -Unique
$bad = @()
foreach ($addr in $addresses) {
    & $python -X utf8 -m edgelab.cli verify-access $addr
    if ($LASTEXITCODE -eq 1) { $bad += $addr }
    elseif ($LASTEXITCODE -ne 0) { Write-Output "Could not verify $addr (see the message above). Check it in a private browser window." ; $bad += $addr }
}
if ($bad.Count -gt 0) {
    Write-Output "NOT PROTECTED or not verified: $($bad -join ', '). Remove the deployment now: Cloudflare dashboard > Workers & Pages > $project > Deployments. Then check the Access application covers these addresses before publishing again."
    exit 2
}
Write-Output "Published and protected by Cloudflare Access: $($addresses -join ', ')"

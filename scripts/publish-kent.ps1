# Wait for the Kent build, then rebuild the web demo and deploy it.
#
#     powershell -File .\scripts\publish-kent.ps1
#
# Runs unattended, so it is deliberately conservative: it refuses to publish
# rather than publish something half-built, and every refusal says why.
#
# The order matters and is the whole point of the script:
#
#   1. Wait for every district PK3 to exist.
#   2. Bump PK3_V *before* the site is rebuilt.
#   3. Rebuild the site per district (districts.json accumulates).
#   4. Deploy once, at the end.
#
# Step 2 is the one that bites. Tile PK3s are cached 30 days at the edge under
# URLs a rebuild reuses, so changed tile contents reach nobody for a month
# unless the launcher asks for them under a new `?v=`. The build is silent about
# it and `rclone check` reports the bucket in sync, because it is.

$districts = @(
    'sevenoaks', 'thanet', 'dartford', 'gravesham', 'medway',
    'tonbridge-and-malling', 'canterbury', 'dover', 'tunbridge-wells',
    'folkestone-and-hythe', 'maidstone', 'swale', 'ashford'
)

$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$python = Join-Path $root '.venv\Scripts\python.exe'
Set-Location $root

function Show-Toast {
    param([string]$Title, [string]$Body, [int]$Ms = 30000)
    try {
        Add-Type -AssemblyName System.Windows.Forms
        $icon = New-Object System.Windows.Forms.NotifyIcon
        $icon.Icon = [System.Drawing.SystemIcons]::Information
        $icon.Visible = $true
        $icon.BalloonTipTitle = $Title
        $icon.BalloonTipText = $Body
        $icon.ShowBalloonTip($Ms)
        Start-Sleep -Seconds 8
        $icon.Dispose()
    } catch { Write-Host "(no notification: $_)" }
}

# ---- 1. wait for the build ------------------------------------------------
#
# Waits on the artifacts rather than on the build process: a process that dies
# and gets restarted by hand should not strand this script, and a PK3 on disk
# is the only thing that actually proves a district finished.
Write-Host "waiting for $($districts.Count) district PK3s..."
$deadline = (Get-Date).AddHours(20)
while ((Get-Date) -lt $deadline) {
    $have = @($districts | Where-Object { Test-Path "out/$_.pk3" })
    if ($have.Count -eq $districts.Count) { break }
    Start-Sleep -Seconds 300
}

$have = @($districts | Where-Object { Test-Path "out/$_.pk3" })
$missing = @($districts | Where-Object { -not (Test-Path "out/$_.pk3") })
if ($missing.Count -gt 0) {
    Write-Host "NOT PUBLISHING - still missing: $($missing -join ', ')"
    Show-Toast 'postcode2wad - publish held' "$($have.Count)/$($districts.Count) districts built; not deploying a partial county."
    exit 1
}
Write-Host "all $($have.Count) districts present at $(Get-Date -Format 'HH:mm:ss')"

# ---- 2. bump PK3_V --------------------------------------------------------
#
# Before the rebuild, because build-webdemo.py copies play.html into the site as
# part of its run. Bumping afterwards would ship the old constant.
$play = 'scripts/webdemo/play.html'
$stamp = Get-Date -Format 'yyyyMMdd'

# Read and write through .NET with an explicit no-BOM UTF-8, NOT Get-Content /
# Set-Content. Windows PowerShell 5.1's `Get-Content -Raw` decodes a BOM-less
# file as the ANSI codepage, so every UTF-8 em-dash arrives as two Latin-1
# characters; `Set-Content -Encoding UTF8` then re-encodes those as UTF-8 and
# adds a BOM. The round trip is lossy in both directions at once.
#
# This shipped. The 7 Aug Kent publish rewrote play.html this way and put
# "no tile selected â€” go back to the map" and "bootingâ€¦" live, plus a BOM
# ahead of the doctype. Nothing failed and nothing was logged: the regex
# matched, the bump was correct, the deploy reported success, and the damage
# was to every non-ASCII character in the file *except* the one being edited.
$utf8 = [System.Text.UTF8Encoding]::new($false)
$full = (Resolve-Path $play).Path
$text = [System.IO.File]::ReadAllText($full, $utf8)
if ($text -match "const PK3_V = '([^']+)'") {
    $old = $Matches[1]
    $new = $stamp + 'k'   # k for Kent; any change is enough, this is just legible
    $text = $text -replace "const PK3_V = '[^']+'", "const PK3_V = '$new'"
    [System.IO.File]::WriteAllText($full, $text, $utf8)
    Write-Host "PK3_V $old -> $new"
} else {
    Write-Host "NOT PUBLISHING - could not find PK3_V in $play"
    Show-Toast 'postcode2wad - publish held' 'PK3_V not found; refusing to ship tiles nobody will fetch.'
    exit 1
}

# ---- 3. rebuild the site, district by district ----------------------------
foreach ($slug in $districts) {
    $work = "work/$slug-800m"
    if (-not (Test-Path $work)) {
        Write-Host "  $slug - no work dir, skipping"
        continue
    }
    Write-Host "  building site for $slug at $(Get-Date -Format 'HH:mm:ss')"
    & $python -u scripts/build-webdemo.py --work $work --out site --engine-dir site/engine
    if ($LASTEXITCODE -ne 0) {
        Write-Host "NOT PUBLISHING - build-webdemo failed on $slug"
        Show-Toast 'postcode2wad - publish held' "build-webdemo failed on $slug; nothing deployed."
        exit 1
    }
}

# ---- 4. deploy ------------------------------------------------------------
Write-Host "deploying at $(Get-Date -Format 'HH:mm:ss')"
& $python -u scripts/deploy-webdemo.py --site site
if ($LASTEXITCODE -ne 0) {
    Show-Toast 'postcode2wad - deploy FAILED' 'rclone returned non-zero. Site may be partly updated.'
    exit 1
}

$area = 'unknown'
try {
    $j = Get-Content 'site/districts.json' -Raw | ConvertFrom-Json
    $area = '{0:N0} km2 across {1:N0} tiles' -f $j.total_km2, $j.tiles_total
} catch {}

Show-Toast 'postcode2wad - Kent is live' "$area`nhttps://doomearth.clawhangout.com" 60000
[System.Console]::Beep(880, 400)
Write-Host "published: $area"

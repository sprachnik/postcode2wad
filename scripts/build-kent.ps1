# Build every Kent district, one after another.
#
#     powershell -File .\scripts\build-kent.ps1
#
# Sequential on purpose. Each district already saturates its own workers, so
# running two at once just makes them contend -- and a tile build is superlinear
# in sector count, so a dense town tile in one district would slow every tile in
# the other.
#
# Resumable at two levels: build-district.py skips any tile whose artifact is
# already on disk, and this script skips a district whose PK3 exists. A machine
# that sleeps halfway through costs nothing but the tile that was in flight.
#
# Measured on Sevenoaks (672 tiles, fully mirrored, 8 workers): 63 minutes wall
# clock, 5.6s per tile. Kent is 6,890 tiles, so expect ~10-11 hours. Sevenoaks
# is at the denser end (9,704 mean sectors against Thanet's 7,363), so the true
# figure is likely a little under.

$districts = @(
    'Sevenoaks',              # 672 - the measured one, first so a regression shows early
    'Thanet',                 # 200 - smallest, and has a deployed build to compare against
    'Dartford',               # 152
    'Gravesham',              # 197
    'Medway',                 # 401
    'Tonbridge and Malling',  # 450
    'Canterbury',             # 561
    'Dover',                  # 570
    'Tunbridge Wells',        # 617
    'Folkestone and Hythe',   # 665
    'Maidstone',              # 693
    'Swale',                  # 701
    'Ashford'                 # 1011 - largest, last
)

$KentTiles = 6890
$python = Join-Path $PSScriptRoot '..\.venv\Scripts\python.exe'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$started = Get-Date

# NotifyIcon rather than BurntToast: it is in the .NET that ships with Windows,
# so there is nothing to install on a machine that may not have the module.
function Show-Toast {
    param([string]$Title, [string]$Body, [int]$Ms = 20000)
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
    } catch {
        Write-Host "(could not raise a notification: $_)"
    }
}

# Hourly progress, in a background job so it never interrupts the build. Counts
# artifacts on disk rather than parsing logs: a tile is done when its zip
# exists, which is exactly what the resume logic believes.
$watcher = Start-Job -ArgumentList $root, $started, $KentTiles -ScriptBlock {
    param($root, $began, $target)
    Add-Type -AssemblyName System.Windows.Forms
    while ($true) {
        Start-Sleep -Seconds 3600
        $done = @(Get-ChildItem "$root\work\*-800m\tiles\*.zip" -ErrorAction SilentlyContinue).Count
        if ($done -le 0) { continue }
        $mins = ((Get-Date) - $began).TotalMinutes
        $rate = $done / [Math]::Max($mins, 1)
        $left = [Math]::Max($target - $done, 0) / [Math]::Max($rate, 0.01)
        $icon = New-Object System.Windows.Forms.NotifyIcon
        $icon.Icon = [System.Drawing.SystemIcons]::Information
        $icon.Visible = $true
        $icon.BalloonTipTitle = 'postcode2wad - Kent build'
        $icon.BalloonTipText = ("{0:N0} of {1:N0} tiles ({2:N0}%), about {3:N1}h left" -f $done, $target, (100 * $done / $target), ($left / 60))
        $icon.ShowBalloonTip(15000)
        Start-Sleep -Seconds 8
        $icon.Dispose()
    }
}

foreach ($district in $districts) {
    $slug = $district.ToLower() -replace '[^a-z0-9]+', '-'
    $work = "work/$slug-800m"
    $out = "out/$slug.pk3"

    if (Test-Path $out) {
        Write-Host "== $district - already built, skipping"
        continue
    }

    Write-Host "== $district - starting at $(Get-Date -Format 'HH:mm:ss')"
    & $python -u scripts/build-district.py --district $district --size 800 --workers 8 --work $work --out $out
    if ($LASTEXITCODE -ne 0) {
        Write-Host "== $district FAILED (exit $LASTEXITCODE) - continuing with the rest"
    }
}

Stop-Job $watcher -ErrorAction SilentlyContinue
Remove-Job $watcher -Force -ErrorAction SilentlyContinue

$elapsed = (Get-Date) - $started
$built = @(Get-ChildItem "$root\work\*-800m\tiles\*.zip" -ErrorAction SilentlyContinue).Count

Show-Toast 'postcode2wad - Kent build finished' ("{0} districts, {1:N0} tiles, {2:hh\:mm\:ss}" -f $districts.Count, $built, $elapsed) 60000
[System.Console]::Beep(880, 400)

Write-Host ""
Write-Host ("Kent: {0} districts, {1:N0} tiles in {2:hh\:mm\:ss}" -f $districts.Count, $built, $elapsed)
Get-ChildItem out/*.pk3 -ErrorAction SilentlyContinue | ForEach-Object {
    Write-Host ("  {0,-28} {1,8:N0} MB" -f $_.Name, ($_.Length / 1MB))
}

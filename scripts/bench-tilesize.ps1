<#
.SYNOPSIS
    Measure how tile size trades off against sector count and map load time.

.DESCRIPTION
    The brief asks for the practical ceiling on sector count, and the answer
    decides the whole world-mode design: if a 1km tile loads acceptably then a
    region pack needs one load per kilometre rather than one per 400m, and the
    case for forking the engine (M5) gets much weaker.

    Load time is measured as wall clock from process start until GZDoom writes
    the "MAPxx - <title>" line to its log, which happens after the nodes are
    built. It therefore includes engine startup -- roughly constant across runs,
    so the *differences* are what matter, and the 400m row doubles as the
    baseline to subtract.

.EXAMPLE
    .\scripts\bench-tilesize.ps1 -Postcode "CT7 0EP" -Sizes 400,600,800,1000
#>
param(
    [string]$Postcode = "CT7 0EP",
    [int[]]$Sizes = @(400, 600, 800, 1000, 1200),
    [string]$OutDir = "out/bench",
    [int]$LoadTimeout = 180
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

$gz   = Join-Path $root "tools\gzdoom\gzdoom.exe"
$iwad = Join-Path $root "tools\freedoom\freedoom-0.13.0\freedoom2.wad"
$cli  = Join-Path $root ".venv\Scripts\postcode2wad.exe"
foreach ($p in $gz, $iwad, $cli) { if (-not (Test-Path $p)) { throw "not found: $p" } }

New-Item -ItemType Directory -Force $OutDir | Out-Null
$results = @()

foreach ($size in $Sizes) {
    Write-Output "=== ${size}m ==="
    $pk3 = Join-Path $OutDir "bench-$size.pk3"
    $log = Join-Path $OutDir "bench-$size.log"

    $genStart = Get-Date
    # No "2>&1" here: redirecting a native command's stderr under
    # ErrorActionPreference=Stop turns any stderr line into a terminating error,
    # which killed the whole sweep on the first tile that printed a warning.
    $out = & $cli $Postcode --size $size --out $pk3
    $genSecs = ((Get-Date) - $genStart).TotalSeconds
    if ($LASTEXITCODE -ne 0) { Write-Warning "generation failed at ${size}m"; continue }

    $stats = ($out | Select-String -Pattern "(\d+) sectors, (\d+) linedefs")
    if (-not $stats) { Write-Warning "no stats line at ${size}m"; continue }
    $sectors  = [int]$stats.Matches[0].Groups[1].Value
    $linedefs = [int]$stats.Matches[0].Groups[2].Value

    Remove-Item $log -ErrorAction SilentlyContinue
    $gzArgs = @(
        "-iwad", (Resolve-Path $iwad).Path,
        "-file", (Resolve-Path $pk3).Path,
        "-width", "640", "-height", "480", "-windowed",
        "+vid_fullscreen", "0", "-nosound",
        "+logfile", $log, "+map", "MAP01"
    )

    $loadStart = Get-Date
    $proc = Start-Process -FilePath (Resolve-Path $gz).Path -ArgumentList $gzArgs -PassThru

    # Poll the log rather than sleeping a fixed amount: the whole point is to
    # find out how long this takes, so a fixed wait would measure the wait.
    $loadSecs = $null
    while (((Get-Date) - $loadStart).TotalSeconds -lt $LoadTimeout) {
        if (Test-Path $log) {
            if (Select-String -Path $log -Pattern "^MAP01 - " -Quiet -ErrorAction SilentlyContinue) {
                $loadSecs = ((Get-Date) - $loadStart).TotalSeconds
                break
            }
        }
        if ($proc.HasExited) { break }
        Start-Sleep -Milliseconds 250
    }
    if (-not $proc.HasExited) { Stop-Process -Id $proc.Id -Force }

    $results += [pscustomobject]@{
        SizeM      = $size
        AreaKm2    = [math]::Round(($size / 1000.0) * ($size / 1000.0), 3)
        Sectors    = $sectors
        Linedefs   = $linedefs
        GenSecs    = [math]::Round($genSecs, 1)
        LoadSecs   = if ($null -eq $loadSecs) { "TIMEOUT" } else { [math]::Round($loadSecs, 1) }
        Pk3MB      = [math]::Round((Get-Item $pk3).Length / 1MB, 2)
    }
    $results[-1] | Format-List | Out-String | Write-Output
}

Write-Output ""
Write-Output "=== summary ==="
$results | Format-Table -AutoSize
$results | Export-Csv -NoTypeInformation -Path (Join-Path $OutDir "tilesize.csv")
Write-Output "csv: $(Join-Path $OutDir 'tilesize.csv')"

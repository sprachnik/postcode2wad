<#
.SYNOPSIS
    Launch a generated PK3 in GZDoom, grab a screenshot of the window, and exit.

.DESCRIPTION
    GZDoom's own +screenshot console command is awkward to drive from the command
    line (the `wait` ccmd only defers inside aliases, and -exec runs too early),
    so we just capture the game window with GDI instead. Reliable and engine
    version independent.

.EXAMPLE
    .\scripts\playtest.ps1 -Pk3 out\m0.pk3 -Shot out\shots\m0.png -Seconds 10
#>
param(
    [Parameter(Mandatory = $true)][string]$Pk3,
    [string]$Shot = "",
    [int]$Seconds = 10,
    [string]$Map = "MAP01",
    [switch]$Interactive,
    [switch]$CheckOnly,
    [string]$GzDoom = "",
    [string]$Iwad = "",
    [string]$LogFile = ""
)

$ErrorActionPreference = "Stop"

# $PSScriptRoot is not populated when param defaults are evaluated, so resolve here.
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $GzDoom) { $GzDoom = Join-Path $root "..\tools\gzdoom\gzdoom.exe" }
if (-not $Iwad)   { $Iwad   = Join-Path $root "..\tools\freedoom\freedoom-0.13.0\freedoom2.wad" }

if (-not (Test-Path $GzDoom)) { throw "GZDoom not found at $GzDoom" }
if (-not (Test-Path $Iwad))   { throw "Freedoom IWAD not found at $Iwad" }
if (-not (Test-Path $Pk3))    { throw "PK3 not found at $Pk3" }

if (-not $LogFile) { $LogFile = Join-Path (Split-Path $Pk3 -Parent) "gzdoom.log" }
Remove-Item $LogFile -ErrorAction SilentlyContinue

$gzArgs = @(
    "-iwad", (Resolve-Path $Iwad).Path,
    "-file", (Resolve-Path $Pk3).Path,
    "-width", "1280", "-height", "720", "-windowed",
    # -windowed loses to a saved fullscreen setting in gzdoom.ini, and an
    # exclusive-fullscreen GZDoom cannot present while another one already holds
    # the display -- the capture then comes back solid black with no error
    # anywhere. Setting the cvar directly wins over the config file.
    "+vid_fullscreen", "0",
    "+logfile", $LogFile,
    "+map", $Map
)
if (-not $Interactive) { $gzArgs += "-nosound" }
#

$proc = Start-Process -FilePath (Resolve-Path $GzDoom).Path -ArgumentList $gzArgs -PassThru

# Load the map, then kill it and read the log. Neither "+quit" nor deferring it
# through an alias works: both fire during console-command processing, which is
# before the map is loaded, so they catch DECORATE and MAPINFO errors (fatal at
# startup) but miss everything the map itself reports. Waiting and killing is
# blunt but it actually gets past D_CheckNetGame. No window raising here, so it
# does not fight whatever else is on screen.
if ($CheckOnly) {
    Start-Sleep -Seconds $Seconds
    if (-not $proc.HasExited) { Stop-Process -Id $proc.Id -Force }

    $log = Get-Content $LogFile -ErrorAction SilentlyContinue
    if (-not ($log | Select-String -Pattern "^$Map -" -Quiet)) {
        Write-Output "map never loaded - last lines:"
        $log | Select-Object -Last 6
        exit 1
    }

    # Show the FIRST problems, with the line after each (GZDoom puts the message
    # on the line following "Script error, ... line N:"). Showing the tail
    # instead is actively misleading: ZScript errors cascade, so the last ones
    # are downstream symptoms and the root cause has already scrolled away.
    $lines = @($log)
    $hits = for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match "Script error|Execution could not continue|Unknown texture|has no lines|Bad sidedef|Warning") {
            $lines[$i]
            if ($i + 1 -lt $lines.Count) { "    " + $lines[$i + 1] }
        }
    }
    if ($hits) {
        Write-Output "=== first problems (root cause is at the top) ==="
        $hits | Select-Object -First 20
        exit 1
    }
    Write-Output "loaded clean: $Pk3 ($($log.Count) log lines)"
    exit 0
}

if ($Interactive) {
    Write-Output "GZDoom running (PID $($proc.Id)). Close the window when done."
    $proc.WaitForExit()
    exit 0
}

Start-Sleep -Seconds $Seconds

if ($Shot) {
    Add-Type -AssemblyName System.Drawing
    Add-Type @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public class Win32Capture {
    [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr hWnd, IntPtr after, int x, int y, int cx, int cy, uint flags);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);
    [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr hWnd, StringBuilder s, int max);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int cmd);
    [StructLayout(LayoutKind.Sequential)]
    public struct RECT { public int Left, Top, Right, Bottom; }
    public static IntPtr TOPMOST = new IntPtr(-1);
    public static IntPtr NOTOPMOST = new IntPtr(-2);
    public const uint NOMOVE_NOSIZE_SHOW = 0x0001 | 0x0002 | 0x0040;
    public static string Title(IntPtr h) {
        var sb = new StringBuilder(512);
        GetWindowText(h, sb, sb.Capacity);
        return sb.ToString();
    }
}
"@
    # MainWindowHandle is not populated immediately; poll for it.
    $handle = [IntPtr]::Zero
    foreach ($attempt in 1..20) {
        $proc.Refresh()
        if ($proc.MainWindowHandle -ne [IntPtr]::Zero) { $handle = $proc.MainWindowHandle; break }
        Start-Sleep -Milliseconds 250
    }

    if ($handle -ne [IntPtr]::Zero) {
        $title = [Win32Capture]::Title($handle)
        # Raising to topmost beats Windows' focus-stealing prevention, which
        # silently no-ops SetForegroundWindow when another app owns the
        # foreground -- that failure captures the wrong window entirely.
        [void][Win32Capture]::ShowWindow($handle, 9)  # SW_RESTORE
        [void][Win32Capture]::SetWindowPos($handle, [Win32Capture]::TOPMOST, 0, 0, 0, 0, [Win32Capture]::NOMOVE_NOSIZE_SHOW)
        Start-Sleep -Milliseconds 1200

        $rect = New-Object Win32Capture+RECT
        [void][Win32Capture]::GetWindowRect($handle, [ref]$rect)
        $w = $rect.Right - $rect.Left
        $h = $rect.Bottom - $rect.Top
        if ($w -gt 0 -and $h -gt 0) {
            $bmp = New-Object System.Drawing.Bitmap $w, $h
            $gfx = [System.Drawing.Graphics]::FromImage($bmp)
            $gfx.CopyFromScreen($rect.Left, $rect.Top, 0, 0, $bmp.Size)
            New-Item -ItemType Directory -Force (Split-Path $Shot -Parent) | Out-Null
            $bmp.Save($Shot, [System.Drawing.Imaging.ImageFormat]::Png)
            $gfx.Dispose(); $bmp.Dispose()
            Write-Output "screenshot: $Shot ($w x $h) from window '$title'"
        } else {
            Write-Warning "window rect was empty; skipping screenshot"
        }
        [void][Win32Capture]::SetWindowPos($handle, [Win32Capture]::NOTOPMOST, 0, 0, 0, 0, [Win32Capture]::NOMOVE_NOSIZE_SHOW)
    } else {
        Write-Warning "no window handle; skipping screenshot"
    }
}

if (-not $proc.HasExited) { Stop-Process -Id $proc.Id -Force }

Write-Output "=== log tail ==="
Get-Content $LogFile -ErrorAction SilentlyContinue | Select-Object -Last 12

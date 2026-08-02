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
    "+logfile", $LogFile,
    "+map", $Map
)
if (-not $Interactive) { $gzArgs += "-nosound" }

$proc = Start-Process -FilePath (Resolve-Path $GzDoom).Path -ArgumentList $gzArgs -PassThru

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

<#
.SYNOPSIS
    Restart the SnipSquiggle tray app - use after editing snipsquiggle.py.

.DESCRIPTION
    Kills any running snipsquiggle.py process (tray or one-shot), waits for it
    to release the PrintScreen hotkey, then relaunches it in tray mode.

    By default it uses pythonw.exe (no console window), matching the
    "SnipSquiggle (tray)" startup shortcut.

.PARAMETER Stop
    Only kill the running instance; do not start a new one.

.PARAMETER Console
    Launch with python.exe instead of pythonw.exe so tracebacks are visible.

.PARAMETER Python
    Full path to a specific python.exe / pythonw.exe to use.

.PARAMETER ExtraArgs
    Extra flags passed through to snipsquiggle.py (e.g. --no-copy, --save-dir D:\Snips).

.EXAMPLE
    .\restart-tray.ps1
    .\restart-tray.ps1 -Console
    .\restart-tray.ps1 -Stop
    .\restart-tray.ps1 -ExtraArgs '--save-dir','D:\Snips'
#>
[CmdletBinding()]
param(
    [switch]$Stop,
    [switch]$Console,
    [string]$Python,
    [string[]]$ExtraArgs = @()
)

$ErrorActionPreference = 'Stop'

$root   = $PSScriptRoot
$script = Join-Path $root 'snipsquiggle.py'
if (-not (Test-Path $script)) {
    throw "snipsquiggle.py not found next to this script ($root)"
}

# ---------------------------------------------------------------- stop old --
$filter  = "Name = 'python.exe' OR Name = 'pythonw.exe'"
$running = @(Get-CimInstance Win32_Process -Filter $filter |
             Where-Object { $_.CommandLine -like '*snipsquiggle.py*' })

if ($running.Count -eq 0) {
    Write-Host "No SnipSquiggle process running." -ForegroundColor DarkGray
} else {
    foreach ($p in $running) {
        Write-Host "Stopping PID $($p.ProcessId) ($($p.Name))" -ForegroundColor Yellow
        try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop }
        catch { Write-Warning "PID $($p.ProcessId): $($_.Exception.Message)" }
    }

    # Wait for the old process to die so it releases the PrintScreen hotkey
    # before the new one tries to register it.
    $deadline = (Get-Date).AddSeconds(10)
    do {
        Start-Sleep -Milliseconds 200
        $left = @(Get-CimInstance Win32_Process -Filter $filter |
                  Where-Object { $_.CommandLine -like '*snipsquiggle.py*' })
    } while ($left.Count -gt 0 -and (Get-Date) -lt $deadline)

    if ($left.Count -gt 0) {
        throw "Gave up waiting for PID(s) $($left.ProcessId -join ', ') to exit."
    }
    Write-Host "Stopped." -ForegroundColor Green
}

if ($Stop) { return }

# --------------------------------------------------------------- start new --
$exeName = if ($Console) { 'python.exe' } else { 'pythonw.exe' }

if ($Python) {
    $py = $Python
} else {
    # Prefer the interpreter the startup shortcut uses - that is the one with
    # pywin32 / pystray / Pillow installed. Fall back to whatever is on PATH.
    $py = Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\$exeName"
    if (-not (Test-Path $py)) {
        $cmd = Get-Command $exeName -ErrorAction SilentlyContinue
        if ($cmd) { $py = $cmd.Source }
    }
}
if (-not (Test-Path $py)) {
    throw "Could not find $exeName. Pass one explicitly: -Python 'C:\path\to\$exeName'"
}

$argList = @("`"$script`"", '--tray') + $ExtraArgs
Write-Host "Starting: $py $($argList -join ' ')" -ForegroundColor Cyan

$proc = Start-Process -FilePath $py -ArgumentList $argList `
                      -WorkingDirectory $root -PassThru

# Give it a moment - if the script blows up on import it dies almost instantly.
Start-Sleep -Milliseconds 1200
$proc.Refresh()
if ($proc.HasExited) {
    Write-Host ""
    Write-Warning ("SnipSquiggle exited immediately (code $($proc.ExitCode)). " +
                   "Re-run with -Console to see the traceback.")
    exit 1
}

Write-Host "SnipSquiggle tray running as PID $($proc.Id). Press PrintScreen to snip." -ForegroundColor Green

# Only one app can own PrintScreen - flag the usual suspects.
$rivals = @(Get-Process -Name 'Greenshot','ShareX','SnippingTool','ScreenSketch','Lightshot' `
            -ErrorAction SilentlyContinue | Select-Object -ExpandProperty ProcessName -Unique)
if ($rivals) {
    Write-Warning "These may be holding PrintScreen: $($rivals -join ', ')"
}

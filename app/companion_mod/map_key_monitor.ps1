param(
    [Parameter(Mandatory = $true)]
    [string]$SignalPath,
    [switch]$PulseOnce,
    [switch]$StartDetached
)

$ErrorActionPreference = "Stop"

if ($StartDetached) {
    $powerShellExe = Join-Path $PSHOME "powershell.exe"
    $pulseArgument = if ($PulseOnce) { " -PulseOnce" } else { "" }
    $arguments = ('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" -SignalPath "{1}"{2}' -f $PSCommandPath, $SignalPath, $pulseArgument)
    Start-Process -FilePath $powerShellExe -ArgumentList $arguments -WindowStyle Hidden
    exit 0
}

# Scrap Mechanic's public Lua API exposes named game actions but no arbitrary
# keyboard callback. This foreground-only helper turns a physical M key-down
# edge into a monotonically increasing JSON sequence. It does not inject into
# the game, suppress any key, or repurpose Logbook/L.
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;

public static class MapCompanionKeyboard {
    [DllImport("user32.dll")]
    public static extern short GetAsyncKeyState(int virtualKey);

    [DllImport("user32.dll")]
    public static extern IntPtr GetForegroundWindow();

    [DllImport("user32.dll")]
    public static extern uint GetWindowThreadProcessId(IntPtr window, out uint processId);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern int GetWindowText(IntPtr window, System.Text.StringBuilder text, int count);
}
"@

$createdNew = $false
$mutex = New-Object System.Threading.Mutex($true, "Local\ScrapMechanicMapCompanionMKey", [ref]$createdNew)
if (-not $createdNew) {
    $mutex.Dispose()
    exit 0
}

function Get-SignalSequence {
    if (-not (Test-Path -LiteralPath $SignalPath -PathType Leaf)) { return [int64]0 }
    try {
        $state = Get-Content -LiteralPath $SignalPath -Raw | ConvertFrom-Json
        return [int64]$state.sequence
    } catch {
        return [int64]0
    }
}

function Write-SignalSequence {
    param([int64]$Sequence, [string]$Action = "toggle")

    $directory = Split-Path -Parent $SignalPath
    if (-not (Test-Path -LiteralPath $directory -PathType Container)) { return }

    $temporary = "$SignalPath.$PID.tmp"
    $json = "{`"sequence`":$Sequence,`"action`":`"$Action`"}`r`n"
    [System.IO.File]::WriteAllText($temporary, $json, (New-Object System.Text.UTF8Encoding($false)))
    if (Test-Path -LiteralPath $SignalPath -PathType Leaf) {
        $backup = "$SignalPath.$PID.bak"
        [System.IO.File]::Replace($temporary, $SignalPath, $backup)
        Remove-Item -LiteralPath $backup -Force -ErrorAction SilentlyContinue
    } else {
        Move-Item -LiteralPath $temporary -Destination $SignalPath
    }
}

function Get-OverlayState {
    $overlayState = Join-Path (Split-Path -Parent $SignalPath) "overlay_state.json"
    if (-not (Test-Path -LiteralPath $overlayState -PathType Leaf)) { return $null }
    try {
        $state = Get-Content -LiteralPath $overlayState -Raw | ConvertFrom-Json
        return $state
    } catch {
        return $null
    }
}

try {
    $gameWasSeen = $false
    $wasDown = $false
    $escapeWasDown = $false
    $initialOverlayState = Get-OverlayState
    $mapRequestedOpen = if ($null -ne $initialOverlayState) { [bool]$initialOverlayState.visible } else { $false }
    $sequence = Get-SignalSequence
    $waitStarted = [System.Diagnostics.Stopwatch]::StartNew()
    $titleBuffer = New-Object System.Text.StringBuilder 256
    $inputCooldown = [System.Diagnostics.Stopwatch]::StartNew()
    $lastAcceptedInput = -1000.0

    # Used by the installer test path; the normal launcher never sets this.
    if ($PulseOnce) {
        $sequence++
        Write-SignalSequence $sequence
        exit 0
    }

    while ($true) {
        $gameProcesses = @(Get-Process -Name "ScrapMechanic" -ErrorAction SilentlyContinue)
        if ($gameProcesses.Count -eq 0) {
            if ($gameWasSeen) { break }
            if ($waitStarted.Elapsed.TotalMinutes -ge 15) { break }
            Start-Sleep -Milliseconds 100
            continue
        }

        $gameWasSeen = $true
        [uint32]$foregroundPid = 0
        $foregroundWindow = [MapCompanionKeyboard]::GetForegroundWindow()
        [void][MapCompanionKeyboard]::GetWindowThreadProcessId($foregroundWindow, [ref]$foregroundPid)
        $gameIsForeground = $gameProcesses.Id -contains [int]$foregroundPid
        [void]$titleBuffer.Clear()
        [void][MapCompanionKeyboard]::GetWindowText($foregroundWindow, $titleBuffer, $titleBuffer.Capacity)
        $mapIsForeground = $titleBuffer.ToString() -eq "Scrap Mechanic World Map"
        $isDown = ([MapCompanionKeyboard]::GetAsyncKeyState(0x4D) -band 0x8000) -ne 0
        $escapeIsDown = ([MapCompanionKeyboard]::GetAsyncKeyState(0x1B) -band 0x8000) -ne 0

        # Once Python acknowledges the last request, copy the overlay's real
        # physical state back into the key state machine. This recovers from a
        # refused pre-render open while still preserving the parity of several
        # fast M presses that arrive before Python can acknowledge each file.
        $overlayState = Get-OverlayState
        if ($null -ne $overlayState -and [int64]$overlayState.ackSequence -ge $sequence) {
            $mapRequestedOpen = [bool]$overlayState.visible
        }

        $nowMs = $inputCooldown.Elapsed.TotalMilliseconds
        $inputReady = ($nowMs - $lastAcceptedInput) -ge 140.0
        if (($gameIsForeground -or $mapIsForeground) -and $isDown -and -not $wasDown -and $inputReady) {
            $mapRequestedOpen = -not $mapRequestedOpen
            $action = if ($mapRequestedOpen) { "open" } else { "close" }
            $sequence++
            Write-SignalSequence $sequence $action
            $lastAcceptedInput = $nowMs
        }
        elseif ($mapIsForeground -and $escapeIsDown -and -not $escapeWasDown -and $inputReady) {
            $mapRequestedOpen = $false
            $sequence++
            Write-SignalSequence $sequence "close"
            $lastAcceptedInput = $nowMs
        }
        $wasDown = $isDown
        $escapeWasDown = $escapeIsDown
        Start-Sleep -Milliseconds 15
    }
} finally {
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}

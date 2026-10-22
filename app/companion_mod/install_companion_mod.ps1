param(
    [ValidateSet("Survival", "CustomGame", "Both")]
    [string]$Mode = "Survival",
    [string]$GameRoot = "",
    [string]$UserRoot = ""
)

$ErrorActionPreference = "Stop"
$ScriptRoot = if ([string]::IsNullOrWhiteSpace($PSScriptRoot)) { (Get-Location).Path } else { $PSScriptRoot }
$DiscoveryScript = Join-Path $ScriptRoot "steam_discovery.ps1"
. $DiscoveryScript
if ([string]::IsNullOrWhiteSpace($GameRoot)) { $GameRoot = Find-ScrapMechanicGameRoot }
if ([string]::IsNullOrWhiteSpace($GameRoot)) { throw "Scrap Mechanic was not found in any registered Steam library." }
$CleanupScript = Join-Path $ScriptRoot "uninstall_companion_mod.ps1"
$OverlayScript = Join-Path $ScriptRoot "companion_overlay.py"
$LocalId = "9bf45fa0-6dc8-4f21-9eb7-cdb06f4312c0"
$BackupRoot = Join-Path $env:LOCALAPPDATA "ScrapMechanicMapCompanion\Backups"

if (Get-Process ScrapMechanic -ErrorAction SilentlyContinue) {
    throw "Close Scrap Mechanic before setting up the external Map Companion."
}
if (-not (Test-Path -LiteralPath $CleanupScript -PathType Leaf)) {
    throw "Cleanup script not found: $CleanupScript"
}
if (-not (Test-Path -LiteralPath $OverlayScript -PathType Leaf)) {
    throw "External overlay not found: $OverlayScript"
}

# Remove every older UI/hotkey/telemetry integration first. The current
# companion is external and reads only a small player-view record from the
# running process; it does not patch game scripts or write process memory.
& $CleanupScript -GameRoot $GameRoot

New-Item -ItemType Directory -Path $BackupRoot -Force | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$userRoots = @()
if (-not [string]::IsNullOrWhiteSpace($UserRoot)) {
    $userRoots += $UserRoot
} else {
    $usersFolder = Join-Path $env:APPDATA "Axolot Games\Scrap Mechanic\User"
    if (Test-Path -LiteralPath $usersFolder -PathType Container) {
        $userRoots += Get-ChildItem -LiteralPath $usersFolder -Directory -Filter "User_*" -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty FullName
    }
}
foreach ($root in $userRoots) {
    $modsRoot = [System.IO.Path]::GetFullPath((Join-Path $root "Mods"))
    $legacyCustom = [System.IO.Path]::GetFullPath((Join-Path $modsRoot $LocalId))
    if (Test-Path -LiteralPath $legacyCustom -PathType Container) {
        if (-not $legacyCustom.StartsWith($modsRoot + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to move an unexpected Custom Game path: $legacyCustom"
        }
        $archive = Join-Path $BackupRoot "Disabled-CustomGame-$stamp"
        if (Test-Path -LiteralPath $archive) { $archive += "-$([guid]::NewGuid().ToString('N').Substring(0,8))" }
        Move-Item -LiteralPath $legacyCustom -Destination $archive
        Write-Host "Archived obsolete Custom Game package to: $archive" -ForegroundColor Green
    }
}

$python = Get-Command python.exe -ErrorAction SilentlyContinue
if (-not $python) {
    throw "Python was not found. Install the editor requirements before using the external companion."
}
& $python.Source $OverlayScript --check-only
if ($LASTEXITCODE -ne 0) {
    throw "The external companion dependency check failed with exit code $LASTEXITCODE."
}

Write-Host "External Map Companion is ready." -ForegroundColor Green
Write-Host "Read-only live player tracking is enabled; no game script hook is installed." -ForegroundColor Green
Write-Host "No developer launch flag is used." -ForegroundColor Green
Write-Host "Use 'Launch Scrap Mechanic with Map Companion.cmd' for a normal Steam launch plus the external overlay." -ForegroundColor Yellow

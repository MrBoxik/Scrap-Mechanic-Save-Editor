param([string]$GameRoot = "")

$ErrorActionPreference = "Stop"
$ScriptRoot = if ([string]::IsNullOrWhiteSpace($PSScriptRoot)) { (Get-Location).Path } else { $PSScriptRoot }
$DiscoveryScript = Join-Path $ScriptRoot "steam_discovery.ps1"
. $DiscoveryScript
if ([string]::IsNullOrWhiteSpace($GameRoot)) { $GameRoot = Find-ScrapMechanicGameRoot }
if ([string]::IsNullOrWhiteSpace($GameRoot)) { throw "Scrap Mechanic was not found in any registered Steam library." }
$BeginMarker = "-- MAP COMPANION INTEGRATION BEGIN"
$EndMarker = "-- MAP COMPANION INTEGRATION END"
$LogBookBeginMarker = "-- MAP COMPANION LOGBOOK BRIDGE BEGIN"
$LogBookEndMarker = "-- MAP COMPANION LOGBOOK BRIDGE END"
$playerScript = Join-Path $GameRoot "Survival\Scripts\game\SurvivalPlayer.lua"
$gameScript = Join-Path $GameRoot "Survival\Scripts\game\SurvivalGame.lua"
$characterScript = Join-Path $GameRoot "Survival\Scripts\game\characters\MechanicCharacter.lua"
$logBookScript = Join-Path $GameRoot "Survival\Scripts\game\tools\LogBook.lua"
$integrationTarget = Join-Path $GameRoot "Survival\Scripts\MapCompanion"
$survivalPackageTarget = Join-Path $GameRoot "Survival\MapCompanion"
$BackupRoot = Join-Path $env:LOCALAPPDATA "ScrapMechanicMapCompanion\Backups"
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Move-VerifiedCompanionDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$Target,
        [Parameter(Mandatory = $true)][string]$AllowedParent,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if (-not (Test-Path -LiteralPath $Target -PathType Container)) { return }
    $resolvedParent = [System.IO.Path]::GetFullPath($AllowedParent).TrimEnd('\')
    $resolvedTarget = [System.IO.Path]::GetFullPath($Target).TrimEnd('\')
    $expected = Join-Path $resolvedParent "MapCompanion"
    if (-not $resolvedTarget.Equals($expected, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to move an unexpected companion path: $resolvedTarget"
    }

    New-Item -ItemType Directory -Path $BackupRoot -Force | Out-Null
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $destination = Join-Path $BackupRoot "Disabled-$Label-$stamp"
    if (Test-Path -LiteralPath $destination) {
        $destination += "-$([guid]::NewGuid().ToString('N').Substring(0,8))"
    }
    Move-Item -LiteralPath $resolvedTarget -Destination $destination
    Write-Host "Archived obsolete companion files to: $destination" -ForegroundColor Green
}

if (Get-Process ScrapMechanic -ErrorAction SilentlyContinue) {
    throw "Close Scrap Mechanic before uninstalling the Map Companion."
}
if (-not (Test-Path -LiteralPath $playerScript -PathType Leaf) -or -not (Test-Path -LiteralPath $gameScript -PathType Leaf) -or -not (Test-Path -LiteralPath $characterScript -PathType Leaf)) {
    throw "The ordinary Survival scripts were not found below: $GameRoot"
}

foreach ($script in @($playerScript, $gameScript, $characterScript)) {
    $text = [System.IO.File]::ReadAllText($script)
    if ($text.Contains($BeginMarker) -or $text.Contains($EndMarker)) {
        $pattern = "(?ms)\r?\n?" + [regex]::Escape($BeginMarker) + ".*?" + [regex]::Escape($EndMarker) + "\r?\n?"
        $clean = [regex]::Replace($text, $pattern, "`r`n").TrimEnd() + "`r`n"
        [System.IO.File]::WriteAllText($script, $clean, $Utf8NoBom)
    }
}
if (Test-Path -LiteralPath $logBookScript -PathType Leaf) {
    $logBookText = [System.IO.File]::ReadAllText($logBookScript)
    if ($logBookText.Contains($LogBookBeginMarker) -or $logBookText.Contains($LogBookEndMarker)) {
        $logBookPattern = "(?ms)\r?\n?" + [regex]::Escape($LogBookBeginMarker) + ".*?" + [regex]::Escape($LogBookEndMarker) + "\r?\n?"
        $logBookClean = [regex]::Replace($logBookText, $logBookPattern, "`r`n").TrimEnd() + "`r`n"
        [System.IO.File]::WriteAllText($logBookScript, $logBookClean, $Utf8NoBom)
    }
}
Move-VerifiedCompanionDirectory -Target $integrationTarget -AllowedParent (Join-Path $GameRoot "Survival\Scripts") -Label "Survival-Scripts-MapCompanion"
Move-VerifiedCompanionDirectory -Target $survivalPackageTarget -AllowedParent (Join-Path $GameRoot "Survival") -Label "Survival-MapCompanion"
foreach ($resolution in @("1280x720", "1920x1080", "2560x1440", "3840x2160")) {
    $resolutionRoot = Join-Path $GameRoot "Data\Gui\Resolutions\$resolution"
    $guiMapTarget = Join-Path $resolutionRoot "MapCompanion"
    Move-VerifiedCompanionDirectory -Target $guiMapTarget -AllowedParent $resolutionRoot -Label "Gui-$resolution-MapCompanion"
}
Write-Host "Confirmed that the in-game Map Companion integration and legacy Logbook bridge are absent." -ForegroundColor Green
Write-Host "Existing save databases were not changed."

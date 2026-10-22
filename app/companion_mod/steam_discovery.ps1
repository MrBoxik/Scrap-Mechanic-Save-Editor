function Get-SteamRoots {
    $roots = [System.Collections.Generic.List[string]]::new()
    foreach ($override in @($env:STEAM_ROOT, $env:STEAM_PATH)) {
        if (-not [string]::IsNullOrWhiteSpace($override)) { $roots.Add($override) }
    }
    foreach ($entry in @(
        @{ Hive = 'HKCU:'; Key = 'Software\Valve\Steam'; Names = @('SteamPath', 'InstallPath') },
        @{ Hive = 'HKLM:'; Key = 'Software\Valve\Steam'; Names = @('InstallPath', 'SteamPath') },
        @{ Hive = 'HKLM:'; Key = 'Software\WOW6432Node\Valve\Steam'; Names = @('InstallPath', 'SteamPath') }
    )) {
        try {
            $item = Get-ItemProperty -LiteralPath (Join-Path $entry.Hive $entry.Key) -ErrorAction Stop
            foreach ($name in $entry.Names) {
                $value = $item.$name
                if (-not [string]::IsNullOrWhiteSpace($value)) { $roots.Add([string]$value); break }
            }
        } catch {}
    }
    foreach ($programRoot in @(${env:ProgramFiles(x86)}, $env:ProgramFiles)) {
        if (-not [string]::IsNullOrWhiteSpace($programRoot)) { $roots.Add((Join-Path $programRoot 'Steam')) }
    }
    return $roots | ForEach-Object { [System.IO.Path]::GetFullPath($_) } | Select-Object -Unique
}

function Get-SteamLibraries {
    $libraries = [System.Collections.Generic.List[string]]::new()
    foreach ($root in Get-SteamRoots) {
        $libraries.Add($root)
        $vdf = Join-Path $root 'steamapps\libraryfolders.vdf'
        if (-not (Test-Path -LiteralPath $vdf -PathType Leaf)) { continue }
        $text = [System.IO.File]::ReadAllText($vdf)
        foreach ($match in [regex]::Matches($text, '"path"\s+"([^"]+)"', 'IgnoreCase')) {
            $libraries.Add($match.Groups[1].Value.Replace('\\', '\'))
        }
    }
    return $libraries | ForEach-Object { [System.IO.Path]::GetFullPath($_) } | Select-Object -Unique
}

function Find-ScrapMechanicGameRoot {
    if (-not [string]::IsNullOrWhiteSpace($env:SCRAP_MECHANIC_GAME_ROOT)) {
        $override = [System.IO.Path]::GetFullPath($env:SCRAP_MECHANIC_GAME_ROOT)
        if (Test-Path -LiteralPath (Join-Path $override 'Release\ScrapMechanic.exe') -PathType Leaf) { return $override }
    }
    foreach ($library in Get-SteamLibraries) {
        $manifest = Join-Path $library 'steamapps\appmanifest_387990.acf'
        $installDir = 'Scrap Mechanic'
        if (Test-Path -LiteralPath $manifest -PathType Leaf) {
            $match = [regex]::Match([System.IO.File]::ReadAllText($manifest), '"installdir"\s+"([^"]+)"', 'IgnoreCase')
            if ($match.Success) { $installDir = $match.Groups[1].Value }
        }
        $candidate = Join-Path $library (Join-Path 'steamapps\common' $installDir)
        if (Test-Path -LiteralPath (Join-Path $candidate 'Release\ScrapMechanic.exe') -PathType Leaf) {
            return [System.IO.Path]::GetFullPath($candidate)
        }
    }
    return $null
}

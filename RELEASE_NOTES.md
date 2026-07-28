# Scrap Mechanic Save Editor 4.4.1

Version 4.4.1 is a release-polish update for the offline desktop application.

## Visual and wording polish

- Uses the new square application artwork for the Windows executable and in-app branding.
- Replaces the squeezed **SM** badge with a crisp 1:1 icon.
- Clarifies the difference between **Back up current save**, **Save edited copy**, and **Save to game**.
- Gives backup history entries friendly labels such as **Automatic before save** and **Edited copy**.
- Simplifies page titles and shortens clipped search, tab, and guidance text.
- Removes the horizontal scrollbar from inventory category filters.
- Improves the advanced-settings warning, unlock warnings, sidebar status, and backup guidance.
- Rounds displayed health and oxygen values to readable two-decimal precision without changing saved precision until edited.

## Safety

Backups remain outside Scrap Mechanic's save folders under
`%LOCALAPPDATA%\ScrapMechanicSaveEditor\Backups`. **Save to game** still creates
an automatic backup of the untouched database before writing any staged changes.

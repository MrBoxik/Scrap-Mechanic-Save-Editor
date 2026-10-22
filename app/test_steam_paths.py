from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scrap_editor_app import steam_paths


class SteamPathTests(unittest.TestCase):
    def tearDown(self) -> None:
        steam_paths.steam_roots.cache_clear()
        steam_paths.steam_library_roots.cache_clear()

    def test_current_library_vdf_and_manifest_are_respected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = root / "Steam"
            library = root / "Games"
            (client / "steamapps").mkdir(parents=True)
            (library / "steamapps" / "common" / "Custom Scrap" / "Release").mkdir(parents=True)
            (library / "steamapps" / "common" / "Custom Scrap" / "Survival").mkdir()
            (library / "steamapps" / "common" / "Custom Scrap" / "Data").mkdir()
            (library / "steamapps" / "common" / "Custom Scrap" / "Release" / "ScrapMechanic.exe").touch()
            (client / "steamapps" / "libraryfolders.vdf").write_text(
                f'"libraryfolders"\n{{\n  "1" {{ "path" "{str(library).replace(chr(92), chr(92) * 2)}" }}\n}}',
                encoding="utf-8",
            )
            (library / "steamapps" / "appmanifest_387990.acf").write_text(
                '"AppState" { "installdir" "Custom Scrap" }', encoding="utf-8"
            )
            with patch.dict(os.environ, {"STEAM_ROOT": str(client)}, clear=False):
                steam_paths.steam_roots.cache_clear()
                steam_paths.steam_library_roots.cache_clear()
                self.assertEqual(steam_paths.find_game_root(), (library / "steamapps" / "common" / "Custom Scrap").resolve())

    def test_explicit_game_override_is_first_and_validated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            game = Path(directory) / "Portable Game"
            (game / "Release").mkdir(parents=True)
            (game / "Survival").mkdir()
            (game / "Data").mkdir()
            (game / "Release" / "ScrapMechanic.exe").touch()
            with patch.dict(os.environ, {"SCRAP_MECHANIC_GAME_ROOT": str(game)}, clear=False):
                self.assertEqual(steam_paths.find_game_root(), game.resolve())

    def test_user_directories_do_not_assume_a_steam_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            appdata = Path(directory)
            user_root = appdata / "Axolot Games" / "Scrap Mechanic" / "User"
            (user_root / "User_111").mkdir(parents=True)
            (user_root / "User_222").mkdir()
            with patch.dict(os.environ, {"APPDATA": str(appdata)}, clear=False):
                self.assertEqual(
                    [path.name for path in steam_paths.scrap_user_directories()],
                    ["User_111", "User_222"],
                )


if __name__ == "__main__":
    unittest.main()

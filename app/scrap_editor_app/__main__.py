from __future__ import annotations

import argparse


def main() -> int:
    parser = argparse.ArgumentParser(description="Scrap Mechanic Save Editor desktop UI")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument(
        "--map-companion",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    args, remaining = parser.parse_known_args()
    if args.map_companion:
        from companion_mod.companion_overlay import main as companion_main

        return companion_main(remaining)
    if remaining:
        parser.error(f"unrecognized arguments: {' '.join(remaining)}")
    from .single_instance import SingleInstance

    instance = SingleInstance("Local\\ScrapMechanicSaveEditor.Desktop")
    if not instance.acquired:
        instance.activate_existing_window()
        return 0
    from .startup_splash import StartupSplash

    splash = StartupSplash()
    splash.start()
    try:
        from .desktop import launch

        return launch(debug=args.debug, startup_splash=splash)
    finally:
        splash.close()
        instance.close()


if __name__ == "__main__":
    raise SystemExit(main())

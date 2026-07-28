from __future__ import annotations

import argparse

from .desktop import launch


def main() -> int:
    parser = argparse.ArgumentParser(description="Scrap Mechanic Save Editor desktop UI")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    return launch(debug=args.debug)


if __name__ == "__main__":
    raise SystemExit(main())

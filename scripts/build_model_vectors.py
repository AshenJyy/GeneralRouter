"""Generate four-dimensional model profiles from a recorded AA snapshot."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from generalrouter.profile_vectors import build_profiles


def _read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", required=True, type=Path)
    parser.add_argument("--components", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    try:
        profiles = build_profiles(
            _read_json(arguments.candidates), _read_json(arguments.components)
        )
        encoded = json.dumps(profiles, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(encoded, encoding="utf-8")
    except (OSError, ValueError, TypeError) as error:
        parser.error(str(error))
    print(f"Wrote {len(profiles['profiles'])} model profiles to {arguments.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

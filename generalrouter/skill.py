"""Load the existing planner contract and its recorded AA anchors."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

from .capabilities import DIMENSIONS


def load_script(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"Cannot load planner skill script: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PlannerSkill:
    def __init__(self, root: Path):
        self.root = root
        self.calibration_path = root / "references" / "calibration.json"
        self.schema = json.loads((root / "references" / "task-profile.schema.json").read_text(encoding="utf-8"))
        self.calibration = json.loads(self.calibration_path.read_text(encoding="utf-8-sig"))
        if self.calibration.get("schema_version") != "aa-four-dimensional-calibration.v1":
            raise ValueError("The planner skill needs the four-dimensional reference calibration")
        if self.calibration.get("dimensions") != list(DIMENSIONS):
            raise ValueError("Planner skill and model profiles must use the same four dimensions")
        self._builder = load_script(root / "scripts" / "build_prompt.py", "generalrouter_planner_prompt")
        self._normalizer = load_script(root / "scripts" / "normalize_profile.py", "generalrouter_profile_normalizer")

    def prompt(self) -> str:
        return self._builder.build_prompt()

    def normalize(self, profile: dict[str, Any]) -> dict[str, Any]:
        return self._normalizer.normalize_profile(profile, self.calibration_path)

    def parse(self, text: str) -> dict[str, Any]:
        text = text.strip()
        if text.startswith("```") and text.endswith("```"):
            lines = text.splitlines()
            text = "\n".join(lines[1:-1])
        value = json.loads(text, object_pairs_hook=self._normalizer.reject_duplicate_keys)
        if not isinstance(value, dict):
            raise ValueError("Planner must return a task-profile.v3 JSON object")
        return value

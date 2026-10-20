"""Local routing settings and execution hints for the main agent."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]

DEFAULTS: dict[str, Any] = {
    "models_file": "data/model-vectors.v0.1.json",
    "skill_dir": "skills/generalrouter-task-profile",
    "intelligence_prior": 0.40,
    "missing_penalty": 0.25,
    "preferred_configuration": "gpt-6-astra:max",
}


def load_config(path: Path | None = None) -> dict[str, Any]:
    """Load routing options, resolving relative paths beside the config file."""
    if path is None:
        local_path = PROJECT_ROOT / "config.local.json"
        path = local_path if local_path.is_file() else PROJECT_ROOT / "config.example.json"
    path = Path(path).resolve()
    supplied = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(supplied, dict):
        raise ValueError("Routing configuration must be a JSON object")
    unknown = set(supplied) - set(DEFAULTS)
    if unknown:
        raise ValueError(f"Unknown configuration fields: {', '.join(sorted(unknown))}")

    config = {**DEFAULTS, **supplied}
    for setting in ("models_file", "skill_dir"):
        value = config[setting]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{setting} must be a nonempty path string")
        candidate = Path(value)
        config[setting] = (
            candidate.resolve()
            if candidate.is_absolute()
            else (path.parent / candidate).resolve()
        )
    for setting in ("intelligence_prior", "missing_penalty"):
        value = config[setting]
        if (
            type(value) not in (int, float)
            or not math.isfinite(value)
            or not 0 <= value <= 1
        ):
            raise ValueError(f"{setting} must be a finite number between 0 and 1")
        config[setting] = float(value)
    config["preferred_configuration"] = _configuration_preference(
        config["preferred_configuration"]
    )
    config["config_path"] = path
    return config


def _configuration_preference(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("preferred_configuration must be a nonempty string")
    return value.strip()


def execution_hint(candidate: dict[str, Any]) -> dict[str, Any]:
    """Describe the AA selection; the main agent resolves and invokes its tools.

    The profile's reference effort is a preference. The host uses its default
    when its invocation tool cannot set that effort, and records the mismatch.
    """
    for key in ("id", "name", "slug"):
        if not isinstance(candidate.get(key), str) or not candidate[key].strip():
            raise ValueError(f"Execution hint requires a nonempty candidate {key}")
    preferred_effort = candidate.get("reference_effort")
    if preferred_effort is not None:
        if not isinstance(preferred_effort, str) or not preferred_effort.strip():
            raise ValueError("Candidate reference_effort must be a nonempty string")
        preferred_effort = preferred_effort.strip().lower()
    else:
        matches = re.findall(
            r"\b(xhigh|max|high|medium|low|minimal|none)\b",
            candidate["name"],
            flags=re.IGNORECASE,
        )
        preferred_effort = matches[-1].lower() if matches else None
    return {
        "configuration_id": candidate.get("configuration_id"),
        "aa_id": candidate["id"],
        "aa_name": candidate["name"],
        "aa_slug": candidate["slug"],
        "model_creator": candidate.get("model_creator"),
        "access_channel": candidate.get("access_channel"),
        "preferred_effort": preferred_effort,
        "effort_policy": "mapped_if_supported_else_default",
        "effort_fallback": "default",
        "model_resolution": "main_agent",
    }

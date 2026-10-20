"""Load recorded four-dimensional model profiles without effort interpolation."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any


DIMENSIONS = (
    "agentic",
    "coding",
    "knowledge_document",
    "scientific_reasoning",
)

# This prior applies across the four dimensions; it is not a fifth capability.
AA_COMPOSITE_WEIGHTS = {
    "agentic": 0.30,
    "coding": 0.20,
    "knowledge_document": 0.30,
    "scientific_reasoning": 0.20,
}


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty string")
    return value


def _capabilities(value: Any, label: str) -> dict[str, float | None]:
    if not isinstance(value, dict) or set(value) != set(DIMENSIONS):
        raise ValueError(f"{label} must contain exactly the four routing dimensions")
    result = {}
    for dimension in DIMENSIONS:
        score = value[dimension]
        if score is None:
            result[dimension] = None
            continue
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise ValueError(f"{label}.{dimension} must be a number or null")
        score = float(score)
        if not math.isfinite(score) or not 0.0 <= score <= 1.0:
            raise ValueError(f"{label}.{dimension} must be finite and between 0 and 1")
        result[dimension] = score
    return result


def load_candidates(path: str | Path) -> tuple[list[dict], dict]:
    """Load enabled exact model/effort profiles and their source snapshot.

    Scores are already derived from recorded benchmark components. No runtime
    min-max fitting, missing-value substitution, or cross-effort copying occurs.
    Disabled profiles remain part of the file hash and recorded model count.
    """
    file_bytes = Path(path).read_bytes()
    artifact = json.loads(file_bytes.decode("utf-8-sig"))
    if not isinstance(artifact, dict):
        raise ValueError("Model vector data must be a JSON object")
    if artifact.get("schema_version") != "generalrouter-model-vectors.v1":
        raise ValueError("Expected generalrouter-model-vectors.v1 model data")
    if artifact.get("dimension_order") != list(DIMENSIONS):
        raise ValueError("Model vector dimension order must match the four routing dimensions")
    derivation = artifact.get("derivation")
    if not isinstance(derivation, dict):
        raise ValueError("Model vector data must record its derivation")
    profiles = artifact.get("profiles")
    if not isinstance(profiles, list) or not profiles:
        raise ValueError("Model vector data must contain a nonempty profiles list")

    candidates = []
    seen_aa_ids = set()
    seen_configurations = set()
    for index, profile in enumerate(profiles):
        label = f"profiles[{index}]"
        if not isinstance(profile, dict):
            raise ValueError(f"{label} must be an object")
        identity = {
            key: _text(profile.get(key), f"{label}.{key}")
            for key in (
                "aa_id", "aa_name", "aa_slug", "configuration_id",
                "reference_effort", "access_channel",
            )
        }
        aa_id = identity["aa_id"]
        configuration_id = identity["configuration_id"]
        if aa_id in seen_aa_ids:
            raise ValueError(f"Duplicate AA model ID: {aa_id}")
        if configuration_id in seen_configurations:
            raise ValueError(f"Duplicate configuration ID: {configuration_id}")
        seen_aa_ids.add(aa_id)
        seen_configurations.add(configuration_id)
        if profile.get("dimension_order") != list(DIMENSIONS):
            raise ValueError(f"{label}.dimension_order must match the artifact")
        enabled = profile.get("routing_enabled")
        if not isinstance(enabled, bool):
            raise ValueError(f"{label}.routing_enabled must be a boolean")
        capabilities = _capabilities(profile.get("capabilities"), f"{label}.capabilities")
        if not enabled:
            continue

        raw = profile.get("raw")
        raw = raw if isinstance(raw, dict) else {}
        candidates.append(
            {
                "id": aa_id,
                "name": identity["aa_name"],
                "slug": identity["aa_slug"],
                "configuration_id": configuration_id,
                "model_family": profile.get("model_family"),
                "reference_effort": identity["reference_effort"],
                "access_channel": identity["access_channel"],
                "routing_enabled": enabled,
                "capabilities": capabilities,
                "model_creator": raw.get("model_creator", raw.get("creator")),
                "profile_status": profile.get("profile_status"),
                "source_status": profile.get("source_status"),
                "source": profile.get("source"),
                "identity": profile.get("identity"),
                "derived_categories": profile.get("derived_categories"),
                "recorded_effort": profile.get("recorded_effort"),
                "effort_policy": "mapped_if_supported_else_default",
            }
        )

    if not candidates:
        raise ValueError("Model vector data has no enabled routing candidates")
    snapshot = {
        "schema_version": artifact["schema_version"],
        "model_count": len(profiles),
        "enabled_model_count": len(candidates),
        "models_sha256": hashlib.sha256(file_bytes).hexdigest(),
        "dimension_order": list(DIMENSIONS),
        "normalization": derivation,
    }
    return candidates, snapshot

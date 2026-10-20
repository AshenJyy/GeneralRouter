"""Build four capability groups from recorded AA benchmark components.

These are project-derived groups using AA's within-category proportions. They
are not AA's separate Coding/Agentic indices, and do not calibrate task weights.
The existing nine-dimensional router uses a different schema and is unchanged.
"""

from __future__ import annotations

from copy import deepcopy
import math
from typing import Any


DIMENSION_ORDER = (
    "agentic",
    "coding",
    "knowledge_document",
    "scientific_reasoning",
)

# Both page representations below have been observed in saved AA public pages.
# No aliases are inferred from display labels or from an Intelligence score.
COMPONENT_PATHS = {
    "briefcase_elo": ("briefcaseElo", "briefcaseBreakdown.overall.elo"),
    "gdpval_elo": ("gdpval",),
    "automationbench": ("automationBenchPartialScore",),
    "terminalbench_4_0": ("terminalBench40",),
    "scicode": ("scicode",),
    "omniscience_accuracy": (
        "omniscienceAccuracy",
        "omniscienceBreakdown.accuracy",
    ),
    "omniscience_hallucination_rate": (
        "omniscienceHallucinationRate",
        "omniscienceBreakdown.hallucinationRate",
    ),
    "gdp_pdf_all_pass": ("gdpPdfAllPass",),
    "aa_lcr": ("lcr",),
    "hle": ("hle",),
    "critpt": ("critpt",),
}

# Original Intelligence component weights are used only *within* each group.
# An eventual task vector can choose different proportions between the groups.
CATEGORY_COMPONENTS = {
    "agentic": (
        ("briefcase_elo", 0.15),
        ("gdpval_elo", 0.10),
        ("automationbench", 0.05),
    ),
    "coding": (("terminalbench_4_0", 0.10), ("scicode", 0.10)),
    "knowledge_document": (
        ("omniscience_accuracy", 0.10),
        ("omniscience_nonhallucination_rate", 0.05),
        ("gdp_pdf_all_pass", 0.10),
        ("aa_lcr", 0.05),
    ),
    "scientific_reasoning": (("hle", 0.10), ("critpt", 0.10)),
}

CATEGORY_TOTALS = {
    "agentic": 0.30,
    "coding": 0.20,
    "knowledge_document": 0.30,
    "scientific_reasoning": 0.20,
}


def _object(value: Any, label: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty string")
    return value


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number or null")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


def _at_path(record: dict, path: str) -> Any:
    value: Any = record
    for key in path.split("."):
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def _component(record: dict, component: str, aa_id: str) -> tuple[Any, dict]:
    paths = COMPONENT_PATHS[component]
    observed = []
    for path in paths:
        value = _at_path(record, path)
        if value is not None:
            numeric = _number(value, f"{aa_id}.{path}")
            observed.append({"field_path": path, "raw_value": value, "value": numeric})
    if not observed:
        return None, {"field_path": None, "checked_paths": list(paths)}
    first = observed[0]
    if any(
        not math.isclose(item["value"], first["value"], rel_tol=1e-12, abs_tol=1e-12)
        for item in observed[1:]
    ):
        raise ValueError(f"Conflicting AA field representations for {aa_id}.{component}")
    return first["raw_value"], {
        "field_path": first["field_path"],
        "checked_paths": list(paths),
        "observed_paths": [item["field_path"] for item in observed],
    }


def _normalize(component: str, value: Any, aa_id: str) -> float | None:
    if value is None:
        return None
    numeric = _number(value, f"{aa_id}.{component}")
    if component in ("briefcase_elo", "gdpval_elo"):
        return min(1.0, max(0.0, (numeric - 500.0) / 2000.0))
    if not 0.0 <= numeric <= 1.0:
        raise ValueError(
            f"{aa_id}.{component} must be a fraction between 0 and 1; "
            "percentage conversion is not inferred"
        )
    return numeric


def _categories(normalized: dict) -> dict:
    result = {}
    for dimension in DIMENSION_ORDER:
        components = CATEGORY_COMPONENTS[dimension]
        total = CATEGORY_TOTALS[dimension]
        missing = [name for name, _ in components if normalized[name] is None]
        known_weight = math.fsum(
            weight for name, weight in components if normalized[name] is not None
        )
        score = None
        if not missing:
            score = math.fsum(
                weight * normalized[name] for name, weight in components
            ) / total
            score = min(1.0, max(0.0, score))
        result[dimension] = {
            "required_components": [name for name, _ in components],
            "component_weights": {name: weight / total for name, weight in components},
            "missing_components": missing,
            "coverage": min(1.0, max(0.0, known_weight / total)),
            "score": score,
            "status": "complete" if not missing else "missing_components",
        }
    return result


def _profile(candidate: dict, row: dict | None) -> dict:
    aa_id = candidate["aa_id"]
    source_status = row["status"] if row is not None else "not_retrieved"
    raw = row.get("raw") if row is not None else None
    record = raw if isinstance(raw, dict) else {}
    if raw is not None and record.get("id") != aa_id:
        raise ValueError(f"Raw AA model ID does not match requested record {aa_id}")

    raw_components = {}
    normalized_components = {}
    component_sources = {}
    for component in COMPONENT_PATHS:
        value, source = _component(record, component, aa_id)
        raw_components[component] = value
        normalized_components[component] = _normalize(component, value, aa_id)
        source["transform"] = (
            "clamp((raw - 500) / 2000, 0, 1)"
            if component in ("briefcase_elo", "gdpval_elo")
            else "identity; raw must be in [0, 1]"
        )
        component_sources[component] = source

    hallucination = normalized_components["omniscience_hallucination_rate"]
    normalized_components["omniscience_nonhallucination_rate"] = (
        None if hallucination is None else 1.0 - hallucination
    )
    component_sources["omniscience_nonhallucination_rate"] = {
        "derived_from": "omniscience_hallucination_rate",
        "transform": "1 - omniscience_hallucination_rate",
    }

    categories = _categories(normalized_components)
    capabilities = {dimension: categories[dimension]["score"] for dimension in DIMENSION_ORDER}
    intelligence = record.get("intelligenceIndex")
    if intelligence is not None:
        _number(intelligence, f"{aa_id}.intelligenceIndex")

    identity = {
        "requested": {
            "aa_id": aa_id,
            "aa_name": candidate["aa_name"],
            "aa_slug": candidate["aa_slug"],
        },
        "recorded": {
            "aa_id": record.get("id"),
            "aa_name": record.get("name"),
            "aa_slug": record.get("slug"),
        },
        "name_matches": record.get("name") == candidate["aa_name"] if record else None,
        "slug_matches": record.get("slug") == candidate["aa_slug"] if record else None,
        "status": "not_retrieved",
    }
    if record:
        identity["status"] = (
            "matching_id_name_slug"
            if identity["name_matches"] and identity["slug_matches"]
            else "matching_id_metadata_differs"
        )

    result = deepcopy(candidate)
    result.update(
        {
            "dimension_order": list(DIMENSION_ORDER),
            "capabilities": capabilities,
            "vector": list(capabilities.values()),
            "derived_categories": categories,
            "raw_components": raw_components,
            "normalized_components": normalized_components,
            "component_sources": component_sources,
            "source_intelligence_index": {
                "value": intelligence,
                "field_path": "intelligenceIndex",
                "is_estimated": record.get("intelligenceIndexIsEstimated"),
                "used_as_component": False,
            },
            "identity": identity,
            "recorded_effort": deepcopy(record.get("effort")),
            "effort_policy": "mapped_if_supported_else_default",
            "effort_mapping_status": "not_resolved_by_profile_builder",
            "source": deepcopy(row.get("source", {})) if row is not None else {},
            "source_status": source_status,
            "profile_status": (
                "not_retrieved"
                if source_status == "not_retrieved"
                else "complete"
                if all(value is not None for value in capabilities.values())
                else "partial"
            ),
            "raw": deepcopy(raw),
        }
    )
    if row is None:
        result["source_error"] = "No snapshot row was collected for this AA model ID"
    elif row.get("error") is not None:
        result["source_error"] = row["error"]
    return result


def build_profiles(candidates: dict, snapshot: dict) -> dict:
    """Derive four-dimensional profiles from exact AA IDs without imputation.

    Candidates supply ``configurations``; an ``aa-component-snapshot.v1``
    supplies ``models`` containing unmodified public-page ``raw`` records.
    A missing component makes its entire category null. The observed weighted
    coverage is still recorded; partial categories are never renormalized.
    """
    _object(candidates, "candidates")
    _object(snapshot, "snapshot")
    if snapshot.get("schema_version") != "aa-component-snapshot.v1":
        raise ValueError("Expected an aa-component-snapshot.v1 snapshot")
    configurations = candidates.get("configurations")
    rows = snapshot.get("models")
    if not isinstance(configurations, list):
        raise ValueError("candidates.configurations must be a list")
    if not isinstance(rows, list):
        raise ValueError("snapshot.models must be a list")

    rows_by_id = {}
    for row in rows:
        _object(row, "snapshot model row")
        aa_id = _text(row.get("aa_id"), "snapshot row aa_id")
        if aa_id in rows_by_id:
            raise ValueError(f"Duplicate snapshot AA model ID: {aa_id}")
        if row.get("status") not in ("retrieved", "not_retrieved"):
            raise ValueError(f"Unknown snapshot status for {aa_id}")
        raw = row.get("raw")
        if row["status"] == "retrieved":
            _object(raw, f"retrieved raw record for {aa_id}")
            if raw.get("id") != aa_id:
                raise ValueError(f"Snapshot/raw AA model ID mismatch for {aa_id}")
        elif raw is not None:
            raise ValueError(f"A not_retrieved row must have raw=null: {aa_id}")
        rows_by_id[aa_id] = row

    profiles = []
    seen = set()
    for candidate in configurations:
        _object(candidate, "candidate configuration")
        for key in (
            "aa_id", "aa_name", "aa_slug", "model_family", "reference_effort", "access_channel"
        ):
            _text(candidate.get(key), f"candidate.{key}")
        if not isinstance(candidate.get("routing_enabled"), bool):
            raise ValueError("candidate.routing_enabled must be a boolean")
        aa_id = candidate["aa_id"]
        if aa_id in seen:
            raise ValueError(f"Duplicate candidate AA model ID: {aa_id}")
        seen.add(aa_id)
        profiles.append(_profile(candidate, rows_by_id.get(aa_id)))

    return {
        "schema_version": "generalrouter-model-vectors.v1",
        "dimension_order": list(DIMENSION_ORDER),
        "derivation": {
            "kind": "project_derived_aa_component_groups",
            "elo_normalization": "clamp((Elo - 500) / 2000, 0, 1)",
            "rate_normalization": "AA raw fractions in [0, 1]; no inferred percentage conversion",
            "missing_policy": "any missing component makes its category null; keep weighted coverage",
            "category_component_weights": {
                name: {component: weight / CATEGORY_TOTALS[name] for component, weight in parts}
                for name, parts in CATEGORY_COMPONENTS.items()
            },
            "official_independent_coding_agentic_indices": False,
            "task_weight_calibration": False,
            "intelligence_index_used_as_component": False,
            "runtime_effort_mapping": "not performed; mapped_if_supported_else_default",
        },
        "profiles": profiles,
    }

"""Normalize TaskProfile v3 planner estimates and recorded AA reference fits.

The local schema checks cover the vocabulary shipped with this skill, rather
than every JSON Schema feature. Planner estimates retain their supplied weights
and provenance; AA-anchor modes retain their exact recorded-weight binding.
No planner or worker model is called.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import deque
from pathlib import Path
from typing import Any

SKILL_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = SKILL_ROOT / "references" / "task-profile.schema.json"
CALIBRATION_PATH = SKILL_ROOT / "references" / "calibration.json"
DIMENSIONS = ["agentic", "coding", "knowledge_document", "scientific_reasoning"]
CALIBRATION_VERSION = "aa-four-dimensional-calibration.v1"


class ProfileError(ValueError):
    """Input violates the task-profile contract."""


def validate_node(value: Any, rule: dict[str, Any], path: str = "$") -> None:
    expected = rule["type"]
    allowed_types = expected if isinstance(expected, list) else [expected]
    checks = {
        "object": lambda x: type(x) is dict,
        "array": lambda x: type(x) is list,
        "string": lambda x: type(x) is str,
        "integer": lambda x: type(x) is int,
        "boolean": lambda x: type(x) is bool,
        "number": lambda x: type(x) in (int, float) and math.isfinite(x),
        "null": lambda x: x is None,
    }
    if any(kind not in checks for kind in allowed_types):
        raise ProfileError(f"Unsupported shipped schema type: {expected}")
    if not any(checks[kind](value) for kind in allowed_types):
        raise ProfileError(f"{path}: expected {expected}")
    if "enum" in rule and value not in rule["enum"]:
        raise ProfileError(f"{path}: value must be one of {rule['enum']!r}")
    if type(value) in (int, float):
        if "minimum" in rule and value < rule["minimum"]:
            raise ProfileError(f"{path}: must be at least {rule['minimum']}")
        if "maximum" in rule and value > rule["maximum"]:
            raise ProfileError(f"{path}: must be at most {rule['maximum']}")
    if expected == "object":
        properties = rule["properties"]
        missing = set(rule.get("required", [])) - set(value)
        extra = set(value) - set(properties)
        if missing:
            raise ProfileError(f"{path}: missing fields {sorted(missing)}")
        if extra and rule.get("additionalProperties") is False:
            raise ProfileError(f"{path}: unexpected fields {sorted(extra)}")
        for key, child in value.items():
            if key in properties:
                validate_node(child, properties[key], f"{path}.{key}")
    elif expected == "array":
        for index, child in enumerate(value):
            validate_node(child, rule["items"], f"{path}[{index}]")


def validate_graph(tasks: list[dict[str, Any]]) -> None:
    by_id: dict[str, dict[str, Any]] = {}
    for task in tasks:
        task_id = task["task_id"]
        for field in ("task_id", "objective", "deliverable", "rationale"):
            if not task[field].strip():
                raise ProfileError(f"Task {task_id!r}: {field} must not be blank")
        if task_id in by_id:
            raise ProfileError(f"Duplicate task_id: {task_id}")
        by_id[task_id] = task
        if any(not item.strip() for item in task["input_artifacts"]):
            raise ProfileError(f"Task {task_id}: input artifact references must not be blank")
    children: dict[str, list[str]] = {task_id: [] for task_id in by_id}
    indegree = {task_id: 0 for task_id in by_id}
    for task_id, task in by_id.items():
        dependencies = task["depends_on"]
        if len(dependencies) != len(set(dependencies)):
            raise ProfileError(f"Task {task_id}: duplicate dependency")
        for dependency in dependencies:
            if dependency not in by_id:
                raise ProfileError(f"Task {task_id}: unknown dependency {dependency!r}")
            if dependency == task_id:
                raise ProfileError(f"Task {task_id}: self dependency")
            children[dependency].append(task_id)
            indegree[task_id] += 1
    queue = deque(task_id for task_id, count in indegree.items() if count == 0)
    visited = 0
    while queue:
        task_id = queue.popleft()
        visited += 1
        for child in children[task_id]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    if visited != len(by_id):
        raise ProfileError("Task dependency graph contains a cycle")


def calibration_snapshot(calibration: dict[str, Any]) -> dict[str, Any]:
    if calibration.get("schema_version") != CALIBRATION_VERSION or calibration.get("dimensions") != DIMENSIONS:
        raise ProfileError("Calibration schema or dimension order does not match task-profile.v3")
    snapshot = calibration.get("snapshot")
    if not isinstance(snapshot, dict):
        raise ProfileError("Calibration must record its four-dimensional reference snapshot")
    model_count = snapshot.get("model_count")
    model_hash = snapshot.get("models_sha256")
    normalization = snapshot.get("normalization")
    vectors = snapshot.get("normalized_model_vectors")
    if type(model_count) is not int or model_count <= 0:
        raise ProfileError("Calibration snapshot model_count must be a positive integer")
    if not isinstance(model_hash, str) or not model_hash.strip():
        raise ProfileError("Calibration snapshot is missing its model-data hash")
    if not isinstance(normalization, dict) or not isinstance(normalization.get("kind"), str) or not normalization["kind"]:
        raise ProfileError("Calibration snapshot is missing its normalization kind")
    if not isinstance(vectors, dict) or len(vectors) != model_count:
        raise ProfileError("Calibration snapshot model count and vector records disagree")
    for model_id, vector in vectors.items():
        if not isinstance(model_id, str) or not model_id or not isinstance(vector, list) or len(vector) != len(DIMENSIONS):
            raise ProfileError("Calibration snapshot contains an invalid model-vector record")
        if any(value is not None and (type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1) for value in vector):
            raise ProfileError(f"Calibration snapshot {model_id}: invalid capability value")
    return {
        "schema_version": calibration["schema_version"],
        "model_count": model_count,
        "models_sha256": model_hash,
        "dimension_order": list(DIMENSIONS),
        "normalization_kind": normalization["kind"],
    }


def fitted_weights(anchor: dict[str, Any], snapshot_vectors: dict[str, Any]) -> dict[str, float]:
    anchor_id = anchor["anchor_id"]
    if anchor.get("status") != "regularized_reference_fit" or anchor.get("fit_kind") != "joint_four_dimension_reference":
        raise ProfileError(f"Anchor {anchor_id}: a complete four-dimensional joint reference fit is required")
    if anchor.get("active_dimensions") != DIMENSIONS:
        raise ProfileError(f"Anchor {anchor_id}: partial-dimension fits cannot replace a complete task vector")
    weights = anchor.get("weights")
    if not isinstance(weights, dict) or set(weights) != set(DIMENSIONS):
        raise ProfileError(f"Anchor {anchor_id}: exactly four recorded weights are required")
    if any(type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1 for value in weights.values()):
        raise ProfileError(f"Anchor {anchor_id}: weights must be finite and nonnegative")
    if abs(sum(weights.values()) - 1.0) > 1e-6:
        raise ProfileError(f"Anchor {anchor_id}: weights must sum to one")
    paired_models = anchor.get("paired_model_ids")
    if not isinstance(paired_models, list) or len(paired_models) < 4 or any(not isinstance(model_id, str) for model_id in paired_models):
        raise ProfileError(f"Anchor {anchor_id}: at least four complete paired reference models must be recorded")
    if len(paired_models) != len(set(paired_models)):
        raise ProfileError(f"Anchor {anchor_id}: duplicate paired model ID")
    for model_id in paired_models:
        vector = snapshot_vectors.get(model_id)
        if vector is None or any(value is None for value in vector):
            raise ProfileError(f"Anchor {anchor_id}: paired model {model_id!r} lacks a complete recorded vector")
    return {dimension: weights[dimension] for dimension in DIMENSIONS}


def normalize_profile(profile: Any, calibration_path: Path = CALIBRATION_PATH) -> dict[str, Any]:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8-sig"), object_pairs_hook=reject_duplicate_keys)
    validate_node(profile, schema)
    tasks = profile["tasks"]
    validate_graph(tasks)
    calibration = json.loads(Path(calibration_path).read_text(encoding="utf-8-sig"), object_pairs_hook=reject_duplicate_keys)
    if not isinstance(calibration, dict):
        raise ProfileError("Calibration must be a JSON object")
    snapshot = calibration_snapshot(calibration)
    recorded_anchors = calibration.get("anchors")
    if not isinstance(recorded_anchors, list):
        raise ProfileError("Calibration anchors must be an array")
    anchors: dict[str, dict[str, Any]] = {}
    for anchor in recorded_anchors:
        if not isinstance(anchor, dict) or not isinstance(anchor.get("anchor_id"), str) or not anchor["anchor_id"]:
            raise ProfileError("Calibration anchor ID must be a nonempty string")
        anchor_id = anchor["anchor_id"]
        if anchor_id in anchors:
            raise ProfileError(f"Duplicate calibration anchor: {anchor_id}")
        if not isinstance(anchor.get("task_id"), str) or not anchor["task_id"]:
            raise ProfileError(f"Anchor {anchor_id}: source task ID is missing")
        anchors[anchor_id] = anchor
    normalized_tasks = []
    for task in tasks:
        supplied_weights = task["task_weights"]
        anchor_id = task["calibration_anchor_id"]
        if task["weight_source"] == "uncalibrated":
            if anchor_id or task["source_task_id"] or any(value is not None for value in supplied_weights.values()):
                raise ProfileError(f"Task {task['task_id']}: uncalibrated requires empty source/anchor IDs and four null weights")
            weights = {dimension: None for dimension in DIMENSIONS}
            basis = {"status": "awaiting_calibration_evidence"}
        elif task["weight_source"] == "planner_estimated":
            if anchor_id or task["source_task_id"]:
                raise ProfileError(f"Task {task['task_id']}: planner_estimated requires empty source/AA anchor IDs")
            if any(value is None for value in supplied_weights.values()):
                raise ProfileError(f"Task {task['task_id']}: planner_estimated requires four numeric weights")
            if abs(sum(supplied_weights.values()) - 1.0) > 1e-6:
                raise ProfileError(f"Task {task['task_id']}: planner_estimated weights must sum to one")
            # Schema validation already requires finite values in [0, 1].
            # Preserve the planner's numbers; never replace them with an AA fit.
            weights = {dimension: supplied_weights[dimension] for dimension in DIMENSIONS}
            basis = {
                "status": "planner_estimated",
                "relationship": "semantic_requirement_estimate",
                "interpretation": "heuristic_planner_weights_not_aa_task_fit_or_measured_difficulty",
            }
        else:
            anchor = anchors.get(anchor_id)
            if anchor is None:
                raise ProfileError(f"Task {task['task_id']}: referenced anchor is not recorded")
            weights = fitted_weights(anchor, calibration["snapshot"]["normalized_model_vectors"])
            for dimension in DIMENSIONS:
                supplied = supplied_weights[dimension]
                if supplied is None or abs(supplied - weights[dimension]) > 1e-6:
                    raise ProfileError(f"Task {task['task_id']}: {dimension} must copy the recorded anchor weight")
            direct = task["weight_source"] == "aa_calibrated_anchor"
            if direct and task["source_task_id"] != anchor["task_id"]:
                raise ProfileError(f"Task {task['task_id']}: direct source binding requires the exact AA task ID")
            if not direct and task["source_task_id"]:
                raise ProfileError(f"Task {task['task_id']}: an analogous new task requires an empty source task ID")
            basis = {
                "status": "regularized_reference_fit",
                "relationship": "same_source_task" if direct else "analogy_transfer",
                "anchor_id": anchor_id,
                "source_task_id": anchor["task_id"],
                "fit_kind": anchor["fit_kind"],
                "source_url": anchor.get("source_url"),
                "bias": anchor.get("bias"),
                "scale": anchor.get("scale"),
                "active_dimensions": anchor["active_dimensions"],
                "paired_model_ids": anchor["paired_model_ids"],
                "fit_diagnostics": anchor.get("fit_diagnostics"),
                "interpretation": "small_sample_predictive_reference_not_semantic_requirement_proportions",
            }
        normalized_tasks.append({
            **task,
            "routing_weights": weights,
            "routing_vector": [weights[dimension] for dimension in DIMENSIONS],
            "calibration_basis": basis,
        })
    weight_sources = sorted({task["weight_source"] for task in tasks})
    aa_sources = {"aa_calibrated_anchor", "estimated_from_aa_anchor"}
    if weight_sources and set(weight_sources).issubset(aa_sources):
        weight_method = "aa_four_dimensional_regularized_reference_fit.v1"
    elif weight_sources == ["planner_estimated"]:
        weight_method = "planner_four_dimensional_semantic_estimate.v1"
    elif weight_sources == ["uncalibrated"]:
        weight_method = "uncalibrated_four_dimensional_weights.v1"
    else:
        weight_method = "four_dimensional_weights_by_task_source.v1"
    return {
        "schema_version": "normalized-task-profile.v3",
        "source_schema_version": profile["schema_version"],
        "weight_method": weight_method,
        "weight_sources": weight_sources,
        "calibration_snapshot": snapshot,
        "dimension_order": list(DIMENSIONS),
        "warnings": ["empty_plan"] if not tasks else [],
        "tasks": normalized_tasks,
    }


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProfileError(f"Duplicate JSON object key: {key}")
        result[key] = value
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", "-i", default="-", help="Raw planner JSON path, or - for stdin")
    parser.add_argument("--output", "-o", default="-", help="Normalized JSON path, or - for stdout")
    parser.add_argument("--calibration", type=Path, default=CALIBRATION_PATH, help="Recorded four-dimensional reference fit file")
    args = parser.parse_args()
    try:
        raw = sys.stdin.read() if args.input == "-" else Path(args.input).read_text(encoding="utf-8-sig")
        profile = json.loads(raw, object_pairs_hook=reject_duplicate_keys)
        result = normalize_profile(profile, args.calibration)
        serialized = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        if args.output == "-":
            if hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(encoding="utf-8")
            sys.stdout.write(serialized)
        else:
            Path(args.output).write_text(serialized, encoding="utf-8")
    except (OSError, ValueError, TypeError, KeyError) as exc:
        sys.stderr.write(f"TaskProfile error: {exc}\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

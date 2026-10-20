#!/usr/bin/env python3
"""Fit four-dimensional AA reference anchors offline, without model calls.

Model capabilities are already component-derived values in [0, 1]; no second
minmax is applied. The objective is mean((y - bias - X beta)**2) + ridge *
sum(beta**2), beta >= 0, with an unpenalized intercept. Fitted coefficients
are reference predictors, not intrinsic difficulty or semantic task labels.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any


DIMENSIONS = ("agentic", "coding", "knowledge_document", "scientific_reasoning")
EPSILON = 1e-12
MINIMUM_COMPLETE_MODELS = 4


class InputError(ValueError):
    """The source evidence does not satisfy the reference-fit contract."""


def require_text(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InputError(f"{location} must be a nonempty string")
    return value


def require_number(value: Any, location: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InputError(f"{location} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise InputError(f"{location} must be a finite number")
    return number


def read_json(path: Path) -> tuple[Any, str]:
    raw = path.read_bytes()
    try:
        value = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeError) as exc:
        raise InputError(f"Cannot parse {path}: {exc}") from exc
    return value, hashlib.sha256(raw).hexdigest()


def prepare_models(data: Any) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    if not isinstance(data, dict) or data.get("schema_version") != "generalrouter-model-vectors.v1":
        raise InputError("--models must use generalrouter-model-vectors.v1")
    if data.get("dimension_order") != list(DIMENSIONS):
        raise InputError("Model snapshot must use the four GeneralRouter dimensions in order")
    profiles, normalization = data.get("profiles"), data.get("derivation")
    if not isinstance(profiles, list) or not profiles:
        raise InputError("Model snapshot profiles must be a nonempty array")
    if not isinstance(normalization, dict) or normalization.get("kind") != "project_derived_aa_component_groups":
        raise InputError("Model snapshot must record its AA component-group derivation")
    models: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(profiles):
        location = f"profiles[{index}]"
        if not isinstance(item, dict):
            raise InputError(f"{location} must be an object")
        model_id = require_text(item.get("aa_id"), f"{location}.aa_id")
        if model_id in models:
            raise InputError(f"Duplicate AA model id: {model_id}")
        capabilities = item.get("capabilities")
        if not isinstance(capabilities, dict) or set(capabilities) != set(DIMENSIONS):
            raise InputError(f"{location}.capabilities must contain exactly four dimensions")
        vector: list[float | None] = []
        for dimension in DIMENSIONS:
            raw = capabilities[dimension]
            value = None if raw is None else require_number(raw, f"{location}.capabilities.{dimension}")
            if value is not None and not 0.0 <= value <= 1.0:
                raise InputError(f"{location}.capabilities.{dimension} must be in [0, 1]")
            vector.append(value)
        models[model_id] = {
            "aa_model_id": model_id,
            "name": item.get("aa_name"),
            "slug": item.get("aa_slug"),
            "configuration_id": item.get("configuration_id"),
            "normalized_scores": vector,
            "source": item.get("source"),
            "sample_configuration_alignment": item.get("sample_configuration_alignment"),
        }
    return models, normalization


def matrix_rank(matrix: list[list[float]], tolerance: float = 1e-10) -> int:
    """Disclose design rank, independently of optimizer convergence."""
    if not matrix or not matrix[0]:
        return 0
    work = [row[:] for row in matrix]
    rows, columns = len(work), len(work[0])
    rank = 0
    for column in range(columns):
        if rank == rows:
            break
        pivot = max(range(rank, rows), key=lambda row: abs(work[row][column]))
        if abs(work[pivot][column]) <= tolerance:
            continue
        work[rank], work[pivot] = work[pivot], work[rank]
        divisor = work[rank][column]
        work[rank] = [value / divisor for value in work[rank]]
        for row in range(rank + 1, rows):
            multiplier = work[row][column]
            if abs(multiplier) > tolerance:
                work[row] = [value - multiplier * base for value, base in zip(work[row], work[rank])]
        rank += 1
    return rank


def nonnegative_ridge(features: list[list[float]], outcomes: list[float], ridge: float,
                      max_iterations: int, tolerance: float) -> dict[str, Any]:
    sample_count, feature_count = len(features), len(features[0])
    x_means = [sum(row[j] for row in features) / sample_count for j in range(feature_count)]
    y_mean = sum(outcomes) / sample_count
    centered = [[row[j] - x_means[j] for j in range(feature_count)] for row in features]
    residuals = [value - y_mean for value in outcomes]
    coefficients = [0.0] * feature_count
    denominators = [sum(row[j] ** 2 for row in centered) / sample_count + ridge for j in range(feature_count)]
    converged, iterations, max_change = False, 0, 0.0
    for iteration in range(1, max_iterations + 1):
        max_change = 0.0
        for j in range(feature_count):
            old = coefficients[j]
            correlation = sum(row[j] * (residuals[i] + row[j] * old)
                              for i, row in enumerate(centered)) / sample_count
            new = max(0.0, correlation / denominators[j])
            change = new - old
            coefficients[j] = new
            max_change = max(max_change, abs(change))
            if change:
                for i, row in enumerate(centered):
                    residuals[i] -= row[j] * change
        iterations = iteration
        if max_change <= tolerance:
            converged = True
            break
    bias = y_mean - sum(coefficient * mean for coefficient, mean in zip(coefficients, x_means))
    predictions = [bias + sum(beta * value for beta, value in zip(coefficients, row)) for row in features]
    errors = [predicted - actual for predicted, actual in zip(predictions, outcomes)]
    mean_squared_error = sum(error ** 2 for error in errors) / sample_count
    gradients = [-2.0 * sum(row[j] * residuals[i] for i, row in enumerate(centered)) / sample_count
                 + 2.0 * ridge * coefficients[j] for j in range(feature_count)]
    stationarity_residual = max(abs(gradient) if coefficient > EPSILON else max(0.0, -gradient)
                               for coefficient, gradient in zip(coefficients, gradients))
    return {
        "coefficients": coefficients, "bias": bias, "predictions": predictions,
        "training_fit": {
            "meaning": "In-sample fitting residuals; no held-out evaluation or predictive validation was performed.",
            "mean_squared_error": mean_squared_error,
            "mean_absolute_error": sum(abs(error) for error in errors) / sample_count,
            "objective": mean_squared_error + ridge * sum(value ** 2 for value in coefficients),
        },
        "optimizer": {
            "method": "cyclic_coordinate_descent", "converged": converged,
            "iterations": iterations, "max_iterations": max_iterations, "tolerance": tolerance,
            "last_max_coefficient_change": max_change,
            "nonnegative_stationarity_residual": stationarity_residual,
        },
    }


def calibrate_task(task: Any, task_index: int, models: dict[str, dict[str, Any]],
                   ridge: float, max_iterations: int, tolerance: float) -> dict[str, Any]:
    location = f"tasks[{task_index}]"
    if not isinstance(task, dict):
        raise InputError(f"{location} must be an object")
    for field in ("task_id", "task_text", "source_url", "benchmark_version"):
        require_text(task.get(field), f"{location}.{field}")
    observations = task.get("observations")
    if not isinstance(observations, list):
        raise InputError(f"{location}.observations must be an array")
    included, excluded, seen_ids = [], [], set()
    for index, observation in enumerate(observations):
        item_location = f"{location}.observations[{index}]"
        if not isinstance(observation, dict):
            raise InputError(f"{item_location} must be an object")
        model_id = require_text(observation.get("aa_model_id"), f"{item_location}.aa_model_id")
        require_text(observation.get("source_url"), f"{item_location}.source_url")
        score = require_number(observation.get("score"), f"{item_location}.score")
        low = require_number(observation.get("score_min"), f"{item_location}.score_min")
        high = require_number(observation.get("score_max"), f"{item_location}.score_max")
        if high <= low or not math.isfinite(high - low) or not low <= score <= high:
            raise InputError(f"{item_location}: score bounds must have a positive span and contain score")
        if model_id in seen_ids:
            raise InputError(f"{item_location}: duplicate observation for AA model {model_id}")
        seen_ids.add(model_id)
        evidence = dict(observation)
        evidence["normalized_score"] = (score - low) / (high - low)
        model = models.get(model_id)
        if evidence.get("binding_status") != "user_approved_reference_binding":
            evidence["exclusion_reason"] = "reference_binding_not_approved"
        elif model is None:
            evidence["exclusion_reason"] = "aa_model_id_not_in_snapshot"
        else:
            missing = [dimension for dimension, value in zip(DIMENSIONS, model["normalized_scores"]) if value is None]
            if missing:
                evidence["exclusion_reason"] = "incomplete_four_dimensional_reference_vector"
                evidence["missing_dimensions"] = missing
        (excluded if "exclusion_reason" in evidence else included).append(evidence)
    warnings = [
        "Four coefficients plus an intercept use four complete models per task. The task vector is underidentified without regularization; these are reference coefficients, not recovered intrinsic requirements.",
        "Capability categories correlate. Coefficients and zeros are not semantic percentages or proof of an irrelevant capability.",
        "The anchors share one business scenario, so general coding and scientific reasoning calibration coverage is not established.",
        "Briefcase-Lite examples do not contribute to the official leaderboard; task grading and aggregate benchmark settings need not be identical.",
        "Fable Max with Opus 4.8 fallback is user confirmed. Gemini Preview sample effort is undisclosed. No independent runtime configuration verification was performed.",
        "No held-out predictive validation, development testing, behavioral review, or model invocation was performed.",
    ]
    result: dict[str, Any] = {
        "anchor_id": task["task_id"], "task_id": task["task_id"], "task_text": task["task_text"],
        "source_url": task["source_url"], "benchmark_version": task["benchmark_version"],
        "metadata": {key: value for key, value in task.items()
                     if key not in {"task_id", "task_text", "source_url", "benchmark_version", "observations"}},
        "status": "insufficient_evidence", "fit_kind": "joint_four_dimension_reference",
        "requested_dimensions": list(DIMENSIONS), "active_dimensions": list(DIMENSIONS),
        "active_mask": [True] * len(DIMENSIONS),
        "weights_interpretation": "Normalized nonnegative ridge coefficients from recorded reference-model scores; not official AA task labels or semantic shares of work.",
        "weights": None, "bias": None, "scale": None, "coefficients": None,
        "paired_model_ids": [item["aa_model_id"] for item in included],
        "observations": included, "excluded_observations": excluded,
        "fit_diagnostics": {"paired_model_count": len(included), "minimum_complete_models": MINIMUM_COMPLETE_MODELS,
                            "ridge": ridge, "warnings": warnings},
    }
    diagnostics = result["fit_diagnostics"]
    if len(included) < MINIMUM_COMPLETE_MODELS:
        diagnostics["reason"] = "fewer_than_four_complete_reference_models"
        return result
    features = [models[item["aa_model_id"]]["normalized_scores"] for item in included]
    outcomes = [item["normalized_score"] for item in included]
    varying = [j for j in range(len(DIMENSIONS))
               if max(row[j] for row in features) - min(row[j] for row in features) > EPSILON]
    if len(varying) != len(DIMENSIONS):
        diagnostics["reason"] = "one_or_more_capability_dimensions_have_no_reference_variation"
        result["active_dimensions"] = [DIMENSIONS[j] for j in varying]
        result["active_mask"] = [j in varying for j in range(len(DIMENSIONS))]
        return result
    rank = matrix_rank([[1.0, *row] for row in features])
    diagnostics.update({"active_feature_count": len(DIMENSIONS),
                        "parameter_count_with_intercept": len(DIMENSIONS) + 1, "design_rank": rank,
                        "rank_tolerance": 1e-10, "identifiable_without_regularization": rank == len(DIMENSIONS) + 1,
                        "underdetermined_without_regularization": rank < len(DIMENSIONS) + 1})
    if max(outcomes) - min(outcomes) <= EPSILON:
        result.update({"status": "constant_outcome", "bias": sum(outcomes) / len(outcomes), "scale": 0.0})
        diagnostics["reason"] = "observed_scores_have_no_variation"
        return result
    fit = nonnegative_ridge(features, outcomes, ridge, max_iterations, tolerance)
    coefficients, scale = fit["coefficients"], sum(fit["coefficients"])
    result.update({"bias": fit["bias"], "scale": scale, "coefficients": dict(zip(DIMENSIONS, coefficients))})
    diagnostics["optimizer"], diagnostics["training_fit"] = fit["optimizer"], fit["training_fit"]
    diagnostics["training_predictions"] = [
        {"aa_model_id": item["aa_model_id"], "observed_score_normalized": item["normalized_score"],
         "predicted_score_proxy": predicted} for item, predicted in zip(included, fit["predictions"])
    ]
    if scale <= EPSILON:
        diagnostics["reason"] = "nonnegative_fit_has_no_positive_capability_signal"
        return result
    if not fit["optimizer"]["converged"]:
        diagnostics["reason"] = "optimizer_did_not_converge"
        return result
    result["status"] = "regularized_reference_fit"
    result["weights"] = {dimension: coefficient / scale for dimension, coefficient in zip(DIMENSIONS, coefficients)}
    result["weight_evidence_label"] = "fitted_to_paired_aa_observations"
    return result


def build_calibration(model_data: Any, observation_data: Any, model_hash: str, observation_hash: str,
                      ridge: float, max_iterations: int, tolerance: float) -> dict[str, Any]:
    models, normalization = prepare_models(model_data)
    if not isinstance(observation_data, dict) or observation_data.get("schema_version") != "aa-observations.v1":
        raise InputError("--observations must use schema_version aa-observations.v1")
    tasks = observation_data.get("tasks")
    if not isinstance(tasks, list):
        raise InputError("observations.tasks must be an array")
    anchors, task_ids = [], set()
    for index, task in enumerate(tasks):
        anchor = calibrate_task(task, index, models, ridge, max_iterations, tolerance)
        if anchor["task_id"] in task_ids:
            raise InputError(f"Duplicate task_id: {anchor['task_id']}")
        task_ids.add(anchor["task_id"])
        anchor["data_provenance"] = {"models_sha256": model_hash, "observations_sha256": observation_hash,
            "task_payload_sha256": hashlib.sha256(json.dumps(task, sort_keys=True, ensure_ascii=False,
                                                              allow_nan=False).encode("utf-8")).hexdigest()}
        anchors.append(anchor)
    return {
        "schema_version": "aa-four-dimensional-calibration.v1", "delivery_version": "0.3",
        "dimensions": list(DIMENSIONS),
        "snapshot": {"models_sha256": model_hash, "model_count": len(models),
            "normalization_method": "use_prederived_component_group_values_without_rescaling",
            "normalization": normalization,
            "normalized_model_vectors": {model_id: model["normalized_scores"] for model_id, model in models.items()},
            "models": {model_id: {key: value for key, value in model.items() if key != "normalized_scores"}
                       for model_id, model in models.items()}},
        "evidence": {"observations_sha256": observation_hash,
            "metadata": {key: value for key, value in observation_data.items() if key not in {"schema_version", "tasks"}},
            "source_policy": "Exact UUID binding to user-approved reference records. Require all four model categories; preserve missing records as excluded observations without imputation."},
        "calibration_method": {
            "requested_dimensions": list(DIMENSIONS), "fit_kind": "joint_four_dimension_reference",
            "algorithm": "nonnegative_ridge", "optimizer": "cyclic_coordinate_descent",
            "prediction": "score_proxy = bias + scale * dot(weights, model_capabilities)",
            "objective": "mean((observed_normalized_score - bias - X beta)**2) + ridge * sum(beta**2), beta >= 0",
            "ridge": ridge, "intercept_penalized": False,
            "weight_derivation": "weights = beta / sum(beta); scale = sum(beta); bias is separate",
            "minimum_complete_models": MINIMUM_COMPLETE_MODELS,
            "minimum_is_project_policy_not_identifiability_guarantee": True,
            "algorithm_origin": "GeneralRouter engineering choice, not an AA task annotation or official AA calibration algorithm.",
            "missing_data_policy": "Exclude an observation when any of its four capability values is null; do not impute or switch to a single-dimension fit.",
            "normalization_scope": "Use the fixed [0, 1] AA component-derived category scores in the snapshot; do not apply model-pool minmax.",
            "score_proxy_is_success_probability": False, "score_proxy_is_task_difficulty": False,
            "validation_performed": False},
        "planner_transfer_policy": {"new_task_evidence_label": "estimated_from_anchors", "rules": [
            "Numerical references require status regularized_reference_fit and fit_kind joint_four_dimension_reference.",
            "Retain anchor ids and snapshot model hash for an analogous-task estimate.",
            "Only close analogues of the four Briefcase-Lite business deliverables are covered. General coding, factual Q&A, long-document reasoning, and science problems remain unsupported.",
            "An analogous task receives a planner estimate, not a newly measured or directly calibrated task vector.",
            "Fitted weights are not semantic work percentages; score proxies are not calibrated probability or difficulty."]},
        "limitations": [
            "Four complete reference models per task are fewer than four coefficients plus an intercept; regularization selects a reference solution, not a uniquely identified task vector.",
            "The 16 included observations cover four outputs within one shared business scenario; the eight incomplete-model observations are retained but excluded.",
            "Reference capabilities and public task-score runtime conditions are not independently verified as identical; user confirmations and undisclosed effort are recorded separately.",
            "The model-side knowledge_document category still includes GDP.pdf aggregate scores. Only GDP.pdf task examples were excluded at the user's request.",
            "No testing, review, predictive validation, or model invocations were performed."],
        "anchors": anchors,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", type=Path, required=True, help="Fixed generalrouter-model-vectors.v1 JSON snapshot")
    parser.add_argument("--observations", type=Path, required=True, help="aa-observations.v1 JSON with approved UUID bindings")
    parser.add_argument("--output", type=Path, required=True, help="Destination aa-four-dimensional-calibration.v1 JSON")
    parser.add_argument("--ridge", type=float, default=0.01)
    parser.add_argument("--max-iterations", type=int, default=20000)
    parser.add_argument("--tolerance", type=float, default=1e-10)
    args = parser.parse_args(argv)
    if not math.isfinite(args.ridge) or args.ridge <= 0:
        parser.error("--ridge must be finite and positive")
    if args.max_iterations < 1:
        parser.error("--max-iterations must be positive")
    if not math.isfinite(args.tolerance) or args.tolerance <= 0:
        parser.error("--tolerance must be finite and positive")
    try:
        if args.output.resolve() in (args.models.resolve(), args.observations.resolve()):
            raise InputError("--output must differ from both input paths")
        model_data, model_hash = read_json(args.models)
        observation_data, observation_hash = read_json(args.observations)
        calibration = build_calibration(model_data, observation_data, model_hash, observation_hash,
                                        args.ridge, args.max_iterations, args.tolerance)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(calibration, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    except (InputError, OSError) as exc:
        print(f"Calibration input error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"output": str(args.output), "anchors": [
        {"anchor_id": anchor["anchor_id"], "status": anchor["status"], "weights": anchor["weights"],
         "included": len(anchor["observations"]), "excluded": len(anchor["excluded_observations"])}
        for anchor in calibration["anchors"]]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

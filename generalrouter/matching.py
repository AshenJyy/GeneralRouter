"""Rank available models by task fit while retaining partial AA evidence."""

from __future__ import annotations

import math
from typing import Any

from .capabilities import AA_COMPOSITE_WEIGHTS, DIMENSIONS


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{label} must be finite")
    return value


def _unit_parameter(value: Any, label: str) -> float:
    value = _number(value, label)
    if not 0 <= value <= 1:
        raise ValueError(f"{label} must be between 0 and 1")
    return value


def _effective_weights(task: dict, intelligence_prior: float) -> tuple[dict, str]:
    weights = task.get("task_weights")
    if not isinstance(weights, dict) or set(weights) != set(DIMENSIONS):
        raise ValueError("task_weights must contain exactly the four dimensions")

    if all(weights[dimension] is None for dimension in DIMENSIONS):
        if task.get("weight_source") != "uncalibrated":
            raise ValueError("Only an uncalibrated task can have all-null weights")
        return (
            dict(AA_COMPOSITE_WEIGHTS),
            "aa_composite_fallback",
        )

    values = {}
    for dimension in DIMENSIONS:
        value = _number(weights[dimension], f"task_weights.{dimension}")
        if value < 0:
            raise ValueError("Task weights cannot be negative")
        values[dimension] = value
    total = math.fsum(values.values())
    if not math.isfinite(total) or total <= 0:
        raise ValueError("Task weights must have a finite positive sum")

    effective = {
        dimension: (
            (1 - intelligence_prior) * values[dimension] / total
            + intelligence_prior * AA_COMPOSITE_WEIGHTS[dimension]
        )
        for dimension in DIMENSIONS
    }
    return effective, "task_weights_with_aa_composite_prior"


def rank_models(
    task: dict,
    candidates: list[dict],
    intelligence_prior: float = 0.40,
    missing_penalty: float = 0.25,
    preferred_configuration: str = "",
) -> list[dict]:
    """Return descending routing scores without modifying the planner's vector.

    Observed capability is averaged over the available task-weight mass. A small
    configurable discount records the uncertainty from missing task dimensions.
    These scores are routing preferences, not measured success probabilities.
    The composite prior blends AA's four-category proportions into the task
    vector. Its default 40% share is a project policy, not a fitted task weight.
    A resource preference only breaks exact score-and-coverage ties.
    """
    if not isinstance(task, dict):
        raise ValueError("Task must be an object")
    intelligence_prior = _unit_parameter(intelligence_prior, "intelligence_prior")
    missing_penalty = _unit_parameter(missing_penalty, "missing_penalty")
    if not isinstance(preferred_configuration, str):
        raise ValueError("preferred_configuration must be a string")
    weights, policy = _effective_weights(task, intelligence_prior)
    ranked = []
    seen_ids = set()

    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ValueError("Each candidate must be an object")
        model_id = candidate.get("id")
        if not isinstance(model_id, str) or not model_id:
            raise ValueError("Each candidate needs a model ID")
        if model_id in seen_ids:
            raise ValueError(f"Duplicate candidate ID: {model_id}")
        seen_ids.add(model_id)
        capabilities = candidate.get("capabilities")
        if not isinstance(capabilities, dict) or set(capabilities) != set(DIMENSIONS):
            raise ValueError(f"Candidate {model_id} needs all four capability keys")

        available = {}
        for dimension in DIMENSIONS:
            value = capabilities[dimension]
            if value is None:
                continue
            value = _unit_parameter(value, f"{model_id}.capabilities.{dimension}")
            available[dimension] = value

        coverage = math.fsum(weights[dimension] for dimension in available)
        if coverage <= 0:
            continue
        coverage = min(1.0, coverage)
        numerator = math.fsum(
            weights[dimension] * value for dimension, value in available.items()
        )
        observed_score = numerator / coverage
        uncertainty_factor = 1 - missing_penalty * (1 - coverage)
        score = observed_score * uncertainty_factor
        contributions = {
            dimension: (
                weights[dimension] * available[dimension] / coverage * uncertainty_factor
                if dimension in available
                else None
            )
            for dimension in DIMENSIONS
        }
        ranked.append(
            {
                **candidate,
                "score": score,
                "observed_score": observed_score,
                "coverage": coverage,
                "effective_weights": dict(weights),
                "missing_dimensions": [
                    dimension
                    for dimension in DIMENSIONS
                    if weights[dimension] > 0 and dimension not in available
                ],
                "weight_policy": policy,
                "intelligence_prior": intelligence_prior,
                "missing_penalty": missing_penalty,
                "dimension_contributions": contributions,
            }
        )

    def order(row: dict) -> tuple:
        return (
            -row["score"],
            -row["coverage"],
            -int(bool(preferred_configuration) and row.get("configuration_id") == preferred_configuration),
            row["id"],
        )

    return sorted(ranked, key=order)

"""Score task/model capability profiles; execution belongs to the main agent."""

from __future__ import annotations

from typing import Any

from .capabilities import AA_COMPOSITE_WEIGHTS, DIMENSIONS, load_candidates
from .config import execution_hint
from .matching import rank_models
from .skill import PlannerSkill


class GeneralRouter:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.skill = PlannerSkill(config["skill_dir"])
        self.candidates, self.model_snapshot = load_candidates(config["models_file"])
        reference_normalization = self.skill.calibration["snapshot"]["normalization"]
        if reference_normalization != self.model_snapshot["normalization"]:
            raise ValueError("Task references and candidate profiles use different capability derivations")

    def route(self, profile: dict[str, Any], *, original_request: str = "") -> dict[str, Any]:
        """Return selected models; the caller retains ownership of actual calls."""
        normalized = self.skill.normalize(profile)
        jobs = []
        for task in normalized["tasks"]:
            ranking = rank_models(
                task,
                self.candidates,
                intelligence_prior=self.config["intelligence_prior"],
                missing_penalty=self.config["missing_penalty"],
                preferred_configuration=self.config["preferred_configuration"],
            )
            if not ranking:
                raise ValueError(f"No candidate has usable capability data for task {task['task_id']}")
            selected = ranking[0]
            tied = [
                row for row in ranking
                if row["score"] == selected["score"] and row["coverage"] == selected["coverage"]
            ]
            preference_used = (
                len(tied) > 1
                and selected.get("configuration_id") == self.config["preferred_configuration"]
            )
            if selected["weight_policy"] == "aa_composite_fallback":
                selection_reason = "Task weights are unknown; use the disclosed AA category composite fallback."
            elif task["weight_source"] == "planner_estimated":
                selection_reason = (
                    "Highest capability match for the planner's estimated task weights "
                    "and composite prior. These weights are semantic routing preferences, "
                    "not an AA task measurement."
                )
            else:
                selection_reason = "Highest capability match for the recorded AA task reference and composite prior."
            if preference_used:
                selection_reason += " The configured resource preference broke an exact score-and-coverage tie."
            jobs.append({
                "task_id": task["task_id"],
                "objective": task["objective"],
                "depends_on": task["depends_on"],
                "selected_model": execution_hint(selected),
                "matching": {
                    "selection_reason": selection_reason,
                    "resource_preference_used": preference_used,
                    "ranking": [
                        {
                            "configuration_id": row["configuration_id"],
                            "aa_id": row["id"],
                            "aa_name": row["name"],
                            "reference_effort": row["reference_effort"],
                            "access_channel": row["access_channel"],
                            "score": row["score"],
                            "coverage": row["coverage"],
                            "dimension_contributions": row["dimension_contributions"],
                            "missing_dimensions": row["missing_dimensions"],
                        }
                        for row in ranking
                    ],
                    "selected_score": {
                        key: selected[key]
                        for key in (
                            "score", "observed_score", "coverage", "effective_weights",
                            "missing_dimensions", "weight_policy", "dimension_contributions",
                        )
                    },
                },
                "task_profile": {
                    "difficulty": task["difficulty"],
                    "task_weights": task["task_weights"],
                    "weight_source": task["weight_source"],
                    "calibration_anchor_id": task["calibration_anchor_id"],
                    "rationale": task["rationale"],
                    "weight_basis": task["calibration_basis"],
                },
                "worker_request": {
                    "original_request": original_request,
                    "objective": task["objective"],
                    "context": task["context"],
                    "input_artifacts": task["input_artifacts"],
                    "deliverable": task["deliverable"],
                    "upstream_task_ids": task["depends_on"],
                },
            })
        snapshot = normalized.get("calibration_snapshot") or {}
        return {
            "schema_version": "generalrouter-handoff.v0.2",
            "execution_owner": "main_agent",
            "execution_status": "awaiting_main_agent" if jobs else "no_tasks",
            "candidate_model_count": len(self.candidates),
            "dimension_order": list(DIMENSIONS),
            "task_weight_method": normalized["weight_method"],
            "calibration_snapshot": {
                "model_count": snapshot.get("model_count"),
                "models_sha256": snapshot.get("models_sha256"),
            },
            "candidate_snapshot": self.model_snapshot,
            "routing_policy": {
                "selection": "highest_match_score",
                "intelligence_prior": self.config["intelligence_prior"],
                "missing_penalty": self.config["missing_penalty"],
                "aa_composite_weights": dict(AA_COMPOSITE_WEIGHTS),
                "uncalibrated_policy": "aa_composite_fallback",
                "resource_preference": {
                    "configuration_id": self.config["preferred_configuration"],
                    "scope": "exact_score_and_coverage_ties_only",
                },
                "scores_are_success_probabilities": False,
            },
            "main_agent_instructions": [
                "Resolve each selected AA identity to the corresponding model in your available tools; do not assume AA slugs are callable local model IDs.",
                "Invoke the selected model to execute worker_request. Forward the referenced inputs and completed upstream results; preserve the original user constraints.",
                "Only dispatch a task after all its dependencies have returned successfully. Independent tasks may run concurrently.",
                "Set a corresponding supported effort when the host has a valid mapping; otherwise omit effort and use the provider default.",
                "If the selected model cannot be resolved or invoked, report the issue to the user. Do not silently choose another model or claim that routing executed the task.",
                "Collect worker results and deliver the final requested work to the user.",
            ],
            "tasks": jobs,
        }

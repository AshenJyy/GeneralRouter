"""JSON-over-stdin bridge for DSH; the main agent retains dispatch ownership."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any

from .config import load_config
from .ledger import RunLedger, utc_now
from .quota import QuotaError, QuotaManager
from .runner import GeneralRouter


class BridgeError(ValueError):
    """A deliberately public diagnostic, never a raw request or exception dump."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class SetupRequired(BridgeError):
    def __init__(self, message: str):
        super().__init__("setup_required", message)


def _profile_digest(profile: dict) -> str:
    try:
        serialized = json.dumps(profile, sort_keys=True, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError) as error:
        raise BridgeError("profile_not_json_safe", "The profile must contain finite, JSON-compatible values.") from error
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _profile_id(run_id: str, digest: str) -> str:
    binding = json.dumps([run_id, digest], ensure_ascii=False).encode("utf-8")
    return "profile-" + hashlib.sha256(binding).hexdigest()[:32]


def _window_summary(window: dict) -> dict:
    return {key: window.get(key) for key in (
        "id", "reported_label", "window", "remaining_percent", "reset_at", "duration_seconds", "applicability"
    ) if key in window}


def _quota_summary(snapshot: dict, run_id: str) -> dict:
    accounts = snapshot.get("accounts", {})
    values = accounts.values() if isinstance(accounts, dict) else accounts
    return {
        **{key: snapshot.get(key) for key in (
            "generated_at", "status", "last_attempt_at", "last_success_at", "stale", "refresh_due_seconds", "error"
        )},
        "accounts": [
            {
                **{key: account.get(key) for key in ("provider", "status", "models_status", "age_seconds", "stale", "error")},
                "windows": [_window_summary(window) for window in account.get("windows", [])],
            }
            for account in values
        ],
        "active_ticket_count": sum(ticket.get("run_id") == run_id for ticket in snapshot.get("reservations", [])),
        "reservation_unit": "Dispatch identity only; no percentage is deducted.",
    }


def _admission_summary(decision: dict) -> dict:
    return {
        **{key: decision.get(key) for key in (
            "accepted", "quota_status", "reason", "detail", "remaining_percent", "minimum_remaining_percent", "catalog_incomplete"
        ) if key in decision},
        "account_evidence": [
            {
                **{key: account.get(key) for key in ("provider", "status", "reason", "remaining_percent")},
                "windows": [_window_summary(window) for window in account.get("windows", [])],
            }
            for account in decision.get("accounts", [])
        ],
        "limitation": "Cached percentages; unknown stays unknown and no task consumption is pre-deducted.",
    }


def _path(base: Path, value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise SetupRequired(f"Configure {label} in the private host configuration.")
    result = Path(value).expanduser()
    return result.resolve() if result.is_absolute() else (base / result).resolve()


def _object_file(path: Path) -> dict:
    if not path.is_file():
        raise SetupRequired("A required configuration or bundled data file is missing; check the selected file locations.")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise BridgeError("configuration_object_required", "A configuration or bundled data file must contain a JSON object.")
    return value


def _merge_config(base: dict, overlay: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge_config(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _quota_settings(value: Any, base: Path) -> dict:
    if not isinstance(value, dict):
        raise SetupRequired("Configure a quota object or quota_file")
    result = copy.deepcopy(value)
    connection = result.get("connection", {})
    if isinstance(connection, dict) and connection.get("management_key_toml_file"):
        connection["management_key_toml_file"] = str(
            _path(base, connection["management_key_toml_file"], "management_key_toml_file")
        )
    return result


class HostBridge:
    def __init__(self, request: dict):
        self.request = request
        self.host = request.get("host") or {}
        if not isinstance(self.host, dict):
            raise BridgeError("invalid_host", "host must be an object.")
        supplied = request.get("config_file", request.get("host_config"))
        project = request.get("project_root") or self.host.get("project_root") or self.host.get("cwd")
        if not project and isinstance(supplied, str) and supplied.strip():
            # Preserve the original standalone bridge's explicit-file interface.
            project = str(Path(supplied).expanduser().resolve().parent)
        if not isinstance(project, str) or not Path(project).expanduser().is_absolute():
            raise SetupRequired("Open the project folder in DSH or provide an absolute projectRoot override.")
        self.project_root = Path(project).expanduser().resolve()
        self.config_path = _path(self.project_root, supplied or "host.local.json", "hostConfig")
        self.runtime_root = Path(request.get("runtime_root") or Path(__file__).resolve().parents[1]).resolve()
        self.config = {"routing_config": None, "rates_file": None, "state_dir": ".runs/host", "routes": {}}
        self.loaded_config_files: list[str] = []
        user_file = request.get("user_config_file")
        self.user_config_path = _path(self.project_root, user_file, "user_config_file") if user_file else None
        explicit = request.get("config_explicit", "user_config_file" not in request and bool(supplied))
        layers = [path for path in (self.user_config_path, self.config_path) if path is not None]
        for path in dict.fromkeys(layers):
            if not path.exists():
                if path == self.config_path and explicit:
                    raise SetupRequired("The explicitly selected local configuration file does not exist.")
                continue
            layer = _object_file(path)
            for field in ("routing_config", "rates_file", "quota_file"):
                if layer.get(field):
                    layer[field] = str(_path(path.parent, layer[field], field))
            # Account connections are replaced as a unit, never assembled from
            # a global credential and a different project's partial connection.
            if "quota_file" in layer or "quota" in layer:
                self.config.pop("quota_file", None)
                self.config.pop("quota", None)
            if "quota" in layer:
                layer["quota"] = _quota_settings(layer["quota"], path.parent)
            self.config = _merge_config(self.config, layer)
            self.loaded_config_files.append(str(path))
        # Shared connection settings must not place every project's ledger next
        # to the shared configuration. State paths belong to this workspace.
        self.state_dir = _path(self.project_root, self.config.get("state_dir", ".runs/host"), "state_dir")
        if not self.state_dir.is_relative_to(self.project_root):
            raise SetupRequired("state_dir must be inside the current project workspace.")
        self.run_id = request.get("run_id") or self.host.get("session_id")
        if not isinstance(self.run_id, str) or not self.run_id:
            raise BridgeError("run_id_required", "A DSH session_id or run_id is required.")
        self.ledger = RunLedger(self.state_dir, self.run_id)
        bundled_mappings = _object_file(self.runtime_root / "data/model-call-mappings.v0.1.json")
        self.routes = copy.deepcopy(bundled_mappings.get("routes", {}))
        overrides = self.config.get("routes", {})
        if not isinstance(self.routes, dict) or not isinstance(overrides, dict):
            raise SetupRequired("routes must be a configuration_id-to-route object")
        for identity, override in overrides.items():
            if not isinstance(override, dict):
                raise SetupRequired("Each optional route override must be an object")
            self.routes[identity] = {**self.routes.get(identity, {}), **override}
        if self.config.get("quota_file"):
            quota_path = Path(self.config["quota_file"])
            self.quota_config = _quota_settings(_object_file(quota_path), quota_path.parent)
        elif "quota" in self.config:
            self.quota_config = self.config["quota"]
        else:
            self.quota_config = _quota_settings(
                _object_file(self.runtime_root / "data/quota-policy.example.json"), self.runtime_root / "data"
            )
        self.quota = QuotaManager(self.quota_config, self.state_dir / "quota")
        self.rate_data = (
            _object_file(Path(self.config["rates_file"]))
            if self.config.get("rates_file") else _object_file(self.runtime_root / "data/api-equivalent-rates.v0.1.json")
        )

    def _rate(self, record: dict) -> dict | None:
        candidates = []
        configuration = record.get("configuration_id")
        for identity, route in self.routes.items():
            if not isinstance(route, dict):
                continue
            if configuration and identity == configuration:
                # An observed route must still agree with this mapping.
                if record.get("provider") and record.get("provider") != route.get("provider"):
                    continue
                if record.get("model") and record.get("model") != route.get("model"):
                    continue
                candidates = [(identity, route)]
                break
            if route.get("provider") == record.get("provider") and route.get("model") == record.get("model"):
                candidates.append((identity, route))
        rates = self.rate_data.get("rates", {})
        entries = []
        for identity, route in candidates:
            entry = rates.get(route.get("pricing_id", identity))
            if isinstance(entry, dict):
                entries.append(entry)
        if not entries:
            return None
        # Different effort vectors may share prices. Never choose between conflicting tariffs.
        prices = {json.dumps(entry.get("usd_per_million_tokens"), sort_keys=True) for entry in entries}
        if len(prices) != 1:
            return None
        return copy.deepcopy(entries[0])

    def _model_catalog(self) -> list[dict]:
        value = self.request.get("allowed_models", self.host.get("available_models"))
        if not isinstance(value, list):
            return []
        return [entry for entry in value if isinstance(entry, dict)]

    def _route(self, configuration_id: str) -> tuple[dict | None, str]:
        route = self.routes.get(configuration_id)
        if not isinstance(route, dict) or route.get("enabled") is not True:
            return None, "route_not_enabled_or_not_mapped"
        if not all(isinstance(route.get(key), str) and route[key].strip() for key in ("provider", "model")):
            return None, "callable_provider_and_model_required"
        catalog = self._model_catalog()
        if not catalog:
            return None, "host_allowed_model_catalog_missing"
        model = next((entry for entry in catalog if entry.get("provider") == route["provider"] and entry.get("model") == route["model"]), None)
        if model is None:
            return None, "route_not_in_this_session_model_allowlist"
        result = {
            "configuration_id": configuration_id,
            "provider": route["provider"],
            "model": route["model"],
            "effort_policy": "mapped_if_supported_else_default",
        }
        effort = route.get("reasoning_effort")
        supported = model.get("reasoning_efforts", model.get("reasoningEfforts", model.get("efforts")))
        if isinstance(supported, dict):
            supported = list(supported)
        if isinstance(supported, list):
            supported = [entry.get("id", entry.get("value")) if isinstance(entry, dict) else entry for entry in supported]
        if isinstance(effort, str) and effort and isinstance(supported, list) and effort in supported:
            result["reasoning_effort"] = effort
            result["effort_resolution"] = "explicit_supported_mapping"
        else:
            result["effort_resolution"] = "provider_default"
            result["effort_fallback_reason"] = "mapping_absent_or_not_supported_by_this_host_catalog"
        result["reference_effort_matches_dispatch"] = result.get("reasoning_effort") == configuration_id.rsplit(":", 1)[-1]
        return result, "mapped"

    def _refresh_if_needed(self) -> dict:
        snapshot = self.quota.snapshot(refresh=False)
        enabled = any(isinstance(route, dict) and route.get("enabled") is True for route in self.routes.values())
        # A failed attempt is cached too; repeated dispatches do not increase polling.
        return self.quota.snapshot(refresh=True) if enabled and snapshot.get("stale", True) else snapshot

    def _planner_input(self) -> tuple[dict, str, str, str]:
        supplied = self.request.get("profile")
        reference = self.request.get("profile_id")
        if supplied is not None and not isinstance(supplied, dict):
            raise BridgeError("profile_object_required", "profile must be a task-profile.v3 object.")
        if reference is not None and (not isinstance(reference, str) or not reference):
            raise BridgeError("invalid_profile_id", "profile_id must be a nonempty saved profile identifier.")
        if supplied is None and reference is None:
            raise BridgeError("profile_required", "Submit the complete profile once, then reuse its returned profile_id.")
        with self.ledger.edit() as state:
            saved_digest = state.get("profile_sha256")
            saved_profile = state.get("profile")
            if saved_digest is not None:
                expected_id = _profile_id(self.run_id, saved_digest)
                if state.get("profile_id") not in (None, expected_id):
                    raise BridgeError("saved_profile_identity_mismatch", "The saved profile identity is inconsistent; preserve the run records and use a new run.")
                if reference is not None and reference != expected_id:
                    raise BridgeError("profile_id_not_in_run", "This profile_id does not belong to the current run.")
                if isinstance(saved_profile, dict) and _profile_digest(saved_profile) != saved_digest:
                    raise BridgeError("saved_profile_digest_mismatch", "The saved profile content no longer matches its registered digest; do not dispatch from this record.")
            elif reference is not None and supplied is None:
                raise BridgeError("profile_not_registered", "This run has no saved profile; submit its complete task-profile.v3 object.")
            if supplied is None:
                if not isinstance(saved_profile, dict):
                    raise BridgeError("saved_profile_body_missing", "This older run stores only a profile digest. Resubmit the original complete profile to enable profile_id reuse.")
                profile = copy.deepcopy(saved_profile)
            else:
                profile = copy.deepcopy(supplied)
            digest = _profile_digest(profile)
            if saved_digest is not None and saved_digest != digest:
                raise BridgeError("profile_changed", "This run already has a different plan. Keep the original profile or start a new run.")
            identity = _profile_id(self.run_id, digest)
            if reference is not None and reference != identity:
                raise BridgeError("profile_id_not_in_run", "The profile_id does not match this run and profile.")
            if "original_request" in self.request:
                original = self.request["original_request"]
            elif "request" in self.request:
                original = self.request["request"]
            else:
                original = state.get("original_request", "")
            if not isinstance(original, str):
                raise BridgeError("invalid_original_request", "original_request must be a string.")
            if saved_digest is not None and original != state.get("original_request", ""):
                raise BridgeError("original_request_changed", "The original request is fixed for this run; omit it on later calls or resend the same text.")
        return profile, original, digest, identity

    def _diagnostics_reference(self) -> dict:
        return {
            "ledger_reference": self.ledger.path.relative_to(self.project_root).as_posix(),
            "detail_request": "Use detail:true on status/report or match when specific diagnostics are needed.",
        }

    def match(self) -> dict:
        profile, original, digest, profile_id = self._planner_input()
        routing_path = (
            _path(self.config_path.parent, self.config["routing_config"], "routing_config")
            if self.config.get("routing_config") else self.runtime_root / "config.example.json"
        )
        router = GeneralRouter(load_config(routing_path))
        handoff = router.route(profile, original_request=original)
        detail = self.request.get("detail") is True
        # Refresh before acquiring the run lock; collection can involve network latency.
        snapshot = self._refresh_if_needed()
        with self.ledger.edit() as state:
            if state.get("profile_sha256") not in (None, digest):
                raise BridgeError("profile_changed", "This run already has a different plan. Keep the original profile or start a new run.")
            if state.get("profile_sha256") is not None and state.get("original_request", "") != original:
                raise BridgeError("original_request_changed", "The saved original request changed before registration; retry with the existing plan.")
            if "profile_sha256" not in state:
                state["profile_sha256"] = digest
                state["original_request"] = original
                state["host"] = {key: self.host.get(key) for key in ("name", "version", "session_id", "agent_id", "cwd")}
                for task in handoff["tasks"]:
                    state["tasks"][task["task_id"]] = {"status": "pending", "depends_on": task["depends_on"], "objective": task["objective"]}
                self.ledger.event(state, "plan_registered", profile_id=profile_id, profile_sha256=digest, candidate_snapshot=handoff["candidate_snapshot"])
            if not isinstance(state.get("profile"), dict):
                state["profile"] = copy.deepcopy(profile)
                self.ledger.event(state, "profile_saved", profile_id=profile_id, profile_sha256=digest)
            elif _profile_digest(state["profile"]) != digest:
                raise BridgeError("saved_profile_digest_mismatch", "The saved profile content no longer matches its registered digest.")
            state["profile_id"] = profile_id
            state.setdefault("original_request", original)
            if {task["task_id"] for task in handoff["tasks"]} != set(state["tasks"]):
                raise BridgeError("saved_task_set_mismatch", "The saved task states do not match this profile; preserve the records and use a new run.")
            requested = self.request.get("task_id")
            if requested is not None and (not isinstance(requested, str) or requested not in state["tasks"]):
                raise BridgeError("task_not_in_plan", "task_id is not present in this plan.")
            if self.request.get("retry_failed") is True and requested:
                item = state["tasks"][requested]
                if item["status"] == "failed":
                    self.ledger.event(state, "retry_requested", task_id=requested, previous_reservation_id=item.get("reservation_id"))
                    item["status"] = "pending"
            ready = [
                task for task in handoff["tasks"]
                if (requested is None or task["task_id"] == requested)
                and state["tasks"][task["task_id"]]["status"] == "pending"
                and all(state["tasks"][dependency]["status"] == "completed" for dependency in task["depends_on"])
            ]
            if not ready:
                statuses = {identity: task["status"] for identity, task in state["tasks"].items()}
                done = bool(statuses) and all(value == "completed" for value in statuses.values())
                response = {"ok": True, "status": "completed" if done or not statuses else "awaiting_worker_or_dependencies", "run_id": self.run_id, "profile_id": profile_id, "task_statuses": statuses}
                if detail:
                    response["details"] = {"task_matching": {
                        identity: item.get("matching") for identity, item in state["tasks"].items()
                        if requested is None or requested == identity
                    }}
                return response
            task = ready[0]
            eligibility = []
            chosen = None
            for rank in task["matching"]["ranking"]:
                identity = rank["configuration_id"]
                route, reason = self._route(identity)
                item = {**rank, "eligible": False, "eligibility_reason": reason}
                if route is not None:
                    decision = self.quota.reserve(route, task["task_id"], self.run_id) if chosen is None else self.quota.admit(route)
                    item["admission"] = decision
                    item["eligible"] = decision["accepted"]
                    item["eligibility_reason"] = decision["reason"]
                    if decision["accepted"] and chosen is None:
                        chosen = (rank, route, decision)
                eligibility.append(item)
            if chosen is None:
                self.ledger.event(state, "no_admissible_route", task_id=task["task_id"], ranking=eligibility)
                reasons = {}
                for item in eligibility:
                    reason = item["eligibility_reason"]
                    reasons[reason] = reasons.get(reason, 0) + 1
                response = {
                    "ok": False, "status": "setup_required_or_no_allowance", "run_id": self.run_id,
                    "profile_id": profile_id, "task_id": task["task_id"], "rejection_summary": reasons,
                    "quota": _quota_summary(snapshot, self.run_id), "detail_available": True,
                    "message": "No candidate currently passes the session allowlist and reported allowance checks. No model was dispatched.",
                }
                if detail:
                    response["details"] = {"ranking": eligibility, "task_profile": task["task_profile"], "routing_policy": handoff["routing_policy"]}
                return response
            rank, route, decision = chosen
            reservation_id = decision["reservation_id"]
            marker = f"[generalrouter-reservation:{reservation_id}]"
            worker_request = task["worker_request"]
            dispatch = {
                "description": task["objective"][:120],
                "prompt": marker + "\n" + json.dumps(worker_request, ensure_ascii=False, indent=2),
                "provider": route["provider"],
                "model": route["model"],
            }
            if route.get("reasoning_effort"):
                dispatch["reasoning_effort"] = route["reasoning_effort"]
            reason = "Highest capability-ranked callable candidate not excluded by reported ECPA percentages; unknown allowance is retained."
            common_score = task["matching"]["selected_score"]
            selection = {
                "score": rank["score"], "coverage": rank["coverage"],
                "capability_rank": next(index for index, row in enumerate(eligibility, 1) if row["configuration_id"] == rank["configuration_id"]),
                "candidate_count": len(eligibility),
                "weight_source": task["task_profile"]["weight_source"],
                "task_weights": task["task_profile"]["task_weights"],
                "effective_weights": common_score["effective_weights"],
                "weight_policy": common_score["weight_policy"],
                "reason": reason,
                "score_is_success_probability": False,
            }
            entry = state["tasks"][task["task_id"]]
            entry.update(status="reserved", reservation_id=reservation_id, route=route, dispatch=dispatch, reserved_at=utc_now(),
                         matching={"ranking": eligibility, "selection": selection, "task_profile": task["task_profile"], "routing_policy": handoff["routing_policy"]},
                         admission=decision)
            self.ledger.event(state, "route_reserved", task_id=task["task_id"], reservation_id=reservation_id, configuration_id=rank["configuration_id"], route=route, ranking=eligibility)
            response = {
                "ok": True, "status": "ready_for_main_agent", "run_id": self.run_id,
                "profile_id": profile_id, "task_id": task["task_id"], "reservation_id": reservation_id,
                "configuration_id": rank["configuration_id"], "route": route, "dispatch": dispatch,
                "depends_on": task["depends_on"], "selection": selection,
                "admission": _admission_summary(decision), "detail_available": True,
                "main_agent_instructions": [
                    "Call DSH's native subagent using dispatch. Keep its reservation marker in the prompt.",
                    "Forward necessary upstream results alongside the worker request; preserve user constraints.",
                    "Use exactly the returned provider/model and supported effort, or the recorded provider default.",
                    "Do not dispatch the same reservation twice. If no call was made, release it as not_dispatched.",
                    "Wait for the worker to finish. A started worker is not a completed dependency.",
                    "Reuse profile_id with generalrouter_match to obtain the next ready task; do not resend the plan.",
                ],
            }
            if detail:
                response["details"] = {"ranking": eligibility, "task_profile": task["task_profile"], "routing_policy": handoff["routing_policy"], "admission": decision}
            return response

    def started(self) -> dict:
        reservation_id = self.request.get("reservation_id")
        with self.ledger.edit() as state:
            task = next((item for item in state["tasks"].values() if item.get("reservation_id") == reservation_id), None)
            if task is None:
                raise BridgeError("reservation_not_in_run", "The reservation does not belong to this run.")
            if task["status"] not in ("reserved", "running"):
                return {"ok": False, "status": "reservation_already_finished"}
            actual = self.request.get("actual_route") or {}
            if not isinstance(actual, dict):
                raise BridgeError("actual_route_required", "actual_route must be an object.")
            expected = task["route"]
            mismatch = any(actual.get(key) is not None and actual[key] != expected.get(key) for key in ("provider", "model", "reasoning_effort"))
            task.update(status="running", started_at=task.get("started_at", utc_now()), child_session_id=self.request.get("child_session_id"), actual_route=actual, route_mismatch=mismatch)
            self.ledger.event(state, "worker_started", reservation_id=reservation_id, child_session_id=task.get("child_session_id"), actual_route=actual, route_mismatch=mismatch)
        return {"ok": True, "status": "worker_recorded", "route_mismatch": mismatch}

    def authorize_dispatch(self) -> dict:
        """Check a native tool's unchanged arguments immediately before dispatch."""
        reservation_id = self.request.get("reservation_id")
        call_id = self.request.get("call_id", self.host.get("call_id"))
        if not isinstance(call_id, str) or not call_id:
            raise BridgeError("dispatch_call_id_required", "Dispatch authorization needs the native tool call_id.")
        actual = self.request.get("actual_route")
        if not isinstance(actual, dict):
            raise BridgeError("actual_route_required", "Dispatch authorization needs an actual_route object.")
        self._refresh_if_needed()
        with self.ledger.edit() as state:
            task = next((item for item in state["tasks"].values() if item.get("reservation_id") == reservation_id), None)
            if task is None or task.get("status") != "reserved":
                return {"ok": False, "accepted": False, "status": "dispatch_denied", "reason": "reservation_not_ready_in_this_run"}
            expected = task["route"]
            mismatch = any(actual.get(key) != expected.get(key) for key in ("provider", "model", "reasoning_effort"))
            if mismatch:
                self.ledger.event(state, "dispatch_denied", reservation_id=reservation_id, reason="route_mismatch", actual_route=actual)
                return {"ok": False, "accepted": False, "status": "dispatch_denied", "reason": "route_mismatch"}
            if task.get("dispatch_call_id") not in (None, call_id):
                return {"ok": False, "accepted": False, "status": "dispatch_denied", "reason": "reservation_already_claimed_by_another_call"}
            decision = self.quota.admit(expected, reservation_id)
            if not decision.get("accepted"):
                self.ledger.event(state, "dispatch_denied", reservation_id=reservation_id, reason=decision.get("reason"))
                return {"ok": False, "status": "dispatch_denied", **decision}
            if not task.get("dispatch_call_id"):
                task["dispatch_call_id"] = call_id
                task["authorized_at"] = utc_now()
                self.ledger.event(state, "dispatch_authorized", reservation_id=reservation_id, call_id=call_id, route=expected)
        return {"ok": True, "accepted": True, "status": "dispatch_authorized", "reservation_id": reservation_id, "call_id": call_id}

    def release(self) -> dict:
        reservation_id = self.request.get("reservation_id")
        outcome = self.request.get("outcome", "completed")
        if outcome not in ("completed", "failed", "cancelled", "not_dispatched", "cancelled_before_dispatch"):
            raise BridgeError("unsupported_worker_outcome", "The worker outcome is not supported.")
        with self.ledger.edit() as state:
            task = next((item for item in state["tasks"].values() if item.get("reservation_id") == reservation_id), None)
            if task is None:
                raise BridgeError("reservation_not_in_run", "The reservation does not belong to this run.")
            if task.get("status") in ("completed", "failed", "pending") and task.get("outcome") == outcome:
                return {"ok": True, "status": "already_released", "reservation_id": reservation_id}
            result = self.quota.release(reservation_id, outcome)
            if result.get("released"):
                task.update(status="completed" if outcome == "completed" else "pending" if outcome in ("not_dispatched", "cancelled_before_dispatch") else "failed", outcome=outcome, finished_at=utc_now())
                self.ledger.event(state, "worker_finished", reservation_id=reservation_id, outcome=outcome)
        return {"ok": result.get("released", False), "status": "released", **result}

    def record_usage(self) -> dict:
        record = self.request.get("record")
        if not isinstance(record, dict):
            raise BridgeError("usage_record_required", "record_usage requires a record object.")
        with self.ledger.edit() as state:
            entry = RunLedger.record_usage(state, record, self._rate(record))
        return {"ok": True, "status": "usage_recorded", "record_id": entry["record_id"], "api_equivalent": entry["api_equivalent"]}

    def status(self) -> dict:
        snapshot = self.quota.snapshot(refresh=self.request.get("refresh") is True)
        mapped = [identity for identity, route in self.routes.items() if isinstance(route, dict) and route.get("enabled") is True]
        detail = self.request.get("detail") is True
        with self.ledger.edit() as state:
            statuses = {identity: task.get("status", "unknown") for identity, task in state.get("tasks", {}).items()}
            counts = {}
            for status in statuses.values():
                counts[status] = counts.get(status, 0) + 1
            result = {
                "ok": True, "status": "configured" if mapped else "setup_required", "run_id": self.run_id,
                "profile_id": state.get("profile_id"),
                "progress": {"total_tasks": len(statuses), "counts": counts, "task_statuses": statuses},
                "active_tickets": [
                    {key: ticket.get(key) for key in ("reservation_id", "task_id", "route", "created_at")}
                    for ticket in snapshot.get("reservations", []) if ticket.get("run_id") == self.run_id
                ],
                "quota": _quota_summary(snapshot, self.run_id),
                "configuration": {"status": "configured" if mapped else "setup_required", "enabled_route_count": len(mapped), "loaded_file_count": len(self.loaded_config_files)},
                "diagnostics": self._diagnostics_reference(),
                "detail_available": True,
            }
            if detail:
                result.update(
                    enabled_route_ids=mapped,
                    quota=snapshot,
                    report=RunLedger.report(state, detail=True),
                    configuration={**result["configuration"], "project_root": str(self.project_root), "state_dir": str(self.state_dir), "loaded_files": self.loaded_config_files},
                )
        return result

    def refresh_quota(self) -> dict:
        # The host scheduler shares this cache with admissions; it creates no run.
        if not any(isinstance(route, dict) and route.get("enabled") is True for route in self.routes.values()):
            return {"ok": False, "status": "setup_required", "message": "No enabled user-mapped models; background collection was skipped."}
        snapshot = self.quota.snapshot(refresh=True)
        accounts = snapshot.get("accounts", {})
        values = accounts.values() if isinstance(accounts, dict) else accounts
        complete = bool(accounts) and any(account.get("status") == "success" for account in values)
        return {"ok": complete, "status": "quota_refreshed" if complete else "quota_refresh_incomplete", "quota": snapshot}

    def report(self) -> dict:
        with self.ledger.edit() as state:
            report = RunLedger.report(state, detail=self.request.get("detail") is True)
        return {"ok": True, "status": "report", "run_id": self.run_id, "report": report, "diagnostics": self._diagnostics_reference()}


def _error_response(error: Exception, action: str, stage: str) -> dict:
    """Expose useful categories and safe constants, never arbitrary exception text."""
    code = "local_operation_failed"
    message = "The local operation failed. Use the error type, action and stage to locate the failing operation."
    diagnostic = {"type": type(error).__name__, "action": action, "stage": stage}
    if isinstance(error, BridgeError):
        code, message = error.code, str(error)
    elif isinstance(error, json.JSONDecodeError):
        code, message = "invalid_json", "A JSON request or local data file could not be parsed."
        diagnostic.update(line=error.lineno, column=error.colno)
    elif isinstance(error, TimeoutError):
        code, message = "local_operation_timeout", "The local operation timed out; retry after the current operation finishes."
    elif isinstance(error, PermissionError):
        code, message = "local_permission_denied", "The local operation lacks permission to read or write a required file."
    elif isinstance(error, FileNotFoundError):
        code, message = "required_file_missing", "A required local or bundled file is missing."
    elif isinstance(error, OSError):
        code, message = "local_io_failed", "A local file or process operation failed; use the OS error code to diagnose it."
    elif isinstance(error, QuotaError):
        safe_quota_errors = {
            "quota state lock timed out": ("quota_lock_timeout", "Quota records are busy; retry after the current operation."),
            "quota state is unreadable; existing tickets were preserved": ("quota_state_unreadable", "Quota records could not be read; existing tickets were preserved."),
            "quota state format is unsupported; existing tickets were preserved": ("quota_state_unsupported", "Quota records use an unsupported format; existing tickets were preserved."),
            "refresh_interval_seconds must be at least 600": ("quota_refresh_interval_invalid", "The quota refresh interval must be at least 600 seconds."),
            "minimum_remaining_percent must be between 0 and 100, excluding 100": ("quota_minimum_invalid", "minimum_remaining_percent must be at least 0 and below 100."),
            "management_key_not_configured": ("management_key_not_configured", "The management key source is not configured."),
            "local_management_key_source_unreadable": ("management_key_source_unreadable", "The configured management key source could not be read."),
        }
        code, message = safe_quota_errors.get(str(error), ("quota_operation_failed", "The quota operation failed; inspect the cached quota status and connection settings."))
    elif type(error).__name__ == "ProfileError":
        code, message = "profile_contract_invalid", "The profile does not satisfy task-profile.v3 or its recorded reference rules."
        raw = str(error)
        location = re.match(r"^(\$(?:\.[A-Za-z_][A-Za-z0-9_]*|\[\d+\])*)\: (.*)", raw)
        if location:
            diagnostic["field"] = location.group(1)[:180]
            reason = location.group(2)
            for prefix, description in (
                ("missing fields", "Required profile fields are missing at the reported location."),
                ("unexpected fields", "Unexpected profile fields are present at the reported location."),
                ("expected", "A profile value has the wrong type at the reported location."),
                ("value must be one of", "A profile value is outside the allowed choices at the reported location."),
                ("must be at least", "A profile number is below its allowed minimum at the reported location."),
                ("must be at most", "A profile number exceeds its allowed maximum at the reported location."),
            ):
                if reason.startswith(prefix):
                    message = description
                    break
        elif raw == "Task dependency graph contains a cycle":
            code, message = "profile_dependency_cycle", "Task dependencies contain a cycle."
        elif raw.startswith("Duplicate task_id:"):
            code, message = "profile_duplicate_task_id", "Each task must have a distinct task_id."
        else:
            safe_profile_rules = {
                "uncalibrated requires empty source/anchor IDs and four null weights": "Uncalibrated tasks require empty source and anchor IDs and four null weights.",
                "planner_estimated requires empty source/AA anchor IDs": "Planner estimates require empty source and AA anchor IDs.",
                "planner_estimated requires four numeric weights": "Planner estimates require four numeric weights.",
                "planner_estimated weights must sum to one": "The four planner-estimated weights must sum to one.",
                "referenced anchor is not recorded": "The selected AA reference anchor is not present in the bundled records.",
                "direct source binding requires the exact AA task ID": "Direct AA references require the exact recorded source task ID.",
                "an analogous new task requires an empty source task ID": "An analogous new task requires an empty source task ID.",
                "duplicate dependency": "A task repeats the same dependency.",
                "self dependency": "A task cannot depend on itself.",
                "input artifact references must not be blank": "Input artifact references must not be blank.",
            }
            for rule, description in safe_profile_rules.items():
                if raw.endswith(": " + rule):
                    message = description
                    break
    elif isinstance(error, ValueError):
        safe_value_errors = {
            "Run record identity mismatch": ("run_record_identity_mismatch", "The saved run identity does not match this request."),
            "Usage needs a stable record_id; stream updates replace the same attempt.": ("usage_record_id_required", "Usage needs a stable record_id so updates replace the same request attempt."),
            "A usage record needs a tokens object with disjoint input and output counts.": ("usage_tokens_required", "Usage needs a tokens object with disjoint input and output counts."),
            "Canonical output includes reasoning; reasoning cannot exceed output.": ("usage_reasoning_exceeds_output", "Reasoning tokens are included in output and cannot exceed it."),
            "latency_ms must be a nonnegative finite number or null": ("usage_latency_invalid", "latency_ms must be a nonnegative finite number or null."),
            "Task references and candidate profiles use different capability derivations": ("capability_derivation_mismatch", "Task references and model vectors use different capability derivations."),
            "Model vector data has no enabled routing candidates": ("no_enabled_candidates", "The model vector data has no enabled routing candidates."),
        }
        for field in ("uncached_input", "cached_input", "cache_write_input", "output", "reasoning"):
            safe_value_errors[f"tokens.{field} must be a nonnegative integer or null"] = (
                "usage_token_count_invalid", f"tokens.{field} must be a nonnegative integer or null."
            )
        code, message = safe_value_errors.get(str(error), ("local_validation_failed", "The request or local routing data failed validation at the reported stage."))
    elif isinstance(error, (KeyError, TypeError, AttributeError)):
        code, message = "local_data_shape_invalid", "The request or saved data has an unexpected structure at the reported stage."
    if isinstance(error, OSError):
        for field in ("errno", "winerror"):
            value = getattr(error, field, None)
            if isinstance(value, int):
                diagnostic[field] = value
    diagnostic.update(code=code, message=message)
    return {"ok": False, "status": "setup_required" if isinstance(error, SetupRequired) else "error", "message": message, "error": diagnostic}


def main() -> int:
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    action = "unknown"
    stage = "decode_request"
    try:
        request = json.loads(sys.stdin.read())
        if not isinstance(request, dict):
            raise BridgeError("request_object_required", "The host request must be a JSON object.")
        requested_action = request.get("action", "match")
        if requested_action not in ("match", "status", "refresh_quota", "authorize_dispatch", "started", "release", "record_usage", "report"):
            raise BridgeError("unsupported_action", "The requested host action is not supported.")
        action = requested_action
        stage = "load_host_configuration"
        bridge = HostBridge(request)
        stage = "execute_action"
        response = getattr(bridge, action)()
    except Exception as error:
        response = _error_response(error, action, stage)
    sys.stdout.write(json.dumps(response, ensure_ascii=False, allow_nan=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

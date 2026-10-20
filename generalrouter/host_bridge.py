"""JSON-over-stdin bridge for DSH; the main agent retains dispatch ownership."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

from .config import load_config
from .ledger import RunLedger, utc_now
from .quota import QuotaError, QuotaManager
from .runner import GeneralRouter


class SetupRequired(ValueError):
    pass


def _path(base: Path, value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise SetupRequired(f"Configure {label} in the private host configuration.")
    result = Path(value).expanduser()
    return result.resolve() if result.is_absolute() else (base / result).resolve()


def _object_file(path: Path) -> dict:
    if not path.is_file():
        raise SetupRequired(f"Create the local configuration file: {path}")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path.name}")
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
            raise ValueError("host must be an object")
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
                    raise SetupRequired(f"Create the explicitly selected local configuration file: {path}")
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
            raise ValueError("A DSH session_id or run_id is required")
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

    def match(self) -> dict:
        routing_path = (
            _path(self.config_path.parent, self.config["routing_config"], "routing_config")
            if self.config.get("routing_config") else self.runtime_root / "config.example.json"
        )
        router = GeneralRouter(load_config(routing_path))
        profile = self.request.get("profile")
        if not isinstance(profile, dict):
            raise ValueError("match requires the planner's task-profile.v3 object")
        original = self.request.get("original_request", self.request.get("request", ""))
        if not isinstance(original, str):
            raise ValueError("original_request must be a string")
        handoff = router.route(profile, original_request=original)
        digest = hashlib.sha256(json.dumps(profile, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()
        # Refresh before acquiring the run lock; collection can involve network latency.
        snapshot = self._refresh_if_needed()
        with self.ledger.edit() as state:
            if state.get("profile_sha256") not in (None, digest):
                raise ValueError("This run already has another plan. Keep the same profile or use a new run_id.")
            if "profile_sha256" not in state:
                state["profile_sha256"] = digest
                state["original_request"] = original
                state["host"] = {key: self.host.get(key) for key in ("name", "version", "session_id", "agent_id", "cwd")}
                for task in handoff["tasks"]:
                    state["tasks"][task["task_id"]] = {"status": "pending", "depends_on": task["depends_on"], "objective": task["objective"]}
                self.ledger.event(state, "plan_registered", profile_sha256=digest, candidate_snapshot=handoff["candidate_snapshot"])
            requested = self.request.get("task_id")
            if requested is not None and requested not in state["tasks"]:
                raise ValueError("task_id is not present in this plan")
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
                return {"ok": True, "status": "completed" if done or not statuses else "awaiting_worker_or_dependencies", "run_id": self.run_id, "tasks": [], "task_statuses": statuses}
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
                return {
                    "ok": False, "status": "setup_required_or_no_allowance", "run_id": self.run_id,
                    "task_id": task["task_id"], "ranking": eligibility, "quota": snapshot,
                    "message": "Provide your local model mappings and session allowlist, or wait for reported exhausted ECPA allowance to reset. No model was dispatched.",
                }
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
            job = {
                "task_id": task["task_id"], "reservation_id": reservation_id,
                "configuration_id": rank["configuration_id"], "route": route, "dispatch": dispatch,
                "worker_request": worker_request, "depends_on": task["depends_on"],
                "task_profile": task["task_profile"], "matching": {**task["matching"], "ranking": eligibility},
                "admission": decision,
            }
            selected_details = dict(rank)
            job["matching"]["selected_score"] = selected_details
            job["matching"]["selection_reason"] = "Highest capability-ranked callable candidate not excluded by reported ECPA percentages. Unknown allowance is recorded without pretending it is unlimited."
            entry = state["tasks"][task["task_id"]]
            entry.update(status="reserved", reservation_id=reservation_id, route=route, dispatch=dispatch, reserved_at=utc_now())
            self.ledger.event(state, "route_reserved", task_id=task["task_id"], reservation_id=reservation_id, configuration_id=rank["configuration_id"], route=route, ranking=eligibility)
            return {
                "ok": True, "status": "ready_for_main_agent", "run_id": self.run_id,
                **job, "tasks": [job],
                "main_agent_instructions": [
                    "Call DSH's native subagent using dispatch. Keep its reservation marker in the prompt.",
                    "Forward necessary upstream results alongside the worker request; preserve user constraints.",
                    "Use exactly the returned provider/model and supported effort, or the recorded provider default.",
                    "Do not dispatch the same reservation twice. If no call was made, release it as not_dispatched.",
                    "Wait for the worker to finish. A started worker is not a completed dependency.",
                    "Call generalrouter_match again with the same plan to obtain the next ready task.",
                ],
            }

    def started(self) -> dict:
        reservation_id = self.request.get("reservation_id")
        with self.ledger.edit() as state:
            task = next((item for item in state["tasks"].values() if item.get("reservation_id") == reservation_id), None)
            if task is None:
                raise ValueError("Reservation does not belong to this run")
            if task["status"] not in ("reserved", "running"):
                return {"ok": False, "status": "reservation_already_finished"}
            actual = self.request.get("actual_route") or {}
            if not isinstance(actual, dict):
                raise ValueError("actual_route must be an object")
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
            raise ValueError("Dispatch authorization needs the native tool call_id")
        actual = self.request.get("actual_route")
        if not isinstance(actual, dict):
            raise ValueError("Dispatch authorization needs actual_route")
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
            raise ValueError("Unsupported worker outcome")
        with self.ledger.edit() as state:
            task = next((item for item in state["tasks"].values() if item.get("reservation_id") == reservation_id), None)
            if task is None:
                raise ValueError("Reservation does not belong to this run")
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
            raise ValueError("record_usage requires a record object")
        with self.ledger.edit() as state:
            entry = RunLedger.record_usage(state, record, self._rate(record))
        return {"ok": True, "status": "usage_recorded", "record_id": entry["record_id"], "api_equivalent": entry["api_equivalent"]}

    def status(self) -> dict:
        snapshot = self.quota.snapshot(refresh=self.request.get("refresh") is True)
        mapped = [identity for identity, route in self.routes.items() if isinstance(route, dict) and route.get("enabled") is True]
        with self.ledger.edit() as state:
            report = RunLedger.report(state)
        return {"ok": True, "status": "configured" if mapped else "setup_required", "run_id": self.run_id,
                "enabled_route_ids": mapped, "quota": snapshot, "report": report,
                "configuration": {"project_root": str(self.project_root), "state_dir": str(self.state_dir),
                                  "loaded_files": self.loaded_config_files}}

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
            report = RunLedger.report(state)
        return {"ok": True, "status": "report", "report": report}


def main() -> int:
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        request = json.loads(sys.stdin.read())
        if not isinstance(request, dict):
            raise ValueError("Host request must be a JSON object")
        bridge = HostBridge(request)
        action = request.get("action", "match")
        if action not in ("match", "status", "refresh_quota", "authorize_dispatch", "started", "release", "record_usage", "report"):
            raise ValueError("Unsupported host action")
        response = getattr(bridge, action)()
    except SetupRequired as error:
        response = {"ok": False, "status": "setup_required", "message": str(error)}
    except (ValueError, TypeError, KeyError, OSError, QuotaError) as error:
        response = {"ok": False, "status": "error", "message": str(error)}
    except Exception:
        # Keep credentials and provider response bodies out of tool errors.
        response = {"ok": False, "status": "error", "message": "The local host operation failed; inspect local configuration and preserved run records."}
    sys.stdout.write(json.dumps(response, ensure_ascii=False, allow_nan=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

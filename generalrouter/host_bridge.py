"""JSON-over-stdin bridge for DSH; the main agent retains dispatch ownership."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re
import sys
from uuid import uuid4
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
        "policy": "reference_only; no execution admission or quota deduction",
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
        try:
            bundled = _object_file(self.runtime_root / "data/model-call-mappings.v0.1.json")
            self.routes = copy.deepcopy(bundled.get("routes", {}))
            if not isinstance(self.routes, dict):
                self.routes = {}
            overrides = self.config.get("routes", {})
            if isinstance(overrides, dict):
                for identity, override in overrides.items():
                    if isinstance(override, dict):
                        self.routes[identity] = {**self.routes.get(identity, {}), **override}
        except (ValueError, OSError, TypeError):
            self.routes = {}
        # Usage prices and quota are optional observations, not selection gates.
        try:
            self.rate_data = (
                _object_file(Path(self.config["rates_file"])) if self.config.get("rates_file")
                else _object_file(self.runtime_root / "data/api-equivalent-rates.v0.1.json")
            )
        except (ValueError, OSError):
            self.rate_data = {"rates": {}}

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

    def _router(self) -> GeneralRouter:
        routing_path = (
            _path(self.config_path.parent, self.config["routing_config"], "routing_config")
            if self.config.get("routing_config") else self.runtime_root / "config.example.json"
        )
        return GeneralRouter(load_config(routing_path))

    def _diagnostics_reference(self) -> dict:
        return {
            "ledger_reference": self.ledger.path.relative_to(self.project_root).as_posix(),
            "detail_request": "Use detail:true on status/report or match when specific diagnostics are needed.",
        }

    def _quota_snapshot(self, *, refresh: bool = False) -> dict:
        try:
            if self.config.get("quota_file"):
                path = Path(self.config["quota_file"])
                settings = _quota_settings(_object_file(path), path.parent)
            elif "quota" in self.config:
                settings = self.config["quota"]
            else:
                settings = _quota_settings(
                    _object_file(self.runtime_root / "data/quota-policy.example.json"), self.runtime_root / "data"
                )
            manager = QuotaManager(settings, self.state_dir / "quota")
            return _quota_summary(manager.snapshot(refresh=refresh), self.run_id)
        except Exception:
            # Quota collection cannot prevent a capability recommendation.
            return {"status": "unknown", "accounts": [], "policy": "reference_only",
                    "error": "quota_observation_unavailable"}

    def _planner_input(self) -> tuple[dict, str, str]:
        supplied = self.request.get("profile")
        reference = self.request.get("profile_id")
        if supplied is not None:
            if not isinstance(supplied, dict):
                raise BridgeError("profile_object_required", "profile must be a task-profile.v3 object.")
            original = self.request.get("original_request", "")
            if not isinstance(original, str):
                raise BridgeError("invalid_original_request", "original_request must be a string.")
            identity = _profile_id(self.run_id, _profile_digest({"profile": supplied, "request": original}))
            return copy.deepcopy(supplied), original, identity
        if not isinstance(reference, str) or not reference:
            raise BridgeError("profile_required", "Provide profile_json, or reuse a saved profile_id.")
        with self.ledger.edit() as state:
            saved = state.get("profiles", {}).get(reference)
            if saved is None and reference == state.get("profile_id") and isinstance(state.get("profile"), dict):
                saved = {"profile": state["profile"], "original_request": state.get("original_request", "")}
            if not isinstance(saved, dict):
                raise BridgeError("profile_not_found", "This profile_id has no saved profile in this session; provide the profile again.")
            return copy.deepcopy(saved["profile"]), saved["original_request"], reference

    def match(self) -> dict:
        profile, original, profile_id = self._planner_input()
        router = self._router()
        handoff = router.route(profile, original_request=original)
        requested = self.request.get("task_id")
        jobs = handoff["tasks"]
        if requested is not None:
            jobs = [job for job in jobs if job["task_id"] == requested]
            if not jobs:
                raise BridgeError("task_not_in_profile", "task_id is not present in the supplied profile.")
        tasks = []
        detail = self.request.get("detail") is True
        for job in jobs:
            score = job["matching"]["selected_score"]
            task = {
                "selection_id": "selection-" + uuid4().hex,
                "task_id": job["task_id"],
                "selected_model": job["selected_model"],
                "selection": {**score, "reason": job["matching"]["selection_reason"],
                              "candidate_count": len(job["matching"]["ranking"]),
                              "weight_source": job["task_profile"]["weight_source"],
                              "task_weights": job["task_profile"]["task_weights"],
                              "score_is_success_probability": False},
                "worker_request": job["worker_request"],
                "depends_on": job["depends_on"],
            }
            if detail:
                task["details"] = {"ranking": job["matching"]["ranking"], "task_profile": job["task_profile"]}
            tasks.append(task)
        warning = None
        try:
            with self.ledger.edit() as state:
                state.setdefault("profiles", {})[profile_id] = {"profile": profile, "original_request": original}
                for task, job in zip(tasks, jobs):
                    identity = task["selection_id"]
                    # A selection record is evidence, never an execution ticket.
                    state["tasks"][identity] = {
                        "task_id": job["task_id"], "profile_id": profile_id,
                        "status": "not_observed", "acceptance_status": "unknown",
                        "selected_at": utc_now(), "selected_model": task["selected_model"],
                        "selection_kind": "recommendation", "matching": job["matching"],
                        "task_profile": job["task_profile"],
                    }
                    self.ledger.event(state, "model_selected", selection_id=identity,
                                      task_id=identity, profile_task_id=job["task_id"],
                                      selected_model=task["selected_model"], selection=task["selection"])
        except Exception:
            warning = "Selection computed, but its local record could not be saved; profile_id reuse and linked result recording are unavailable for this response."
        response = {
            "ok": True, "status": "matched" if tasks else "no_tasks",
            "mode": "selection_only", "run_id": self.run_id, "profile_id": profile_id,
            "record_saved": warning is None, "execution_owner": "main_agent",
            "invocation_verified": False, "tasks": tasks,
            "main_agent_instructions": [
                "Use your existing tools to invoke the selected model. GeneralRouter does not choose a provider or tool, supply dispatch arguments, or authorize execution.",
                "Use the corresponding effort when the chosen tool supports it; otherwise use default and disclose that fallback.",
                "The main agent owns task ordering, worker calls and failures. For repair, start a fresh worker of the same model with the failure evidence; no Router preparation or rematching is needed.",
                "selection_id is only an optional result-recording link. No marker, ticket, status poll or recording call is required to execute work.",
            ],
        }
        if len(tasks) == 1:
            # Keep the common single-task response compact.
            response.pop("tasks")
            response.update(tasks[0])
        if detail:
            response["routing_policy"] = handoff["routing_policy"]
        if warning:
            response["recording_warning"] = warning
        return response

    def record_result(self) -> dict:
        identity = self.request.get("selection_id")
        execution = self.request.get("execution_status", "unknown")
        acceptance = self.request.get("acceptance_status", "unknown")
        if execution not in ("unknown", "running", "completed", "failed", "cancelled", "not_started"):
            raise BridgeError("invalid_execution_status", "Use a documented execution status or unknown.")
        if acceptance not in ("unknown", "passed", "failed"):
            raise BridgeError("invalid_acceptance_status", "Acceptance must be unknown, passed or failed.")
        summary = self.request.get("summary", "")
        evidence = self.request.get("evidence", [])
        if not isinstance(summary, str) or not isinstance(evidence, list) or any(not isinstance(item, str) for item in evidence):
            raise BridgeError("invalid_result_evidence", "Provide a summary string and an array of evidence references.")
        with self.ledger.edit() as state:
            task = state["tasks"].get(identity) if isinstance(identity, str) else None
            if task is None:
                raise BridgeError("selection_not_found", "The optional result link has no saved selection. Execution is not blocked by this recording error.")
            result = {"execution_status": execution, "acceptance_status": acceptance,
                      "summary": summary, "evidence": evidence, "source": "main_agent_reported", "at": utc_now()}
            for key in ("tool_name", "provider", "model", "effort", "child_session_id"):
                value = self.request.get(key)
                if value is not None:
                    if not isinstance(value, str) or not value.strip():
                        raise BridgeError("invalid_result_identity", "Optional result identities must be nonempty strings or omitted.")
                    result[key] = value
            task.setdefault("result_history", []).append(result)
            task.update(status=execution, acceptance_status=acceptance, outcome=execution)
            # Keep reported identities separate from provider/header observations.
            task["reported_execution"] = result
            if result.get("child_session_id"):
                task["child_session_id"] = result["child_session_id"]
            self.ledger.event(state, "result_reported", task_id=identity,
                              child_session_id=result.get("child_session_id"), result=result)
        return {"ok": True, "status": "result_recorded", "selection_id": identity,
                "source": "main_agent_reported", "verified_by_router": False}

    def observe_tool(self) -> dict:
        observed = self.request.get("observation")
        if not isinstance(observed, dict):
            raise BridgeError("observation_required", "A read-only tool observation is required.")
        # Never persist arbitrary tool input, output, prompts or credentials.
        fields = {key: observed.get(key) for key in (
            "call_id", "tool_name", "child_session_id", "is_error", "observed_at"
        )}
        with self.ledger.edit() as state:
            self.ledger.event(state, "delegation_tool_result_observed", **fields)
        return {"ok": True, "status": "tool_observation_recorded"}

    def status(self) -> dict:
        with self.ledger.edit() as state:
            progress = {identity: {"task_id": task.get("task_id", identity),
                                   "execution_status": task.get("status", "unknown"),
                                   "acceptance_status": task.get("acceptance_status", "unknown")}
                        for identity, task in state.get("tasks", {}).items()}
        return {"ok": True, "status": "selection_only", "run_id": self.run_id,
                "records": progress, "execution_owner": "main_agent",
                "quota": self._quota_snapshot(refresh=self.request.get("refresh") is True),
                "diagnostics": self._diagnostics_reference()}

    def refresh_quota(self) -> dict:
        snapshot = self._quota_snapshot(refresh=True)
        return {"ok": True, "status": "quota_reference", "quota": snapshot}

    def report(self) -> dict:
        with self.ledger.edit() as state:
            report = RunLedger.report(state, detail=self.request.get("detail") is True)
        return {"ok": True, "status": "report", "mode": "selection_only", "run_id": self.run_id,
                "report": report, "diagnostics": self._diagnostics_reference(),
                "coverage": "Passive DSH session usage only. External plugin workers may not expose request/token events here. Missing worker usage or task links is unknown, never zero; totals are observed subtotals, not guaranteed whole-workflow totals."}

    def record_usage(self) -> dict:
        record = self.request.get("record")
        if not isinstance(record, dict):
            raise BridgeError("usage_record_required", "record_usage requires a record object.")
        with self.ledger.edit() as state:
            entry = RunLedger.record_usage(state, record, self._rate(record))
        return {"ok": True, "status": "usage_recorded", "record_id": entry["record_id"], "api_equivalent": entry["api_equivalent"]}


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
        if requested_action not in ("match", "record_result", "observe_tool", "status", "refresh_quota", "record_usage", "report"):
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

"""Read real ECPA allowance percentages with an activity-driven ten-minute cache.

Dispatch tickets record task identity and observed windows; no estimated quota
percentage is held or deducted.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import copy
import json
import math
import os
from pathlib import Path
import time
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4


class QuotaError(RuntimeError):
    """A local or collection error without secret details."""


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _fraction(value: Any) -> float | None:
    if isinstance(value, str) and value.strip().endswith("%"):
        parsed = _number(value.strip()[:-1])
        return None if parsed is None else max(0.0, min(1.0, parsed / 100.0))
    parsed = _number(value)
    return None if parsed is None else max(0.0, min(1.0, parsed))


def _instant(value: Any) -> float | None:
    numeric = _number(value)
    if numeric is not None:
        return numeric / 1000 if numeric > 1e12 else numeric
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc).timestamp() if parsed.tzinfo is None else parsed.timestamp()
    except ValueError:
        return None


def _iso(value: float | None) -> str | None:
    return None if value is None else datetime.fromtimestamp(value, timezone.utc).isoformat()


def _pick(value: dict[str, Any], *keys: str) -> Any:
    return next((value[key] for key in keys if value.get(key) is not None), None)


def _codex_account_id(metadata: dict[str, Any]) -> str | None:
    records = [metadata] + [metadata[key] for key in ("metadata", "attributes") if isinstance(metadata.get(key), dict)]
    for record in list(records):
        for key in ("id_token", "idToken"):
            claims = record.get(key)
            if isinstance(claims, dict):
                records.append(claims)
                auth = claims.get("https://api.openai.com/auth")
                if isinstance(auth, dict):
                    records.append(auth)
    for record in records:
        value = _pick(record, "chatgpt_account_id", "chatgptAccountId", "account_id", "accountId")
        if isinstance(value, str) and value:
            return value
    return None


def _reset(window: dict[str, Any], collected_at: float) -> float | None:
    absolute = _instant(_pick(window, "reset_at", "resetAt", "reset_time", "resetTime"))
    relative = _number(_pick(window, "reset_after_seconds", "resetAfterSeconds"))
    return absolute if absolute is not None else (collected_at + relative if relative is not None else None)


def _codex_windows(payload: dict[str, Any], collected_at: float) -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []

    def append_limit(raw: Any, prefix: str) -> None:
        if not isinstance(raw, dict):
            return
        blocked = raw.get("allowed") is False or _pick(raw, "limit_reached", "limitReached") is True
        for kind in ("primary", "secondary"):
            value = _pick(raw, f"{kind}_window", f"{kind}Window")
            if not isinstance(value, dict):
                continue
            used = _number(_pick(value, "used_percent", "usedPercent"))
            remaining = None if used is None else max(0.0, min(100.0, 100.0 - used))
            if blocked:
                remaining = 0.0
            windows.append({"id": f"{prefix}.{kind}", "remaining_percent": remaining,
                            "reset_at": _iso(_reset(value, collected_at)),
                            "duration_seconds": _number(_pick(value, "limit_window_seconds", "limitWindowSeconds")),
                            "applicability": "account_shared" if prefix == "rate_limit" else "window_model_scope_unknown"})

    append_limit(_pick(payload, "rate_limit", "rateLimit"), "rate_limit")
    append_limit(_pick(payload, "code_review_rate_limit", "codeReviewRateLimit"), "code_review")
    additional = _pick(payload, "additional_rate_limits", "additionalRateLimits")
    for index, item in enumerate(additional if isinstance(additional, list) else []):
        if isinstance(item, dict):
            name = str(_pick(item, "limit_name", "limitName", "metered_feature", "meteredFeature") or index)
            append_limit(_pick(item, "rate_limit", "rateLimit"), f"additional.{name}")
    return windows


def _antigravity_windows(payload: dict[str, Any], collected_at: float) -> list[dict[str, Any]]:
    nested = payload.get("body")
    if isinstance(nested, str):
        try:
            nested = json.loads(nested)
        except ValueError:
            nested = None
    if not isinstance(payload.get("groups"), list) and isinstance(nested, dict):
        payload = nested
    windows: list[dict[str, Any]] = []
    for group_index, group in enumerate(payload.get("groups", [])):
        if not isinstance(group, dict):
            continue
        for index, bucket in enumerate(group.get("buckets", []) if isinstance(group.get("buckets"), list) else []):
            if not isinstance(bucket, dict):
                continue
            fraction = _fraction(_pick(bucket, "remaining_fraction", "remainingFraction"))
            windows.append({"id": f"reported:{group_index}:{index}",
                            "reported_label": _pick(group, "display_name", "displayName"),
                            "window": bucket.get("window"), "description": bucket.get("description"),
                            "remaining_percent": None if fraction is None else fraction * 100.0,
                            "reset_at": _iso(_reset(bucket, collected_at)),
                            "applicability": "window_model_scope_unknown"})
    return windows


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        return None


class QuotaManager:
    """Only snapshot(refresh=True) performs requests; no idle timer runs here."""

    def __init__(self, config: dict[str, Any], state_dir: Path):
        self.config = copy.deepcopy(config)
        interval = _number(self.config.get("refresh_interval_seconds", 600))
        minimum = _number(self.config.get("minimum_remaining_percent", 0))
        if interval is None or interval < 600:
            raise QuotaError("refresh_interval_seconds must be at least 600")
        if minimum is None or not 0 <= minimum < 100:
            raise QuotaError("minimum_remaining_percent must be between 0 and 100, excluding 100")
        self.refresh_interval = interval
        self.minimum_remaining = minimum
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.state_dir / "quota-percent-state.json"
        self.lock_path = self.state_dir / "quota-percent-state.lock"

    @contextmanager
    def _locked_state(self, write: bool = False) -> Iterator[dict[str, Any]]:
        with self.lock_path.open("a+b") as lock:
            lock.seek(0, os.SEEK_END)
            if lock.tell() == 0:
                lock.write(b"0")
                lock.flush()
            deadline = time.monotonic() + float(self.config.get("lock_timeout_seconds", 15))
            while True:
                try:
                    lock.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except (BlockingIOError, OSError):
                    if time.monotonic() >= deadline:
                        raise QuotaError("quota state lock timed out") from None
                    time.sleep(0.05)
            try:
                state = {"schema_version": "generalrouter-quota-state.v2", "accounts": {}, "reservations": {},
                         "last_attempt_at": None, "last_success_at": None, "collection_status": "unknown"}
                if self.state_path.exists():
                    try:
                        state = json.loads(self.state_path.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        raise QuotaError("quota state is unreadable; existing tickets were preserved") from None
                    if (not isinstance(state, dict) or state.get("schema_version") != "generalrouter-quota-state.v2"
                            or not isinstance(state.get("accounts"), dict) or not isinstance(state.get("reservations"), dict)):
                        raise QuotaError("quota state format is unsupported; existing tickets were preserved")
                yield state
                if write:
                    temporary = self.state_dir / f".quota-percent-{uuid4().hex}.tmp"
                    try:
                        with temporary.open("w", encoding="utf-8", newline="\n") as destination:
                            json.dump(state, destination, ensure_ascii=False, indent=2, allow_nan=False)
                            destination.write("\n")
                            destination.flush()
                            os.fsync(destination.fileno())
                        os.replace(temporary, self.state_path)
                    finally:
                        temporary.unlink(missing_ok=True)
            finally:
                lock.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _management_json(self, operation: str, payload: dict[str, Any] | None = None,
                         query: dict[str, str] | None = None) -> dict[str, Any]:
        connection = self.config.get("connection", {})
        version = connection.get("management_api_version")
        if version not in ("v0", "v8"):
            raise QuotaError("management_api_version must be explicitly v0 or v8")
        base = str(connection.get("management_base_url", "")).rstrip("/")
        parsed = urlsplit(base)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
            raise QuotaError("management_base_url must be an origin without credentials or a path")
        if parsed.scheme == "http" and parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise QuotaError("remote management connections require HTTPS")
        key_env = connection.get("management_key_env")
        key = os.environ.get(str(key_env), "") if key_env else ""
        if not key and connection.get("management_key_toml_file"):
            try:
                import tomllib
                with Path(connection["management_key_toml_file"]).open("rb") as source:
                    stored = tomllib.load(source)
                value = stored.get("management-secret-key")
                key = value if isinstance(value, str) else ""
            except (ImportError, OSError, ValueError):
                raise QuotaError("local_management_key_source_unreadable") from None
        if not key:
            raise QuotaError("management_key_not_configured")
        root = "/auth-files" if version == "v0" else "/credentials"
        paths = {"api_call": "/api-call" if version == "v0" else "/requests/api-call",
                 "credentials": root, "credential_models": f"{root}/models"}
        if operation not in paths:
            raise QuotaError("unsupported_management_operation")
        suffix = paths[operation]
        if query:
            suffix += "?" + urlencode(query)
        request = Request(f"{base}/{version}/management{suffix}",
                          data=None if payload is None else json.dumps(payload, allow_nan=False).encode("utf-8"),
                          method="GET" if payload is None else "POST",
                          headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        try:
            with build_opener(_NoRedirect()).open(request, timeout=float(connection.get("timeout_seconds", 20))) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise QuotaError("management response exceeded size limit")
            envelope = json.loads(raw)
        except (HTTPError, URLError):
            raise QuotaError("management quota request failed") from None
        if not isinstance(envelope, dict):
            raise QuotaError("management response is not an object")
        return envelope

    def _management_api_call(self, payload: dict[str, Any]) -> dict[str, Any]:
        envelope = self._management_json("api_call", payload)
        status = _number(_pick(envelope, "status_code", "statusCode"))
        if status is None or not 200 <= status < 300:
            raise QuotaError("upstream quota request failed")
        body = _pick(envelope, "body", "bodyText")
        body = json.loads(body) if isinstance(body, str) else body
        if not isinstance(body, dict):
            raise QuotaError("upstream quota response is not an object")
        return body

    def _collect_account(self, credential: dict[str, Any]) -> dict[str, Any]:
        provider = str(_pick(credential, "provider", "type") or "").lower().replace("anti-gravity", "antigravity")
        auth_index = str(_pick(credential, "auth_index", "authIndex") or "")
        name = str(_pick(credential, "name", "id") or "")
        identity = f"{provider}:{auth_index or name}"
        result: dict[str, Any] = {"account_id": identity, "provider": provider,
                                  "credential_type": str(credential.get("account_type", "unknown")).lower().replace("-", "_"),
                                  "auth_index": auth_index, "model_ids": [], "models_status": "unknown",
                                  "status": "unknown", "windows": [], "fetched_at": _iso(time.time()),
                                  "error": "allowance_not_reported"}
        try:
            if not name:
                raise QuotaError("credential_name_missing")
            catalog = self._management_json("credential_models", query={"name": name})
            models = catalog.get("models")
            if not isinstance(models, list):
                raise QuotaError("credential_model_catalog_missing")
            result["model_ids"] = sorted({str(model["id"]) for model in models
                                           if isinstance(model, dict) and isinstance(model.get("id"), str)})
            result["models_status"] = "success"
        except (QuotaError, ValueError, TypeError, OSError) as error:
            result["models_error"] = str(error) if isinstance(error, QuotaError) else "credential_model_catalog_failed"
        if result["credential_type"] != "oauth" or provider not in ("codex", "antigravity"):
            return result
        try:
            if not auth_index:
                raise QuotaError("credential_auth_index_missing")
            headers = {"Authorization": "Bearer $TOKEN$", "Content-Type": "application/json"}
            request: dict[str, Any] = {"authIndex": auth_index, "header": headers}
            if provider == "codex":
                headers["User-Agent"] = "codex-tui/0.149.1"
                account_id = _codex_account_id(credential)
                if account_id:
                    headers["Chatgpt-Account-Id"] = account_id
                request.update(url="https://chatgpt.com/backend-api/wham/usage", method="GET")
                payload = self._management_api_call(request)
                windows = _codex_windows(payload, time.time())
            else:
                project = credential.get("project_id")
                if not isinstance(project, str) or not project:
                    raise QuotaError("antigravity_project_not_reported_in_metadata")
                headers["User-Agent"] = "antigravity/hub/2.23.0"
                request.update(method="POST", data=json.dumps({"project": project}))
                windows = []
                for origin in ("daily-cloudcode-pa.googleapis.com", "daily-cloudcode-pa.sandbox.googleapis.com", "cloudcode-pa.googleapis.com"):
                    try:
                        request["url"] = f"https://{origin}/v1internal:retrieveUserQuotaSummary"
                        payload = self._management_api_call(request)
                        windows = _antigravity_windows(payload, time.time())
                        if windows:
                            break
                    except (QuotaError, ValueError, TypeError, OSError):
                        continue
                if not windows:
                    raise QuotaError("antigravity_allowance_not_reported")
            result.update(status="success", windows=windows, fetched_at=_iso(time.time()))
            result.pop("error", None)
        except (QuotaError, ValueError, TypeError, OSError) as error:
            result.update(status="unknown", fetched_at=_iso(time.time()),
                          error=str(error) if isinstance(error, QuotaError) else "quota_collection_failed")
        return result

    def snapshot(self, refresh: bool = False) -> dict[str, Any]:
        attempt_id = None
        if refresh:
            with self._locked_state(write=True) as state:
                now = time.time()
                last_attempt = _instant(state.get("last_attempt_at"))
                if last_attempt is None or now - last_attempt >= self.refresh_interval:
                    attempt_id = uuid4().hex
                    state.update(last_attempt_at=_iso(now), refresh_attempt_id=attempt_id, collection_status="loading")
            if attempt_id:
                accounts = None
                error = None
                try:
                    payload = self._management_json("credentials")
                    records = payload.get("files")
                    if not isinstance(records, list):
                        raise QuotaError("credential_list_missing")
                    accounts = {}
                    for record in records:
                        if not isinstance(record, dict) or record.get("disabled") is True or str(record.get("status", "")).lower() == "disabled":
                            continue
                        observed = self._collect_account(record)
                        accounts[observed["account_id"]] = observed
                except (QuotaError, ValueError, TypeError, OSError) as problem:
                    error = str(problem) if isinstance(problem, QuotaError) else "quota_collection_failed"
                with self._locked_state(write=True) as state:
                    if state.get("refresh_attempt_id") == attempt_id:
                        if accounts is not None:
                            state.update(accounts=accounts, collection_status="success", last_success_at=_iso(time.time()))
                            state.pop("error", None)
                        else:
                            state.update(collection_status="unknown", error=error)
        with self._locked_state() as state:
            now = time.time()
            last_attempt = _instant(state.get("last_attempt_at"))
            due = 0.0 if last_attempt is None else max(0.0, self.refresh_interval - (now - last_attempt))
            accounts = copy.deepcopy(state["accounts"])
            for account in accounts.values():
                fetched = _instant(account.get("fetched_at"))
                account["age_seconds"] = None if fetched is None else max(0.0, now - fetched)
                account["stale"] = fetched is None or now - fetched >= self.refresh_interval or state.get("collection_status") != "success"
            return {"schema_version": "generalrouter-quota-snapshot.v2", "generated_at": _iso(now),
                    "status": state.get("collection_status", "unknown"), "accounts": accounts,
                    "last_attempt_at": state.get("last_attempt_at"), "last_success_at": state.get("last_success_at"),
                    "stale": due == 0, "refresh_due_seconds": due, "refresh_interval_seconds": self.refresh_interval,
                    "error": state.get("error"),
                    "reservations": [copy.deepcopy(item) for item in state["reservations"].values() if item.get("status") == "active"],
                    "reservation_unit": "dispatch ticket only; no percentage is estimated, held, or subtracted"}

    @staticmethod
    def _route_identity(route: dict[str, Any]) -> dict[str, Any]:
        return {key: route.get(key) for key in ("configuration_id", "provider", "model", "reasoning_effort")}

    def _admit(self, route: dict[str, Any], state: dict[str, Any], reservation_id: str | None = None) -> dict[str, Any]:
        decision: dict[str, Any] = {"accepted": True, "quota_status": "unknown", "reservation_id": reservation_id,
                                    "reason": "allowance_not_reported", "remaining_percent": None,
                                    "minimum_remaining_percent": self.minimum_remaining}
        ticket = state["reservations"].get(reservation_id) if reservation_id else None
        if reservation_id and (not ticket or ticket.get("status") != "active" or ticket.get("route") != self._route_identity(route)):
            return {**decision, "accepted": False, "reason": "dispatch_ticket_missing_or_route_mismatch"}
        model = route.get("model")
        accounts = state["accounts"]
        matches = [account for account in accounts.values() if model in account.get("model_ids", [])]
        if not matches:
            return {**decision, "detail": "no_exact_credential_model_evidence"}
        now = time.time()
        evidence = []
        incomplete_catalog = any(account.get("models_status") != "success" for account in accounts.values())
        all_known = not incomplete_catalog
        positives = []
        for account in matches:
            item = {"account_id": account["account_id"], "provider": account.get("provider"),
                    "remaining_percent": None, "status": "unknown", "reason": "allowance_not_reported", "windows": []}
            fetched = _instant(account.get("fetched_at"))
            fresh = (state.get("collection_status") == "success" and fetched is not None
                     and 0 <= now - fetched < self.refresh_interval)
            if not fresh:
                item["reason"] = "allowance_snapshot_stale_or_unavailable"
            elif account.get("credential_type") == "oauth" and account.get("status") == "success":
                applicable = [window for window in account.get("windows", []) if window.get("applicability") == "account_shared"]
                if not applicable:
                    item["reason"] = "window_model_scope_unknown"
                else:
                    invalid = False
                    for window in applicable:
                        percent = _number(window.get("remaining_percent"))
                        reset = _instant(window.get("reset_at"))
                        item["windows"].append(copy.deepcopy(window))
                        if percent is None or not 0 <= percent <= 100 or (reset is not None and reset <= now):
                            invalid = True
                    if invalid:
                        item["reason"] = "reported_allowance_window_unknown_or_reset"
                    else:
                        remaining = min(window["remaining_percent"] for window in applicable)
                        item.update(status="reported", remaining_percent=remaining, reason="reported_percentage")
                        if remaining > self.minimum_remaining:
                            positives.append(remaining)
            if item["status"] != "reported":
                all_known = False
            evidence.append(item)
        decision["accounts"] = evidence
        decision["catalog_incomplete"] = incomplete_catalog
        if positives:
            decision.update(quota_status="reported", reason="reported_percentage_available",
                            remaining_percent=max(positives))
        elif all_known and evidence:
            decision.update(accepted=False, quota_status="reported", reason="reported_percentage_at_or_below_minimum",
                            remaining_percent=max(item["remaining_percent"] for item in evidence))
        else:
            decision["detail"] = next((item["reason"] for item in evidence if item["status"] != "reported"), "allowance_not_reported")
        decision["limitation"] = "Percentages are cached observations; actual credential selection remains with ECPA and no per-job consumption is assumed."
        return decision

    def admit(self, route: dict[str, Any], reservation_id: str | None = None) -> dict[str, Any]:
        with self._locked_state() as state:
            return self._admit(route, state, reservation_id)

    def reserve(self, route: dict[str, Any], task_id: str, run_id: str) -> dict[str, Any]:
        if not task_id or not run_id:
            raise QuotaError("task_id and run_id are required for a dispatch ticket")
        with self._locked_state(write=True) as state:
            for identifier, item in state["reservations"].items():
                if item.get("status") == "active" and item.get("task_id") == task_id and item.get("run_id") == run_id:
                    if item.get("route") != self._route_identity(route):
                        return {"accepted": False, "reason": "task_already_reserved_for_another_route", "reservation_id": identifier}
                    return {**self._admit(route, state, identifier), "reservation": copy.deepcopy(item)}
            decision = self._admit(route, state)
            if not decision["accepted"]:
                return decision
            identifier = uuid4().hex
            bindings = [{"account_id": account["account_id"], "window_ids": [window["id"] for window in account["windows"]]}
                        for account in decision.get("accounts", [])]
            state["reservations"][identifier] = {"reservation_id": identifier, "task_id": task_id, "run_id": run_id,
                                                   "route": self._route_identity(route), "observed_windows": bindings,
                                                   "created_at": _iso(time.time()), "status": "active",
                                                   "basis": "dispatch_identity_only; no assumed quota consumption"}
            return {**decision, "reservation_id": identifier, "reservation": copy.deepcopy(state["reservations"][identifier])}

    def release(self, reservation_id: str, outcome: str = "completed") -> dict[str, Any]:
        with self._locked_state(write=True) as state:
            item = state["reservations"].get(reservation_id)
            if not item:
                return {"released": False, "reservation_id": reservation_id, "reason": "reservation_not_found"}
            if item.get("status") != "active":
                return {"released": True, "reservation_id": reservation_id, "reason": "already_released"}
            item.update(status="released", outcome=outcome, released_at=_iso(time.time()))
            return {"released": True, "reservation_id": reservation_id, "outcome": outcome}

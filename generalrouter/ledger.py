"""Local, idempotent run records and token-priced API equivalents."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import time
from typing import Any


TOKEN_FIELDS = ("uncached_input", "cached_input", "cache_write_input", "output", "reasoning")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def exclusive_file(path: Path, timeout: float = 15.0):
    """Use an OS lock: process termination releases it without stale-lock deletion."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    raise TimeoutError("Local run records are busy; retry after the current operation.") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def canonical_tokens(raw: Any) -> dict[str, int | None]:
    if not isinstance(raw, dict):
        raise ValueError("A usage record needs a tokens object with disjoint input and output counts.")
    result = {}
    for field in TOKEN_FIELDS:
        value = raw.get(field)
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError(f"tokens.{field} must be a nonnegative integer or null")
        result[field] = value
    if result["reasoning"] is not None and result["output"] is not None:
        if result["reasoning"] > result["output"]:
            raise ValueError("Canonical output includes reasoning; reasoning cannot exceed output.")
    return result


def api_equivalent(tokens: dict, rate: dict | None) -> dict:
    """Reasoning is an output subset; never add it a second time."""
    rates = rate.get("usd_per_million_tokens", {}) if isinstance(rate, dict) else {}
    components = {}
    missing = []
    for field in TOKEN_FIELDS[:-1]:
        count = tokens[field]
        unit_price = rates.get(field)
        if count == 0:
            components[field] = 0.0
        elif count is None:
            components[field] = None
            missing.append(f"tokens.{field}")
        elif (
            isinstance(unit_price, bool)
            or not isinstance(unit_price, (int, float))
            or not math.isfinite(unit_price)
            or unit_price < 0
        ):
            components[field] = None
            missing.append(f"rate.{field}")
        else:
            components[field] = count * unit_price / 1_000_000
    subtotal = math.fsum(value for value in components.values() if value is not None)
    return {
        "currency": "USD",
        "kind": "api_equivalent_not_subscription_invoice",
        "complete": not missing,
        "total": subtotal if not missing else None,
        "known_subtotal": subtotal,
        "missing": missing,
        "components": components,
        "rate_snapshot": rate,
        "reasoning_accounting": "included_in_output",
    }


def _instant(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
    except ValueError:
        return None


def _usage_summary(records: list[dict]) -> dict:
    tokens = {}
    for field in TOKEN_FIELDS:
        values = [entry.get("tokens", {}).get(field) for entry in records]
        known = sum(value for value in values if value is not None)
        unknown = sum(value is None for value in values)
        tokens[field] = {
            "known_sum": known,
            "unknown_records": unknown,
            "total": known if records and not unknown else None,
        }
    costs = [entry.get("api_equivalent", {}) for entry in records]
    subtotal = math.fsum(cost.get("known_subtotal", 0.0) for cost in costs)
    incomplete = sum(cost.get("complete") is not True for cost in costs)
    missing = {}
    for cost in costs:
        for field in cost.get("missing", []):
            missing[field] = missing.get(field, 0) + 1
    latencies = [entry.get("latency_ms") for entry in records]
    known_latency = math.fsum(value for value in latencies if value is not None)
    return {
        "usage_record_count": len(records),
        "usage_observed": bool(records),
        "tokens": tokens,
        "api_equivalent_cost_usd": subtotal if records and not incomplete else None,
        "known_api_equivalent_subtotal_usd": subtotal,
        "incomplete_cost_records": incomplete,
        "cost_missing_fields": missing,
        "reported_request_latency_ms": {
            "known_sum": known_latency,
            "unknown_records": sum(value is None for value in latencies),
            "total": known_latency if records and all(value is not None for value in latencies) else None,
            "meaning": "Sum of reported request latencies, not agent or workflow wall-clock time.",
        },
    }


class RunLedger:
    def __init__(self, state_dir: Path, run_id: str):
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id must be a nonempty session or case-study identifier")
        self.run_id = run_id
        digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
        self.path = Path(state_dir) / "runs" / (digest + ".json")
        self.lock_path = self.path.with_suffix(".lock")

    @contextmanager
    def edit(self):
        with exclusive_file(self.lock_path):
            if self.path.exists():
                state = json.loads(self.path.read_text(encoding="utf-8-sig"))
            else:
                state = {
                    "schema_version": "generalrouter-run.v1",
                    "run_id": self.run_id,
                    "created_at": utc_now(),
                    "tasks": {},
                    "usage": {},
                    "events": [],
                }
            if state.get("run_id") != self.run_id:
                raise ValueError("Run record identity mismatch")
            yield state
            state["updated_at"] = utc_now()
            atomic_json(self.path, state)

    @staticmethod
    def event(state: dict, kind: str, **details) -> None:
        state["events"].append({"kind": kind, "at": utc_now(), **details})

    @staticmethod
    def record_usage(state: dict, record: dict, rate: dict | None) -> dict:
        identity = record.get("record_id")
        if not isinstance(identity, str) or not identity:
            raise ValueError("Usage needs a stable record_id; stream updates replace the same attempt.")
        tokens = canonical_tokens(record.get("tokens"))
        # Whitelist metadata: credentials and arbitrary request bodies are not usage data.
        entry = {
            key: record.get(key)
            for key in (
                "record_id", "session_id", "task_id", "reservation_id", "provider", "model",
                "reasoning_effort", "role", "started_at", "finished_at", "latency_ms",
                "call_id", "outcome", "source", "usage_final", "configuration_id",
            )
        }
        latency = entry.get("latency_ms")
        if latency is not None and (
            isinstance(latency, bool) or not isinstance(latency, (int, float))
            or not math.isfinite(latency) or latency < 0
        ):
            raise ValueError("latency_ms must be a nonnegative finite number or null")
        previous = state["usage"].get(identity, {})
        # Final assistant messages may contain fuller counts than earlier stream snapshots.
        # Merge unknown fields, but do not sum two representations of the same request.
        old_tokens = previous.get("tokens", {})
        for field, count in tokens.items():
            if count is None and old_tokens.get(field) is not None:
                tokens[field] = old_tokens[field]
        entry = {key: previous.get(key) if value is None else value for key, value in entry.items()}
        entry["tokens"] = tokens
        entry["api_equivalent"] = api_equivalent(tokens, rate or previous.get("api_equivalent", {}).get("rate_snapshot"))
        entry["recorded_at"] = utc_now()
        state["usage"][identity] = entry
        return entry

    @staticmethod
    def report(state: dict, *, detail: bool = False) -> dict:
        records = list(state.get("usage", {}).values())
        tasks = state.get("tasks", {})
        events = state.get("events", [])
        instants = [instant for instant in (_instant(value) for value in (
            [event.get("at") for event in state.get("events", [])]
            + [record.get(field) for record in records for field in ("started_at", "finished_at")]
        )) if instant is not None]
        recorded = [instant for instant in (_instant(record.get("recorded_at")) for record in records) if instant is not None]
        by_role: dict[str, list[dict]] = {"planner": [], "worker": []}
        by_task: dict[str, list[dict]] = {identity: [] for identity in tasks}
        unassigned = []
        for record in records:
            role = record.get("role")
            role = role if isinstance(role, str) and role else "unknown"
            by_role.setdefault(role, []).append(record)
            identity = record.get("task_id")
            if isinstance(identity, str) and identity:
                by_task.setdefault(identity, []).append(record)
            else:
                unassigned.append(record)
        task_summaries = {}
        for identity, usage in by_task.items():
            task = tasks.get(identity, {})
            started = _instant(task.get("started_at"))
            finished = _instant(task.get("finished_at"))
            terminal = task.get("status") in ("completed", "failed")
            elapsed = (finished - started).total_seconds() if terminal and started and finished and finished >= started else None
            task_summaries[identity] = {
                **_usage_summary(usage),
                "status": task.get("status", "unknown"),
                "outcome": task.get("outcome"),
                "route": task.get("route"),
                "actual_route": task.get("actual_route"),
                "route_mismatch": task.get("route_mismatch"),
                "started_at": task.get("started_at"),
                "finished_at": task.get("finished_at"),
                "recorded_start_to_finish_seconds": elapsed,
            }
        result = {
            "schema_version": "generalrouter-run-report.v2",
            "run_id": state["run_id"],
            "profile_id": state.get("profile_id"),
            "created_at": state.get("created_at"),
            "updated_at": state.get("updated_at"),
            "snapshot": {
                "generated_at": utc_now(),
                "usage_record_count": len(records),
                "event_count": len(events),
                "usage_recorded_through": max(recorded).isoformat() if recorded else None,
                "scope": "Saved records at this snapshot; later requests and the final reply are not included.",
            },
            **_usage_summary(records),
            "by_role": {role: _usage_summary(usage) for role, usage in by_role.items()},
            "role_meaning": {"planner": "main_agent", "worker": "native_subagent", "unknown": "not_reported"},
            "by_task": task_summaries,
            "unassigned_task_usage": _usage_summary(unassigned),
            "reasoning_is_output_subset": True,
            "cost_kind": "api_equivalent_not_subscription_invoice",
            "prices": "Stored rate snapshots; this report does not fetch or verify current prices.",
            "task_outcomes": {identity: task.get("outcome", task.get("status")) for identity, task in tasks.items()},
            "observed_first_at": min(instants).isoformat() if instants else None,
            "observed_last_at": max(instants).isoformat() if instants else None,
            "observed_span_seconds": (max(instants) - min(instants)).total_seconds() if instants else None,
            "scope": "Observed DSH requests, including planner, workers, retries and failed attempts when reported. Missing usage stays unknown.",
            "wall_clock": "Observed span covers saved event and request timestamps. Task start-to-finish can span retries. Neither is browser acceptance time or a fixed-protocol end-to-end app completion measurement; main-agent wall-clock time is not inferred.",
            "detail_available": True,
        }
        if detail:
            result.update(
                usage=records,
                events=events,
                task_records=[
                    {"task_id": identity, **{key: task.get(key) for key in ("status", "outcome", "reservation_id", "route", "actual_route", "route_mismatch", "reserved_at", "authorized_at", "started_at", "finished_at", "child_session_id")}}
                    for identity, task in tasks.items()
                ],
            )
        return result

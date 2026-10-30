# DSH integration — v0.1.13

GeneralRouter is a selector. DSH and its main agent own execution.

1. Call `generalrouter_match(profile_json, original_request, task_id?, detail?)` with task-profile.v3. Alternatively reuse a saved `profile_id`.
2. Read `selected_model`, `selection` and `worker_request` for a single task; multiple tasks are returned under `tasks`. `preferred_effort` is a recommendation. The main agent resolves the actual tool/model ID and uses a supported effort, otherwise default.
3. Invoke the appropriate existing native, Codex or Antigravity subagent tool. No reservation marker, authorization call, provider lock or model-allowlist precheck exists in GeneralRouter.

New/revised plans are accepted directly. Dependencies remain planning information for the main agent. Repairs use the same model in a fresh worker with a concise memento; they do not require another Router call.

Optional tools:
- `generalrouter_record_result`: link a saved selection to a parent-reported result and evidence. Optional identity fields are `tool_name`, `provider`, `model`, `effort`, `child_session_id`. Reports distinguish these assertions from observed provider usage.
- `generalrouter_status`: saved records and quota reference, no execution gate.
- `generalrouter_report`: passive DSH usage and recorded results. An external plugin may not publish its request/token events, so unobserved worker usage remains unknown.

The old prepare-repair, append-task, release and record-acceptance tools have been removed. Historical run files remain readable. No tool guard or pre-execute handler is installed. Failed recording does not prevent the main agent from using another tool. ECPA quota collection retains its ten-minute cache and is active only while observed agents work; percentages do not affect this version's capability ranking.

Package compatibility remains `>=0.2.1-rc.2 <0.2.2-0`. This change has not been runtime-tested.

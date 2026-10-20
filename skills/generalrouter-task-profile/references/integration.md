# Four-dimensional planner integration

GPT, Gemini, and Claude planners share the instructions, examples, JSON Schema, and reference resources. Native skill discovery and request envelopes remain host-specific. No provider client or API key is included.

## Contract

1. Load the rubric, semantic anchors, and `task-profile.v3` schema through the native skill or `build_prompt()`. The default reference is `references/semantic-anchors.json`, version `generalrouter-semantic-anchors.v1`, containing 12 human-defined task examples. The portable prompt also includes compact legacy AA anchors; the larger historical examples are optional through `--include-examples`.
2. Plan executable subtasks with context, input artifacts, deliverables, and dependencies. For ordinary tasks, use `planner_estimated`: estimate four nonnegative weights summing to one from the dimension definitions and closest semantic examples. Keep `source_task_id` and `calibration_anchor_id` empty, and explain the analogy and any adjustments in `rationale`.
3. Use four null weights with `uncalibrated` only when the task is too underspecified or its central requirements are outside the four-dimensional representation. An absent matching AA sample alone is not a reason to return null. Existing AA reference bindings remain available under their original source labels and must copy the recorded complete weights.
4. Pass the parsed object to `normalize_profile(profile, calibration_path)`. The host checks four-dimensional numerical shape, source consistency and dependency graphs. It still rejects legacy v2/nine-dimensional output, partial vectors, invalid AA reference bindings, changed weights for those AA bindings, and duplicate JSON keys when parsing. `planner_estimated` values are not required to copy a recorded AA fit.
5. Match the normalized four-dimensional weights against model vectors built with the AA category derivation. Preserve capability scores and resource preferences separately. The main agent invokes the selected model and maps effort where supported, otherwise using provider default.

Canonical order: `agentic`, `coding`, `knowledge_document`, `scientific_reasoning`. The normalizer returns `normalized-task-profile.v3`, task `routing_weights` and `routing_vector`, and the recorded evidence basis. AA fit metadata belongs to the legacy reference fits; it must not be presented as validation of a planner-estimated task. The normalizer does not select a model or invoke an API.

The initial matcher blends a numerical task vector with the AA category prior `(0.3, 0.2, 0.3, 0.2)`: the default `intelligence_prior=0.4` gives `effective_weights = 0.6 * task_weights + 0.4 * category_prior`. This adds no fifth Intelligence dimension. For a task with four nulls, `aa_composite_fallback` uses that category prior to rank candidates while the task's four weights remain null; it does not create calibration evidence. The handoff records all 12 candidate scores and component contributions, retaining the task's weight source and rationale. The configured preferred candidate only breaks exact score-and-coverage ties. Price, latency, task difficulty and an effort resource policy do not enter the capability ranking in this update.

`references/semantic-anchors.json` supplies the ordinary task examples and their manually defined vectors. They express relative capability emphasis; they are not measured difficulty, successful-execution probabilities, or fitted AA scores. The same capability emphasis can describe tasks of different difficulty.

`references/calibration.json` contains the historical joint fits and six-model reference snapshot. `references/planner-anchors.json` retains the compact view of those fitted AA anchors; the observed task context remains recorded in the reference materials. Only four complete reference models enter each four-dimensional fit. Old nine-dimensional and marginal outputs are not compatible inputs.

All reference material is bundled at a fixed version. An update may replace that material with a new version; neither live benchmark searches during planning nor an automatic periodic refresh are part of this workflow. Extending the source enum within `task-profile.v3` preserves existing legal profiles. Previously saved profiles, including four-null profiles, are not rewritten: use a new profile in a new run to apply the new estimation instructions.

## Portable exports

For the optional DSH Desktop plugin, see [DSH integration](dsh-integration.md). That host accepts the same profile as a tool input and continues to native worker dispatch; the standalone planner still returns JSON only.

From the skill directory:

```text
python scripts/build_prompt.py --output planner-prompt.md
python scripts/build_prompt.py --provider-format openai-responses --output output-format.json
python scripts/normalize_profile.py --input planner-output.json --output routed-tasks.json
```

`build_prompt()` resolves resources relative to its own skill folder. Provider-format exports are request fragments, not complete requests:

- `openai-responses`: `text.format` JSON Schema.
- `openai-chat`: `response_format.json_schema`.
- `claude-messages`: `output_config.format`.
- `gemini-interactions`: JSON response format for Interactions, not GenerateContent.
- `schema`: bare JSON Schema for a host's own adapter.

Endpoint support and response extraction remain host responsibilities. No provider compatibility trial has been run.

## Evidence scope

The 12 semantic anchors are human-defined examples with explicit reasons for their weight assignments. They support consistent estimation and are not an empirical calibration dataset. `planner_estimated` records that distinction; the source label does not establish accuracy.

The historical fitted anchors are four related AA-Briefcase-Lite business due-diligence tasks, using 24 original observations. Each is a `regularized_reference_fit`; four paired models do not establish a uniquely identified four-dimensional task requirement. Their coefficients describe statistical associations in a small sample and are not semantic ground truth for the manual examples. Direct source binding and analogy transfer remain separate. The legacy enum `aa_calibrated_anchor` means exact binding to the recorded reference fit, not validated task difficulty.

The user reported a completed 0.1.4 native-subagent smoke workflow that created an addition webpage; browser functional acceptance was still outstanding. That result does not validate these new estimates. Development tests, validators, review, behavioral trials, and model calls for this 0.1.5 update were skipped at the user's request. Runtime checks are implemented without a claim that the update has been executed successfully. Local benchmark runs are a possible future improvement, with no new benchmark results in this release.

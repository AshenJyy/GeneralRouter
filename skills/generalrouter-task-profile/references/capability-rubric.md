# Four-dimensional capability rubric

The four positions group observed AA benchmark scores. They are overlapping indicators, not independent cognitive abilities. Model values lie in [0, 1] and do not sum to one. Task weights describe relative capability relevance, are nonnegative, and sum to one; use four nulls only when a useful estimate cannot be made. There is no 0–3 scale.

| Position | Field | Work represented by the indicator | AA components |
|---|---|---|---|
| 1 | agentic | Operating tools and environments through multiple actions to complete an artifact or workflow | AA-Briefcase, GDPval-AA, AutomationBench-AA |
| 2 | coding | Substantive program generation, comprehension, modification, debugging, and computational implementation | Terminal-Bench 4.0, SciCode |
| 3 | knowledge_document | Factual reliability, use of evidence, and understanding long documents | Omniscience accuracy and nonhallucination, GDP.pdf, AA-LCR |
| 4 | scientific_reasoning | Scientific and academic problem solving | HLE, CritPt |

## Estimating the work

Use `planner_estimated` for ordinary planning, with [semantic-anchors.json](semantic-anchors.json) as human-defined examples. Anchor vectors express an explicit design convention for consistent task descriptions. They were not measured by AA, fitted to benchmark outcomes, or verified through model runs.

Estimate what the worker must do after receiving the parent context. Consider the objective, available inputs, required tools, deliverable, and acceptance conditions. The biggest weight should reflect the main source of work or likely errors. Use simple proportions; the examples use tenths, but they are reference points rather than a compulsory numerical grid.

- `agentic`: Coordinating several tool actions, maintaining environment or workflow state, and reaching an operational result. Merely writing a file through a tool does not make an otherwise straightforward coding task substantially agentic.
- `coding`: Understanding, producing, changing, or debugging substantive program logic. Producing a PDF or spreadsheet through an existing tool does not by itself imply coding.
- `knowledge_document`: Finding and accurately using facts, interpreting supplied specifications or lengthy documents, reconciling evidence, and supporting conclusions with sources. Ordinary reading of a short request does not require a positive weight.
- `scientific_reasoning`: Mathematical, scientific, or academic inference that is part of the actual solution. Generic complexity, business reasoning, or the presence of numbers alone does not establish this dimension.

Multiple dimensions may matter. A numerical-method implementation can need coding and scientific reasoning; configuring an existing local model inside an app may need coding, tool coordination, and documentation, without requiring new machine-learning research.

Use the closest semantic anchors and adjust for real differences. A mixed task can draw on multiple examples, but explain the resulting balance instead of averaging labels. Include the relevant semantic anchor IDs and a concise reason in the profile's existing `rationale`. Keep `source_task_id` and `calibration_anchor_id` empty for planner estimates.

The vector describes task type, not total workload or quality threshold. Easy and hard tasks may share a vector. Record difficulty separately and put concrete acceptance requirements in the task. Neither semantic weights nor their dot product with model scores are calibrated probabilities.

Use `uncalibrated` and four nulls if the task is too underspecified to identify its work, or if its primary requirements are not represented by these four dimensions, such as a task whose success mainly depends on an unmeasured image-generation capability. Do not output nulls merely because a coding or scientific task lacks AA per-question scores.

## Bundled benchmark descriptions

This is the `generalrouter-semantic-anchors.v1` description snapshot, maintained with the skill and its recorded model-vector basis. It is not a claim about the latest leaderboard release. Read it locally during planning; update it when the project adopts new benchmark definitions or revises its reference examples.

| Benchmark | Work used as a reference | Boundary when describing a new task |
|---|---|---|
| AA-Briefcase v1.1 | Completing multi-step business work and producing files | Artifact completion may combine tool use, evidence, and analysis; it does not make every business task identical |
| GDPval-AA v2.1 | Completing professional work with deliverables evaluated by comparison | A broad work-artifact reference, not a per-task success predictor |
| AutomationBench-AA | Carrying out SaaS workflows through REST tools | Relevant when stateful tool operations are the work; merely explaining an API is different |
| Terminal-Bench 4.0 | Completing tasks in a terminal environment with executable acceptance checks | May involve both program changes and environment operations; choose weights from the concrete task |
| SciCode | Implementing scientific computations in code | Relevant when domain reasoning and numerical implementation are both required |
| AA-Omniscience | Answering factual questions, including reliability of answers | Useful for factual dependence; it does not measure arbitrary writing quality |
| GDP.pdf | Answering work-related questions grounded in long PDFs | Useful for document evidence and synthesis; PDF output alone is unrelated |
| AA-LCR v1.1 | Reasoning over long context | Useful when the supplied context itself must be understood and combined |
| HLE | Answering demanding academic questions | Academic subject-matter reasoning; it is not a general measure of all difficult work |
| CritPt | Solving challenging physics problems | Scientific reasoning reference; an ordinary app with numerical inputs is not a similar task |

These descriptions keep task terminology related to the benchmarks behind the model vectors. They do not supply numerical task weights. The semantic-anchor values are independently human-defined project choices. The recorded component definitions and versions are retained in the project data; no fresh leaderboard lookup is needed for each task.

## Model-vector basis

Model vectors use within-category weighted averages of published components. Briefcase and GDPval Elo are mapped with `clamp((Elo - 500) / 2000, 0, 1)`; fractional scores retain their original 0–1 scale, and Omniscience nonhallucination is `1 - hallucinationRate`. Component weights are AA's recorded Intelligence Index component weights, divided by each category's total. This is a GeneralRouter category derivation, not a separately published AA capability index or a new min-max normalization.

A category stays null if any required component is missing. Effort configurations have separate measured records; no effort interpolation or overall-Intelligence substitution is performed. The reference snapshot and execution pool are distinct: six historical example models support evidence collection, while the router chooses among its current 12 execution configurations.

## Optional historical task evidence and fit

The current four anchors reuse 24 observed task/model rubric grades from AA-Briefcase-Lite: four related business due-diligence tasks with six model outcomes each. Analytical-quality and presentation pairwise verdicts are retained separately, not mixed into the rubric pass fraction. These public Lite grades do not contribute to the official leaderboard.

Four reference models have all four category values and participate in each joint fit: Fable 5, Gemini 3.1 Pro Preview, GPT-5.5 Xhigh, and GLM-5.2 Max. The user confirms Fable's AA setting as Max with Opus 4.8 fallback; Gemini's sample effort remains undisclosed. o3 and Opus 4 have missing categories and do not enter these joint fits. Missing values stay unknown. GDP.pdf task examples have been excluded from this task-calibration round; GDP.pdf remains a component of the model-vector category.

The implemented score model is nonnegative ridge regression with an intercept:

`y_proxy = bias + sum(beta_d * model_capability_d)`, with `beta_d >= 0`.

Where positive signal exists, `task_weight_d = beta_d / sum(beta)` and `scale = sum(beta)`. Thus `y_proxy = bias + scale * dot(task_weights, model_capabilities)`. Regularization strength and optimization are project choices. The planner copies stored weights rather than solving this regression itself.

Four paired models do not identify four independent coefficients plus an intercept without assumptions. Regularization chooses one reference fit. Its coefficients are predictive associations, not measured semantic proportions of work; a scientific-reasoning coefficient on a business artifact does not prove that artifact requires scientific problem solving. In-sample residuals do not demonstrate generalization, success probability, or calibrated difficulty.

## Historical transfer rule

Use this historical path when the specific recorded empirical reference is needed. Use an exact anchor only for the same recorded task. Copy a single anchor for an analogous business task only when the work, inputs, tools, and deliverable support the analogy; explain what differs. Topic keywords or overlapping file formats are insufficient.

The current observations do not empirically calibrate pure programming, scientific reasoning, or all document tasks. Those tasks normally receive `planner_estimated` vectors from the semantic process above. Do not label those estimates as AA-calibrated, mix fitted coefficients with semantic proportions, combine legacy single-dimension slopes, or turn unknown model measurements into zeros.

Source records and configuration caveats are retained with the fit inputs and calibration file. No model run, development test, review, held-out validation, or provider API trial is implied by numerical reference fitting.

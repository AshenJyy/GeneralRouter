# Design decisions in v0.1.7

These are implementation choices and their boundaries, rather than claims of measured optimality. The [project overview](PROJECT-OVERVIEW.md) describes the prototype and the [case study](experiments/translation-ab-v2/README.md) reports its observed behavior.

| Decision | Reason | Current boundary |
| --- | --- | --- |
| Build a DSH plugin | Concentrate on task-level selection while reusing sessions, tools and worker execution. | GeneralRouter is not a separate orchestrator. The recorded dispatch path is DSH native subagent. |
| Replace the initial nine-dimensional representation with four capability groups | Use a shared four-group contract for model capabilities, task emphasis and matching. The project author proposed this architectural change. | The groups reference AA's categories. This was a redesign of the representation, not a learned projection of the old vectors; improved routing performance over nine dimensions has not been demonstrated. |
| Represent model and effort as a configuration | Different supported effort settings can have different capability records. | Twelve configurations are not twelve different model families. Corresponding effort is used when supported; otherwise the interface can fall back to its default. Not every fallback path has been exercised. |
| Separate model capability from task relevance | Benchmark performance describes models; a task profile expresses which capabilities the task needs. | Planner weights are heuristic relative emphasis, not a measured difficulty scale. A weighted score is not a success probability. |
| Record the source of task weights | Distinguish ordinary planner estimates from the small set of fitted task references. | The four historical business-task fits do not establish general coding or scientific calibration. |
| Apply execution constraints separately from scoring | A high-scoring configuration may not be callable through the current entry point. | The case scored twelve configurations but admitted only Flash on the native path. Other host plugin tools existed but were not used. |
| Fix a selected configuration within a task | Keep the task's route, outcome and usage attributable to one selection. | Further repair becomes a new linked task; there is no automatic upgrade policy. A repair receives extra information and is not a clean repeat of the original task. |
| Keep execution and acceptance separate | A worker can finish while the delivered result still fails a requirement. | Acceptance records store reported evidence; the recording tool does not itself run acceptance tests. |
| Reuse the saved profile and return compact defaults | Avoid repeatedly passing the same plan and long diagnostic payloads to the main agent. | This design reduces repeated material; a measured improvement in total cost or time is not established by that fact alone. |
| Use reported quota percentages and a ten-minute active cache | Avoid inventing per-task quota debits and unnecessary idle polling. | Other clients and long tasks can consume quota between reads. Unknown scope or missing percentages stay unknown. |
| Report API-equivalent cost separately | Compare recorded token usage under a dated rate schedule even when access is subscription-backed. | The result is not cash paid, a subscription-fee allocation or a complete bill when usage fields are missing. |

## A concrete task profile

The translation-extension repair used the planner-estimated weights:

| Agentic | Coding | Knowledge/document | Scientific reasoning |
| ---: | ---: | ---: | ---: |
| 0.10 | 0.85 | 0.05 | 0.00 |

The work mainly involved DOM/CSS and restoration logic. The scientific subject of the paper did not make the coding task a scientific reasoning problem. The vector described relative emphasis; it did not mean the task had an 85% chance of success or that coding difficulty was 0.85.

The current policy mixes those task weights with a 0.4 general prior, yielding effective weights `(0.18, 0.59, 0.15, 0.08)` in this run. The prior is a fixed design parameter, not a fitted optimum. The saved ranking then separates capability scores from eligibility. Argon scored highest, but Flash was the only configuration admitted for native dispatch. This explains why the observed route cannot be attributed to a price preference.

## How the scope changed

The project author proposed moving from the original nine-dimensional representation to four groups: agentic, coding, knowledge/document and scientific reasoning. The change affected model-vector construction, the task-profile contract and the matching interface. AA supplied benchmark measurements and category references; the decision to use this representation for the router, and its integration into the routing pipeline, belong to the project. The [core algorithm](CORE-ALGORITHM.md) documents the current transforms and scoring rules.

The project moved from a proposed standalone orchestration system to a plugin. Early task-reference work relied on a small number of paired benchmark examples; ordinary tasks later gained explicitly labelled planner estimates because that coverage was too narrow. Task execution also gained separate acceptance and linked follow-up records after a completed task could not be reopened for repair.

The browser-extension case then exposed a different boundary: host-wide tool availability and the router's current executable candidate set are not the same thing. Connecting those paths is a more direct next step than tuning weights against this single constrained run.

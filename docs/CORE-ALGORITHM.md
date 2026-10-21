# Core algorithm: capability vectors, task weights and matching

This document describes the v0.1.9 implementation. The scoring formula remains unchanged from v0.1.7: convert recorded benchmark components into comparable capability groups, obtain an explicit task-weight profile, and rank configurations while keeping missing evidence visible. Repair handoff inherits the selected route without another ranking. v0.1.9 removes Router-side native-catalog and allowlist membership prefiltering; mapping, quota and dispatch-identity controls remain. These updates have not been runtime-verified.

The benchmark measurements and category proportions come from retained Artificial Analysis (AA) material. The grouping, fixed transforms, missing-data policy, task-profile contract and routing calculation are implemented in this project. Weighted averages, dot products and ridge regression are established methods; they are not claimed as new mathematical algorithms.

The project author proposed replacing the initial nine-dimensional representation with four capability groups. Model-vector construction, task weights and the matching interface were updated around that shared representation. This was an architectural redesign informed by AA's categories, not a learned projection of the old nine-dimensional vectors. No comparative experiment has established that four dimensions outperform nine.

## 1. Model capability vectors

Each exact model/effort configuration has a vector:

$$
\mathbf{m}=(A,C,K,S)
$$

The dimensions are agentic work, coding, knowledge/document work and scientific reasoning. Each observed component lies in $[0,1]$. **A model vector does not need to sum to one:** these are capability scores, not a distribution over abilities.

The builder reads an exact AA model ID from the candidate configuration and its saved benchmark record. It records the source fields and transforms alongside each result. Different efforts have separate records; no cross-effort copying or interpolation is performed.

### Normalize the source components

For Briefcase and GDPval Elo, the implementation uses the fixed project mapping:

$$
e(x)=\min(1,\max(0,(x-500)/2000)).
$$

Other recorded rates must already be fractions in $[0,1]$. The builder does not guess percentage units. The Omniscience non-hallucination component is $1-h$, where $h$ is the recorded hallucination rate.

This is not candidate-pool min-max normalization: adding another candidate does not change the scale. No softmax is used. The saved Intelligence Index is metadata, not an extra input capability.

### Aggregate within the four groups

The coefficients below preserve the relative AA component proportions inside each category. Elo terms denote their transformed values; all other terms denote recorded fractions.

| Dimension | Calculation |
| --- | --- |
| Agentic $A$ | $\tfrac12\,Briefcase + \tfrac13\,GDPval + \tfrac16\,AutomationBench$ |
| Coding $C$ | $\tfrac12\,TerminalBench + \tfrac12\,SciCode$ |
| Knowledge/document $K$ | $\tfrac13\,OmniscienceAccuracy + \tfrac16(1-h) + \tfrac13\,GDPpdf + \tfrac16\,AALCR$ |
| Scientific reasoning $S$ | $\tfrac12\,HLE + \tfrac12\,CritPt$ |

These are project-derived groups, not AA's separately published Coding or Agentic indices, and not a claim to reproduce its complete official Intelligence Index implementation.

If a required benchmark component is absent, its entire category is `null`. The builder also records the fraction of the category's component weight supported by observed data. It does not fill missing values with zero or rescale a partial category into an apparently complete score. The current execution snapshot has all required components for its twelve configurations; historical reference models have separate coverage records.

Implementation: [`profile_vectors.py`](../generalrouter/profile_vectors.py), especially `_normalize`, `_categories` and `build_profiles`; input [`aa-components.v0.1.json`](../data/aa-components.v0.1.json); output [`model-vectors.v0.1.json`](../data/model-vectors.v0.1.json). The offline builder entry point is [`build_model_vectors.py`](../scripts/build_model_vectors.py).

## 2. Task-weight generation and normalization

An ordinary task uses:

$$
\mathbf{t}=(t_A,t_C,t_K,t_S),\qquad t_d\geq0,\quad\sum_d t_d=1.
$$

Unlike a model vector, this vector expresses relative emphasis. The planner generates it using the documented dimension definitions and twelve semantic anchors, records a short rationale and marks the source `planner_estimated`. The profile processor checks the schema, provenance and unit-sum constraint; the matcher divides by the weight sum when forming effective weights. Neither operation deterministically infers the task's true requirements from text.

For the extension repair, the saved profile was:

$$
\mathbf{t}=(0.10,0.85,0.05,0.00).
$$

Most work was DOM/CSS programming. Repairing the display of a scientific paper did not itself require solving a scientific problem. The coding weight of 0.85 is neither a difficulty score nor a predicted success rate. Difficulty is a separate field and is not used by the current scoring function.

The existing four business-task reference fits remain a distinct source. For each reference, the offline implementation fits nonnegative coefficients with an unpenalized intercept:

$$
\min_{b,\,\boldsymbol\beta\geq0}
\frac1n\sum_{i=1}^{n}(y_i-b-\mathbf{x}_i^\mathsf{T}\boldsymbol\beta)^2
+\lambda\sum_d\beta_d^2.
$$

It uses coordinate descent and retains normalized coefficients as reference weights when their sum is positive, with bias and scale recorded separately. The retained setting is $\lambda=0.01$. Four complete model observations per reference are too few to identify general task requirements; these fits are not the source of ordinary coding-task weights and are not evidence of general calibration. No fitting is rerun for this document.

Implementation and contract: [`SKILL.md`](../skills/generalrouter-task-profile/SKILL.md), [`semantic-anchors.json`](../skills/generalrouter-task-profile/references/semantic-anchors.json), [`task-profile.schema.json`](../skills/generalrouter-task-profile/references/task-profile.schema.json), [`normalize_profile.py`](../skills/generalrouter-task-profile/scripts/normalize_profile.py), and the historical [`calibrate.py`](../skills/generalrouter-task-profile/scripts/calibrate.py).

## 3. Effective weights and candidate scores

The current policy blends the task weights with a general prior:

$$
\mathbf{p}=(0.30,0.20,0.30,0.20),\qquad
q_d=(1-\alpha)\frac{t_d}{\sum_jt_j}+\alpha p_d,
\quad\alpha=0.40.
$$

This retains some general capability emphasis even for a concentrated task profile. The 0.40 setting is a fixed project policy, not a learned optimum. The extension example yields:

$$
\mathbf{q}=(0.18,0.59,0.15,0.08).
$$

An `uncalibrated` profile with four `null` weights uses $\mathbf q=\mathbf p$ directly. Partial-null profiles are not treated as ordinary numeric vectors.

For a model with all four observed capabilities, scoring is a dot product:

$$
score(m)=\sum_dq_dm_d.
$$

For incomplete vectors, let $O_m$ be the dimensions with observed capability values. The scorer computes the observed task-weight coverage and a discounted score:

$$
c_m=\sum_{d\in O_m}q_d,
\qquad
score(m)=\frac{\sum_{d\in O_m}q_dm_d}{c_m}
\left[1-\gamma(1-c_m)\right],
\quad\gamma=0.25.
$$

A candidate with $c_m=0$ is skipped. The numerator is averaged only over observed dimensions; the second term applies an explicit uncertainty discount. With full coverage, this reduces to the ordinary dot product. The discount is a configurable heuristic, not a statistical confidence bound. Category-level missing-data handling in the builder and dimension-level scoring coverage are separate operations.

The output includes the observed score, coverage, missing dimensions and each dimension's contribution. Candidates sort by score, then coverage; a configured preference breaks only exact score-and-coverage ties, followed by a stable ID order. No softmax probability or calibrated success estimate is produced.

Implementation: [`matching.py`](../generalrouter/matching.py), `_effective_weights` and `rank_models`; exact configuration loading in [`capabilities.py`](../generalrouter/capabilities.py).

## 4. From ranking to an executable route

The host bridge chooses the highest-ranked enabled candidate with a valid call mapping that passes reported-quota constraints. It does not prefilter by membership in DSH's native model catalog or allowlist. Missing catalog metadata does not produce a setup requirement. Unknown quota remains unknown rather than being treated as measured capacity. Score and Router admission are saved separately; actual DSH invocation still owns its native permission checks.

Known optional metadata can resolve the effort for a first selection; otherwise the model default is used. Once the route is frozen, retries and repairs retain that exact provider/model/effort rather than deriving it again from changed metadata. Authorization still checks tickets and route identity. Removing the membership filter neither integrates other execution plugins nor establishes that every recorded configuration can execute.

```text
Load the recorded model/effort capability vectors.
Read the task profile and its stated weight source.
Form effective weights using the task vector or explicit fallback.
Score candidates, retaining coverage and per-dimension contributions.
Select the highest-ranked candidate with an enabled valid mapping and quota admission.
Return its fixed route and dispatch arguments to the DSH main agent.
Record the actual route, execution, acceptance and usage separately.
```

Once selected, a task's provider, model and effort stay fixed. An independent added task receives its own first selection. A repair instead inherits the predecessor's fixed configuration: the main agent receives the failure, decides whether to repair, and dispatches a new native subagent with a short memento. `generalrouter_prepare_repair` retains the relationship, checks mapping and quota admission and issues a fresh dispatch ticket; it does not generate new weights, call the ranking algorithm or check native-allowlist membership. A mapping/quota block or an actual DSH rejection is reported rather than triggering fallback. See [repair handoff](REPAIR-HANDOFF.md).

Cost, latency and difficulty are not current scoring terms, and the prototype does not automatically select a lower effort for a simple task.

The saved v0.1.7 case used the previous membership filter: Argon high ranked first, Flash high ranked twelfth, but eleven configurations were excluded by the native allowlist. Flash was the sole admitted configuration; neither the Codex nor Antigravity plugin tool was called. This historical result remains unchanged and is not evidence that the score optimized price or that v0.1.9 can invoke the excluded configurations. See the [candidate ranking and actual dispatch](experiments/translation-ab-v2/results.json).

Implementation boundary: [`host_bridge.py`](../generalrouter/host_bridge.py) performs the host integration; the [DSH adapter](../dsh-generalrouter) provides native dispatch authorization and recording. This document describes retained source and data without executing the algorithm or adding a new experiment.

---
license: apache-2.0
pretty_name: AA-Briefcase-Lite
language:
- en
task_categories:
- other
tags:
- agents
- agentic-evaluation
- benchmark
- private-equity
- due-diligence
configs:
- config_name: checks
  data_files:
  - split: train
    path: checks.jsonl
---

# AA-Briefcase-Lite

**The public example scenario for AA-Briefcase, Artificial Analysis' frontier agentic evaluation of realistic, long-horizon knowledge work.**

- [Leaderboard and detailed results](https://artificialanalysis.ai/evaluations/aa-briefcase)
- [Launch article](https://artificialanalysis.ai/articles/aa-briefcase)

AA-Briefcase extends frontier model benchmarking beyond coding and short-form reasoning to the professional deliverables knowledge workers produce day to day. It consists of four private scenarios in which agents complete realistic professional workflows across data science, product management, banking operations, and heavy industry strategy.

**This repository releases one week of a fifth scenario as the public AA-Briefcase-Lite example.** It shows what AA-Briefcase looks like in practice - the scenario context, source-file structure, task format, grading rubric, and the deliverables agents are expected to produce. It is a smaller, less challenging scenario than the full benchmark, and it is **not part of the scored leaderboard**. The four scenarios used in the evaluation and shown in the [leaderboard on the Artificial Analysis website](https://artificialanalysis.ai/evaluations/aa-briefcase) remain private.

## The scenario

AA-Briefcase-Lite is a commercial due-diligence engagement. The evaluated model is tasked with assisting a **Vice President** at *Halberd Capital Partners*, a fictional US mid-market private-equity firm conducting outside-in commercial due diligence on *Aurora Eggs Ltd*, a privately-held New Zealand egg producer being evaluated as a potential acquisition. The deal is at pre-LOI / indicative-bid prep stage: the agent must form an initial view from the materials at hand and surface the open questions a confirmatory phase would need to answer. Both Halberd Capital Partners and Aurora Eggs Ltd are fictional companies, but the scenario is grounded in real data from the New Zealand egg market. The source pool uses real and synthetic documents, including deliberate cross-source contradictions the agent is expected to identify and reconcile.

The week contains four deliverables, each independently completable from the source pool:

| Task | Workstream | Deliverable | Format |
|---|---|---|---|
| `w1_t1` | Market structure & competitive landscape | `market_overview.tex` + `market_overview.pdf` (single page) | LaTeX + PDF |
| `w1_t2` | Market sizing, forecast & cage-ban transition model | `market_model.xlsx` | XLSX |
| `w1_t3` | Target assessment, opportunities & risks | `target_assessment.pptx` | PPTX |
| `w1_t4` | Preliminary findings briefing video + subtitles | `briefing.mp4` + `briefing.srt` | MP4 + SRT |

The source pool holds 67 sources (147 files) in two groups, mirroring how AA-Briefcase structures every scenario: `source_files/shared/` (persistent organizational context available across the whole scenario) and `source_files/week/` (materials introduced for this week's tasks) - emails, Slack threads, datasets, meeting notes, and reports.

## Dataset files

### `checks.jsonl`

This file has one row per check for a total of 63 rows:

| Column | Meaning |
|---|---|
| `check_id` | `W{week}-T{task}-{type}-{NN}`, e.g. `W1-T1-A-01` |
| `task_id` | Task the check belongs to (`w1_t1` ... `w1_t4`) |
| `week` | Week number (always `1` in this release) |
| `check_type` | `A` (Accuracy: single-source factual / instruction-following), `C` (Critical Insight: requires reconciling contradictory sources or catching a deliberate trap), `AQ` (Analytical Quality), `P` (Presentation) |
| `scoring_type` | `binary` (pass/fail against the rubric; all `A` and `C` checks) or `pairwise` (head-to-head comparison against another model's submission on the same task; the `AQ` and `P` checks) |
| `taskdoer_output_file` | The deliverable file the check is graded against |
| `files_needed` | Source files containing the evidence required to pass |
| `check_description` | What the check verifies |
| `score_1_criteria` / `score_0_criteria` | Pass / fail criteria provided to the judge |
| `specific_file_locations` | Where in the source files the supporting evidence sits |

### `tasks.jsonl`

This file has one row per task for a total of 4 rows:

| Column | Meaning |
|---|---|
| `task_id` / `week` | Task identifier and week number |
| `task_md_path` | Task brief given to the agent (`tasks/<task_id>.md`) |
| `deliverable_filenames` | Files the agent must produce |
| `shared_files` / `week_files` | The exact source files mounted for the task |
| `scenario_overview_path` / `week_overview_path` | Context documents given to the agent |
| `rubric_path` / `traceability_path` | Grader-side check definitions and evidence chains |

## Methodology

### Inputs

For each task, the agent under evaluation receives:

- The scenario overview (`summary_docs/scenario_overview.md`) and week overview (`summary_docs/week_1/week_1_overview.md`)
- The task brief (`tasks/<task_id>.md`)
- The task's source pool mounted as files (`shared_files` + `week_files` from `tasks.jsonl`)

### Harness setup

- Built on [**Stirrup**](https://github.com/ArtificialAnalysis/Stirrup), Artificial Analysis' open-source agent framework
- Week-scoped, fully offline E2B sandbox built from the week's source files, with a broad scientific-computing and document-processing stack preinstalled (documented in the agent task prompt below)
- Code-execution sandbox plus an image-viewing tool; the agent reads the pool's formats (DOCX, PPTX, XLSX, PDF, HTML, CSV, email, and chat exports) through the preinstalled document-processing stack
- Up to 500 turns per task; when the context window fills, Stirrup summarizes earlier history so long tasks can continue
- Two terminal tools: a finish tool (brief summary plus absolute paths of every deliverable; validates the paths are real files) and an abandon_task_finish (give-up) tool, called with a reason only when the task is genuinely impossible

### Grading

- Check definitions live in `grader/`: `grading_rubric_w1.json` (checks and scoring criteria), `source_graph_w1.json` (source-to-check mapping), and `traceability_w1.json` (evidence chain per check; also supports a hinted submission mode used to validate task fairness)
- Rubric checks (`A`, `C`): strict binary pass/fail against the rubric, no partial credit
- Pairwise checks (`AQ`, `P`): head-to-head comparison of two models' submissions on the same task, returning a preferred submission or tie
- Every check is graded by a model drawn from a panel of three frontier judges from leading labs, with a different grader chosen per check; judges see the submission and rubric context only, never the source files
- On the leaderboard, binary outcomes and pairwise judgments are combined via a maximum-likelihood Elo aggregation into Rubric, Analytical Quality, Presentation, and overall AA-Briefcase Elo scores - documented in full on the [methodology page](https://artificialanalysis.ai/methodology/intelligence-benchmarking#aa-briefcase)

## Prompts

The prompts used for generation and binary rubric grading, reproduced verbatim from the AA-Briefcase harness. The raw files ship in the `prompts/` folder of this dataset. The prompts are templates: `{placeholder}` fields are filled at runtime, and `<<<...>>>` markers show where the parsed submission content - text blocks, image blocks, or parser notes for unsupported content - is inlined into the judge's user message.

### Agent system prompt (`eval_system.txt`)

System prompt for the evaluated agent during task generation.

```text
You are an AI agent working on a specific task within a multi-week simulated workplace scenario. Each task is part of a longer workflow; your job is to complete the current task using the tools provided in up to {max_turns} steps, then submit your deliverables.

When you are done you must call the `{finish_tool_name}` tool as your final step, passing a brief summary of what you accomplished and a list of absolute paths for every deliverable file.

If you have genuinely concluded that the task cannot be completed — for example because required inputs are missing, a hard dependency is unavailable, or the request itself is incoherent — call the `{abandon_task_finish}` tool with a brief reason instead. Do not use it to escape difficulty.

You cannot interact with the user during the task. Record any clarifying assumptions you made in your finish summary.
```

### Agent task prompt (`eval_submission.txt`)

User message given to the agent at the start of each task: it documents the sandbox, the offline runtime, and the submission rules, then inlines the scenario overview, week overview, task brief, and expected deliverable filenames.

```text
<execution_context>
## Sandbox

You operate inside an isolated Linux container through the `code_exec` tool, which runs shell commands and lets you read, create, and edit files. Commands run as the unprivileged user `user` (UID 1000), starting from `/home/user`. Passwordless `sudo` exists but is rarely needed, since your home directory is fully writable.

Every command runs independently: no working directory, environment variable, or other shell state carries over from one call to the next. Prefer absolute paths for both files and commands, and do not navigate with `cd` across calls — a `cd` in one command is gone by the next, so relying on it leaves you silently operating in the wrong place. When a step genuinely needs a different directory, chain it into the same command (e.g. `cd /home/user/work && python build.py`).

## No network

The container has no outbound connectivity, and there is no proxy, allowlist, or flag that can turn it on — treat the environment as permanently offline. Anything that reaches for the internet will fail, including package installs (`pip`, `npm`, `apt`), remote `git` operations, and any HTTP/HTTPS client request from any language.

Identify a network block by its error signature rather than by guessing: failed name resolution (`Could not resolve host`, `Temporary failure in name resolution`), an unreachable route (`Network is unreachable`, a refused or timed-out connection to a public host), or a stalled TLS handshake. When you see these, the failure is structural — do not retry the same call and do not hunt for a workaround (mirrors, alternate hosts, cached copies). Re-plan using only what is already installed and what ships inside your workspace.

## Filesystem

- Writable: everything under `/home/user/` plus `/tmp`. Use these for deliverables, intermediate files, and caches.
- Read-only inputs:
  - `/home/user/shared/` — reference material shared across the whole scenario
  - `/home/user/week/` — documents specific to this week's tasks
  Copy these into a working folder before transforming them rather than editing them in place.

## Runtime

A broad scientific-computing and document-processing stack is already installed, so confirm what is present before assuming a gap:
- Python 3.13 with the usual data stack (numpy, pandas, polars, scipy), plotting (matplotlib, plotly), the scikit-learn ML family, and document tooling (python-docx, python-pptx, openpyxl, PyMuPDF, pdfplumber, reportlab, weasyprint, Pillow, opencv), plus Playwright.
- System tools include LibreOffice, Pandoc, Tesseract, FFmpeg, ImageMagick, Ghostscript, TeX Live, OpenJDK, Chromium, jq, and git.
- Check availability with `pip show <pkg>` or `which <tool>` instead of installing — installs fail offline, but almost anything you would reach for is already here.
- matplotlib runs headless (`MPLBACKEND=Agg`): write figures to files; never call `plt.show()`.
- Commands are terminated after 20 minutes. Keep them bounded, persist intermediate results to disk, and split long jobs into smaller steps.

## Submitting your work

Finish by calling the `{finish_tool_name}` tool — anything not submitted through it is not graded. Your call must include:
1. A short summary of what you accomplished.
2. Absolute paths to every deliverable (files only, not folders).

Save each deliverable directly in `/home/user` under the exact filename the task asks for — not in a subdirectory.

Save deliverables as ordinary, visible files. Do not leave the only copy of your work in a dot-prefixed file or directory (e.g. `.submission.txt`, `.outputs/report.md`), including inside an archive; a `.zip` is fine when the task explicitly asks for one. Assume your files will be opened and edited by others after submission, so write them to last.

If the task genuinely cannot be completed, call the `{abandon_task_finish}` tool with a brief reason instead. Use it only when you have concluded the work is impossible — not to escape a difficult task.
</execution_context>

<scenario_overview>
{scenario_overview}
</scenario_overview>

<week_overview>
{week_overview}
</week_overview>

<task_description>
{task}
</task_description>

<deliverables>
Submit these files, by exact name, saved directly in `/home/user`:
{expected_output_filenames}
</deliverables>

Please begin working on the task now.
```

### Binary rubric grading prompt (`judge_system.txt`)

System prompt for the binary rubric judge, which grades one submitted deliverable against one rubric check.

```text
You are grading a submitted deliverable against one binary rubric check.

The user message contains:
- the task instructions,
- the rubric item,
- the submitted artifact content.

Submitted artifacts may appear as text blocks, image blocks, or parser notes for unsupported content.

Use only evidence from the submitted artifact content. Do not infer facts from filenames, task instructions, or rubric text unless the submitted artifact content supports them.

Beyond the task instructions and rubric in the user message, you only ever receive the submitted artifact itself, never the external source files it cites. Do not fail an item merely because you cannot open or cross-check a cited source — judge citations on whether they are present, specific, and well-formed in the submission, not on whether the source's contents can be independently confirmed.

Return a strict binary judgment:
- passed=true only if the pass criteria are satisfied.
- passed=false if any required element is missing, materially wrong, unsupported, or not evidenced.

Write concise reasoning that cites submitted artifact evidence or the absence of evidence.
Do not award partial credit.
```

### Binary rubric grading - user message template (`judge_user.txt`)

User message template for the binary rubric judge: it carries the task instructions, the rubric item's pass and fail criteria, and the parsed submission content.

```text
<original_task_instructions>
{task_markdown}
</original_task_instructions>

<rubric_item>
<description>
{check_description}
</description>

<pass_criteria>
{score_1_criteria}
</pass_criteria>

<fail_criteria>
{score_0_criteria}
</fail_criteria>
</rubric_item>

<submitted_artifact>
Submitted artifact content follows in this same user message.

<<<SUBMISSION CONTENT MESSAGES>>>
</submitted_artifact>
```

## Example submissions

The `submissions/` folder contains the deliverable files produced by **six frontier models** spanning both today's leading systems and their counterparts from roughly a year ago:

| Short slug             | Model                          |
|------------------------|--------------------------------|
| claude-4-opus-thinking | Claude Opus 4 (reasoning)      |
| claude-fable-5         | Claude Fable 5                 |
| glm-5-2                | GLM 5.2 (max)                  |
| gemini-3-1-pro         | Gemini 3.1 Pro                 |
| gpt-5-5                | GPT-5.5 (xhigh)                  |
| o3                     | o3                             |

Not every model produced every deliverable: where a task's files are missing for a model (for example, o3 has no `w1_t1` outputs), the model did not produce them. These examples are illustrative only and do not contribute to any model's leaderboard results.

## Third-Party References

AA-Briefcase-Lite contains limited references to third-party brands and trademarks solely for research and evaluation purposes. No affiliation or endorsement is intended or implied. All trademarks are the property of their respective owners. Names and identifying references to private individuals in AA-Briefcase-Lite are fictitious. Any resemblance to actual persons or entities is purely coincidental.

## Citation

If you use AA-Briefcase in your research, please cite:

```bibtex
@dataset{artificialanalysis2026briefcase,
  title={AA-Briefcase},
  author={Artificial Analysis},
  year={2026},
  publisher={Artificial Analysis},
  url={https://huggingface.co/datasets/ArtificialAnalysis/AA-Briefcase-Lite}
}
```

## License

Licensed under the Apache License 2.0

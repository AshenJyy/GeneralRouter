# Third-party material notices

## AA-Briefcase-Lite dataset

The files under `data/task-resources/aa-briefcase-lite/` include task descriptions, task metadata, rubric/check definitions, scenario summaries and the upstream dataset README from **AA-Briefcase-Lite**, published by Artificial Analysis. They are retained as historical reference material for the four Briefcase task fits.

- Dataset: <https://huggingface.co/datasets/ArtificialAnalysis/AA-Briefcase-Lite>
- Upstream README: `data/task-resources/aa-briefcase-lite/briefcase-lite-README.md`
- Recorded source URLs and hashes: `data/task-resources/aa-briefcase-lite/source-manifest.json`
- License text: `skills/generalrouter-task-profile/licenses/Apache-2.0.txt`

The upstream dataset declares the Apache License, Version 2.0. Retain the upstream attribution and license with copies of this dataset material. The skill also contains shortened Chinese task summaries; its material-specific notice is in `skills/generalrouter-task-profile/THIRD_PARTY_NOTICES.md`.

AA-Briefcase-Lite is the public illustrative scenario, separate from the private scenarios contributing to the official AA-Briefcase leaderboard. This repository does not contain the full source-file pool or model-produced deliverables. Public rubric definitions retained here are reference material, not newly executed evaluation results.

## Artificial Analysis website data and model scores

The recorded model benchmark components, derived capability vectors, source metadata and public task-score observations in `data/` and `skills/generalrouter-task-profile/references/` draw on Artificial Analysis model and evaluation pages, including:

- Model pages: <https://artificialanalysis.ai/models>
- AA-Briefcase evaluation and public illustrative results: <https://artificialanalysis.ai/evaluations/aa-briefcase>
- Other evaluation and upstream URLs recorded in `data/aa-resource-inventory.v0.1.json`

Exact source URLs, retrieval metadata and available hashes are retained in the component files, resource inventory, `references/aa-source-manifest.json` and `references/aa-observed-results.json`. Paths under `work/` identify local collection artifacts that are not distributed with this source repository.

The observed Briefcase task scores are rubric pass counts from the public illustrative scenario. Separate pairwise verdicts are not interchangeable with those pass counts or official leaderboard scores. Model-level benchmark averages are not individual-task outcomes. Missing values and differences in model configuration or evaluation protocol remain documented in the source records.

The AA-Briefcase-Lite dataset's Apache license is not asserted to cover Artificial Analysis website data, website code, other benchmarks or linked third-party materials. These materials retain their respective ownership and applicable terms; no broader license is granted by this notice.

## GeneralRouter-derived material

GeneralRouter's routing code, schemas, human-defined semantic anchors and numerical reference fits are project-authored material. The derived task weights are not published, verified or endorsed by Artificial Analysis. Historical task fits are limited reference fits; the human-defined semantic anchors are heuristic examples, not measured benchmark outcomes.

This notice does not imply affiliation with or endorsement by Artificial Analysis or any benchmark author.

# Portable integration

The main agent estimates task weights using SKILL.md and returns task-profile.v3. The host normalizes the profile and ranks recorded model/effort capability vectors. The caller resolves the selected model to an actual tool/model ID and owns execution, effort fallback, dependency order and failures. GeneralRouter has no dispatcher or execution-permission layer.

For DSH, see [DSH integration](dsh-integration.md). The same semantic contract can be exported for other planners:

```text
python scripts/build_prompt.py --output planner-prompt.md
python scripts/build_prompt.py --include-references --include-examples --output expanded-planner-prompt.md
python scripts/build_prompt.py --provider-format openai-responses --output output-format.json
```

These are developer exports, not mandatory task-time steps. Provider fragments describe output formatting only, not verified endpoint compatibility. Available fragment names: schema, openai-responses, openai-chat, claude-messages, gemini-interactions. The last targets Interactions, not GenerateContent.

The 12 authored semantic anchors are heuristic examples. Four historical AA Briefcase fits describe small-sample statistical associations and do not provide general task-difficulty calibration. No new benchmark, model call, runtime test or resource-saving result was produced for v0.1.13.

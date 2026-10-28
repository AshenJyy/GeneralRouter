# DSH compatibility

GeneralRouter targets DSH Desktop `0.2.1-rc.2` and `0.2.1-rc.3`. This compatibility update is based on the official source contracts. It has not been installed or runtime-verified against rc.3.

## Pinned upstream sources

| Release | Official commit |
| --- | --- |
| [dsh-v0.2.1-rc.2](https://github.com/deepseek-ai/deepseek-harness/releases/tag/dsh-v0.2.1-rc.2) | `687f282e8625c6b8d11ff58211a90524be2e4317` |
| [dsh-v0.2.1-rc.3](https://github.com/deepseek-ai/deepseek-harness/releases/tag/dsh-v0.2.1-rc.3) | `1f81c047fc48fa190a683e469daa699857e9afe8` |

The rc.3 release describes plugin-entry menus, draft preservation when creating sessions, and DeepSeek model-list updates. The GeneralRouter-facing source files listed below are unchanged between these two commits; their DSH package versions changed from rc.2 to rc.3.

## Package compatibility

The old manifest required exactly `0.2.1-rc.2`. DSH evaluates `engines.dsh` before installing or activating a plugin, so that declaration excludes rc.3 even when the imported APIs are unchanged. The updated manifest lists the two inspected versions explicitly: `0.2.1-rc.2 || 0.2.1-rc.3`. The `@deepseek-ai/dsh-tools` and `@deepseek-ai/dsh-llm` peer ranges use the same pair. This does not assert support for later releases. See the [official compatibility rule](https://github.com/deepseek-ai/deepseek-harness/blob/1f81c047fc48fa190a683e469daa699857e9afe8/packages/boot/app-boot/src/plugin-compatibility.ts#L54).

DSH's source-development Node requirement remains `^22.19.0 || >=24.0.0`, and its package manager remains `pnpm@11.28.5`. `@deepseek-ai/schemastery` remains `3.18.5`. These are upstream runtime/build declarations, not additional commands required for a Desktop user adding the packaged `.tgz`. See the [root manifest](https://github.com/deepseek-ai/deepseek-harness/blob/1f81c047fc48fa190a683e469daa699857e9afe8/package.json) and [Schemastery manifest](https://github.com/deepseek-ai/deepseek-harness/blob/1f81c047fc48fa190a683e469daa699857e9afe8/vendor/schemastery/package.json).

## Interfaces retained

| Surface | Contract used by GeneralRouter |
| --- | --- |
| Tool definitions | [`defineTool`](https://github.com/deepseek-ai/deepseek-harness/blob/1f81c047fc48fa190a683e469daa699857e9afe8/packages/core/tools/src/schema.ts#L554) and `ctx.tools.register` retain their schema/implementation interface. |
| Dispatch policy and outcomes | [`tools/pre-execute`, `tools/post-execute`, `tools/result`](https://github.com/deepseek-ai/deepseek-harness/blob/1f81c047fc48fa190a683e469daa699857e9afe8/packages/core/tools/src/index.ts#L143) remain separate policy and observation stages. GeneralRouter gates dispatch through pre-execute plus `ctx.tools.guard`; final results are immutable observations. |
| Native worker start | The native tool returns [`{ kind: 'activation', subagentId }`](https://github.com/deepseek-ai/deepseek-harness/blob/1f81c047fc48fa190a683e469daa699857e9afe8/packages/subagent/tool-subagent/src/index.ts#L330). Actual execution remains owned by DSH. |
| Worker completion | [`subagent/start` and `subagent/end`](https://github.com/deepseek-ai/deepseek-harness/blob/1f81c047fc48fa190a683e469daa699857e9afe8/packages/subagent/subagent/src/types.ts#L77) share `runId`, child `id`, backend `provider` and `local`; end adds `stopReason`. Creation failure before residency does not invent a start/end pair. |
| Model metadata | [`llm.resolveModelInfo(provider, model, signal)`](https://github.com/deepseek-ai/deepseek-harness/blob/1f81c047fc48fa190a683e469daa699857e9afe8/packages/llm/llm/src/index.ts#L756) resolves an exact route's reasoning metadata. Reading metadata does not establish that a session may dispatch that route. |
| Usage settlements | [`assistant/message` and `assistant/attempt`](https://github.com/deepseek-ai/deepseek-harness/blob/1f81c047fc48fa190a683e469daa699857e9afe8/packages/core/session/src/types.ts#L331) preserve the timed stream, including failed attempts. Missing provider usage remains unknown. `session/event` observes new settlements; `session/flush` provides an awaited recording boundary. |
| Workspace and projection | [`workingDirectory.get(session)`](https://github.com/deepseek-ai/deepseek-harness/blob/1f81c047fc48fa190a683e469daa699857e9afe8/packages/session/working-directory/src/index.ts#L122) and `session.header.cwd` remain available. The native model-selection projection still describes session policy, not the complete provider model catalog. |
| Bundled skill | [`skills.registerProvider`](https://github.com/deepseek-ai/deepseek-harness/blob/1f81c047fc48fa190a683e469daa699857e9afe8/packages/skill/skill/src/index.ts#L390) retains its list/get provider contract and disposal ownership. |

The compatibility update does not alter DSH provider settings, its native model-selection policy, or the separate Codex/Antigravity plugin entry points. A route returned by GeneralRouter can still be rejected by DSH's actual dispatch rules.

## GeneralRouter v0.1.10 corrections

The same update addresses lifecycle and reporting defects in the adapter:

- Effort metadata is looked up for enabled, valid effective mappings, independently of the native allowlist projection. A missing metadata record still yields the recorded default fallback; frozen routes are unchanged.
- Automatic pre-start failure cleanup carries the authorized tool-call identity. A rejected duplicate call cannot settle another call's reservation. Actual worker completion remains a separate host event.
- Background quota refresh follows active host execution and in-flight preparation/authorization. An unused reservation alone does not keep polling alive; the ten-minute cache remains in force.
- Repeated child binding merges observed route evidence. Incomplete identity remains unknown, and a later partial notification cannot erase an observed mismatch. Late binding retains pending lifecycle and usage observations.
- Optional cache-read/cache-write token fields absent from DSH remain unknown rather than becoming zero. This changes reporting for new observations; historical experiment data is not rewritten.

No score formula, model capability snapshot, same-model repair policy or user DSH configuration changes accompany these corrections.

Cleanup depends on host observations: a final tool result settles only that call's authorized pre-start reservation, while an activated child waits for its real end event. Crash recovery, entirely missing host result/end events, and authorization committed with its response lost are not a complete durable-recovery feature in this release. Host records label rc.3 as the compatibility target, not as a detected running application version.

## Evidence limit

The source comparison establishes that these interface files did not change across the pinned releases. It does not establish successful installation, model dispatch, quota retrieval, or usage recording in a running rc.3 application. No tests, compilation/import checks, simulated routing, or model calls were performed for this compatibility update.

# DSH compatibility

GeneralRouter v0.1.12 admits DSH Desktop `>=0.2.1-rc.2 <0.2.2-0`. This covers the 0.2.1 release family from rc.2 onward, including the stable 0.2.1 release. The latest inspected source is rc.5; the range is an admission policy, not evidence about future interfaces. This compatibility update is based on the official source contracts. It has not been installed or runtime-verified against rc.5.

## Pinned upstream sources

| Release | Official commit |
| --- | --- |
| [dsh-v0.2.1-rc.2](https://github.com/deepseek-ai/deepseek-harness/releases/tag/dsh-v0.2.1-rc.2) | `687f282e8625c6b8d11ff58211a90524be2e4317` |
| [dsh-v0.2.1-rc.3](https://github.com/deepseek-ai/deepseek-harness/releases/tag/dsh-v0.2.1-rc.3) | `1f81c047fc48fa190a683e469daa699857e9afe8` |
| [dsh-v0.2.1-rc.5](https://github.com/deepseek-ai/deepseek-harness/releases/tag/dsh-v0.2.1-rc.5) | `b830ec950faba0a46dda564be3e2d854afed94f1` |

The previous update compared rc.2 to rc.3. For v0.1.11, the official rc.3 and rc.5 sources were compared: tools, agent/agent-loop, session, system prompt, LLM/PI provider, subagent, skill, session projection, working directory, app boot and plugin-manager code is unchanged; their package versions advanced. Schemastery is unchanged.

The intervening [reasoning-body migration](https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.2.1-rc.5/docs/upgrade-guide/v0.2.1-rc.4/reasoning-body-props/guide.md) adds rawText for custom UI renderers. GeneralRouter does not contribute to that UI slot; it records durable session settlements, so no renderer migration is needed.

## Package compatibility

Previous manifests enumerated individual RC versions, requiring a package update even when only the host version changed. DSH evaluates `engines.dsh` with `includePrerelease: true`. The manifest now uses `>=0.2.1-rc.2 <0.2.2-0`; `@deepseek-ai/dsh-tools` and `@deepseek-ai/dsh-llm` use the same peer range. The explicit lower prerelease comparator also allows same-tuple RC versions under ordinary npm range matching. The upper `-0` excludes even 0.2.2 prereleases, avoiding accidental admission across that boundary. These comparator and prerelease semantics are documented by [node-semver](https://github.com/npm/node-semver#prerelease-tags).

This admits later 0.2.1 RCs and stable 0.2.1 without editing the manifest for each RC. Actual interface changes can still require plugin work. Releases before rc.2 or from 0.2.2 onward remain outside this policy. See the [official compatibility rule](https://github.com/deepseek-ai/deepseek-harness/blob/b830ec950faba0a46dda564be3e2d854afed94f1/packages/boot/app-boot/src/plugin-compatibility.ts#L54).

DSH's source-development Node requirement remains `^22.19.0 || >=24.0.0`, and its package manager remains `pnpm@11.28.5`. `@deepseek-ai/schemastery` remains `3.18.5`. These are upstream runtime/build declarations, not additional commands required for a Desktop user adding the packaged `.tgz`. See the [root manifest](https://github.com/deepseek-ai/deepseek-harness/blob/b830ec950faba0a46dda564be3e2d854afed94f1/package.json) and [Schemastery manifest](https://github.com/deepseek-ai/deepseek-harness/blob/b830ec950faba0a46dda564be3e2d854afed94f1/vendor/schemastery/package.json).

## Interfaces retained

| Surface | Contract used by GeneralRouter |
| --- | --- |
| Tool definitions | [`defineTool`](https://github.com/deepseek-ai/deepseek-harness/blob/b830ec950faba0a46dda564be3e2d854afed94f1/packages/core/tools/src/schema.ts#L554) and `ctx.tools.register` retain their schema/implementation interface. |
| Dispatch policy and outcomes | [`tools/pre-execute`, `tools/post-execute`, `tools/result`](https://github.com/deepseek-ai/deepseek-harness/blob/b830ec950faba0a46dda564be3e2d854afed94f1/packages/core/tools/src/index.ts#L143) remain separate policy and observation stages. GeneralRouter gates dispatch through pre-execute plus `ctx.tools.guard`; final results are immutable observations. |
| Native worker start | The native tool returns [`{ kind: 'activation', subagentId }`](https://github.com/deepseek-ai/deepseek-harness/blob/b830ec950faba0a46dda564be3e2d854afed94f1/packages/subagent/tool-subagent/src/index.ts#L330). Actual execution remains owned by DSH. |
| Worker completion | [`subagent/start` and `subagent/end`](https://github.com/deepseek-ai/deepseek-harness/blob/b830ec950faba0a46dda564be3e2d854afed94f1/packages/subagent/subagent/src/types.ts#L77) share `runId`, child `id`, backend `provider` and `local`; end adds `stopReason`. Creation failure before residency does not invent a start/end pair. |
| Model metadata | [`llm.resolveModelInfo(provider, model, signal)`](https://github.com/deepseek-ai/deepseek-harness/blob/b830ec950faba0a46dda564be3e2d854afed94f1/packages/llm/llm/src/index.ts#L756) resolves an exact route's reasoning metadata. Reading metadata does not establish that a session may dispatch that route. |
| Usage settlements | [`assistant/message` and `assistant/attempt`](https://github.com/deepseek-ai/deepseek-harness/blob/b830ec950faba0a46dda564be3e2d854afed94f1/packages/core/session/src/types.ts#L331) preserve the timed stream, including failed attempts. Missing provider usage remains unknown. `session/event` observes new settlements; `session/flush` provides an awaited recording boundary. |
| Workspace and projection | [`workingDirectory.get(session)`](https://github.com/deepseek-ai/deepseek-harness/blob/b830ec950faba0a46dda564be3e2d854afed94f1/packages/session/working-directory/src/index.ts#L122) and `session.header.cwd` remain available. The native model-selection projection still describes session policy, not the complete provider model catalog. |
| Bundled skill | [`skills.registerProvider`](https://github.com/deepseek-ai/deepseek-harness/blob/b830ec950faba0a46dda564be3e2d854afed94f1/packages/skill/skill/src/index.ts#L390) retains its list/get provider contract and disposal ownership. |

The compatibility update does not alter DSH provider settings, its native model-selection policy, or the separate Codex/Antigravity plugin entry points. A route returned by GeneralRouter can still be rejected by DSH's actual dispatch rules.

## GeneralRouter v0.1.10 corrections

v0.1.10 introduced the following lifecycle and reporting corrections, retained by v0.1.11:

- Effort metadata is looked up for enabled, valid effective mappings, independently of the native allowlist projection. A missing metadata record still yields the recorded default fallback; frozen routes are unchanged.
- Automatic pre-start failure cleanup carries the authorized tool-call identity. A rejected duplicate call cannot settle another call's reservation. Actual worker completion remains a separate host event.
- Background quota refresh follows active host execution and in-flight preparation/authorization. An unused reservation alone does not keep polling alive; the ten-minute cache remains in force.
- Repeated child binding merges observed route evidence. Incomplete identity remains unknown, and a later partial notification cannot erase an observed mismatch. Late binding retains pending lifecycle and usage observations.
- Optional cache-read/cache-write token fields absent from DSH remain unknown rather than becoming zero. This changes reporting for new observations; historical experiment data is not rewritten.

No score formula, model capability snapshot, same-model repair policy or user DSH configuration changes accompany these corrections.

Cleanup depends on host observations: a final tool result settles only that call's authorized pre-start reservation, while an activated child waits for its real end event. Crash recovery, entirely missing host result/end events, and authorization committed with its response lost are not a complete durable-recovery feature in this release. Host records retain rc.5 as the source compatibility target and add the admitted compatibility range; neither is a detected running application version.

## Evidence limit

The source comparison establishes that these interface files did not change across the pinned releases. It does not establish successful installation, model dispatch, quota retrieval, or usage recording in a running rc.5 application. No tests, compilation/import checks, simulated routing, or model calls were performed for this compatibility update.

## v0.1.13 selection-only change

The version range is unchanged. This release removes Router pre-execute/guard handlers and dispatch management, retaining tool registration, bundled skill delivery and passive session/tool-result observations. No new runtime compatibility test was performed. External plugin request/usage event coverage is unknown until observed; model invocation belongs to the main agent.

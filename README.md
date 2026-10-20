# GeneralRouter

GeneralRouter 是基于 DeepSeek Harness（DSH）的任务级模型路由插件，当前版本为 **v0.1.6 experimental**。它根据任务参考向量、候选模型能力和可用额度选择模型，并记录选择理由、实际调用和用量。

DSH 主 agent 配合 planner skill 拆分任务，将任务资料提交给 GeneralRouter；主 agent 收到匹配结果后，通过 DSH 原生 `subagent` 派遣工作。

| 组件 | 职责 |
| --- | --- |
| DSH | 主 agent、会话、工具、任务编排和子 agent 执行 |
| GeneralRouter | 任务参考资料、模型能力匹配、额度准入、路由解释和用量记录 |
| EasyCLIProxyAPI（ECPA） | 模型网关及上游报告的额度状态 |

## v0.1 范围

- 四维能力资料：agentic、coding、knowledge/document、scientific reasoning；候选池包含 12 组模型与 effort 配置。
- 使用 AA 分项成绩建立模型能力快照；planner 根据四维定义和 12 个人工语义任务锚点估计任务权重，并记录判断理由。
- 区分 planner 估计与已有 AA 样题参考拟合。只有任务信息不足或不适合四维描述时保留未知权重并显式回退，缺少 AA 同类样题本身不要求回退。
- 将匹配结果交回 DSH 主 agent；effort 有对应支持时传入，否则使用模型默认设置。
- 读取 ECPA 报告的适用额度百分比；活动期间最多每十分钟自动刷新，空闲时停止刷新。
- 记录调用、重试、tokens、观测耗时和 API 等效成本；未提供的数据保留未知值。
- 默认向主 agent 返回精简结果，使用 `detail: true` 可获取细节；首次提交任务 profile 后可用返回的 `profile_id` 继续匹配。
- 按 run 在本地保存记录失败的诊断与同项补写恢复信息，避免诊断只随一次工具回复出现。

当前匹配按能力评分和可用约束选取模型。成本和耗时用于记录与后续比较，尚未用于优化选择，也尚未证明节省效果。额度快照不能保证未来任务绝不耗尽账号。

普通任务默认使用 `weight_source: planner_estimated`：四个非负权重之和为 1，表示各类能力的相对侧重；任务难度单列，不能从权重大小推断。skill 中的人工锚点提供一致的判断示例，向量是设计者制定的启发式参考，不是 AA 测得的任务要求。资料随插件固定版本发布，后续随资料版本更新，不在任务执行时实时搜索榜单，也没有自动定期更新。

历史 AA 任务参考拟合仅覆盖四个相关商业交付样例，反映小样本模型表现的统计关联；它们继续可用，但不能当作通用语义标尺，也不能视为编程或科学任务已校准。`task-profile.v3` 扩展了权重来源，保留原有 profile 的兼容性。匹配仍采用默认 0.4 综合先验；新估计来源不构成成功率、质量保证或自动降低 effort 的规则。

## 当前运行记录与局限

v0.1.5 已有两次由用户在 DSH 发起的闭环运行记录：

- 加法网页：通过路由派遣原生子 agent，产物生成并记录任务完成。
- 离线 CSV 分析：完成有先后依赖的 A → B 子任务流程，保留派遣与完成记录。

数值检查的依据是子 agent 的报告及持久运行记录，浏览器功能尚未完整验收。这些记录说明具体流程已跑通，不能证明全部候选均适用，也未证明成本、耗时或额度节省。公开榜单题评测与正式对照尚未执行。

v0.1.6 针对告警诊断信息丢失和主 agent 上下文开销增加了四项改动：match/status/report 默认精简返回、`profile_id` 复用、skill 的简短调用流程，以及独立的 run 级记录诊断。正常流程不要求主 agent 读取内部账本。诊断保存在实际工作区的 `.runs/generalrouter/diagnostics/<run-id-hash>.json`，可用于定位失败记录及后续同项补写的恢复状态。

本次发布未运行测试、review、编译校验、模拟路由或实际模型验收。上述改动尚未证明减少 tokens，也不能保证全部记录错误已根治；v0.1.5 的两次记录不能替代 v0.1.6 的运行验收。

## 构建与使用

目标宿主为 DSH Desktop `0.2.1-rc.2`，本机需要 Python 3。在仓库根目录生成安装包：

```text
py -3 scripts/package_dsh_plugin.py
```

生成的 `dsh-generalrouter-0.1.6.tgz` 位于仓库根目录。通过 DSH App 的 **Add plugin** 输入该文件在本机的绝对路径，安装并启用；已有安装通过 App 插件管理更新包并新建会话，本机配置继续保留。打包命令只生成发布文件，不运行模型。

插件在各会话中提供包内 planner skill；本机连接配置共用 `$DSH_HOME/generalrouter/host.local.json`（未设置 `DSH_HOME` 时为 `~/.dsh/generalrouter/host.local.json`）。换工作区后新建会话即可，无需复制配置或 skill。项目根目录下的同名配置和 skill 可覆盖共享默认，用量与任务记录始终保存在实际工作区。首次接入仍需配置 DSH provider、子模型允许列表和 ECPA 管理连接；缺失额度连接时保留 unknown，不阻止内置默认模型匹配。详见[插件安装说明](dsh-generalrouter/README.zh-CN.md)。

后续计划使用同一个本地翻译 App 开发任务，对比 DSH 默认编排与 DSH 加入 GeneralRouter 后的质量、耗时、tokens、额度变化及 API 等效成本。两组使用相同的本地翻译模型；该对照尚未执行。

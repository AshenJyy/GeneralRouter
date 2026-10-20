# GeneralRouter 的 DSH Desktop 插件

GeneralRouter 是依托 DeepSeek Harness（DSH）的子任务模型路由插件，当前版本为 **v0.1.6 experimental**。DSH 提供主 agent、会话、工具和原生子 agent 的执行编排；GeneralRouter 提供模型能力与任务资料、候选匹配、额度准入、选择解释和用量记录。主 agent 接收路由结果后，通过 DSH 原生 `subagent` 发起执行。

面向 **DeepSeek Harness Desktop 0.2.1-rc.2**，按官方 bundle 方式分发标准 npm tarball：`dsh-generalrouter-0.1.6.tgz`。包内包含预先写好的 JavaScript ESM 入口、`dsh.bundle.patch`、完整 Python 匹配模块、能力快照和 planner skill；没有编译、安装生命周期脚本，也不依赖原始源码仓库。Python 解释器需要在本机可用，Windows 默认 `py -3`。

主 agent 使用 skill 生成四维任务资料，调用 `generalrouter_match`，再将返回的 `dispatch` 交给 DSH 原生 `subagent`。插件保留原生派遣接口，通过派遣前授权检查选择结果。包内 `runtime/data/model-call-mappings.v0.1.json` 内置 12 组候选的 provider、模型 ID 和 effort 调用映射，默认全部启用；DSH 原生允许列表仍限制实际可派遣范围。用户负责在 DSH App 注册相应 provider 并设置子模型允许列表与 effort 元数据。插件不代改 DSH App 的这些设置。

## 安装

在 DSH App 内安装，无需运行 PowerShell 脚本，也无需退出 App：

1. 打开 **Plugins（插件）→ Add plugin（添加插件）**。
2. 在 **Package name or address（包名或地址）** 中输入已下载或构建的 `dsh-generalrouter-0.1.6.tgz` 在本机的完整绝对路径，点击 **Install（安装）**。
3. 安装完成后选择 **Enable now（立即启用）**。
4. 在任意实际工作区新建主会话。`projectRoot` 和 `hostConfig` 均可留空，工作区内不必预先放置 GeneralRouter 配置或 skill。

包内 planner skill 由插件向 DSH 的 skill 服务提供，供各工作区会话使用；不再要求逐项目复制。项目中已有同名 skill 时，使用项目版本作为覆盖。安装插件不改变 DSH 的主模型、provider、原生子模型允许列表或 effort 元数据，这些仍由用户在 DSH App 配置。

已有安装通过 App 的插件管理更新到 0.1.6 包，然后新建主会话使用新版 skill。现有共享连接配置、项目覆盖和历史运行记录继续保留，无需重新填写。项目或用户目录中的同名旧 skill 仍会优先于包内版本；使用这类覆盖时，应同步其内容或备份后移走覆盖，才能使用本次更新。

## 任务向量

planner 默认参考包内四维定义、榜单说明和 12 个人工语义任务锚点，输出 `weight_source: planner_estimated`。`task_weights` 的四个值均为非负数且总和为 1，分别表示工具与流程操作、编程、知识与文档、科学推理的相对侧重。`rationale` 简要说明参照哪个锚点、如何根据实际任务调整；`source_task_id` 和 `calibration_anchor_id` 留空，避免把人工示例标成 AA 实测。

人工锚点是可解释的判断规范，不是通过跑分拟合出来的任务真值。任务难度与侧重单独记录；简单网页与复杂项目都可能偏重 coding，向量本身不决定 effort。缺少 AA 同类样题不再要求四个 `null`；只有任务信息确实不足，或主要能力超出四维覆盖时，使用 `uncalibrated` 和四个 `null`，并说明原因。

历史 Briefcase 参考拟合仍保留精确绑定和类比迁移两种来源，继续要求使用其原始完整权重。它们来自小样本统计关联，不能拿来充当通用语义权重标准。`task-profile.v3` 只扩展来源类型，现有合法 profile 保持兼容；选择记录保留来源和理由，来源标签本身不证明估计准确。

参考资料固定在 skill 内，随插件和资料版本更新。运行时不搜索榜单，没有自动定期更新。模型能力快照、12 组候选和匹配公式保持原有设计：数值任务向量默认与 0.4 的 AA 类别综合先验混合；四个 `null` 使用显式综合回退。价格、耗时和 effort 资源优化本次尚未加入选择策略。

## 共享配置与工作区

本机共享配置位于 `$DSH_HOME/generalrouter/host.local.json`，默认是 `~/.dsh/generalrouter/host.local.json`；对应额度连接文件通常放在同一目录的 `quota.local.json`。ECPA 的管理地址和密钥来源在本机准备一次即可，切换工作区后无需重复填写。密钥不进入模型映射或发布包。

`host.local.json` 按以下顺序叠加：包内默认值 → 本机共享配置 → 项目配置。默认的项目配置为实际工作区根目录下的 `host.local.json`；显式填写 `hostConfig` 时，改用该文件作为最后一层覆盖。项目文件可只填写需要覆盖的字段；`routes` 等普通对象递归合并，额度对象 `quota` 或其文件 `quota_file` 则完整替换，不需要复制整份共享配置。共享文件和项目文件均可省略，内置的 12 组映射仍可用于匹配；未配置额度连接时如实报告 unknown，不能视作无限额度。

额度配置不跨来源拼接账号或凭据：项目配置若明确指定 `quota_file`，就使用其引用的整份额度配置；未指定时沿用共享来源。`routing_config`、`rates_file` 和 `quota_file` 的相对路径，分别以定义该字段的配置文件所在目录解析；`routing_config: null` 和 `rates_file: null` 使用包内路由资料与价格快照。`runtimeRoot` 默认指向已安装包内的 `runtime/`，无需填写。

任务和用量记录与连接配置分开。`state_dir` 的相对路径始终从当前工作区根目录解析，默认写入 `.runs/host`，不会因读取共享配置而写入共享目录。绝对 `state_dir` 也必须位于当前工作区内，否则返回配置错误，避免不同工作区共用账本。插件不向安装包写入私有配置或运行状态。

`projectRoot` 优先取 DSH 主会话创建时的项目根目录（`session.header.cwd`），仅当该信息缺失时才使用 DSH 的会话工作目录服务；不取插件安装目录或启动 DSH 的进程目录。非空的显式配置可覆盖默认值。若 DSH 无法提供主会话目录，插件返回配置错误，需先打开工作区，或显式指定 `projectRoot`。建立 run 后，子 agent、重试和后续记录均沿用该 run 绑定的根目录；主会话或子 agent 中的 `cd` 不改变此绑定。切换工作区后新建主会话，配置会自动按新工作区解析。

已有项目的 `host.local.json`、`quota.local.json` 和同名 skill 不会被插件自动删除或覆盖，它们仍可作为项目定制。若希望完全采用共享模式，可自行备份后移走项目覆盖文件。发布资料中的 `GeneralRouter-DSH-配置模板/` 和安装包 `examples/` 仅用于首次配置或项目覆盖，不是每个工作区的安装前提。

包内默认映射指向 `easy-cliproxyapi` provider：GPT-6 Astra、GPT-6.1 Sol 各五组 effort，以及固定 high 的 Gemini 3.8 Flash、Gemini 4 Argon。GPT 的最大档传入 `max`，不是 `ultra`。DSH App 中的 provider ID 需与插件映射一致；设置好 provider、连接凭据、子模型允许列表与 effort 元数据后新建会话。无需 quota group，也无需逐项填写这 12 组模型 ID。`routes` 仅用于可选覆盖，空对象采用包内默认值。

源码中的 `dsh-generalrouter/Install-Desktop.ps1` 和安装包内的 `Install-Desktop.ps1` 是可选命令行辅助工具，不是 App 安装或切换工作区的前置步骤。正常安装与使用均不需要运行它。

## 额度读取

直接读取 ECPA 返回的剩余额度百分比，不做本地 quota group、固定每任务百分比扣减或额度预留估算。ECPA 仍可能按模型/窗口/账号分别报告百分比，这些是上游提供的状态。

不用时没有后台额度刷新。首次匹配按需读取，worker 运行期间默认每 **600 秒（10 分钟）**刷新；最后一个 worker 结束后停止。手动 status 可要求刷新。已有快照在 10 分钟内复用，派遣前检查记录是否可用；刷新失败时保留错误，不能将旧百分比当成新读数。账号其他客户端或一次长任务仍可能在两次读取之间消耗额度，不能保证绝不耗尽。

## 执行与记录

- `generalrouter_match` 首次接收完整的 `profile_json`、`original_request`，返回 `profile_id`；同一 run 后续可使用该 ID 继续匹配，无需重复发送完整任务资料。原有完整 profile 调用方式继续兼容，已有计划内容保持不变。
- match/status/report 默认返回 compact 精简结果，供主 agent 继续当前工作；需要候选评分或记录详情时显式传入 `detail: true`。精简回复不表示相应持久记录被删除。
- 每次匹配选择一个依赖就绪任务；主 agent 保留返回的完整 `dispatch` 和派遣标记，再调用原生 `subagent`。
- 原生派遣前核对 provider/model/effort、允许列表和已选任务。插件不改写工具参数，worker 的进一步派遣交回主 agent。
- effort 仅在本地映射和宿主支持列表均允许时传入；否则省略，使用 provider default 并记录差异。
- `generalrouter_status` 查看百分比、已选任务和剩余工作；`refresh: true` 手动读取。
- `generalrouter_report` 汇总 run，`generalrouter_release` 取消尚未启动的选择。派遣授权标记用来防止错误或重复执行，不代表估算并预扣额度。
- 失败任务可由当前 `profile_id`（或原完整 `profile_json`）、`task_id` 和 `retry_failed: true` 重新匹配。实际完成事件才记为 completed，错误和中断分别保留。

skill 采用简短流程：准备一次 profile → 匹配 → 原样派遣 → 等待完成 → 使用 `profile_id` 匹配下一个任务 → 输出精简报告。主 agent 直接使用工具结果，正常工作不读取内部账本，也不反复抄写完整 profile、候选列表或用量明细。用户需要解释或排错时再请求 `detail: true`。

插件使用原生会话事件记录主 agent 和 worker 的 provider/model、重试次数及宿主报告的 tokens。不写完整提示词到用量日志，未报告的数值保留为空；reasoning 已包含在 output 时不重复计价。缓存读写与未缓存输入分别计算 API 等效成本。该指标按模型 token 用量和价格快照计算，不是分摊订阅账单。

当前匹配依据能力与可调用约束，成本和耗时用于记录与后续对照。缩短工期、控制订阅用量是评估目标；已有最小闭环记录尚不足以证明这些目标已实现。

记录从本次插件加载后的事件开始；正式对照使用新会话。匹配前暂存的 planner 用量会在建立 run 后补写，不回填更早的历史调用。

## 记录诊断

记录写入失败的诊断独立按 run 保存到实际工作区的 `.runs/generalrouter/diagnostics/<run-id-hash>.json`。它与主要任务、用量账本分开，用于定位失败发生在哪一项记录，并保留同一项后续补写成功时的恢复状态。诊断仍属于本地运行资料，不放进安装包或共享连接目录。

这项改动补充诊断留存与恢复信息，不能据此保证所有错误已根治。出现告警时先看工具返回的摘要，排错时再请求详情；正常任务流程无需读取诊断文件或内部账本。

## 包与开发状态

发布包布局：

```text
package/
  package.json          # dsh.bundle.patch 和 engines.dsh
  cordis.patch.yml      # 插入插件 row
  index.mjs             # 已写好的 ESM，无需编译
  runtime/
    generalrouter/      # Python 标准库实现
    data/               # 能力、价格、额度模板和 model-call-mappings.v0.1.json
    skills/generalrouter-task-profile/
    config.example.json
  examples/             # 外部连接与可选覆盖模板，不含密钥；routes 可留空
  Install-Desktop.ps1
  README.zh-CN.md
```

在仓库根目录运行以下命令，生成根目录下的 `dsh-generalrouter-0.1.6.tgz`、安装说明和配置模板：

```text
py -3 scripts/package_dsh_plugin.py
```

该命令仅生成发布文件，不导入或安装插件，不执行模型。

v0.1.5 已有两次用户在 DSH 发起的闭环运行记录：一次加法网页任务，以及一次有 A → B 依赖关系的离线 CSV 分析任务。记录涵盖路由派遣、子任务完成与用量。数值检查来自子 agent 报告及持久运行记录，浏览器功能未完整验收。这里列出的是已有运行记录，本次文档整理没有重新执行测试。

这两次运行不代表全部候选已验证，也未证明成本、耗时或额度节省。公开榜单题实跑和正式对照仍留作后续工作。

v0.1.6 为部分告警诊断丢失和主 agent 上下文开销偏大增加了 compact 默认输出、profile ID 复用、简短 skill 流程及独立诊断留存。本次保留 12 组候选、评分与默认 0.4 综合先验，以及活动期间每十分钟额度刷新规则；没有加入价格或 effort 资源优化。发布时未运行测试、review、编译校验、模拟或实际模型验收，尚不能声称这些改动已经节省 tokens 或完全解决记录问题。

接口依据官方 tag `dsh-v0.2.1-rc.2`：

- [官方 bundle、tarball 与配置层顺序](https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.2.1-rc.2/docs/user/develop/basic/publish.md)
- [App 添加、启用与配置插件的界面用词](https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.2.1-rc.2/packages/client/ui-plugin-manager/src/client/locales.ts)
- [Desktop 安装所有权与随附命令](https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.2.1-rc.2/apps/desktop/README.md#bundled-command-runtime)
- [原生 subagent 工具](https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.2.1-rc.2/packages/subagent/tool-subagent/src/index.ts)
- [四维任务资料说明](runtime/skills/generalrouter-task-profile/SKILL.md)

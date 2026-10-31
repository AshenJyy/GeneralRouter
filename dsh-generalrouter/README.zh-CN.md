# GeneralRouter 安装与使用

当前版本 **v0.1.13 experimental**。Router 只负责根据任务向量选出模型与首选 effort，并解释评分。主 agent 使用自己已有的原生、Codex 或 Antigravity 插件工具执行。Router 不再绑定 ECPA、不签票据、不拦截调用，也不管理任务顺序和修复流程。

## 安装

通过 DSH App 内 **Add plugin** 添加本机完整路径 `E:/GeneralRouter/dsh-generalrouter-0.1.13.tgz`，安装并启用。旧版通过 App 插件管理更新后新建会话，避免旧会话仍带着原生派发要求。无需运行安装脚本或复制源码仓库。

包采用官方 npm tarball 格式，自带 JavaScript 入口、Python 匹配代码、数据与 skill；本机需有 Python 3，Windows 默认 `py -3`。宿主范围仍为 `>=0.2.1-rc.2 <0.2.2-0`；源码对照到 rc.5，范围声明不代表未来版本已经运行验证。

## 配置

`projectRoot` 与 `hostConfig` 默认留空。项目根目录跟随 DSH 主会话工作区；配置按内置默认、`$DSH_HOME/generalrouter/host.local.json`、项目根目录 `host.local.json` 顺序读取。未设置 DSH_HOME 时使用 `~/.dsh`。切换工作区后新建会话即可。

模型调用工具的登录、模型 ID 和权限由现有 DSH/插件配置提供，GeneralRouter 不修改它们。旧 routes 映射保留用于识别已观测用量的价格，已不决定选模、调用工具或 provider。项目/个人同名 skill 优先于包内版本，旧覆盖需同步新版内容。

ECPA 管理连接仅用于可选额度参考。活动时读取，十分钟内复用缓存，空闲时不刷新；不预扣、不开额度许可、不用百分比过滤本版排名。不保证 ECPA 的账户窗口对应其他插件登录的账户。

## 使用

1. 主 agent 用 `generalrouter-task-profile` 准备四维任务权重，调用 `generalrouter_match`。
2. Router 返回模型、首选 effort、匹配分数和理由；`detail: true` 可看所有候选。主 agent 自己选择对应调用工具。有条件设置 effort，无条件则 default 并说明。
3. 失败由主 agent 处理；修复沿用同模型、新开 subagent，并交接简短 memento。无需经过 Router 授权、准备或再次匹配。

首次可提交完整 profile，后续可用 `profile_id` 复用；也可以直接提交新计划。`selection_id` 只是可选记录关联，不是票据，不用放进 worker 提示词。

`generalrouter_record_result` 可保存主 agent 报告的执行与验收结果；`generalrouter_status`、`generalrouter_report` 均为可选。记录不是测试。自动用量仅覆盖插件收到的 DSH 会话事件，外部插件若不提供 token 事件则保持 unknown；报告是已观测小计，不保证全流程完整。价格按保存的 API 单价换算，不是订阅账单。

## 构建与边界

在项目根目录运行 `py -3 scripts/package_dsh_plugin.py` 仅生成安装包与说明。v0.1.13 保留原四维数据与评分公式，删除执行管控。随后一次[真实演示](../docs/demos/2026-10-30.md)记录了推荐、Antigravity worker 调用、HTML 交付和用户固定样例验收；实际 worker 模型/effort 与完整用量仍未确认，修复分支未验证。历史实验记录未改写，也没有新的性能对照结论。

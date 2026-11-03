# 模型路由兼容性与额度口径

核验日期：2026-11-04（Asia/Shanghai）。范围为 Claude Code、Codex CLI 与 Google Antigravity CLI（`agy`），继续复用官方 CLI 的账号登录。本页记录官方能力与实现依据。0.3.0 已接入 Codex 的只读额度查询；模型发现和重路由通知尚未接入，详见 [本版功能](model-routing.md)。

## 档位应当映射到账号可用的模型

`fast`、`balanced`、`deep` 是本工具的任务档位，不是三家通用的模型名称，也不代表固定金额或固定额度。模型型号与推理强度要分别保存。

| 成员 | 单次调用选择 | 账号可用模型的核验入口 | 可供配置的候选映射 |
| --- | --- | --- | --- |
| Claude | `--model <alias-or-id>`；`--effort <level>` | 交互式 `/model`；Agent SDK 另有 `supportedModels()` | `haiku` / `sonnet` / `opus`；别名会随 CLI、账号与官方发布变化 |
| GPT | `-m <id>`；`-c 'model_reasoning_effort="medium"'` | 交互式 `/model`；本地 `codex app-server` 的 `model/list` | 当前官方候选：`gpt-6-luna` / `gpt-6.1-sol` / `gpt-6-astra`；需先确认该账号可用 |
| Gemini | `--model <slug>`；`--effort <level>` | `agy models`；可另行执行 `agy -p "/effort" --output-format json` 查看当前模型努力等级 | 从当前账号列表选 Flash 类与较强 Gemini 模型；不要从产品展示名猜 slug |

这组映射是可修改的初始建议，不是已经验证过用户会员权限的清单。没有可用模型证据时，应提示主持人配置，或明确显示“沿用 CLI 默认模型，尚未配置档位映射”；不能显示为已经完成型号切换。手动填写型号也必须允许官方 CLI 返回不可用错误。

Claude 的三个别名有官方明确的轻量、日常、复杂任务定位；可以采用它们避免固定旧版型号。若需要复现结果，应保存 CLI 版本以及实际报告的模型列表，必要时使用完整型号。不要将 `best` 当成省额度默认值。

Codex 当前官方模型目录推荐 Luna 处理明确、可重复任务，Sol 处理日常复杂工作，Astra 处理最困难任务。此目录还记录了旧 GPT-5.4、GPT-5.4 mini 与 GPT-5.5 在 ChatGPT 登录方式下的退役；不能照搬旧 API 型号清单。不同官方帮助页的迁移示例可能更新不同步，**当前账号的模型列表优先**。

Antigravity 的官方示例包括 `gemini-3.8-flash-medium`、`gemini-4-argon-high`，仅可作为配置示例；账号、套餐和版本会影响可用项。若 `--model` 不认识指定型号，headless 模式会失败退出，不会像交互模式那样静默改用别的型号。

依据：[Claude 模型配置](https://code.claude.com/docs/en/model-config)、[Claude 账号模型与用量说明](https://support.claude.com/en/articles/14552983-models-usage-and-limits-in-claude-code)、[Codex 模型目录](https://learn.chatgpt.com/docs/models)、[Antigravity headless](https://antigravity.google/docs/cli/headless/)。

## 推理强度不宜跨厂商强行统一

- 可用值随模型和客户端变化。优先取发现接口返回的支持列表；未核验时留空，沿用模型默认值，比强行传一个共同值更可靠。
- Claude 支持单次 `--effort`，但 `CLAUDE_CODE_EFFORT_LEVEL` 环境变量优先级更高。实现若宣称控制本次努力等级，必须处理这项覆盖，否则界面只能显示“请求努力等级”。组织上限与 CLI 回退也会影响最终等级。
- Codex 用 `model_reasoning_effort` 配置。当前目录建议 Luna 从 High 起步、Astra 从 Light（CLI 中为 `low`）起步，Sol 从客户端默认起步。因此 `fast=low / balanced=medium / deep=high` 不是三家都适用的官方模型推荐。
- `agy` 的 `--effort` 只作用于本次调用；交互式 `/effort <level>` 会保存默认值。`low`、`medium`、`high`、`xhigh`、`max` 是否可选由当前模型决定，固定推理预算模型不可调。headless `/effort` 是查询，不能用 `/effort low` 代替参数设置。

首版可以先做明确的模型档位切换，让努力等级保持模型默认，再由主持人显式选择已核验的强度。以后根据本项目实际质量、用量与失败重试统计调整映射，不预设“低努力一定更省总额度”。

依据：[Claude 努力等级](https://code.claude.com/docs/en/model-config#adjust-effort-level)、[Codex 配置参考](https://learn.chatgpt.com/docs/config-file/config-reference)、[Antigravity effort](https://antigravity.google/docs/cli/commands/effort/)。

## 请求模型、CLI 回显与使用记录必须区分

| 来源 | 可读取的信息 | 可以诚实展示什么 |
| --- | --- | --- |
| 路由器自己的选择 | 档位、型号/别名、努力等级、自动或手动、选择理由 | 请求配置；不是后端实际运行证明 |
| Claude JSON `modelUsage` | 模型名到用量的映射；可能包含多个模型、子代理与回退调用 | “CLI 报告使用模型”列表及原始用量；不能只取第一个作为最终回答型号 |
| Claude JSON `usage`、`total_cost_usd` | 主代理用量与估算美元成本；具体范围见下文 | CLI 报告用量、估算成本；不是订阅实际扣费或剩余额度 |
| Codex `exec --json` 的 `turn.completed.usage` | 输入、缓存输入、输出，当前示例另含推理输出 token | CLI 报告的本次用量；标准示例未保证有实际模型字段 |
| `agy` stream-json 的 `init.model` | 只有显式设置 `--model` 时才出现的运行配置 | CLI 回显模型配置；不能据此独立证明服务端最终型号 |
| `agy` stream-json 的 `result.usage` | 输入、输出、思考、缓存读取与总 token | CLI 报告的会话累计用量；结果协议未保证实际模型字段 |

缺失字段显示“CLI 未报告”，不能把请求值填入实际值，也不能让模型通过自述“我是某型号”来充当证明。应保留字段来源，便于未来协议更新。

Claude `modelUsage` 是调用累计值，包含恢复会话带入的历史和子代理用量；`usage` 仅覆盖主代理本轮。当前独立进程、单次提示词、不恢复会话的调用方式可直接记录该次结果。将来若改为长连接，应取累计差值或最后结果，不能每轮累加累计值。失败结果也可能已经消耗用量。

同样，`agy` 的 `num_turns`、`usage`、`duration_seconds` 在流式输入会话中累计；`response` 才对应当前轮。当前每次只发一个用户事件时没有多轮重复累计问题。

依据：[Claude 用量字段语义](https://code.claude.com/docs/en/agent-sdk/cost-tracking)、[Claude 类型参考](https://code.claude.com/docs/en/agent-sdk/typescript#sdkresultmessage)、[Codex 非交互协议](https://learn.chatgpt.com/docs/non-interactive-mode)、[Antigravity 输出协议](https://antigravity.google/docs/cli/headless/)。

## 模型发现和额度查询的官方接口

Codex 官方本地 `app-server` 通过标准输入/输出提供 JSON-RPC，可复用现有登录，不需要用户新增模型 API Key：

- `model/list`：模型 ID、默认与支持的努力等级、是否隐藏、分页游标等；不要写死文档示例。
- `account/rateLimits/read`：后端报告的已用百分比、窗口、重置时间及可能存在的额度桶和 credit 信息。本版已读取安全的窗口/使用状态字段，保留来源与查询时间；空字段仍是未知。不采集账号个人信息、令牌或 credits 金额。
- `model/rerouted` 通知：`fromModel`、`toModel`、`reason`。这是未来实现切换披露的可靠来源之一，不能假设当前 `exec` 输出已经提供这个通知。

Claude 官方交互式 `/usage`、Codex `/status`、Antigravity `/usage`（别名 `/quota`）可供用户检查账户。Claude Agent SDK 有 `supportedModels()`，本次没有核验到独立 `claude models --json` 命令。不要虚构命令。`agy` 的 `/model`、`/usage` 不应塞进群聊使用的 stream-json 用户事件流；官方要求另开独立命令执行。

依据：[Codex App Server](https://learn.chatgpt.com/docs/app-server)、[Claude SDK 模型发现](https://code.claude.com/docs/en/agent-sdk/typescript#query)、[Claude 命令速查](https://support.claude.com/en/articles/14553413-claude-code-cheatsheet)、[Antigravity usage](https://antigravity.google/docs/cli/commands/usage/)。

## “省额度”的产品承诺边界

选择更适合任务的模型、限制无效讨论、缩短重复上下文，有助于控制消耗；不能由 API 价格表推算三家会员还剩多少额度，也不能预先承诺节省百分比。订阅消耗受模型、任务、上下文、推理、工具与服务端规则共同影响。自动路由应记录可观测用量与完成质量，把节省视为优化目标。

Claude 官方说明不同产品端共享使用限制，模型和努力等级都影响消耗。Codex 官方说明可完成任务数并不固定，依套餐和任务设置而变；其用量查询应使用后端实际数据。Antigravity 官方说明额度与智能体完成的工作量相关，并受容量及套餐影响。

还要区分“复用会员登录”和“保证不会消耗额外付费 credits”。三家账号都可能在官方产品设置中启用额外 credits；本工具没有读取并控制全部厂商计费设置，不能仅凭账号登录宣称绝无额外消耗。尤其应避免把高价加速模式当作本工具的轻量 `fast` 档位。路由器也不应在会员用尽时自动改成 API Key 或其它计费通道。

本次没有调用任何真实会员模型验证具体账号的可用项、消耗或节省比例。应在用户已登录设备上先核对模型列表，再执行短任务连通验证。

依据：[Claude 使用限制](https://support.claude.com/en/articles/11647753-how-do-usage-and-length-limits-work)、[Claude 额外 credits](https://support.claude.com/en/articles/12429409-manage-usage-credits-for-paid-claude-plans)、[Codex 会员登录与消耗](https://help.openai.com/en/articles/11369540-using-codex-with-your-chatgpt-plan)、[Codex 用量管理](https://help.openai.com/en/articles/20001516-managing-usage-with-gpt-6-astra-in-work-and-codex)、[Antigravity 套餐](https://antigravity.google/docs/plans)、[Antigravity CLI credits](https://antigravity.google/docs/cli/credits)。

# 模型选择、额度查询和中断恢复

0.3.0 继续使用已登录的官方 CLI，不新增模型 API 或额度估算模型。账号额度和计费规则以官方服务为准。

## 选择优先级

1. 本群主持人覆盖：每家单独指定固定模型或自动选择。
2. 成员全局设置：固定模型，或轻量/均衡/深入三档映射。
3. 固定模型留空：由 CLI 自己选默认模型，工具不会假称已确定型号。

自动配置缺少档位时拒绝调用，不悄悄换模型。默认仍为手动，保留旧版行为。候选型号可由账号官方列表核验并修改。Claude 支持模型别名及 `[1m]` 一类容量后缀；自动档位不会调整推理强度或开启付费加速。

本地 `heuristic-v1` 根据问题与上下文长度、阶段、代码/堆栈、设计、数学证明和约束等给出分数：0 轻量，1–3 均衡，4 以上深入。分数是可解释规则，不代表真实任务难度或额度成本。每次调用记录信号和请求型号；三家可映射到完全不同型号。同轮设置冻结，后续发言上下文增加仍可能影响自动档位。

`model_route` 包含 mode、source、tier、requested_model、reason、complexity、quota_snapshot。Claude `modelUsage` 的模型列表标为 usage，可能包含多个模型；agy `init.model` 标为 configuration，只是配置回显。Codex 未报告实际型号时保持缺失。模拟调用另有 simulation 标志。

## 官方额度快照

| 成员 | 当前实现 | 未获取时的行为 |
| --- | --- | --- |
| GPT / Codex | `codex app-server --listen stdio://`；initialize → initialized → account/read（refreshToken=false）→ account/rateLimits/read | 缺少 CLI、非 ChatGPT 登录、网络或协议错误均显示未知 |
| Claude | 尚未接入无需生成请求的独立查询；官方 statusline 中的额度需会话先收到模型响应 | 显示未支持独立读取，可在官方 `/usage` 查看 |
| Gemini / agy | 不自动执行 `/usage`；未核验稳定机读格式，且本机禁用斜杠命令时可能把它发给模型 | 显示未支持，可在官方交互界面查看 |

Codex 查询不创建 thread、turn，不登录、不要求生成回复，丢弃原始账号信息和诊断日志。只保存官方额度桶、窗口已用比例、重置时间和明确返回的使用状态。剩余比例是 `100 - usedPercent` 的显示换算，不代表剩余 token、次数或金额。不同额度桶分别显示，不能跨模型相加。

调用前按成员查询，缓存 60 秒；打开面板可手动刷新。不是持续推送。演示模式不执行任何额度子进程。默认每次只读查询最多约 8 秒；查询失败不影响用户已经请求的模型调用。每个发言记录当时的额度快照，状态轮询仅取缓存，不另发查询。

额度快照不提供任务能完成的保证。其他窗口可同时消耗额度；百分比为 100、重置时间已过，都不作为自动恢复或切模型的依据。本版也不会凭查询结果自动启用账号 credits。

## 中途失败与恢复

明确的额度耗尽和限流错误分别分类，但都会暂停该成员后续新调用。状态独立持久化在数据目录的 `model-availability.json`，影响同一工具实例的所有群，重启后保留。已在运行的其他请求不因这个标记立即终止；新的发言和摘要会跳过暂停成员。无法识别的错误保留原错误，主持人可手动暂停。

暂停不移出群，不将未投票视为同意，不触发只为等待暂停成员的自动续轮。若其他成员提出分歧，原有上限内的讨论仍可继续。

每条失败发言保存为系统事件，标注 incomplete、retry_member、task、model_route。CLI 失败或取消时，部分正文与状态块不写入有效聊天、看板、候选记忆。其他成员已经成功提交的内容保留。失败可能已经消耗官方额度，本工具不能退回。

恢复调用只清除暂停；本轮开始时暂停的成员要等下一轮才参与。点击「补答此成员」必须在当前讨论结束且该成员恢复之后，使用当前模型设置、最新共享记忆和讨论上下文重新请求该成员。该补答不安排其他成员发言，也不触发自动摘要或自动续轮。成功后解除其待补状态。

这不是 CLI 内部推理的断点续传。未完成的输出不会被当成可靠检查点。当前记录在进程运行时处理 CLI 的超时、失败和手动取消；如果操作系统或整个工具进程突然崩溃，已持久化内容保留，但尚未写下的失败事件无法保证存在。

## HTTP 接口

所有写请求需 `X-AIGC: 1`，使用现有认证和本机访问限制。

- `PUT /api/settings`：每家 `routing: {mode, profiles: {fast, balanced, deep}}`；`model` 仍表示固定模型。
- `PATCH /api/groups/:gid`：`model_overrides: {claude: {mode, model}, ...}`；省略成员表示继承，清空对象恢复全继承。
- `POST /api/groups/:gid/model-preview`：`{text, task}`；返回 routes、errors、estimate，不保存消息或调用模型。
- `GET /api/members/:member/quota?refresh=1`：返回 `{quota}`；不传 refresh 则使用有效缓存。
- `POST /api/members/:member/pause`、`resume`：返回 `{availability}`，不启动模型请求。
- `POST /api/groups/:gid/retry/:member`：提交仅该成员的补答；无待补消息、暂停中或讨论中返回 400。
- 群 state 包含 model_availability、model_quota、incomplete_members；聊天导出包含请求模型与已报告的使用模型来源。

官方证据与协议边界见 [模型兼容性核验](model-routing-compatibility.md)。

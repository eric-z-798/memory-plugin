# 记忆接口（0.5.0，R1 修复）

应用仍只依赖 Python 标准库。`aigc.memory.MemoryStore` 可独立使用，不需要三家 CLI、模型 API 或网页服务。

```python
from pathlib import Path
from aigc.memory import MemoryStore

memory = MemoryStore(Path("local-data"))
project = memory.create_project("产品设计")
entry = memory.add(project["id"], "第一版先支持网页", kind="decision",
                   source={"author": "user", "method": "manual"})
context = memory.context(project["id"], "第一版支持什么端？", budget=6000)
print(context["text"])
```

宿主必须提供可信来源元数据。模型候选应使用 `status="pending"`；只有用户明确确认后才将状态更新为 `confirmed`。预算单位为 Unicode 字符，不是 token；context 返回实际入选条目、快照 revision 与是否截断。查询没有命中时可能仍附带通用偏好及决定，其他项目不会混入。

## HTTP

所有 HTTP API（含 GET、HEAD 和导出）均要求本次启动的 `X-Xingshu-Token` 请求头。修改仍需 `X-AIGC: 1`；远程密码模式另需 Basic 认证。令牌不写入数据文件，不要把真实令牌放进示例或共享日志。

成员调用前会按当前存在/confirmed 状态过滤冻结的记忆快照，撤回或删除的条目记入消息 `memory_withdrawn_ids`；其余内容编辑下一轮生效。已发出调用不能收回。详见 [安全与确认规则](review-security.md)。

所有修改请求需要 `Content-Type: application/json` 和 `X-AIGC: 1`。如果启用了服务密码，使用相同的 HTTP Basic 身份验证。服务本身面向单个用户；项目隔离不是多租户鉴权。

| 方法与路径 | 行为 |
| --- | --- |
| GET /api/projects | 列出项目，首次访问时登记旧群的独立项目 |
| POST /api/projects | `{ "name": "项目名" }` 创建项目 |
| POST /api/groups | 可选 `project_id` 关联现有项目；省略即创建独立项目 |
| GET /api/groups/{gid}/memories?q=预算&status=all | 当前项目及全局记忆，status 可为 all/pending/confirmed |
| POST /api/groups/{gid}/memories | 新增记忆；content 必填，kind/status/scope 可选，source_seq 可关联本群原消息 |
| PATCH /api/groups/{gid}/memories/{mid} | 带 version，修改 content/kind/status；版本冲突返回 409 |
| DELETE /api/groups/{gid}/memories/{mid} | JSON 请求体带 version，删除记忆与历史；停止使用相关项目记忆的当前讨论 |
| GET /api/groups/{gid}/memories/{mid}/history | 查询各版本，当前版本排在前面 |
| GET /api/groups/{gid}/memory-context?q=问题 | 预览已确认记忆的检索与裁剪结果 |
| GET /api/memories/export | 导出所有项目、记忆和版本历史；不包含账号凭据 |

新增示例：

```json
{
  "content": "项目预算调整为 3 万元",
  "kind": "decision",
  "status": "confirmed",
  "scope": "project",
  "source_seq": 12
}
```

scope 为 `project` 或 `global`。`source_seq` 省略时视为人工录入；服务从真实消息生成来源，不接受客户端自行指定原作者。单条记忆最多 4000 字符。

群聊的 `STATUS` 可含 `memories: [{"content": "…", "kind": "note"}]`，每条响应最多保存 5 项候选。调度器强制当前项目、真实发言者与消息来源、pending 状态，并把原始候选存入消息的 `memory_proposals`。模型不能通过此字段自行确认记忆或写入全局范围。

修改已有记忆请使用其 ID 和 version；创建另一条记忆不会自动识别并替代语义冲突的旧条目。本版不会按 AI 自报置信度自动解决此类冲突。

## 备份和删除

停止服务后备份整个数据目录，包含 JSON/JSONL 和 memory.sqlite3。恢复同样在停机状态进行。JSON 导出用于迁移与审阅，本版未提供 JSON 自动导入端点。

删除记忆清除当前记录与记忆历史。原聊天、看板、已发送的第三方输入和独立备份不属于此删除操作；群聊源数据中的候选也保留作来源记录。完整遗忘需要另行处理这些源数据。


R1 HTTP 来源默认值：`POST /api/groups/<gid>/memories` 未传 `status` 且 `source_seq` 指向非用户聊天作者时，默认 `pending`；显式传入 `confirmed` 表示用户主动确认。手动创建和用户消息默认 `confirmed` 不变。底层独立 `MemoryStore.add` 的默认值不变。

删除记忆响应仍有 `ok`，另外返回 `warnings` 数组；某个群停止后的系统消息写入失败不会阻止继续停止其他相关群。删除、编辑和撤回的原有生效语义未调整。

# CLI 接入与验证记录

核验日期：2026-11-04。记忆层无需新增模型 API，模型回复继续通过用户安装的官方 CLI 获取。官方 CLI 的账号登录、额度与适用套餐仍由对应提供商管理。

## 当前适配协议

| 成员 | 调用协议 | 成功判定 |
| --- | --- | --- |
| Claude | `claude -p --output-format json`，完整提示词从标准输入传入 | 退出码为 0，结果 JSON 无错误，正文非空 |
| GPT | `codex exec ... -`，标准输入，`-o` 保存最终回复 | 退出码为 0，最终正文非空；临时输出文件随调用清理 |
| Gemini | `agy --input-format stream-json --output-format stream-json`，发送一个 user 事件后关闭标准输入 | 退出码为 0，恰有一个最终 result，状态为 SUCCESS，正文为非空字符串 |

失败或超时返回的部分正文不会当成成功发言提交给看板与记忆。Gemini 长上下文也通过标准输入发送，不依赖模型调用文件读取工具。请使用支持以上协议的当前 CLI 版本；旧版报不支持参数时，应先升级，不自动换用未经验证的协议。

Gemini 保留用户原项目的 Antigravity CLI 适配器。Google 官方目前发布了 [Antigravity headless 协议](https://antigravity.google/docs/cli/headless/) 与 [Gemini CLI 迁移文档](https://antigravity.google/docs/cli/gcli-migration/)。本工具没有实现旧 `gemini` 命令的独立适配器，不应把其可执行路径直接填进 `agy` 配置。原 README 中有关某一天停止会员服务的描述本次没有得到明确核验，不作为安装前提。

## 账号与设置

开启“只用会员额度”时：

- Claude 调用前执行 `auth status`，只接受明确的账号登录方式；API Key、第三方或未知方式停止调用。
- Codex 调用前执行 `login status`，确认 ChatGPT 登录，同时向本次调用设置 `forced_login_method="chatgpt"` 与 `model_provider="openai"`。
- 三家调用环境去掉已知的 API Key、第三方服务与 ADC 路由变量。Gemini 只检查公开 `settings.json` 中的路由字段，发现 `modelProvider` 或 Cloud 路由时拒绝调用；不读取 keyring、token 或密码文件。
- **Gemini 没有在本适配器中可完整验证的个人账号计费检查。** 成功连通不能证明具体计费方式；用户仍应在官方 `agy` 界面确认个人账号及额度。任何一家的会员套餐都不等于无限额度。

依据：[Claude CLI 认证状态说明](https://code.claude.com/docs/en/cli-reference)、[Codex 配置参考](https://developers.openai.com/codex/config-reference/)、[Antigravity 认证与 API/ADC 路由](https://antigravity.google/docs/install?tab=cli)。

附加参数仅用于调优，防止覆盖账号路由、输入协议或恢复不相关会话：

| 成员 | 支持的附加参数 |
| --- | --- |
| Claude | `--effort high`、`--max-turns 5` |
| GPT | `-c model_reasoning_effort=low`、`-c model_reasoning_summary=auto`、`-c model_verbosity=low`（具体值由所用 CLI 验证） |
| Gemini | `--effort high` |

模型名称请填写模型栏。任意附加开关、权限绕过、其他 provider 或会话恢复参数会被拒绝。Claude 限制内置工具及 MCP，Codex 使用只读沙箱并禁用 shell 工具；Gemini 使用临时工作目录及官方权限行为。**本工具不是三家 CLI 的操作系统级隔离容器，不能把提示词中的“不读写文件”视为完整的工具权限保证。**

## 已完成与未完成的验证

- 使用合成 CLI 子进程验证标准输入、中文长文本、结果解析、失败/部分结果、账号类型拦截、参数保护、超时、取消与临时文件清理。
- 读取本环境 Codex `0.166.0-alpha.1` 的 `exec --help` 验证相关命令入口；没有读取或使用本环境的账号凭据。
- 没有进行真实三家会员账号模型调用。交付后需在用户已登录的设备上分别执行“测试连通”，再验证群聊与共享记忆完整流程。

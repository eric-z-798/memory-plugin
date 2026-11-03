# CLI 安装、登录和代理核验

核验日期：2026-11-04（Asia/Shanghai）。以下是官方能力与本版实现边界的依据；未在真实账号或三种操作系统上执行安装、登录。本工具辅助启动官方流程，用户在官方浏览器页面完成授权，账号密码、验证码与令牌不应交给本工具收集。

## 官方安装入口

| 成员 | macOS / Linux | Windows PowerShell | 默认命令位置 |
| --- | --- | --- | --- |
| Claude Code | `curl -fsSL https://claude.ai/install.sh \| bash` | `irm https://claude.ai/install.ps1 \| iex` | 本机用户安装目录；完成后以 `claude --version` 核验 |
| Codex | `curl -fsSL https://chatgpt.com/codex/install.sh \| sh` | `irm https://chatgpt.com/codex/install.ps1 \| iex` | Unix `~/.local/bin`；Windows `%LOCALAPPDATA%\Programs\OpenAI\Codex\bin` |
| Antigravity CLI | `curl -fsSL https://antigravity.google/cli/install.sh \| bash` | `irm https://antigravity.google/cli/install.ps1 \| iex` | Unix `~/.local/bin/agy`；Windows `%LOCALAPPDATA%\agy\bin` |

表格中的竖线为真实 shell 管道。安装器本身会下载并执行官方发布程序，可能修改用户 PATH 或 shell 配置；工具不应声称完全没有系统变更。辅助流程无需自行添加 sudo 或管理员提权。安装完成要重新检测实际命令，而不能只凭终端已打开显示成功。

Claude 当前原生安装不依赖 Node；Windows 10 1809+、macOS 13+、官方列出的现代 Linux、x64/ARM64 为其主要支持范围。Git for Windows 目前是推荐项，缺少时可使用 PowerShell，不应按旧文档强制阻止安装。Codex 官方 `CODEX_NON_INTERACTIVE` 会跳过安装器提示，官方将其定位为脚本安装/更新而非首次配置，首次引导宜保留交互。agy 首次启动可能还会询问主题、终端呈现方式及工作区信任。

依据：[Claude 安装与要求](https://code.claude.com/docs/en/setup)、[Codex 安装](https://learn.chatgpt.com/docs/cli)、[Codex 安装器变量](https://learn.chatgpt.com/docs/config-file/environment-variables)、[agy 安装](https://antigravity.google/docs/getting-started)。

## 登录与状态

| 成员 | 辅助启动的官方命令 | 可无生成请求检查的状态 | 限制 |
| --- | --- | --- | --- |
| Claude | `claude auth login` | `claude auth status`，JSON；登录返回 0，未登录返回 1 | 不传 `--console`，该选项选择 API 用量计费；检查 `authMethod` 是否为账号方式 |
| Codex | `codex login` | `codex login status`，显示当前认证方式；存在登录凭据时返回 0 | 默认打开 ChatGPT OAuth 页面；有凭据不等于当前套餐有可用额度 |
| agy | `agy` | 本次未核验到独立、稳定的账号状态子命令 | 交互启动从系统钥匙串尝试已有登录，无有效会话时打开浏览器；不可虚构 `agy login` 或 `agy auth status` |

Codex 的 `codex login --device-auth` 是官方设备码流程，适用于无浏览器或 localhost 回调受阻的场景。它要求个人安全设置或工作区管理员允许设备码登录。普通浏览器登录默认回调为 `localhost:1455`；浏览器所在设备、CLI 所在设备和回调路径必须匹配。

Claude 官方命令参考将无 `--console` 的登录描述为 Claude 订阅方式。本次官方页面未列出 `--claudeai`；如果要显式传该参数，应先由本机 `claude auth login --help` 确认当前安装版本支持，不能仅凭第三方文章硬编码。

agy 在真实 SSH 环境下会显示授权 URL，要求用户在浏览器完成登录后将授权码粘回远端终端；本工具应保留该官方终端交互，不自动收集或转存授权码。没有图形桌面的服务器不能假称已经在用户电脑打开登录窗口。手机访问服务器上的网页也不会把 CLI 安装到手机。

本版不应通过读取、复制 `auth.json`、钥匙串令牌或登录日志来“同步账号”。已有身份由官方 CLI 管理，用户取消浏览器授权或关闭终端都不代表成功。安装/登录后的“测试连通”属于另一次模型请求，可能消耗额度，不能混入只读安装检测。

依据：[Claude 命令参考](https://code.claude.com/docs/en/cli-reference)、[Codex 登录命令](https://learn.chatgpt.com/docs/cli/reference#codex-login)、[Codex 设备码与回调](https://learn.chatgpt.com/docs/auth#login-on-headless-devices)、[agy 认证](https://antigravity.google/docs/install?tab=cli)。

## 代理配置

- Claude 官方支持 `HTTPS_PROXY`、`HTTP_PROXY`、`NO_PROXY`，也接受小写变量，且小写 HTTPS 项优先；官方明确不支持 SOCKS。新进程才能读取新的 shell 环境。
- Codex 当前官方源码支持 HTTP(S)/ALL/NO_PROXY 环境变量；启用系统代理策略时，Windows/macOS 的系统或 PAC 结果可能优先于环境变量。因此设置子进程环境不等于一定覆盖操作系统路由。
- agy 官方更新记录包含代理继承及 localhost 后端代理故障修复，但不同版本、后台进程和产品界面的行为可能不同；本次未核验统一、独立的 CLI 代理配置命令。

首版统一输入宜限 HTTP/HTTPS 地址；不把 SOCKS 地址强塞给全部三家。对于自定义代理，大小写变量要一致，清除冲突的旧值；回环地址加入绕过列表，避免 OAuth 回调或本地服务误走代理。网络检查只能说明指定官方站点在当前配置下的连通结果，不能证明浏览器使用同样代理、OAuth 成功、模型可用或订阅有额度。

Windows 安装下载还有 PowerShell 版本差异：微软仅从 PowerShell 7.0 起明确支持网络 cmdlet 读取代理环境变量，Windows PowerShell 5.1 应显式传 `-Proxy`。为当前辅助进程设置 `Invoke-RestMethod:Proxy` 和 `Invoke-WebRequest:Proxy` 默认参数，可覆盖这些 cmdlet 的调用；不能据此承诺覆盖安装器内其他下载客户端。`-NoProxy` 从 PowerShell 6.0 引入，不可直接用于 5.1。本次未取得安装脚本正文，未验证其所有后续下载路径。

不要通过关闭 TLS 校验来处理证书错误。企业证书场景应使用官方 CA 设置：Claude 提供 `NODE_EXTRA_CA_CERTS` 等机制；Codex 登录支持 `CODEX_CA_CERTIFICATE`，未设置时回退 `SSL_CERT_FILE`。普通代理设置功能不自动改全局证书或系统代理。

依据：[Claude 代理与证书](https://code.claude.com/docs/en/network-config)、[Codex 官方代理选择源码](https://github.com/openai/codex/blob/main/codex-rs/http-client/src/outbound_proxy.rs)、[Codex 登录证书](https://learn.chatgpt.com/docs/auth#custom-ca-bundles)、[Antigravity 更新记录](https://antigravity.google/docs/changelog)、[PowerShell 网络 cmdlet](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.utility/invoke-restmethod?view=powershell-7.5)。

## 引导实现建议

安装、登录按钮应显示将启动的官方命令及执行位置，只接受固定的成员/动作，不接受网页提交的任意 shell 命令。代理地址作为经过校验的环境值传入；不要直接拼到可执行命令字符串。终端脚本、路径、完成标记应由工具生成并限制为当前用户可访问。演示模式和自动化测试不得真的执行安装或登录。

桌面可用时，辅助打开系统终端并继承本工具选定的子进程网络配置；无法启动终端时给出准确的可复制命令和手动核验步骤。用户自己的浏览器授权仍由用户完成，这一步不能通过“一键”绕过。

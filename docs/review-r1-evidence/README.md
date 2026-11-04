# 原始测试证据

`manifest.json` 列出实际命令、工作目录、退出码、原始输出字节数与 SHA256。`.log.gz` 是未删节的 stdout/stderr 合并输出，用 Python `gzip.decompress` 或 `gzip -dc` 读取。保留了失败的基线和中间迭代，不只存成功摘要。

基线用 `git archive 433a43a2656782c4ff3567be2c721e81299a8d52 | tar -x -C /tmp/xingshu-r1-baseline` 解包，然后复制新增回归文件到其 tests 目录。`regression-baseline-final` 使用与修复分支相同的最终 20 项回归；49 个失败和 9 个错误是 unittest 子测试累计，退出码 1 属预期失败证据。修复分支原有 246 + 新增 20 = 266 项，全部通过。

jsdom 首次安装因默认 npm 缓存目录不可写失败；改用 `/tmp` 缓存后成功，没有改变权限或绕过限制。仅 QA 使用，不增加运行时依赖。

以上记录来自作者自测；准确最终提交 SHA 的再次完整运行另随最终交付归档提供。截图和所有运行日志也保存在 `/workspace/r1-evidence`。未执行真实 CLI 或真机测试。

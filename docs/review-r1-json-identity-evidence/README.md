# 顶层 JSON 身份修复原始证据

manifest.json 保留实际命令、cwd、退出码及未删节原始输出SHA256；.log.gz为stdout/stderr合并原文。before在711306e上复现Store写出记录被嵌套seq误隔离（退出1）。after六项新增回归通过，含键排列、嵌套/字符串/重复键/结构错误/身份来源。全量282 Python、90 DOM和四浏览器通过。

本次为代码作者复现和自测；前序独立QA报告libfile_329a3e93cc3881918c009b6238fddc11已完整读取，但不能替代新SHA独立验收。准确新提交的再次全量原始日志随最终交付归档提供。

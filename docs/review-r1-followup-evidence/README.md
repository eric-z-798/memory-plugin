# B/G 后续修复原始证据

manifest.json 保存原始命令、cwd、退出码和原始输出 SHA256。所有 .log.gz 都是 stdout/stderr 原样合并输出的 gzip 压缩，未删节。

repro-before 是在 6bd1f713 生产代码上复现父线程给出的两个独立探针：1 failure、1 error、退出码 1。repro-after 为原探针加两个边界回归，4 项通过。全量 Python 270 项、DOM 90 断言及四浏览器脚本通过。新提交准确 SHA 的再次完整验证保存在最终交付归档中。

独立 QA 的原 ZIP 下载失败，未取得可读本地文件；本地复现使用父线程提供的完整最小脚本和原始输出，不把此目录伪称为原 ZIP。这里是代码作者自测结果，不是新提交的独立验收或 Claude 复审。

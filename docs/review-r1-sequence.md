# R1 B 序号进位与恢复边界修复

前序提交：`f1e8430b2e68536cfcae20ceea50cc7f54bfbc33`。沿用 `fix/claude-r1-20261104`，不改 main、不合并、不部署、不建 PR。

独立 QA 对前序提交确认原两探针、270 项 Python、90 项 DOM、四浏览器及取消竞态通过，但发现 4300 个 9 的坏行 seq 可以解析，恢复消息的 `high + 1` 却变成 4301 位，`json.dumps` 因整数转换限制失败。已完整读取可读 Library 报告 `memory-plugin-f1e8430-independent-review.md`（`libfile_19fc1fe5ccc48191942d1b69c38e2fc9`，181 行），未下载或重试旧 ZIP。该报告针对前序 SHA，不是新提交的独立验收。

## 修复原则与实现

生产修改仅在 `aigc/store.py`。这次不在 Python 的 4300 位默认限制边缘预留一两次进位，也不提高/关闭全局转换限制。

消息 seq 同时作为原生 JavaScript 前端的 JSON 数字、来源和轮询标识，必须能被精确识别。明确采用正整数范围 **1 至 `2^53−1`**。坏行中的数字先按原始十进制字节长度/字典序检查范围，再转换；超范围整行直接隔离，不能污染恢复高水位。JSON 解码后的 seq 另行校验，覆盖转义属性名等绕开字节匹配的情况。

- 超范围记录（包括语法完整的 JSON 行）保留在完整备份和原字节隔离文件中，不改号后重新插回聊天，不当作可准确引用的正常消息。
- 恢复系统消息可见说明“未采用”异常序号；`recovery.ignored_sequences` 逐项记录原文件的 `line`、十进制 `digits` 和 `reason: outside_exact_sequence_range`。`recovery.max_exact_sequence` 记录范围上限。完整数值可在隔离原字节中核对，不将数千位数字复制进普通界面。
- 正常有效记录的 seq 原样保留；正常坏行能识别的 seq 仍计入高水位。例如坏尾 seq=99 后，恢复提示为100，新消息为101、102，两次重启一致。
- 统一 `_next_message_seq` 检查分配上界。若有效数据确实已经用尽范围，明确抛 `StoreError`，原文件不动，不绕回、不重编号；需要分配恢复提示时也先保留完整备份和坏字节，再明确报序号耗尽。这种极端有效高水位需要人工处理或新群，不承诺无界追加。
- 正常追加先完成 JSON 编码与 UTF-8 编码，再打开文件补换行和写入，避免编码失败时产生部分修改。

这是对现有数字标识可表示范围的技术校验，没有改变 R1–R5 的任何待决定语义。已有超范围数字行现在会被明确隔离，属于本次需要复审的数据兼容边界；原字节始终保留。

## 回归与原始证据

新增 `tests/test_review_r1_sequence.py`，既有测试未删除、跳过或放宽。

1. 原始4300位复现：修复前 1 error、退出码1，错误为 JSON序列化超4300位；修复后通过。
2. 4299、4300、4301、5000位 × 完整/截断行：正常前后记录不变，备份与隔离内容逐字节一致；恢复原因与原行号/位数准确，连续追加与两次重启一致。Python整数转换限制前后不变。
3. 普通坏尾 seq=99：确认高水位未复用，恢复100、追加101/102。
4. 精确范围末值：有效 MAX 记录保留，追加明确拒绝，文件不变。
5. 有效 MAX 记录加坏尾：先备份原文件，恢复需新seq时明确拒绝，原文件不变。
6. MAX+1、转义seq属性名、MAX−1进位：越界隔离有原因，最后一个精确seq仍能写入，下一次明确拒绝。

命令、完整未删节 stdout/stderr、退出码及哈希见 `review-r1-sequence-evidence/manifest.json`；准确新提交 SHA 的再次完整验证随最终交付归档提供。命令：

```sh
python3 tests/test_review_r1_sequence.py SequenceRecovery.test_original_4300_digit_repro
python3 tests/test_review_r1_sequence.py
python3 -m unittest discover -s tests
NODE_PATH=/tmp/xingshu-domqa/node_modules node tools/test_ui_dom.cjs
python3 tools/test_browser.py
python3 tools/test_routing_browser.py
python3 tools/test_setup_browser.py
python3 tools/test_review_r1_browser.py
```

本次新增验证是作者自测，不是非作者复验或 Claude 复审。前序独立 QA 原报告已成功读取；本次没有取证下载阻碍。

## 原有未修范围

B 仍不能全面关闭：B-6 多进程写入未修；C 默认本机授权与敏感配置问题、D 当轮撤回问题未修。R1–R5 全部待产品决定。V1–V7 真实 CLI、Windows/macOS、真实断电、真机与屏幕阅读器仍未核实。仅临时合成数据、假CLI与演示模式，无凭据/真实聊天/付费模型调用。没有重建 DOT_HANDOFF.md，不宣布验收完成。


后续 `711306e` 的独立 QA 发现嵌套seq误隔离有效记录的阻断回归；移除全行正则、分离完整记录与畸形行解析的修复见 [顶层身份修复](review-r1-json-identity.md)，仍需对新SHA独立复审。

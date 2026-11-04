# R1 独立 QA 遗漏修复（B / G，2026-11-04）

沿用 `fix/claude-r1-20261104`，直接前序提交为 `6bd1f713c168b3b2cdc9f7156c838544e19c52f7`。非代码作者的独立 QA 对这个确切前序提交复验了 266 项 Python、90 项 DOM、四个浏览器脚本及队列合并；额外两项探针发现 G 摘要失败重试与 B 超长损坏 seq 遗漏，故前序提交不能视为这两项已完全解决。

## 本次改动

- **G**：摘要调用抛出 `MemberError` 或 `ValueError` 时，在未被群主取消的情况下，将成员加入当前 job 与自动续轮共用的 `_failed_members`。后续自动续轮及摘要候选继续使用这个失败集合。新的非自动 job 仍重置集合。提示改为明确区分失败发言的补答与摘要等待新用户回合重试，未添加摘要专用补答语义。
- **B**：将坏行 seq 的整数转换放到已有逐行异常隔离范围内。转换被 Python 拒绝时，原始整行进入隔离文件，完整原文件备份仍保留，其他有效记录照常恢复和追加。没有修改 `sys.set_int_max_str_digits`，没有提高或关闭全局限制。高水位只能采用成功解析的序号；无法解析的畸形数字保留原字节供人工核对，不猜测修补或强行转换。

修改生产文件仅 `aigc/engine.py` 和 `aigc/store.py`。新增 `tests/test_review_r1_followup.py` 保留提供的两项独立探针断言，并增加新用户任务重置、有效相邻消息、追加及两次重启一致性回归。既有测试没有删除、跳过或放宽；独立探针仅将导入根路径改为基于 `__file__`，以便加入 unittest discover，业务与断言未变。

## 本地复现与证据来源

Library 文件标识 `libfile_9fbc660c79a881919f6159ad925fbf53` 读取返回名称 `memory-plugin-6bd1f71-independent-review.zip`、12486 字节，但 ZIP 无可读正文。通过当前 Library 技能准备物化并调用其下载助手后，得到 `library file transfer failed: download failed`，退出码 1；未取得可读本地 ZIP，未猜 URL、未绕过访问限制、未重新尝试下载。具体失败原因未由助手给出，不能把它称为审批拒绝。独立结论与最小复现来源为父线程随后提供的原始脚本、输出与 SHA；本地再次执行确认了两个失败。

修复前命令：

```sh
python3 tests/test_review_r1_followup.py IndependentProbes
```

在 `6bd1f713` 的生产代码上得到 `SUMMARY_COUNTS {'claude': 3, 'gpt': 3, 'gemini': 3}`，`Ran 2 tests`，`FAILED (failures=1, errors=1)`，退出码 1。异常是 `int(match.group(1))` 转换 5000 位数字超过当前 4300 位限制。

修复后命令：

```sh
python3 tests/test_review_r1_followup.py
python3 -m unittest discover -s tests
NODE_PATH=/tmp/xingshu-domqa/node_modules node tools/test_ui_dom.cjs
python3 tools/test_browser.py
python3 tools/test_routing_browser.py
python3 tools/test_setup_browser.py
python3 tools/test_review_r1_browser.py
```

专项四项通过，失败摘要计数均为 1；Python 全量 270 项通过。DOM 和四个浏览器脚本的完整原始输出与退出码随 `review-r1-followup-evidence/manifest.json` 保存，最终确切提交上再次运行的证据随交付归档提供。所有新增验证由本次代码作者执行，不冒充新提交的独立验收。

## 复审边界保持不变

- B 仍为部分修复，**B-6 多进程写入问题未修**；没有新增锁或决定 R3。
- C 默认本机无凭据与敏感设置问题、D 当轮记忆撤回问题仍未修。
- R1–R5 全部待产品决定；共识/单成员生效、记忆快照、默认令牌与权限、WebFetch/WebSearch 默认权限均未改变。
- V1–V7 真实 CLI 范围全部未核实；仅临时合成数据和演示/假 CLI，无真实账号、凭据、聊天或付费模型调用。Windows/macOS、真实断电、真机及屏幕阅读器未验。
- 没有改 main、合并、部署或新建 PR；没有重建 DOT_HANDOFF.md。
- 这次是处理独立 QA 的遗漏，**不是 Claude 已复审，也不是验收完成**。请对最终交付新 SHA 复审上述两项，并保留 [原 R1 交接](review-r1.md) 的剩余边界。


后续针对 `f1e8430` 的独立复验发现4300位seq进位的B边界；进一步修复、精确序号范围及兼容边界见 [序号恢复修复](review-r1-sequence.md)。该修复仍需新SHA独立复审。

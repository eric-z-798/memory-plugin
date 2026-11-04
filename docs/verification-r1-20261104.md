# R1 修复验证（2026-11-04）

基线 `433a43a2656782c4ff3567be2c721e81299a8d52`，分支 `fix/claude-r1-20261104`。Linux、Python 3.12.14、Node 24.22.1、Playwright 1.62.0、Chromium 155.0.8059.79；临时合成数据、fake CLI 和演示模式。

| 命令 | 退出码 | 原始结果 |
| --- | --- | --- |
| `python3 -m unittest discover -s tests`（修改前） | 0 | 246 tests，OK |
| `python3 -m unittest discover -s /tmp/xingshu-r1-baseline/tests -p test_review_r1.py`（最终新增用例对基线） | 1 | 20 tests；49 failures、9 errors，复现失败 |
| `python3 -m unittest discover -s tests -p test_review_r1.py`（修复后） | 0 | 20 tests，OK |
| `python3 -m unittest discover -s tests`（修复后） | 0 | 266 tests，OK，无跳过 |
| `NODE_PATH=/tmp/xingshu-domqa/node_modules node tools/test_ui_dom.cjs` | 0 | 90 DOM assertions |
| `python3 tools/test_browser.py` | 0 | 项目/记忆 CRUD、冲突、来源、导出、手机布局通过 |
| `python3 tools/test_routing_browser.py` | 0 | 路由/暂停/补答/额度模拟界面通过 |
| `python3 tools/test_setup_browser.py` | 0 | 安装登录界面模拟通过，native_actions_executed=0 |
| `python3 tools/test_review_r1_browser.py` | 0 | 41 节点、100% 目标框重叠=0/控件遮挡=0；240% 触摸可达41/41，键盘遍历41/41；AI默认待确认及主动确认通过，无 pageerror |
| `git diff --check` | 0 | 无空白错误 |

[完整原始日志及命令/退出码清单](review-r1-evidence/README.md)。[逐项复审交接与尚未完成项](review-r1.md)。截图存入最终证据归档；触摸来自浏览器 CDP 输入，不能代表真实 iPhone/屏幕阅读器。

没有删除、跳过或放宽原有测试。全部验证由代码作者执行，未将其标为独立验收。C、D、B-6 未修，R1–R5 未决定，V1–V7 真实 CLI 范围、Windows/macOS、真实断电等均未核实。不合并、不部署，仍待 Claude 对最终相同 SHA 复审。


后续独立 QA 在 `6bd1f713` 上发现 B 超长 seq 与 G 摘要失败重复调用遗漏；修复与再次验证见 [R1 后续修复](review-r1-followup.md)。前文 266 项记录是前序版本自测，不代表遗漏不存在或新版本已独立验收。


后续针对 `f1e8430` 的独立复验发现4300位seq进位的B边界；进一步修复、精确序号范围及兼容边界见 [序号恢复修复](review-r1-sequence.md)。该修复仍需新SHA独立复审。

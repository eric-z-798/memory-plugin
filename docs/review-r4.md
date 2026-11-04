# R4: persistence failures and corrupt-group archival

Baseline: 5e8d7afe21011ed97c5b30b3d3211a31d0d8dce3.

W: first-request anchors are written together under the group lock before accepting the user message. Initial progress failure therefore leaves no user message or queued discussion. The runner is resolved before the message commit. An append failure may leave a future unread anchor; it never advances successful seen and is ignored until a corresponding message exists. This does not make group JSON and JSONL a single transaction or solve actual disk/OS failure ambiguity.

A durable member reply is no longer labelled incomplete because progress or later board/candidate persistence failed. Visible system warnings identify the saved reply and unfinished persistence; progress is not advanced on failure. Retry is rejected, avoiding a second model reply. If writing the warning also fails, stderr retains it; the interface cannot promise a persisted notice during total storage failure. Missing board/candidate effects are not silently rebuilt or represented as saved.

X: stop_existing only looks up a runner and signals cancellation; it never creates one or loads group data. DELETE uses it before moving the original directory to archive. Error list entries have a standalone archive action independent of S.group. It works before or after a failed group load and preserves another group's draft. Normal deletion retains its archive semantics.

Existing assertion changes (required behavior changes, not weaker failure coverage):
- test_review_r3 progress-save failure retains disk/in-memory seen assertions, now requires no incomplete member, a warning, and retry rejection.
- test_review_r2 board-save failure retains the durable chat and missing-board assertions; replaces duplicate model retry with rejection, one chat and a warning.
- test_review_r2 candidate failure retains one need_user and one task note; now requires retry rejection and one chat.
- test_review_r1 postprocessing matrix retains actual injected failure and all-members-called assertions in sequential/parallel modes. Message append failure still requires incomplete; board/candidate failures after commit require a warning, no incomplete, and retry rejection.

New regressions inject pre-message progress failure, message append failure, post-reply progress failure, secondary warning write failure, and corrupt group archival/stop without reads. Chromium tests exercise unrelated and selected broken entries, byte preservation, draft preservation, initial HTTP failure with retained input, completed reply without retry, and normal deletion. Only temporary synthetic data and demo adapters are used. The initial development suite failure included two test-fixture assumptions corrected to compare unchanged pre-existing system messages; raw development outputs remain in evidence.

The empty-history text now distinguishes existing history omitted by budget. Sending-draft prefix behavior is unchanged.

This checkpoint does not implement C/D/B-6. Subsequent separately authorized security work may follow on the same branch. AI consensus and WebFetch/WebSearch defaults remain unchanged. Real CLI, actual disk/power failure, Windows/macOS, no-hardlink filesystems, real devices and screen readers remain unverified. Author checks are not independent QA or Claude approval. No deployment/merge is performed.

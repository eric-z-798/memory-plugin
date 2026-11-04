# R3 follow-up: queued triggers and summary advancement

Baseline: d1f6dbeb876110eb9cbb699ad8bc359dba832f3c.

External independent review found that summarization at the end of a running job could cover user messages queued for the next merged job. The prompt builder selected current triggers from users already filtered by summary_upto, losing required full text.

Current triggers now come from the complete eligible user message collection. The summary lower bound and unread budget apply only to older backlog. Current merged triggers remain full, including when they exceed the 60000-character target; the existing explicit overflow notice remains. Restart/new-member fallback, persisted successful progress and failure semantics are unchanged.

Regression: block the first synthetic member with threading.Event, enqueue 30 requests of about 3000 characters, verify one merged queued job, release the first call, assert summary advancement before the second member call, and assert every queued request appears in full plus the overflow notice. Before the fix the 11-test R3 suite failed this test; after the fix all 11 passed. No existing assertions were removed or relaxed.

Changed files: aigc/prompts.py, tests/test_review_r3.py, and this document. Full same-commit command/output/exit-code evidence is delivered separately, including Python, DOM, existing targeted suites, deterministic draft-race browser checks and consecutive default/mixed browser runs. Independent verification is reported separately from author runs.

B-6/C/D remain unresolved; R1–R5 remain pending. This change does not implement multi-process locking, access tokens, memory withdrawal semantics or network-tool policy. Real CLI V1–V7, other platforms, real devices and filesystems without hard links remain unverified. No merge/deployment or Claude approval is claimed.

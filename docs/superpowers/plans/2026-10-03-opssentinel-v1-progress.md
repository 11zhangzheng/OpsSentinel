# Execution ledger — plan: 2026-10-03-opssentinel-v1.md

Base: 07f1a7e. Original checkout clean. Isolated worktree: D:/Desktop/SRE-Agent/.worktrees/opssentinel-v1.

Pre-flight: M1 returns trusted records consumed by M2 projections and M4 scorer; M3 consumes validated action support but independently enforces authorization. No interface conflicts.

Ruling: User's explicit sequential execution authorizes implementation; no repeat design/plan approval. Existing checkout receives reviewed changes without automatic publishing.

Baseline environment: default Anaconda Python lacks pytest-asyncio and temp directory is inaccessible; C drive nearly full. Using D-drive venv and TEMP/TMP, with explicit pytest basetemp. No product workaround.

Baseline after environment setup: 125 passed, 1 skipped, 3 failed. Existing failures: test_transfer_preserves_selected_history_and_removes_other_service, test_report_uses_persisted_action_and_excludes_raw_context, test_report_endpoint_requires_controller_auth. Root cause: missing Store.incident_actions and report API. Repair these existing report contracts in M4.

M1 RED: test_grounding.py 14 expected failures before implementation. Additional fail-closed persistence test reproduced a fallback write after Store failure; fix denies that write.
Ruling: Existing model unit fixture now emits the approved cited-diagnosis schema; accepting its former plain JSON would undermine M1. Existing positional Analyzer inputs remain supported.
M1 contract/regression: 140 passed, 1 skipped; only the same 3 baseline report failures remain. M1 evidence tests passed, including fail-closed persistence. M2 RED: six budget/deadline/cancellation tests failed as expected.
M2 complete: 44 focused tests passed; full suite 149 passed, 1 skipped, same 3 baseline report failures. Added measured/estimated usage, bounded requests, paired context projection, deadline/cancellation/freshness, and model-visible reference tracking. Ruling: use UTF-8 conservative estimation with explicit configuration; no tokenizer dependency. No model credentials consumed.

M3 RED: expired/changed plans, incomplete target identity and host target drift tests reproduced missing enforcement. M3 full regression initially 160 passed, 1 skipped, 4 failed: the three known report failures plus one concurrency fixture with incomplete target identity. Updated that fixture with realistic identity; 70 focused plan/host/engine/monitoring tests pass, 1 Windows skip. The concurrency test now reaches both blocked actions and proves unrelated probes continue.
M3 adds 300-second plan binding, specific-plan approval, target/policy checks, intent metadata and verification deadline. Ruling: demo port changes on owned-process restart are not a change of authority; demo policy binds its owned exercise directory via target fingerprint instead. Public host action API requires plan/target hashes; legacy internal calls remain available only for existing isolated tests. Controller/host/UI/lab clients updated together.

M4 RED: 10 evaluation contract tests failed with missing runner. Implemented separate inputs/oracles and real Analyzer/Engine replay. Additional RED numeric-normalization test prevented Pydantic int-to-float normalization from reducing evidence recall. 26 focused evaluation tests pass, including 15 controlled-response cases (wiring only, not real model quality). Repaired three pre-existing report contracts; first full suite 174 passed, 1 skipped before the 16 added evaluation cases. Final full suite follows.
M4 offline: 15 scenarios x 3 rules runs; RCA/GDR 1/14 = 7.14%, safe abstention 100%, unsafe replay dispatch 0. Live recovery/rollback/MTTR remain null. OPS_MODEL_API_KEY and OPS_MODEL_NAME are absent; no real-model benchmark claim. Ruling: S14 lacks necessary business evidence, so exclude it from answerable RCA/GDR denominator and score abstention separately; cost if wrong: denominator comparability, explicitly recorded in result notes.
M4 final pre-review regression: 190 passed, 1 skipped, 2 existing dependency deprecation warnings, 56.29 seconds. Rules replay evidence recall 100% after numeric-normalization correction. Review range starts at 07f1a7e. No OpenSRE changes, no remote publishing.

Final fresh review (gpt-6-astra, read-only): four Important findings: rotate descriptor not bound to approved inode; stale restore identity at file/deploy writes; full ledger suffix credited despite shortened model context; compensation after unknown deployment. All reproduced RED. Additional adversarial test reproduced assistant-authored fake record laundering unseen IDs. Fixed with trusted-role visibility and actual offered values. One-pass focused verification: 80 passed, 1 Windows skip.
Final: Ruling: usage-average finding promoted from Minor to Important — averaging missing usage as zero misreports measured consumption; cost if wrong: excludes partially measured runs from that average (their known subtotals and estimates remain recorded). RED→GREEN coverage test verifies exclusion and request measurement coverage.
Final: Ruling: declined real-model/live Docker judgments stand — environment is absent and no such success claimed; cost if wrong: field effectiveness remains unverified.
Final: Ruling: deterministic validation remains structural, without a semantic judge — approved v1 scope; cost if wrong: false reasoning can pass structure and must fail scenario evaluation or human review.
Final: Ruling: no new global administrator/Docker coordination — approved bounded checks, fresh identity and descriptor binding fix avoidable races; cost if wrong: narrow races with external mutation remain, no atomicity claim.
Final: fixed approved-log binding — test_rotation_descriptor_must_match_approved_inode RED→GREEN.
Final: fixed restore mutation identity — test_restore_reinspects_container_at_each_mutation[archive/validation] RED→GREEN.
Final: fixed uncertain deployment compensation — test_restore_failure_compensates_only_before_deployment[deployment] RED→GREEN.
Final: fixed shortened evidence scoring — test_unseen_suffix_does_not_score_as_found_evidence RED→GREEN.
Final: fixed assistant visibility laundering — test_assistant_cannot_launder_hidden_fact_as_visible RED→GREEN (fixture confirms the original provider request actually omitted the fact).
Final: fixed missing-usage averaging — test_missing_provider_usage_is_excluded_from_measured_average RED→GREEN.
Final fix-pass suite: 196 passed, 1 skipped, 2 warnings, 56.28 seconds. Four additional positive contract cases verify all fixed host actions still execute for an unchanged, correctly bound target (4 passed). Final original-checkout suite includes these cases. No deferred Minor findings remain after regrading usage reporting.

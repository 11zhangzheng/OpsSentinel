# OpsSentinel v1 Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans inline, with TDD and one fresh final review. The user explicitly authorized sequential execution on 2026-10-03.

**Goal:** Evidence-grounded diagnosis with bounded investigation, controlled remediation, and reproducible evaluation.

**Architecture:** Extend existing Analyzer, Engine, Store, connectors and host agent. Preserve incident states, per-service locks, persisted intent, operation IDs, unknown-outcome handling and recovery verification. No second runtime or generic registry.

**Tech Stack:** Existing Python/FastAPI/Pydantic/httpx/SQLite/pytest stack.

**Spec:** The approved Phase 2 design in this conversation. Binding requirements are copied below so execution can resume without conversation history.

## Global Constraints

- OpenSRE remains untouched; preserve existing UI, monitoring, maintenance, reports and isolated exercises.
- Four read-only diagnostic tools; no shell, integrations, multi-agent, graph, memory or new production module/database table.
- Evidence IDs belong to incident/run; immutable records contain source, timestamps, tool-call ID, successful-source facts or operational errors. Ledger is separate from model context.
- Validate all claims/citations deterministically; no semantic LLM judge. Known root causes require root-cause claims; actions require support and existing connector/policy authorization. Rules use the same evidence contract.
- At most 4 model requests, 4 calls/request, 16 model tool calls, 12-second model request timeout, 35-second run deadline, 2-second tool deadline, 1000 output tokens, default 8192 context tokens with 512 safety reserve, 32000 total tokens.
- Preserve tool exchanges, important facts, source scopes and explicit truncation. Never send a knowingly oversized request. Stop reasons include success, unresolved, no evidence, max turns, model/tool failure, context exhaustion, timeout and cancellation.
- Plans expire after 300 seconds and bind incident/run/action/target/policy; approval names the plan. Re-observe before persisted intent; host rechecks expected target. Unknown outcomes never retry. Maximum two actions, existing cooldown.
- Verify fresh health/business observations; deadline max(180, (recovery_threshold+1)*interval+40). Rollback is a newly authorized bounded action, never automatic compensation after unknown outcomes.
- 15 scenarios; oracle never enters model inputs. Replay and real recovery metrics are separate. Grounded diagnosis requires correct root cause AND all required evidence AND real citations covering required facts.

## Review Focus

- Failed upstream collection wrapped in a successful local read must not become business evidence.
- Citation IDs, fact paths and incident/run ownership must be checked against trusted records, not model-supplied copies.
- Large Unicode logs, tool arguments and injected instructions must not bypass request budgets or authorization.
- Approval expiry during re-observation, changed target/policy and operation-ID collisions must block writes.
- Oracle leakage, missing provider usage and simulated recovery must never inflate benchmark results.

## Task 1: M1 Evidence and diagnosis contract

**Files:** models.py, analyzer.py, store.py, engine.py, connectors.py, host_agent.py; tests/test_grounding.py and existing integration tests.
**Interfaces:** Analyzer.diagnose keeps existing positional inputs and adds incident_id/persist_run_update keywords; returns compatible diagnosis/action/source/context_key plus grounded diagnosis and agent_run. Store saves append-only run records inside incident documents. EvidenceRecord/Diagnosis are the only new production data model classes.
- [x] Write tests for fabricated/cross-run/failed/unseen citations, invalid tool arguments, model grounding rejection, immutable persisted facts and rules fallback.
- [x] Run tests RED; implement deterministic extraction/validation and typed source outcomes.
- [x] Run focused tests and full suite GREEN; record results and commit.

## Task 2: M2 Bounded harness and evidence-aware context

**Files:** analyzer.py and tests/test_agent_budget.py.
**Interfaces:** Pure context projection functions operate on complete exchanges and ledger references. Runtime returns explicit stop_reason, tool_events, usage and duration; cancellation propagates after persistence.
- [x] Write tests for oversized logs, pinned facts, paired messages, iteration limits, argument failures, deadlines/cancellation and context exhaustion.
- [x] Run RED; implement limits, projections, usage and explicit failures inside existing Analyzer.
- [x] Run focused tests and full suite GREEN; record results and commit.

## Task 3: M3 Bound plans to controlled execution

**Files:** engine.py, store.py, connector_helpers.py, connectors.py, host_agent.py, app.py, static/app.js; existing execution tests plus plan tests.
**Interfaces:** approve(iid, plan_id); action requests carry expected target fingerprint. Intent persists plan metadata. Existing commands/locks/operation semantics remain.
- [x] Write tests for expired/changed plans, target drift, incomplete fingerprints, verification timeout, duplicate and unknown actions.
- [x] Run RED; implement plan identity/expiry and target checks, updating existing clients and real contract fixtures.
- [x] Run focused tests and full suite GREEN; record results and commit.

## Task 4: M4 Evaluation contract and runner

**Files:** one tests/evaluation/run.py, fixtures, evaluation contract tests; small existing report additions if necessary.
**Interfaces:** Input and oracle are separate; runner uses real Analyzer/Engine contracts. Results include mode/source/stop/citations/evidence/tool counts/actual or estimated tokens/latency. Live recovery is never inferred from replay.
- [x] Write tests for all 15 fixture shapes, oracle isolation, correct-but-ungrounded failure, incorrect-but-cited failure and forbidden dispatches.
- [x] Run RED; implement runner/scorer and fixtures. Preserve existing live demo/lab constraints.
- [x] Run full suite and offline replay contract checks; run real-model evaluation only if credentials/configuration are present, report missing measurements honestly.
- [x] Fresh final review, test-first fixes, full suite, then transfer reviewed changes to the original clean checkout for user review. No push/publish.

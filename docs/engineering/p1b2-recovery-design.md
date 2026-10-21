# P1-B2 durable generation recovery — design only

This proposal follows P1-B1 structural verification. It is not authorization or implementation. The real corpus currently has zero eligible units, so evidence adjudication and useful source-scoped generation should be demonstrated before investing in execution recovery. Original NEJM fixture regressions and production/clinical approval remain open gates.

## Evidence supporting the design

P1-B1 supplies stable document/configuration/canonical-buffer, concept and unit identities; source ownership distinct from context; explicit warning decisions; a complete source partition; and conservative coverage reconciliation. Thirty plan/evidence artifacts round-trip through the existing compressed interface. Synthetic and native-PDF fixtures demonstrate isolated eligible versus affected units. Qualified exact-source coverage cannot be completed by quoting only context or by one passing chunk covering an entire section.

The existing generator still keeps successful earlier results only in memory. A later request failure rejects the returned package, while diagnostics retain candidate counts. That is the concrete recovery gap. A generation response is not equivalent to a validated draft, and neither implies clinician approval.

## Proposed boundary and execution identity

Keep one immutable source-map/plan artifact using the existing P1-A.1 storage interface. Unit execution records reference IDs and ownership/context fingerprints rather than repeating buffers or copying the evidence graph. Do not introduce another database or orchestration framework without measured need.

A future execution fingerprint should combine the source plan/unit identity with exact materialized request bytes, prompt hash, provider/deployment/API-version configuration, generator normalization/schema version, clinical-validator version and relevant budget/policy configuration. Credentials are never part of persisted metadata. Changing any material component should invalidate automatic reuse of execution results or create a new review version. A source-only hash is insufficient, as the P1-B1 legacy-cache regression demonstrated.

Do not assume old P1-A/A.1 outputs or P1-B1 partial packages constitute reusable per-unit success. Any migration must validate actual source ownership, request provenance and coverage before reuse and preserve original artifacts.

## Proposed unit lifecycle

Distinguish blocked evidence, ready-to-request, request-started, received-but-unvalidated, validated-draft, transient failure and rejected response. These are proposed local execution concepts, not new production schemas in this milestone.

1. Confirm current plan identity, eligibility, context completeness, budget and warning decisions.
2. Atomically record request intent before contacting the existing Azure provider.
3. On response, retain a bounded sanitized response artifact with native completion outcome/usage/fingerprints; never log secrets. Source-containing response data remains protected local review material.
4. Run the existing strict schema/reference/content and clinical validation boundaries. Reject truncated, refused, filtered or malformed outcomes using the current provider guards.
5. Atomically record a validated draft only after required checks. Store ownership and context IDs separately. Never credit a context-only proposition as owned work.
6. Reconcile aggregate coverage from validated owned evidence. Unresolved clinical/uncertain document evidence continues to block complete-document approval. Human medical approval remains a separate mandatory state.

On restart, verify artifact checksums, execution fingerprint, identities and validation decisions before reusing a validated draft. Revalidate when safety rules change. Do not silently retain a prior PASS under a changed validator. Conflicting or incomplete records require review rather than optimistic completion.

## Targeted retries and concurrency

Retry only eligible source units whose execution state permits it. A dependency or source correction changes affected fingerprints; unrelated validated work may remain reusable only after identity and qualification checks. Never retry evidence blocks as transport failures, loosen validation, or fill missing figures/table relationships from model memory.

An interrupted request may have completed remotely even if no response was saved. Local request-intent records cannot guarantee Azure executed at most once. The retry policy must explicitly address this ambiguity, request costs and duplicate responses; do not claim provider idempotency that has not been verified for the installed SDK/deployment. Exact output deduplication must preserve every valid origin while avoiding duplicate clinical coverage credit.

Use atomic local writes and a narrowly scoped single-writer/lease mechanism if concurrent processes are supported. Record ownership of an in-flight unit and handle stale intents conservatively. This is a proposed capability; no locking/recovery framework or worker pool was added in P1-B1.

## Proposed acceptance tests before any live request

- Crash before request, after response, during validation and before atomic commit; restart must not report lost/unvalidated work as complete.
- One unit succeeds and a later unit fails; previously validated work remains intact and read-back verified.
- Source/configuration/prompt/provider/validator changes invalidate the correct execution records.
- Warning/dependency changes block affected retries and preserve document incompleteness.
- Corrupt, truncated, conflicting or duplicate execution records fail closed.
- Concurrent claim attempts do not both commit a completed unit.
- Context-only or qualification-incomplete outputs cannot complete concepts, sections or documents.
- Old plain/compressed artifacts remain readable and historical outputs remain unchanged.
- Performance/resource tests verify that recovery loads shared evidence once and reuses ID-based manifests.

Mock Azure for all initial recovery tests. A separately approved live Azure test should use an isolated review destination, explicit request budget and existing clinical validation. Do not write to production RAG/storage or upload medical content. Restore the original five NEJM fixtures before calling their release requirements verified.

## Preconditions for a further authorization

Review the P1-B1 zero-eligibility corpus outcome, obtain independently supported warning/dependency adjudication on representative layouts, demonstrate useful qualified drafts without relaxed verification, and agree on execution fingerprint/retry/concurrency policies. Only then authorize P1-B2 implementation. No responses, checkpoints, resume API, recovery state store or production integration are implemented by this design document.

# DataExtractor PRDs

Three engineering-ready PRDs derived from the agentic component architecture and the design-time/runtime environment split. Written 2026-09-20. Scope: full target, with the local CSV/Excel PoC written up as Phase 1 in each.

| PRD | Covers | Requirement prefix | File |
|---|---|---|---|
| 1 — Shared Contracts & Artifact Store | Workflow package format, the four artifact schemas (field schema, detection rules, skill manifest, thresholds), the three runtime output contracts, registry/versioning/promotion | `CTR-01`…`CTR-31` | `01-shared-contracts.md` |
| 2 — Design-Time Activities | Eight design agents, corpus intake and labelling, evaluation harness and metrics, review and sign-off, UAT environment and promotion triggers | `DT-01`…`DT-49` | `02-design-time.md` |
| 3 — Runtime Activities | Orchestrator and 16 pipeline agents, skills/tools boundary, working memory and stores, infrastructure controllers, flag routing and review queue, AWS production deployment | `RT-01`…`RT-67` | `03-runtime.md` |

Live editable copies also exist as Claude docs:

- PRD 1 — https://claude.ai/artifact/Su5cVknv3RHkuLg78QDNx1
- PRD 2 — https://claude.ai/artifact/RwHW4LYPyMNKnvaAq4otnr
- PRD 3 — https://claude.ai/artifact/EMC7oHNTqBpdS6gz5Mo1nV

The files here, the project docs under `prd/` in the DataExtractor project, and the live docs all hold the same content as of 2026-09-20. They are not synced to each other.

## Load-bearing decisions captured in these PRDs

- The only object crossing between design-time and runtime is an immutable, versioned **workflow package** (`client_id/workflow_id@version`). Runtime never writes to the registry; design-time never reads production data.
- Per-client behaviour lives entirely in package artifacts. Onboarding a client is an authoring run, not an engine deployment.
- Promotion requires a passing evaluation report plus recorded human sign-off. No autonomous promotion.
- Every extracted value is `{value, source, confidence}`; every run emits an audit record, including on failure.
- The lowest gate is false-accept rate (DT-27): a wrong critical field accepted without a flag is the failure that matters most.

## Phase 1 (PoC) boundary, consistent across all three

Design agents local, one client workflow, CSV and Excel attachments only. Contracts built in full; AWS deployment, promotion gate, encryption at rest, PDF/DOC/OCR, encryption agent, embedded email and concurrency deferred to Phase 2.

## Unresolved across the set

- Package granularity: one per client per use case (current assumption) vs. shared artifacts beneath the client package.
- Model version pinning: a model upgrade can change behaviour with no package change. Belongs in `engine_range` or the manifest — undecided.
- Result delivery mechanism to the client's finance system, and who staffs the review queue.
- Minimum corpus size per email type (25 is a placeholder) and who does the labelling.

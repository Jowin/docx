# DataExtractor — design-time

The design-time half of DataExtractor: eight design agents, a Postgres artifact
registry, and an HTTP API that exposes **every component on its own** so each
can be tested in isolation.

Implements **PRD 2 (Design-Time Activities)** against the contracts in
**PRD 1 (Shared Contracts & Artifact Store)**. Requirement IDs (`CTR-`, `DT-`,
`RT-`) appear in docstrings and test names, so any behaviour traces back to the
document that asked for it.

## Quick start

```bash
cp .env.example .env
docker compose up --build          # Postgres 16 + migrations + API on :8000
open http://localhost:8000/docs    # OpenAPI: every component, typed
```

Without Docker:

```bash
pip install -e ".[dev]"
export DATABASE_URL=postgresql+psycopg2://designtime:designtime@localhost:5432/designtime
alembic upgrade head
uvicorn dataextractor_designtime.main:app --app-dir src --reload
```

## Testing a component on its own

Every agent is a pure module with a typed input and output, mounted at
`POST /agents/{name}/run`. The OpenAPI schema for that route *is* the agent's
contract — there is no second definition to drift from.

```bash
python samples/generate_corpus.py /tmp/acme          # 30 invoices + 10 out-of-scope
curl -s localhost:8000/components                    # what is exposed
curl -sX POST localhost:8000/agents/corpus-profiler/run \
     -H 'content-type: application/json' \
     -d @/tmp/acme/corpus.request.json
```

| Component | Endpoint | Covers |
| --- | --- | --- |
| Corpus Profiler | `POST /agents/corpus-profiler/run` | DT-06 |
| Type Discovery | `POST /agents/type-discovery/run` | DT-07 |
| Field Schema Agent | `POST /agents/field-schema/run` | DT-08, DT-09 |
| Detection Rules Agent | `POST /agents/detection-rules/run` | DT-10, DT-11 |
| Skill Author Agent | `POST /agents/skill-author/run` | DT-12, DT-13 |
| Threshold Tuner | `POST /agents/threshold-tuner/run` | DT-14 |
| Evaluation Agent | `POST /agents/evaluation/run` | DT-23 … DT-31 |
| Packager | `POST /agents/packager/run` | DT-15, DT-16, CTR-17 |
| Registry | `/registry/...` | CTR-01 … CTR-05, CTR-17 … CTR-22 |
| Authoring run | `POST /runs` | DT-01 … DT-05 |

## The registry

Postgres, five tables: `packages`, `artifacts`, `signoffs`, `promotions`,
`activations`. What it enforces, and where each rule comes from:

- **CTR-01** a published `client_id/workflow_id@version` is immutable —
  republishing returns `409 version_exists`.
- **CTR-02** every artifact checksum is recomputed on load; a mismatch fails
  with `package_corrupt` and the package is not used.
- **CTR-03** a package outside the engine's `engine_range` is refused, not
  partially loaded.
- **CTR-04 / CTR-05** artifact paths stay inside the package, and every declared
  email type has both a schema and a thresholds entry.
- **CTR-17** the semver bump is derived from the artifact diff; one that
  understates it is refused.
- **CTR-19** promotion needs a passing evaluation report *and* a recorded human
  sign-off. Gate-failed packages (DT-34) publish to UAT but never promote.
- **CTR-20** exactly one active version per workflow, with the previous one kept
  resident so rollback is a pointer change.

## The model seam

Mechanical work lives in the agents; judgment goes through `ModelClient`
(`src/dataextractor_designtime/model/`). The shipped `StubModelClient` answers
each judgment task with a deterministic rule, so the pipeline runs offline and
DT-35 (identical inputs, identical metrics) is checkable. Point
`app.state.model_client` at a real client to swap the judgment steps without
touching anything around them.

## Layout

```
src/dataextractor_designtime/
  contracts/       PRD 1 as Pydantic models, validated on both sides of the seam
  registry/        Postgres tables, semver rules, publish / sign-off / promote
  agents/          the eight design agents, plus file-type and tabular tools
  engine/          reference runtime engine the evaluation harness replays
  model/           the ModelClient seam and its deterministic stub
  api/             one router per component
  orchestrator.py  the authoring run (DT-01)
alembic/           migrations
samples/           deterministic corpus generator
tests/             run against a live Postgres
```

## Known gaps

- **DT-29 is not yet satisfied.** The harness replays `engine/reference.py`, a
  stand-in implementing the Phase 1 runtime path (classify, body + CSV/XLSX
  extraction, merge, completeness, confidence, flag routing). The real engine is
  PRD 3's build. Today's metrics describe this engine, not production; the
  class interface is the swap point.
- **Phase 1 attachment types only:** CSV and XLSX. PDF, DOC, OCR, encrypted
  attachments and embedded email flag rather than parse.
- **Stubbed judgment.** Alias grouping, term weighting, criticality proposal,
  band choice and skill-body authoring are rules, not model calls. Defensible
  and deterministic, not clever.
- **No review surface.** Proposals come back as JSON with their evidence;
  DT-32's accept/edit/reject UI is not built.
- **No UAT / production split.** One registry, one channel; promotion sets state
  and activation rather than copying between AWS accounts.

## Tests

```bash
createdb designtime_test
TEST_DATABASE_URL=postgresql+psycopg2://designtime:designtime@localhost:5432/designtime_test \
  python -m pytest -q
```

The suite drops and recreates the schema per test, so point it at a database it
is allowed to own. `tests/test_registry.py` and `tests/test_api.py` exercise
real Postgres; the rest are pure.

# DataExtractor — design-time

The design-time half of DataExtractor: the design agents, **pattern learning**
(extract a sample in an isolated runtime and, when it fails, write a skill for
its pattern into a new config version), a Postgres artifact registry, and an
HTTP API that exposes **every component on its own** so each can be tested in
isolation.

Multi-step flows (the authoring run, the learning loop) are **LangGraph** state
graphs. Contracts, agent inputs and outputs are typed records on stdlib
dataclasses (`records.py`); **no pydantic** is imported anywhere in this code.

Implements **PRD 2 (Design-Time Activities)** against the contracts in
**PRD 1 (Shared Contracts & Artifact Store)**. Requirement IDs (`CTR-`, `DT-`,
`RT-`) appear in docstrings and test names, so any behaviour traces back to the
document that asked for it.

## Quick start

```bash
cp .env.example .env
docker compose up --build          # Postgres 16 + migrations + API on :8000, runtime on :8001
open http://localhost:8000/docs    # OpenAPI: every component, typed
```

The image is built from the repository root because it carries the runtime as
well (pattern learning runs it); compose mounts `../runtime/configs`, so learned
versions land in the repo and the runtime service serves them straight away.

Without Docker:

```bash
pip install -e ".[dev]"
pip install -r ../runtime/requirements.txt      # the isolated runtime runs with this interpreter
export DATABASE_URL=postgresql+psycopg2://designtime:designtime@localhost:5432/designtime
alembic upgrade head
uvicorn dataextractor_designtime.main:app --app-dir src --reload
```

## Testing a component on its own

Every agent is a pure module with a typed input and output, mounted at
`POST /agents/{name}/run`. The route reads the raw JSON body into the agent's
input record (a bad payload is a 422 listing every problem with its location),
and the OpenAPI schema for the route is generated from the same records, so it
*is* the agent's contract — there is no second definition to drift from.

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
| Extraction Judge | `POST /agents/extraction-judge/run` | pattern learning |
| Pattern Skill Writer | `POST /agents/pattern-skill-writer/run` | pattern learning |
| Registry | `/registry/...` | CTR-01 … CTR-05, CTR-17 … CTR-22 |
| Authoring run | `POST /runs` | DT-01 … DT-05 |
| Pattern learning | `POST /learning/runs`, `GET /learning/runs[/{id}]` | see below |

## Pattern learning

Each call takes one sample of a document pattern and tries to extract it with
the runtime. When the extraction fails, design-time writes a skill for the
pattern, tests it, and saves it as a new config version.

```bash
curl -sX POST localhost:8000/learning/runs -H 'content-type: application/json' -d '{
  "client": "default", "usecase": "invoice", "object": "invoice",
  "source": "hooli-remit.csv",
  "pattern_name": "hooli-remittance",
  "ground_truth": {"invoice_number": "RA-501", "invoice_date": "2026-08-03",
                   "due_date": "2026-09-02", "vendor": "Hooli Inc", "total_amount": 2000},
  "reference_text": "Hooli remittance advice. The vendor is the \"Biller\" column."
}'
```

| Input | |
| --- | --- |
| `source` | the sample file, inside `LEARNING_INPUT_ROOT` (absolute or relative) |
| `pattern_name` | names the pattern; its skill is `skills/<pattern_name>.md` |
| `client`, `usecase`, `version` | the config to learn from; `version` defaults to the **latest** |
| `object` | the data object the pattern produces (the dictionary's `name`); checked |
| `ground_truth` | optional: the expected record(s) for this source |
| `reference_text` | optional: notes on the pattern, e.g. `invoice_number: "Our Ref"` |
| `max_iterations`, `publish`, `strict` | attempts (default 3), dry run, review-status strictness |

The loop is a LangGraph graph (`learning/graph.py`):

```
extract_base -> judge_base --passed--> finish
                           \-failed-> load_regressions -> write_skill -> build_candidate -> test_candidate
test_candidate --passes, no regressions--> publish -> finish
               \-attempts left---------> write_skill
               \-out of attempts-------> publish (best accepted candidate) | finish
```

- **Isolated runtime.** Every extraction runs the real runtime
  (`python -m extractor_service.cli`) in a child process against a scratch copy
  of the configs, with a minimal environment and no audit output. The base and
  each candidate are tested by the engine that will serve them, and nothing
  touches the live configs until `publish`.
- **Did it fail?** With ground truth, the Extraction Judge compares every field
  it names, record by record (paired on the dictionary's `record_key`), plus any
  review status when `strict` (the default). Without ground truth, *any* review
  reason fails: a missing required field, an unverified value, low confidence,
  unplaced content.
- **The skill.** The Pattern Skill Writer finds each failing field's expected
  value in the runtime's evidence and reads where it sits: the text before it in
  a cell or line is a **label** (`Our Ref: RA-501`), a phrase before it
  mid-sentence or inside a table body is an **anchor**
  (`... kindly remit $310.50`), and a value alone in a cell takes its column
  header or the label to its left. Reference text adds labels
  (`field: "Label"`, or a quoted label in a sentence naming one field). With no
  ground truth, unclaimed `Label: value` pairs whose words fit a failing field
  are proposed. A label another field already uses is never taken. The hints go
  in the skill's YAML front matter, which the runtime folds into its data
  dictionary; the markdown body is written by the judgment step: the stub's
  template offline, the **model gateway** when `MODEL_GATEWAY_URL` is set (it may
  propose hints too, which are validated the same way).
- **Versions.** A candidate is the base version copied to the next free patch
  (1.0.0 -> 1.0.1), with the skill added to `manifest.json`'s `skills` and
  `learned_patterns`. Published versions are never edited; learning on top of a
  learned version carries its hints forward (1.0.1 -> 1.0.2).
- **Regressions.** Earlier samples of the same client and use case whose last run
  passed are re-run against every candidate. A candidate that breaks one is
  rejected, however much it helps the new sample.
- **Outcomes.** `passed` (nothing to learn), `learned` (the new version passes),
  `improved` (better, still failing; published so a later sample can build on
  it), `failed` (no candidate beat the base; nothing written). Every call is a
  row in `learning_runs`, with both verdicts, every attempt and the skill.

`defaults.json` in the runtime configs still pins the version the *default*
config serves; move it when a learned default version should go live. Client
configs (`acme/...`) serve their latest version, so they pick learned versions
up at once.

## The registry

Postgres, six tables: `packages`, `artifacts`, `signoffs`, `promotions`,
`activations`, and `learning_runs` (pattern learning's record and regression set). What it enforces, and where each rule comes from:

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

With `MODEL_GATEWAY_URL` set, `GatewayModelClient` answers the tasks it has a
prompt for (today: `pattern_skill.write`) through the runtime's gateway function,
`extractor_service.gateway.call_model`, the one function every model call in the
system goes through, and leaves every other task to the stub. Design-time asks
for a model *alias* only; which vendor and model ids sit behind it is known to
the gateway alone.

## Layout

```
src/dataextractor_designtime/
  records.py       typed records on dataclasses: validation, JSON, JSON Schema
  contracts/       PRD 1 as records, validated on both sides of the seam
  registry/        Postgres tables, semver rules, publish / sign-off / promote
  agents/          the design agents, the judge and pattern skill writer, file-type and tabular tools
  learning/        the pattern-learning graph, isolated runtime, config versions, run store
  engine/          reference runtime engine the evaluation harness replays
  model/           the ModelClient seam, its deterministic stub, and the gateway client
  api/             one router per component; typed.py reads bodies and publishes schemas
  orchestrator.py  the authoring run (DT-01) as a LangGraph graph
alembic/           migrations
samples/           deterministic corpus generator
tests/             run against a live Postgres (learning tests also run the real runtime)
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
- **Learned hints apply to the whole config version.** A pattern's labels and
  anchors are not scoped to documents of that pattern; the regression set is
  what guards other patterns. Pattern detection would scope them.
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

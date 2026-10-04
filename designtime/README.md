# DataExtractor — design-time

The design-time half of DataExtractor: the design agents, the **authoring run**
(a labelled corpus becomes a config version), **pattern learning** (one sample
refines a version), the registry that records versions, sign-offs and
releases, and an HTTP API that exposes **every component on its own** so each
can be tested in isolation.

**The runtime's config folders are the source of truth.** Both flows build a
config version folder in the runtime's own layout, score it with the **real
runtime** (in an isolated process), and publish it into the config root as a
**candidate**. Nothing is served until a person signs it off and releases it,
which rewrites the use case's `releases.json`. The Postgres registry is the
record *about* the folders, never a second copy of them.

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
docker compose up --build          # Postgres 16 + migrations + API on :8000, runtime on :8001 (+ a worker)
open http://localhost:8000/docs    # OpenAPI: every component, typed
open http://localhost:4173         # the console (../ui): both flows, releases, review
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
| Packager | `POST /agents/packager/run` | DT-15, DT-16, CTR-17 (the CTR-02 manifest view; runs publish config folders) |
| Extraction Judge | `POST /agents/extraction-judge/run` | pattern learning |
| Pattern Skill Writer | `POST /agents/pattern-skill-writer/run` | pattern learning |
| Config versions | `/configs/...`, `/lookups` | CTR-11, CTR-19, CTR-20, DT-34 |
| Authoring run | `POST /runs` | DT-01 … DT-05 |
| Pattern learning | `POST /learning/runs`, `POST /learning/runs/{id}/resume`, `GET /learning/runs[/{id}]` | see below |
| Learning from corrections | `POST /learning/corrections` | runtime RT-46, DT-39 |
| Learning memory | `GET /learning/memory`, `DELETE /learning/memory/rejected-hints` | see below |

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
| `max_iterations`, `publish`, `strict` | attempts (default 3), dry run, whether flags fail a ground-truth run |
| `scope` | `pattern` (default): the skill applies only to documents matching the sample's fingerprint; `global`: to every document |

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
  runtime flag when `strict` (the default). Without ground truth, *any* flag
  fails: a missing required field, an unverified value, low confidence,
  unplaced content, an attachment that could not be read.
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
- **Fingerprints.** A pattern's skill carries `applies_to`: the sender's domain,
  the sample's table headers, or failing both a short title line. The runtime
  then uses its hints and body only on documents that look like that pattern, so
  one layout's labels and anchors cannot misread another's. An existing
  fingerprint is kept and extended when the pattern is learned again.
- **Regressions.** Earlier samples of the same client and use case whose last run
  passed are re-run against every candidate. A candidate that breaks one is
  rejected, however much it helps the new sample.
- **Outcomes.** `passed` (nothing to learn), `learned` (the new version passes),
  `improved` (better, still failing; published so a later sample can build on
  it), `failed` (no candidate beat the base; nothing written). Every call is a
  row in `learning_runs`, with both verdicts, every attempt and the skill.

### Memory

Learning keeps what it has learned between calls in LangGraph's long-term store
(`PostgresStore`, on the registry database; `learning/memory.py`), per client and
use case:

| Namespace | Holds | Used for |
| --- | --- | --- |
| `rejected_hints` | hints whose candidate broke an earlier sample, with why | the writer never proposes them again (`avoided:` notes) |
| `patterns` | each pattern's fingerprint, samples, outcomes and versions | keeping a pattern's fingerprint across samples |
| `corrections` | runtime corrections already learned from | importing corrections idempotently |

`GET /learning/memory?client=&usecase=` shows it all.
`DELETE /learning/memory/rejected-hints?...&key=` forgets one rejected hint.

**Resumable calls.** Every node of the learning graph is checkpointed
(`PostgresSaver`). The call is recorded as `running` before the graph starts. If
the process dies, the record stays `running`, or becomes `interrupted` on an
engine error. `POST /learning/runs/{id}/resume` carries on from the last node,
with nothing before it re-run. The scratch configs live under
`LEARNING_STATE_DIR` until a call finishes.

**Corrections as ground truth.** `POST /learning/corrections` takes the runtime's
`GET /review/corrections/export` items, or fetches them from `runtime_url` /
`RUNTIME_URL`. Each corrected result is learned like any sample with ground
truth: the pattern is named after the sender's domain unless one is given, and
the call is recorded as requested by `review:<reviewer>`. Re-importing the same
corrections is a no-op. The source files must be readable at the same
`file_location` under `LEARNING_INPUT_ROOT` (compose mounts the same `/data`).

LangGraph's store and checkpointer manage their own tables (`store`,
`checkpoints`, ...) with their own migrations, run on first use. Alembic manages
the registry tables.

A learned version is a candidate: the runtime serves it only when a request
names it (or asks for `latest`) until someone signs it off and releases it
(`/configs/...`, or the console).

## One path for authoring and learning

```
corpus ──► authoring run ──(next minor)──┐
                                         ├─► version folder (candidate) ─► scored by the runtime
sample ──► pattern learning ─(next patch)┘          │
corrections ─┘                                      ▼
                         sign-off (not by its creator) ─► release (releases.json) ─► served
                                                    rollback / reject ◄─┘
```

| | Authoring run (`POST /runs`) | Pattern learning (`POST /learning/runs`) |
| --- | --- | --- |
| Input | a labelled corpus, confirmed types and critical fields | one sample, optional ground truth and reference text |
| Builds | schemas per type, detection rules, skills, tuned thresholds (`configwriter.py`) | a skill with hints and a fingerprint, added to the base |
| Builds on | the use case's latest version (settings, prompt, every learned skill), else the template config | the latest version that is not rejected |
| Scored by | the runtime over the held-out corpus, twice (bootstrap, then tuned); out-of-scope samples must be turned away | the runtime on the sample and every earlier passing sample |
| Publishes | the next minor (1.1.0 → 1.2.0), or 1.0.0 | the next patch (1.0.0 → 1.0.1) |

Both publish through `configroot.publish_version`. The first publish into a use
case without `releases.json` writes one that pins what is served at that moment,
so publishing never changes serving. Both are logged in `learning_runs`
(`kind` = `authoring` or `pattern`; `GET /learning/runs?kind=`).

The evaluation agent (`/agents/evaluation/run`) scores with the runtime by
default (`engine: "runtime"`, DT-29); `engine: "reference"` keeps the old
in-process stand-in for quick component tests.

## Config versions and releases

| Route | Does |
| --- | --- |
| `GET /configs` | every use case: versions, status, what is served |
| `GET /configs/{c}/{u}` | versions with their record, `releases.json`, the release log |
| `GET /configs/{c}/{u}/{v}` | manifest, files, provenance, evaluation, sign-offs, whether the folder is intact |
| `GET /configs/{c}/{u}/{v}/files/{path}` · `/diff?against=` | one file; a diff against the served version |
| `POST /configs/{c}/{u}/{v}/signoff` | `{identity, note}` |
| `POST /configs/{c}/{u}/{v}/release` | `{by, note, accept_gate_failure}` |
| `POST /configs/{c}/{u}/{v}/reject` | `{by, note}`: never "latest", never built on |
| `POST /configs/{c}/{u}/rollback` | `{by, note, to?}`: re-release the previous version |
| `GET /lookups` · `PUT /lookups` | ingestion lookups at the use case, client or global level |
| `GET /graphs/authoring` · `/graphs/learning` | the graphs as Mermaid |

What a release enforces:

- **CTR-19** at least one sign-off, and not from the version's creator (the run's `requested_by`).
- **Integrity** the folder must still hash to what was recorded at publish; an edited version is refused (`config_tampered`).
- **DT-34** a version whose evaluation gates failed is released only with `accept_gate_failure` and a note; the release log marks the override.
- A hand-written folder is recorded (origin `manual`) the first time it is signed off or released.

The registry tables are `config_versions`, `config_signoffs`, `config_releases`
and `learning_runs`. The old package tables (`packages`, `artifacts`,
`signoffs`, `promotions`, `activations`) are dropped by migration `7b1d0c2f9a41`.

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
  configroot.py    the config folders: versions, releases.json, publish, release, rollback, reject
  configwriter.py  authoring artifacts -> a runtime config version folder
  registry/        Postgres record of config versions, sign-offs, releases, design runs
  agents/          the design agents, the judge and pattern skill writer, file-type and tabular tools
  learning/        the pattern-learning graph, isolated runtime, config versions, run store, memory
  engine/          runtime.py (the real runtime as evaluator) and reference.py (in-process stand-in)
  model/           the ModelClient seam, its deterministic stub, and the gateway client
  api/             one router per component; typed.py reads bodies and publishes schemas
  orchestrator.py  the authoring run (DT-01) as a LangGraph graph, publishing a config version
alembic/           migrations
samples/           deterministic corpus generator
tests/             run against a live Postgres (learning tests also run the real runtime)
```

## Known gaps

- **Evaluation needs the runtime's interpreter.** The authoring run and the
  evaluation agent spawn `python -m extractor_service.cli` (`RUNTIME_DIR`,
  `RUNTIME_PYTHON`); the image carries both.
- **Stubbed judgment.** Alias grouping, term weighting, criticality proposal,
  band choice and skill-body authoring are rules, not model calls. Defensible
  and deterministic, not clever.
- **The console reviews versions, not proposals.** `../ui` signs off, releases,
  rolls back and diffs config versions; DT-32's step-by-step accept/edit of each
  proposed artifact inside a run is not built (types are confirmed in the request).
- **Fingerprints are heuristic.** A pattern is recognised by its sender domain,
  table headers or a title line. Two layouts with the same headers from the same
  sender share a fingerprint; `scope: global` skills apply everywhere, and the
  regression set is what guards them.
- **One config root per environment.** Candidates and releases live in one
  root; moving a released version between AWS accounts is copying its folder.
- **No authentication.** `by` and `identity` are whatever the caller sends.

## Tests

```bash
createdb designtime_test
TEST_DATABASE_URL=postgresql+psycopg2://designtime:designtime@localhost:5432/designtime_test \
  python -m pytest -q
```

The suite drops and recreates the schema (and LangGraph's store and checkpoint
tables) per test, so point it at a database it is allowed to own.
Every test gets its own copy of `../runtime/configs` as `RUNTIME_CONFIG_ROOT`.
`tests/test_api.py`, `tests/test_determinism.py` and `tests/test_learning.py`
exercise real Postgres; the authoring and learning tests also run the real runtime from
`../runtime` in a child process (its requirements must be installed).

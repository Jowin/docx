# DataExtractor runtime

Extracts the fields a data dictionary defines from an email (`.eml` or Outlook
`.msg`, attached emails included), a zip, or a single CSV, Excel (`.xlsx`,
`.xlsm`, `.xls`), PDF (scanned pages by OCR) or DOCX file. One FastAPI service
and one self-contained Docker image, with its state in Postgres. It answers
synchronously, by webhook, or by polling. The pipeline is a LangGraph state graph, every model call goes
through one gateway function, and no pydantic is imported by this code.

```
POST /extract                          run an extraction: now, by webhook, or queued for polling
GET  /extractions/{job_id}             poll: inprogress, or extracted with the result
GET  /extractions                      recent jobs
POST /extractions/{job_id}/redeliver   send the result to the webhook again
GET  /review, /review/{job_id}         flagged results waiting for a person
POST /review/{job_id}/resolve          correct or reject; corrections are delivered as human_corrected
GET  /review/corrections/export        corrected results as ground truth for design-time learning
GET  /metrics, /metrics/prometheus     throughput, flag rate, latency, cost per client and use case
GET  /audit/verify                     check the audit hash chain
GET  /configs, /graph, /health, /docs
```

## Run it

**Docker**

```
docker build -t dataextractor-runtime .
docker run --rm -p 8000:8000 -v D:\data:/data ^
  -e DATABASE_URL=postgresql://user:pass@dbhost:5432/extractor dataextractor-runtime
```

Input files go in the mounted `/data` folder; `file_location` is relative to it.
Jobs, results, deliveries, the review queue, the audit chain, spooled
attachment bytes and LangGraph checkpoints all live in Postgres, in the schema
`DB_SCHEMA` (default `extractor`, created on start). Containers keep no state,
so API and worker containers scale out freely. To use your own configs, add
`-v D:\configs:/app/configs`. For configs that use a
model, add `-e MODEL_GATEWAY_URL=... -e MODEL_GATEWAY_TOKEN=...`. More workers:
`docker run ... dataextractor-runtime python -m extractor_service.worker`.

**Without Docker**

```
pip install -r requirements.txt         (tesseract on PATH for OCR, optional)
set INPUT_ROOT=D:\data                  (export INPUT_ROOT=... on macOS/Linux)
set DATABASE_URL=postgresql://user:pass@localhost:5432/extractor
uvicorn extractor_service.api:app --port 8000
```

**Sample files and tests**

```
pip install -r requirements-dev.txt
python -m tests.samples D:\data     # writes eight sample inputs
set TEST_DATABASE_URL=postgresql://user:pass@localhost:5432/extractor_test
pytest                              # 202 tests; each test gets (and drops) its own schema
```

## Request

`POST /extract` takes a JSON object:

| Field | Required | Meaning |
| --- | --- | --- |
| `file_location` | yes | Path to the input, inside `INPUT_ROOT` (absolute, or relative to it) |
| `client`, `usecase`, `version` | no | The config; defaults from `configs/defaults.json`; `version` may be `latest` |
| `extended` | no | `true`: always return the extended result |
| `callback_url` | no | Webhook: answer `202` now and POST the result there when ready |
| `async` | no | `true`: answer `202` now; poll `GET /extractions/{job_id}` |
| `idempotency_key` | no | The same key within the dedupe window is the same job and result |

Unknown fields are rejected, so a typo like `extnded` fails loudly. A bad body is a
422 `invalid_request` whose `detail.errors` lists each problem, e.g.
`{"loc": "extended", "msg": "must be a boolean"}`. That is the only time an
`/extract` call does not end in a result.

## Status and result

`X-Extraction-Status` is **`inprogress`** (queued or running) or **`extracted`**
(finished). Nothing else. Every finished extraction has a result:

- **Clean** (no flags): the plain data, an **array of records** shaped by the data dictionary.
- **Flagged**: the **extended** form, the same data plus `flagged: true`, `flags`, confidence, per-field sources and metadata. This is exactly what `"extended": true` returns.
- **Failed outright** (no such file, unknown config, unsupported input, a model outage): a flagged result with `data: []` and `flags: ["error:<code>"]`, plus an `error` object.

`X-Extraction-Flagged` says which form came back, and `X-Job-Id` and
`X-Audit-Id` identify the run.

Plain, for `invoices.csv` (a statement of three invoices):

```json
[
  {"invoice_number": "INV-1001", "invoice_date": "2026-08-01", "due_date": "2026-08-31",
   "vendor": "Acme Corp", "currency": "USD", "total_amount": 1250, "tax_amount": null,
   "po_number": null, "line_items": []},
  {"invoice_number": "INV-1002", ...},
  {"invoice_number": "INV-1003", ...}
]
```

Extended (flagged, or asked for):

```json
{
  "data": [ { ...record 1... } ],
  "status": "extracted",
  "flagged": true,
  "confidence": 0.65,
  "flags": ["missing_field:invoice_number", "low_confidence"],
  "records": [
    {"data": { ...record 1... }, "flagged": true, "confidence": 0.65,
     "flags": ["missing_field:invoice_number", "low_confidence"],
     "fields": {"total_amount": {"value": 10, "confidence": 0.85, "grounding": "verified",
                                 "source": "file:partial.csv#B2"}, ...}}
  ],
  "metadata": {
    "audit_id": "run_...", "record_count": 1, "record_key": "invoice_number",
    "config": {"client": "default", "usecase": "invoice", "version": "1.0.0", "sha256": "...", ...},
    "model": {"provider": "stub", "name": "deterministic-stub"},
    "input": {"file_location": "...", "kind": "csv", "sha256": "...", "subject": null, "sender": null},
    "documents": [...], "skipped": [...], "skills_applied": [...], "keys_used": {...},
    "cost": {...}, "graph": {"path": [...]}, "timings_ms": {...}
  }
}
```

**Flags**: `missing_field:<name>`, `unverified_value:<name>`,
`schema_validation_failed:<name>`, `critical_field_conflict:<name>`,
`low_confidence`, `unplaced_content`, `no_readable_content`,
`attachment_parse_failed:<file>`, `encrypted_no_key`, `unsupported_attachment:<kind>`,
`archive_limit_exceeded:<zip>`, `embedded_depth_exceeded`, `agent_timeout:parse:<file>`,
`agent_timeout:run`, `cost_ceiling_exceeded`, `ingestion_rule_error`,
`input_ignored:<rule>`, `error:<code>`, `dead_lettered`.

## Delivery: now, webhook, or polling

| Request | Response | Then |
| --- | --- | --- |
| `{"file_location": ...}` | `200`, the result | nothing |
| `+ "callback_url": "https://erp/hook"` | `202 {"job_id", "status": "inprogress"}` | the result is POSTed to the URL |
| `+ "async": true` | `202 {"job_id", "status": "inprogress", "poll": "/extractions/<id>"}` | `GET` it until `extracted` |

Every extraction is a job in a durable Postgres queue (RT-49). Workers in the API
process (`WORKERS`, default 2), plus any number of `python -m extractor_service.worker`
processes on any host pointed at the same database, claim jobs with
`FOR UPDATE SKIP LOCKED` and a lease. A job's completion (its result, review
entry, audit-chain entry and webhook delivery) commits in one transaction.

**The webhook** receives:

```json
{"event": "extraction.completed", "job_id": "job_...", "status": "extracted", "flagged": false,
 "human_corrected": false, "audit_id": "run_...",
 "request": {"file_location": "...", "idempotency_key": "..."},
 "result": [ ...plain data, or the extended object when flagged... ]}
```

Headers: `X-Extraction-Status: extracted`, `X-Job-Id`, `X-Event`, `X-Timestamp`
and, with `WEBHOOK_SECRET` set, `X-Signature: sha256=<HMAC-SHA256 of "<X-Timestamp>.<body>">`.

- **Retries:** a non-2xx answer or a network error is retried with exponential backoff, up to `WEBHOOK_MAX_ATTEMPTS`. After that the delivery is marked failed and `POST /extractions/{id}/redeliver` sends it again.
- **Events:** a correction is delivered as `extraction.corrected`, with the same `audit_id` and `human_corrected: true`. A rejection is delivered as `extraction.rejected`.
- **Callback safety:** callback URLs must be http(s). Link-local and cloud-metadata addresses are refused, and `WEBHOOK_ALLOWED_HOSTS` restricts hosts to an allowlist.

**Idempotency (RT-04).**
- A job is keyed by `idempotency_key`, or by the input's SHA-256 together with the resolved config.
- Within `DEDUPE_WINDOW_DAYS` the same key returns the same job and `audit_id`, not a new extraction.

**Faults.**
- An engine fault is retried with backoff.
- After `JOB_MAX_ATTEMPTS` the job is dead-lettered (RT-56). It still ends in a result flagged `error:internal_error` and `dead_lettered`, and that result is delivered.

**Resume (RT-40).**
- Every graph node is a LangGraph checkpoint (`PostgresSaver`, in the runtime's schema), and a job's thread is its job id.
- When a worker dies mid-run, the lease expires, another worker claims the job, and the run continues from its last checkpoint. Nothing already parsed is parsed again.
- Checkpoint deserialization accepts only the run state's own types.

## Ingestion filter

Attachments, zip members, attached emails, and the input file itself are
checked against lookup data **before anything is parsed**. An ignored item is
never read and never reaches the model. It is listed in `metadata.skipped` with
the rule that matched, and it raises no flag. An ignored input file gives an
empty result flagged `input_ignored:<rule>`.

Lookup data lives beside the version folders, at three levels, so a change
applies at once without cutting a config version:

| Level | Folder | Applies to |
| --- | --- | --- |
| use case | `configs/<client>/<usecase>/lookups/` | one client's use case |
| client | `configs/<client>/lookups/` | every use case of the client |
| global | `configs/lookups/` | everything |

The most specific level is checked first; within a level a *keep* match beats
an *ignore* match, and the first level with a verdict decides. So a use case
can keep a document type the global list ignores:

```json
// ingestion.json
{
  "ignore_names":  ["docusign", "*.ics", "re:^certificate of completion"],
  "ignore_hashes": ["<sha256 hex>", "md5:<md5 hex>"],
  "ignore_kinds":  ["image"],
  "keep_names":    ["docusign invoice"],
  "keep_hashes":   []
}
```

`GET /lookups?client=&usecase=` shows the levels that apply and what each holds.
`lookups` is a reserved name: never a client or a use case.

A name entry matches the file name case-insensitively:
- plain text matches anywhere in the name, so `docusign` ignores `Summary_DocuSign.pdf`;
- an entry with `*`, `?` or `[` is a glob over the whole name;
- `re:` starts a regular expression.

Anything richer goes in `ingestion.decision.json`, a ZEN engine decision (JDM, as
the ZEN editor exports it). It is evaluated for each item with `name`, `path`,
`extension`, `kind`, `size`, `sha256`, `md5`, `container` (file, email, zip or
embedded), `depth`, `sender`, `sender_domain`, `subject`, `client` and `usecase`,
and returns `{"action": "ignore" | "keep", "rule": "..."}`; "keep" ends the
search like a keep list does.

A decision that errors keeps the item
and flags `ingestion_rule_error`, so a broken rule never silently drops a
document. Malformed lookup data is a config problem reported by `/health`.

## What gets read

- **Email threads.** The body is split into messages, newest first (RT-12): reply and forward markers, Outlook header blocks, `On … wrote:` lines and quoted runs. Each body line records its `segment`, and older messages count for less, so a corrected figure in the latest reply beats the quoted original.
- **Attached emails** (`.eml`, `.msg`, Outlook items, and message parts) are read recursively as `embedded:<n>:<name>` sources, with their bodies and attachments. The depth is checked before recursing (`intake.max_email_depth`, default 3; RT-18, RT-36).
- **Encrypted attachments.** PDFs, Office files and ZipCrypto zip members are opened with keys found in the email they came with: "password: …", "the pwd is …", newest message first (RT-15). Nothing is brute-forced. `metadata.keys_used` records where each working key was found (e.g. `body#L2`), never the key. With no working key the item is flagged `encrypted_no_key` and the run carries on.
- **DOCX** paragraphs (`P12`) and tables (`T2:R3C4`). Legacy `.doc` is flagged as unsupported.
- **OCR.** PDF pages without a text layer are rendered and read by tesseract (`p2:O3` locators). Only those pages are OCR'd, and each page notes `page_source:pN=pdf_text|ocr` (RT-16). OCR'd values are weighted by the engine's confidence. Images are OCR'd only when a config sets `intake.ocr_images: true`. Without tesseract, such pages stay flagged.
- **Zips** are unpacked within limits (members, size, compression ratio, depth).

Each item is parsed in its own LangGraph branch (`Send`), so items run in parallel,
bounded by `concurrency.max_parallel` (RT-41). Each runs in a **sandboxed child
process** with a time budget (`evidence.parse_timeout_s`) and an address-space
cap (`SANDBOX_MEMORY_MB`) (RT-39, RT-65). A crash or overrun is a flagged,
failed document (`agent_timeout:parse:<name>`), never a hung run. Network
isolation of the parser is left to the deployment (a container without
egress).

Item bytes are spooled once per run into Postgres (`spool`, bytea) and
referenced from the run state, so checkpoints stay small and any worker can
resume the run (RT-34). The parent reads the bytes before the sandboxed child
starts, so no database connection crosses into it. With `STATE_KEY` (a Fernet
key) the spool is encrypted at rest, and the retention sweep deletes it after
the run (RT-35, RT-51).

## Pipeline

```
START -> resolve_config -> ingest --(one Send per item)--> parse_item (parallel, sandboxed)
                                 \--(no items)----------------\
      -> assemble -> classify -> (readable and in scope?) extract -> expand -> verify -> route -> END
```

| Node | Does |
| --- | --- |
| `resolve_config` | Picks the config folder; decides the model provider and name |
| `ingest` | Checks the location, applies the ingestion filter, unpacks emails, zips and encrypted files, spools bytes |
| `parse_item` | One branch per item: CSV, Excel, PDF (+OCR), DOCX, email body (+threads), in the sandbox |
| `assemble` | The single join (RT-09): numbers documents |
| `classify` | Runs the config's detection rules (`rules/detection.json`) to pick the email type; narrows the config to that type's dictionary, skills and threshold; picks the skills whose fingerprint matches. Out of scope skips extraction |
| `extract` | The stub or the model through the gateway; run and cost ceilings; a model failure becomes a flag |
| `expand` | Row-per-record dictionaries: reads every table row the extractor did not answer for, using the columns its answer cited |
| `verify` | Grounds every value in the evidence, validates types, scores confidence; flags `content_truncated` |
| `route` | Flags per record and for the run, in rule order, all of them (RT-43) |

`metadata.graph` and `metadata.timings_ms` hold the path and each node's time
(one entry per parsed item). `GET /graph` draws it as Mermaid text.

## Default use case: settlement instructions

A request that names no client and use case is extracted with
`default/settlements/1.0.0`. Each record is one settlement instruction:

| Field | Type | Required | Read from labels such as |
| --- | --- | --- | --- |
| `settlement_date` | date | yes | Settlement Date, Settle Date, Value Date |
| `currency` | ISO code | yes | CCY, Currency; or a symbol / code next to the amount |
| `amount` | decimal, critical | yes | Net Amount, Settlement Amount, Amount; brackets and "DR" are negative |
| `trade_date` | date | | Trade Date, Deal Date, TD |
| `comments` | text | | Comments, Remarks, Notes, Narrative |
| `portfolio` | text | yes | Portfolio, Fund, Account (code) |
| `cash_purpose_code` | text | | Cash Purpose Code, Purpose Code, Reason Code |
| `transaction_type` | text | | Transaction Type, Trade Type, Txn Type |
| `security_id` | text | | Security ID, ISIN, CUSIP, SEDOL; empty for cash movements |

**Whole files, not slices.** The extractor (model or stub) only ever sees a
slice of each table: the first `evidence.rows` (200) and last `evidence.tail_rows`
(10). For a row-per-record dictionary the `expand` step then reads the rest:
the column each field was cited from in the extractor's answer is that field's
column (a header equal to one of the field's labels if nothing was cited), and
code streams every remaining row of the CSV or sheet through the same reader
tools, building and validating one record per row. One model call covers a
blotter of any length; a 5,000-row CSV takes under 2 s. `metadata.expanded`
shows the mapping, the constants and the row counts. `limits.max_records`
(default 25,000) caps the records per run (`records_truncated:<n>` beyond it).
In any use case, content that no step read in full (rows, pages or sheets past
the evidence limits) is flagged `content_truncated:<file>`, so a cut is never
silent.

`"record_key": "@row"` makes every row of a blotter its own record (no single
field identifies an instruction); a value labelled once elsewhere in the email
("Portfolio: GLB-EQ-01") fills rows that lack it. An email with labelled values
and no table is one instruction. Its detection rules flag mail that doesn't
look like settlements (`out_of_scope`) but still extract it. The invoice config
(`default/invoice`) and `acme/ap-invoices` remain for invoice mail; name them
in the request.

## Configs

The config folders are the **source of truth**: what is served, and every
version that ever was. Design-time writes new versions here (never edits one)
and records releases in `releases.json`; the runtime only reads.

```
configs/
  defaults.json                        {"client": "default", "usecase": "settlements", "version": "1.0.0"}
  lookups/                             global ingestion lookups
  <client>/lookups/                    the client's
  <client>/<usecase>/
    lookups/                           the use case's
    releases.json                      which version is served, and the release history
    <version>/
      manifest.json                    model, skills, types, thresholds, evidence, intake, limits, output
      schema.json                      the data dictionary (the default type's)
      schemas/<type>.json              one more dictionary per extra email type
      rules/detection.json             email-type detection rules (CTR-08)
      prompts/system.md                system prompt
      skills/<name>.md                 one per skill
      provenance.json                  written by design-time: origin, base version, run, author
```

Each part the request leaves out is filled in like this:

| Missing | Taken from |
| --- | --- |
| `client` | `defaults.json` |
| `usecase` | `defaults.json` when the client is the default client; otherwise the client's only use case |
| `version` | `releases.json`'s `active` when the use case has one (none active: `config_not_released`); otherwise `defaults.json` when client and use case are the defaults; otherwise the highest version |

`"version": "latest"` asks for the highest version that is not rejected, and a
request may name any version: that is how a **candidate** (a version newer than
the active one) is tried before it is released. `metadata.config.release_status`
says which kind served a result: `active`, `candidate`, `retired`, `rejected`
or `unmanaged` (no `releases.json` yet).

```json
// releases.json (written by design-time when a person releases, rolls back or rejects)
{"active": "1.2.0", "rejected": ["1.1.1"],
 "history": [{"action": "release", "version": "1.2.0", "previous": "1.1.0", "by": "joe@acme.example",
              "at": "2026-10-04T09:12:00+00:00", "note": "UAT ok"}]}
```

Treat a version folder as read-only: change it by adding a new version.

### Email types and detection rules

A version may declare several email types, each with its own dictionary,
skills and accept threshold; top-level `skills` are shared by every type:

```json
"default_type": "invoice",
"types": {
  "invoice":    {"schema": "schema.json", "skills": [], "thresholds": {"accept_at": 0.85}},
  "remittance": {"schema": "schemas/remittance.json", "skills": ["remit-fields"]}
}
```

`rules/detection.json` is what design-time's Detection Rules agent writes:
per type, keywords and patterns with weights, entity weights (MONEY and DATE
are recognised), a `classification_threshold`, and `negative_signals` that
each take 0.2 off. The `classify` node scores every type on the subject and
document text, and records `metadata.classification` (`type`, `score`,
per-type `scores`, `status`):

| Status | What happens |
| --- | --- |
| `matched` | extract as that type |
| `ambiguous` | two types within `classification.ambiguity_margin` (0.1): extract as the higher, flag `classification_ambiguous:<a>\|<b>` |
| `out_of_scope` | nothing matched, or a type this config does not extract: flag `out_of_scope`, no records (or extract anyway with `"classification": {"out_of_scope": "extract"}`) |
| `unclassified` | the version has no detection rules |

`manifest.json` sections beyond the model and skills:

| Section | Keys (defaults) |
| --- | --- |
| `thresholds` | `accept_at` (0.8); per type under `types.<t>.thresholds` |
| `classification` | `out_of_scope` (`skip` or `extract`), `ambiguity_margin` (0.1) |
| `evidence` | `rows` (200), `tail_rows` (10), `pages` (10), `tail_pages` (1), `max_sheets` (5), `max_chars_per_document` (60000), `ocr` (true), `ocr_timeout_s` (60), `parse_timeout_s` (120), `max_attachment_mb` (25: the readers' per-attachment guard) |
| `intake` | `max_zip_members` (200), `max_zip_uncompressed_mb` (200), `max_compression_ratio` (100), `max_zip_depth` (1), `max_email_depth` (3), `ocr_images` (false) |
| `limits` | `run_ceiling_s` (300, RT-60), `max_cost_usd` (none, RT-62), `max_records` (25,000) |
| `concurrency` | `max_parallel` (4, RT-41) |
| `output` | `decimal_format`: `number` or `string`; `formats`: result files to write (default from `OUTPUT_FORMATS`) |
| `locale` | `date_order`: `DMY`, `MDY` or `YMD` |

### Skills, hints and fingerprints

A skill is markdown for the model. It may open with YAML front matter whose
`hints` are machine-readable. They are folded into the data dictionary, so every
extractor uses them, the stub included; the model reads only the body.
Design-time writes these when it learns a pattern (`kind: learned-pattern`).

```markdown
---
name: hooli-remittance
kind: learned-pattern
applies_to:                                       # this pattern's fingerprint
  headers: [Our Ref, Bill Date, Biller]
  min_header_share: 0.6
hints:
  fields:
    invoice_number: {labels: ["Our Ref"]}         # labels become aliases
    total_amount: {anchors: ["kindly remit"]}     # value follows this phrase, anywhere in a line
    line_items:
      items: {description: {labels: ["Service"]}}
---
## Skill: hooli-remittance pattern
...
```

`applies_to` scopes a skill to documents that look like its pattern:
`sender_domains`, `file_names` (globs), `headers` (with `min_header_share`) or
`texts`. One criterion matching is enough. Skills without it apply everywhere.
`metadata.skills_applied` lists what a run used. Bad hints or fingerprints make
the config fail to load (`config_invalid`).

### Data dictionary (`schema.json`)

```json
{"name": "invoice", "version": "1.0.0", "record_key": "invoice_number", "fields": [
  {"name": "total_amount", "type": "decimal", "required": true, "critical": true,
   "description": "Total amount due, including tax.", "aliases": ["total due", "amount due"]},
  {"name": "po_number", "type": "string", "pattern": "^PO-?\\d+$"},
  {"name": "currency", "type": "string", "grounding": "optional"},
  {"name": "line_items", "type": "array", "items": [
    {"name": "description", "type": "string"}, {"name": "amount", "type": "decimal"}]}
]}
```

Types: `string`, `integer`, `decimal`, `date` (output `YYYY-MM-DD`), `boolean`,
`enum` (with `values`), `array` of objects. A field may also carry `anchors`
(phrases its value follows). `record_key` tells records apart: a table with a
key column gives one record per key, and sources sharing a key merge into one
record.

## Models, the gateway, and cost

`manifest.json`'s `model.provider` is `stub` (deterministic label and column
matching; offline) or `gateway` (a model reached through the gateway; `model.name`
is an alias such as `default`).

Every model call goes through one function, `call_model(ModelRequest) ->
ModelResponse`, in `extractor_service/gateway.py`. It is the only code that knows
the vendor, the model ids and the wire protocol. Everything else passes a neutral
request (alias, system prompt, messages, tool specs as JSON Schema, the tool the
model must call).

The gateway handles four things:
- **Retries:** transient failures are retried with backoff.
- **Request headers:** `X-Request-Id`, `X-Client-Id`, `X-Usecase` and `X-Config-Version` join gateway logs to audit records.
- **Errors:** a failed call becomes the flag `error:model_unavailable` or `error:model_failed` on a result; it is never an HTTP error.
- **Cost (RT-62):** with `MODEL_GATEWAY_PRICES` set (USD per million tokens, by alias or model id), each run reports `metadata.cost.model_usd`. A run whose projected cost exceeds the config's `limits.max_cost_usd` skips the model call and is flagged `cost_ceiling_exceeded`.

`MODEL_PROVIDER=stub` forces every config onto the stub.

### How confidence works

| Check | Effect on the value's confidence |
| --- | --- |
| Found at the cited cell or line (`verified`) | × 1.0 |
| Found elsewhere; source corrected (`relocated`) | × 0.8 |
| Not written, field allows inference (`inferred`) | × 0.6 |
| Not found, field requires grounding | value dropped, `unverified_value:<field>` |
| Breaks its type, pattern or values | × 0.5, `schema_validation_failed:<field>` |
| Another source holds the same value | + 0.05 |
| Older message in a thread / OCR'd text | lower starting score |

A record's confidence is the lowest among its required fields. Below
`thresholds.accept_at` it is flagged `low_confidence`.

## Result files: CSV by default

Every finished job (clean, flagged or failed) is also written as files under
`OUTPUT_ROOT`, **CSV by default**:

```
<OUTPUT_ROOT>/<client>/<usecase>/<YYYY-MM-DD>/<job_id>.csv    (.xlsx, .docx, .pdf when asked)
```

The formats come from the request's `output_formats` (`["csv", "xlsx", "docx", "pdf"]`, `[]` for none),
else the config's `output.formats`, else `OUTPUT_FORMATS` (`csv`). What was written is listed in the
result under `metadata.outputs` (path relative to `OUTPUT_ROOT`, bytes, SHA-256), so the webhook and
the poll say where the files are; a synchronous answer also carries `X-Output-Files`. A file that
cannot be written never fails the job: it is listed with its error and the result is flagged
`output_failed:<format>`. `GET /extractions/{job_id}/output/{csv|xlsx|docx|pdf}` renders any format
from the stored result on demand. Batch runs from the command line write no files. The retention
sweep does not delete result files; age them out with the storage's own lifecycle rules.

The files come from the writer tools in `extractor_tools` (pure functions, also served on their own
at `POST /tools/{csv,excel,docx,pdf}-writer` with `{"result": ..., "params": {...}}`):

| Tool | Gives |
| --- | --- |
| `write_csv` | one row per record; with exactly one array field (line items) one row per item, the record's fields repeated and the item's columns as `line_items.<col>`; `_confidence`, `_flagged`, `_flags` for an extended result. UTF-8 with BOM. Params: `explode`, `include_meta`, `delimiter`, `bom` |
| `write_xlsx` | sheets `Records` (typed cells, flagged rows shaded), one per array field, `Sources` (value, confidence, locator), `Run` |
| `write_docx` | a Word report: run summary, each record's field table with confidence and source, its line items, flags highlighted. Param: `title` |
| `write_pdf` | the same report as PDF (DejaVu Sans, so ₹ € £ print). Param: `title` |

Text a spreadsheet would run as a formula (`=…`, `+…`, `@…`) is written with a leading `'`. Every
writer is deterministic: the same result gives the same bytes.

## Review, audit and metrics

- **Review queue (RT-44..48).** Every flagged result opens an entry (`GET /review`). `POST /review/{job_id}/resolve` takes `{"reviewer", "corrections": [{"record", "field", "value"}]}`, or `"records"` to replace them, or `"action": "reject"`. Each change is logged with the original and corrected value, who made it, and when. The corrected result keeps the `audit_id`, is marked `human_corrected` and is delivered again. `GET /review/corrections/export` returns corrected results as ground truth for design-time learning (`POST /learning/corrections`).
- **Audit.** Every run writes its extended result to `AUDIT_DIR` (RT-06), failures included. Every completed, corrected or rejected result is appended to a SHA-256 hash chain in Postgres, one appender at a time (an advisory lock). `GET /audit/verify` checks every link, and that each stored result is the one last chained.
- **Metrics (RT-54).** `GET /metrics` reports, per client and use case: jobs, in progress, flagged and flag rate, corrections, dead letters, p50/p95 latency, cost, and the most common flags. `GET /metrics/prometheus` gives the same in Prometheus text format.
- **Retention.** A sweep deletes finished jobs' spools and checkpoints, and purges results older than `RETENTION_DAYS` whose review is closed.

## Batch runs from the command line

`python -m extractor_service.cli` reads `{"runs": [{"file_location", "client"?,
"usecase"?, "version"?}], "include_evidence"?: bool}` on stdin. It writes each
run's extended result, plus its config, dictionary, skills and (when asked)
evidence blocks, as JSON on stdout. Design-time uses it to run this engine in
isolation when it learns a pattern.

## Environment

| Variable | Default (image) | Meaning |
| --- | --- | --- |
| `CONFIG_ROOT` | `/app/configs` | Config folders |
| `INPUT_ROOT` | `/data` | Only files under this folder can be read |
| `AUDIT_DIR` | `/audit` | One JSON audit record per run; unset to disable |
| `DATABASE_URL` | required | Postgres for jobs, results, deliveries, review, audit chain, spool, checkpoints |
| `DB_SCHEMA` | `extractor` | The schema those tables live in (created if missing) |
| `STATE_KEY` | unset | Fernet key: encrypts spooled attachments at rest |
| `WORKERS` | `2` | Queue workers inside the API process (0 = none) |
| `JOB_MAX_ATTEMPTS`, `JOB_RETRY_BACKOFF_S`, `JOB_LEASE_S` | `3`, `2`, `420` | Engine-fault retries; how long a worker owns a job |
| `DEDUPE_WINDOW_DAYS` | `7` | Idempotency window |
| `WEBHOOK_SECRET` | unset | Signs deliveries (HMAC-SHA256) |
| `WEBHOOK_MAX_ATTEMPTS`, `WEBHOOK_BACKOFF_S`, `WEBHOOK_TIMEOUT_S` | `8`, `5`, `15` | Delivery retries |
| `WEBHOOK_ALLOWED_HOSTS` | unset | Comma-separated callback host allowlist |
| `PARSE_SANDBOX` | `process` | `off` parses in-process (no fork on the platform, debugging) |
| `SANDBOX_MEMORY_MB` | `1024` | Address space a parse may add |
| `RETENTION_DAYS` | `90` | Finished results kept this long |
| `OUTPUT_ROOT` | `./output` (`/output` in the image) | Where result files are written; empty = none |
| `OUTPUT_FORMATS` | `csv` | Default formats, comma-separated: `csv`, `xlsx`, `docx`, `pdf`; `none` = none |
| `MAX_FILE_MB` | `25` | Largest input file |
| `MODEL_PROVIDER` | unset | Force a provider for every config, e.g. `stub` |
| `MODEL_GATEWAY_URL`, `MODEL_GATEWAY_TOKEN` | unset | The model gateway and its credential |
| `MODEL_GATEWAY_AUTH_HEADER` | `Authorization` | `Authorization` sends `Bearer <token>`; any other name sends the token as is |
| `MODEL_GATEWAY_MODELS` | unset | JSON map of aliases to model ids |
| `MODEL_GATEWAY_PRICES` | unset | JSON prices per million tokens, for cost reporting and ceilings |
| `MODEL_GATEWAY_TIMEOUT_S`, `MODEL_GATEWAY_RETRIES` | `120`, `2` | Per attempt; extra attempts on transient failures |

## Limits worth knowing

- **Postgres is required.** The API refuses to start without `DATABASE_URL`; `/health` reports whether the database is reachable. The batch CLI (`extractor_service.cli`) needs no database.
- **The parser sandbox caps time and memory, not network.** Run the image without egress to complete RT-65.
- **OCR needs tesseract**, which the image installs. English only by default; add tesseract language packs for others.
- **`.msg` is read with olefile (BSD) and compressed-rtf (MIT), not the GPL `extract-msg`.** The tests use synthetic files built to the published format; run a few real Outlook exports through before relying on it.
- **The stub finds only what the aliases and skill hints name.** Real documents need a model through the gateway; design-time pattern learning adds hints for the layouts it has seen.
- **Encrypted zips:** ZipCrypto members open with a key from the email; AES-encrypted members do not (Python's zipfile cannot read them), so they flag `encrypted_no_key`.

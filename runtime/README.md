# DataExtractor runtime

Extracts the fields a data dictionary defines from an email (`.eml` or Outlook `.msg`), a zip, or a
single CSV, Excel (`.xlsx`, `.xlsm`, `.xls`) or PDF file. Images are recorded and
skipped. One FastAPI service, one self-contained Docker image. The pipeline is a
LangGraph state graph, and every model call goes through one gateway function.
Requests are validated by plain functions; no pydantic is imported by this code
(FastAPI still installs it for itself).

```
POST /extract   {"file_location": "invoice-email.eml"}                    -> the data
POST /extract   {"file_location": "invoice-email.eml", "extended": true}  -> data + confidence + metadata
GET  /configs   every client / use case / version, and the defaults
GET  /graph     the pipeline graph, as Mermaid text
GET  /health    liveness, plus any config that failed to load
GET  /docs      Swagger UI for trying requests by hand
```

## Run it

**Docker**

```
docker build -t dataextractor-runtime .
docker run --rm -p 8000:8000 -v D:\data:/data dataextractor-runtime
```

Put input files in the mounted folder (`D:\data` above); `file_location` is
relative to it. To use your own configs instead of the ones baked into the image,
add `-v D:\configs:/app/configs`. For configs that use a model, add
`-e MODEL_GATEWAY_URL=... -e MODEL_GATEWAY_TOKEN=...` (see Model gateway).

**Without Docker**

```
pip install -r requirements.txt
set INPUT_ROOT=D:\data              (export INPUT_ROOT=... on macOS/Linux)
uvicorn extractor_service.api:app --port 8000
```

Then open http://localhost:8000/docs and try `POST /extract`.

**Sample files and tests**

```
pip install -r requirements-dev.txt
python -m tests.samples D:\data     # writes eight sample inputs
pytest                              # 169 tests (service and reader tools)
```

## Request

| Field | Required | Meaning |
| --- | --- | --- |
| `file_location` | yes | Path to the input, inside `INPUT_ROOT` (absolute, or relative to it) |
| `client` | no | Config client; defaults from `configs/defaults.json` |
| `usecase` | no | Config use case |
| `version` | no | Config version, or `latest` |
| `extended` | no | `true` adds confidence, sources, review reasons and metadata |

Unknown request fields are rejected, so a typo like `extnded` fails loudly. A bad
body is a 422 `invalid_request` whose `detail.errors` lists each problem, e.g.
`{"loc": "extended", "msg": "must be a boolean"}`.

## Response

The response is **always an array of records**, each shaped by the data
dictionary: one key per field, in dictionary order, `null` where a field was
not found. One invoice gives a one-item array; a statement, a multi-row CSV or
an email with several attached invoices gives one record per invoice.

Plain (`extended` false), for `invoices.csv`, a statement of three invoices:

```json
[
  {"invoice_number": "INV-1001", "invoice_date": "2026-08-01", "due_date": "2026-08-31",
   "vendor": "Acme Corp", "currency": "USD", "total_amount": 1250, "tax_amount": null,
   "po_number": null, "line_items": []},
  {"invoice_number": "INV-1002", "invoice_date": "2026-08-05", "due_date": "2026-09-04",
   "vendor": "Globex Ltd", "currency": "USD", "total_amount": 980.4, "tax_amount": null,
   "po_number": null, "line_items": []},
  {"invoice_number": "INV-1003", "invoice_date": "2026-08-09", "due_date": "2026-09-08",
   "vendor": "Initech LLC", "currency": "USD", "total_amount": 3100, "tax_amount": null,
   "po_number": null, "line_items": []}
]
```

and for `invoice-email.eml`, a single invoice:

```json
[
  {"invoice_number": "INV-20194", "invoice_date": "2026-08-15", "due_date": "2026-09-14",
   "vendor": "Acme Corp", "currency": "USD", "total_amount": 12400, "tax_amount": 2000,
   "po_number": "PO-5531",
   "line_items": [{"description": "Consulting", "quantity": 10, "unit_price": 1000, "amount": 10000},
                  {"description": "Licence", "quantity": 1, "unit_price": 400, "amount": 400}]}
]
```

An input with nothing readable (only images, say) gives `[]` with status `review`.

Every response also carries headers `X-Extraction-Status` (`extracted` or
`review`) and `X-Audit-Id`, so a plain caller can still tell a confident result
from one that needs a person. The status is `review` if any record needs review.

Extended (`extended` true) keeps the same array as `data` and adds detail per record:

```json
{
  "data": [ { ...record 1... }, { ...record 2... } ],
  "status": "extracted",
  "confidence": 0.85,
  "review_reasons": [],
  "records": [
    {
      "data": { ...record 1... },
      "status": "extracted",
      "confidence": 0.85,
      "review_reasons": [],
      "fields": {
        "total_amount": {"value": 1250, "confidence": 0.85, "grounding": "verified",
                         "source": "file:invoices.csv#E2"},
        "currency": {"value": "USD", "confidence": 0.36, "grounding": "inferred", ...},
        "line_items": {"value": [...], "confidence": 0.75,
                       "items": [{"value": {...}, "confidence": 0.75, "source": "..."}]}
      }
    }
  ],
  "metadata": {
    "audit_id": "run_...",
    "record_count": 3,
    "record_key": "invoice_number",
    "config": {"client": "default", "usecase": "invoice", "version": "1.0.0", "sha256": "...",
               "resolved_by": {"client": "default", "usecase": "default", "version": "default"}},
    "model": {"provider": "stub", "name": "deterministic-stub"},
    "input": {"file_location": "...", "kind": "csv", "sha256": "...", "subject": null, "sender": null},
    "documents": [...], "skipped": [...], "graph": {"path": [...]}, "timings_ms": {...}
  }
}
```

The top-level `confidence` is the lowest of the records' confidences, and the
top-level `review_reasons` collect every record's reasons plus run-level ones
(an unreadable attachment, content that fits no record).

### How records are formed

The data dictionary's `record_key` (`invoice_number` in the sample configs) tells
records apart:

- **A table with a key column** gives one record per key value. Rows that share a key are one record, and their item columns become its `line_items`.
- **A label outside such a table** (e.g. "Supplier: Globex Ltd" above it) fills that field in every record of the document, unless the table has its own column for it.
- **Any other document** is one record. An email body and its attached invoice that share the same key merge into one record.
- **Content with no key** joins the only record when there is exactly one. With several records, it can't be placed, so the run gets `unplaced_content` and goes to review.

**Sources** name the exact place a value came from: `file:<name>#<locator>`,
`attachment:<name>#<locator>`, zip members as `file:bundle.zip/statement.csv#D2`,
and `body#L4` or `body#subject` for the email itself. Locators: CSV `B14`; Excel
`Sheet!B14`; PDF `p1:L4` (line) or `p1:T1:R2C3` (table cell).

**Review reasons**: `missing_field:<name>`, `unplaced_content`, `unverified_value:<name>`,
`schema_validation_failed:<name>`, `critical_field_conflict:<name>`,
`low_confidence`, `attachment_parse_failed:<file>`, `encrypted_no_key`,
`unsupported_attachment:<kind>`, `archive_limit_exceeded:<zip>`,
`archive_encrypted:<zip>`, `embedded_email_not_supported`, `no_readable_content`.

**Errors** are JSON `{"error", "message", "detail"}`: `location_outside_input_root`
(400), `file_not_found` (404), `malformed_email` (422), `config_not_found` (404), `config_ambiguous` (400),
`input_too_large` (413), `unsupported_input:<kind>` (422), `model_unavailable`
(503), `model_failed` (502).

## Configs

```
configs/
  defaults.json                        {"client": "default", "usecase": "invoice", "version": "1.0.0"}
  <client>/<usecase>/<version>/
    manifest.json                      model, skills order, thresholds, evidence size, output format
    schema.json                        the data dictionary
    prompts/system.md                  system prompt
    skills/<name>.md                   one per skill, used in the order manifest.json lists them
```

**Skill hints.** A skill may open with YAML front matter. Its `hints` are
machine-readable and are folded into the data dictionary when the config loads,
so every extractor uses them, the stub included; the model sees only the
markdown body. Design-time writes these when it learns a document pattern
(`kind: learned-pattern`), but a hand-written skill can carry them too.

```markdown
---
name: hooli-remittance
kind: learned-pattern
hints:
  fields:
    invoice_number: {labels: ["Our Ref"]}           # labels become aliases
    total_amount: {anchors: ["kindly remit"]}       # value follows this phrase, anywhere in a line
    line_items:
      items: {description: {labels: ["Service"]}}  # a line-item column header
---
## Skill: hooli-remittance pattern
...
```

A hint for a field the dictionary lacks, an unknown hint key, or front matter
that is not YAML makes the config fail to load (`config_invalid`).

Each part the request leaves out is filled in like this:

| Missing | Taken from |
| --- | --- |
| `client` | `defaults.json` |
| `usecase` | `defaults.json` when the client is the default client; otherwise the client's only use case (more than one is an error) |
| `version` | `defaults.json` when client and use case are the defaults; otherwise the highest version (`1.10.0` beats `1.9.0`) |

The response's `metadata.config.resolved_by` says which rule chose each part, and
`sha256` fingerprints every file in the folder. Treat a published version folder
as read-only: change it by adding a new version.

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

`record_key` names the field that tells records apart. It defaults to the first
required string field.

Types: `string`, `integer`, `decimal`, `date` (output `YYYY-MM-DD`), `boolean`,
`enum` (with `values`), `array` of objects. `required` fields missing send the
run to review; `critical` fields that disagree across documents do too.
`grounding: optional` lets a value be inferred rather than read, such as `USD`
from a `$` sign. `manifest.json`'s `output.decimal_format` is `number` (default)
or `string` (exact text such as `"12400.5"`).

### Batch runs from the command line

`python -m extractor_service.cli` reads `{"runs": [{"file_location", "client"?,
"usecase"?, "version"?}], "include_evidence"?: bool}` on stdin and writes each run's
extended output (plus its config, data dictionary, skills and, when asked, its
evidence blocks) as JSON on stdout. Settings come from the same environment
variables as the service. Design-time uses it to run this engine in isolation
when it learns a pattern.

### Pipeline

The extraction runs as a LangGraph state graph (`extractor_service/graph.py`):

```
START -> resolve_config -> read_input -> build_evidence --(readable)--> extract -> verify -> route -> END
                                                       \--(nothing readable)-----------^
```

| Node | Does |
| --- | --- |
| `resolve_config` | Picks the config folder; decides the model provider and name |
| `read_input` | Checks the file location, detects the kind, unpacks email and zip, sets the audit id |
| `build_evidence` | Runs the CSV, Excel and PDF readers into cited lines |
| `extract` | Calls the extractor: the stub, or the model through the gateway. Skipped when nothing is readable |
| `verify` | Checks every value against the evidence, validates types, scores confidence |
| `route` | Overall confidence, review reasons, status |

The path a run took and each node's time are in `metadata.graph` and
`metadata.timings_ms`. `GET /graph` returns the graph as Mermaid text.

### Models and the model gateway

`manifest.json`'s `model.provider` picks the extractor:

| Provider | What runs |
| --- | --- |
| `stub` | Deterministic label and column matching from the dictionary's aliases. No model call; offline; used by the default config and the tests. |
| `gateway` | A model reached through the gateway. `model.name` is an alias (`default` when omitted) that the gateway resolves to a model. |

```json
"model": {"provider": "gateway", "name": "default", "max_tokens": 4096}
```

Every model call goes through one function, `call_model(ModelRequest) ->
ModelResponse`, in `extractor_service/gateway.py`. That file is the only place
that knows which vendor and models sit behind the gateway, how aliases map to
model ids, and what the wire protocol looks like. Everything else passes a
neutral request: a model alias, a system prompt, messages, tool specs whose
parameters are JSON Schema, and the tool the model must call.

- **Headers:** each request carries `X-Request-Id` (the run's audit id), `X-Client-Id`, `X-Usecase`, `X-Config-Version` and `X-Model-Alias`, so gateway logs join to audit records.
- **Retries:** rate limits, server errors, timeouts and connection failures are retried with backoff (2 extra attempts by default).
- **Errors:** refused credentials fail at once as `model_unavailable`; a rejected request fails at once as `model_failed`.
- **Recorded:** the model id that answered, token usage, the gateway's request id and latency go into `metadata.model.call`.

To use a different gateway or vendor, replace `call_model`, or pass your own
function to `create_app(model_gateway=...)`. The contract is the dataclasses in
`gateway.py`, and its module docstring documents the default implementation's
endpoint and settings. The model must answer through the `record_extraction`
tool, whose schema is generated from the data dictionary, at temperature 0.
`MODEL_PROVIDER=stub` forces every config onto the stub.

### How confidence works

Every value is checked against the evidence before it is returned:

| Check | Effect on the model's confidence |
| --- | --- |
| Found at the cited cell or line (`verified`) | × 1.0 |
| Found elsewhere; source corrected (`relocated`) | × 0.8 |
| Not written, field allows inference (`inferred`) | × 0.6 |
| Not found, field requires grounding | value dropped, `unverified_value:<field>` |
| Breaks its type, pattern or values | × 0.5, `schema_validation_failed:<field>` |
| Another source holds the same value | + 0.05 |

Overall confidence is the lowest among required fields. The run is `extracted`
when there are no review reasons and overall confidence reaches
`thresholds.accept_at`; otherwise `review`, with the partial data still returned.

## Environment

| Variable | Default (image) | Meaning |
| --- | --- | --- |
| `CONFIG_ROOT` | `/app/configs` | Config folders |
| `INPUT_ROOT` | `/data` | Only files under this folder can be read |
| `AUDIT_DIR` | `/audit` | One JSON audit record per run; unset to disable |
| `MAX_FILE_MB` | `25` | Largest input file |
| `MODEL_PROVIDER` | unset | Force a provider for every config, e.g. `stub` |
| `MODEL_GATEWAY_URL` | unset | Base URL of the model gateway |
| `MODEL_GATEWAY_TOKEN` | unset | Gateway credential |
| `MODEL_GATEWAY_AUTH_HEADER` | `Authorization` | `Authorization` sends `Bearer <token>`; any other header name sends the token as is |
| `MODEL_GATEWAY_MODELS` | unset | JSON map of aliases to model ids, overriding the gateway's built-in map |
| `MODEL_GATEWAY_TIMEOUT_S` | `120` | Per attempt |
| `MODEL_GATEWAY_RETRIES` | `2` | Extra attempts on transient failures |

## Limits worth knowing

- **Outlook `.msg` files are read with a built-in reader**, using `olefile` (BSD) and `compressed-rtf` (MIT). The GPL-licensed `extract-msg` is deliberately not used. The reader takes the subject, the sender, and the body: plain text first, then HTML, then RTF (including Outlook's HTML-in-RTF bodies). It also takes every attachment stored by value.
- **Some `.msg` attachments are not opened.** An attached Outlook item is flagged as an attached email, the same as with `.eml`. Attachments stored only as a link (`attachment_by_reference`) are skipped.
- **The `.msg` tests use synthetic files.** They are built to the published format, because no permissively licensed real samples exist. Run a few real Outlook exports through `/extract` before relying on it.
- **Images are skipped.** This includes scanned PDFs with no text layer, which end in review.
- **Zip unpacking is capped.** One level of nested zip is allowed. The defaults are 200 members, 200 MB unpacked and a 100:1 compression ratio, all adjustable in `manifest.json` under `intake`.
- **The stub finds only what the aliases and skill hints name.** Real documents need a model through the gateway; design-time's pattern learning adds hints for the layouts it has seen.
- **No checkpointing yet.** The graph runs without a LangGraph checkpointer, so an interrupted run starts again rather than resuming. RT-40 needs a durable checkpointer, plus a serialisable run state.
- **Evidence is paged.** By default the model sees 200 rows per sheet plus the last 10, and 10 PDF pages plus the last one. Larger files are truncated, and this is noted in `metadata.documents[].notes`.

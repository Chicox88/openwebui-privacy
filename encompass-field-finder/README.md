# Encompass Field Dependency Finder (`efinder`)

Give it one or more Encompass field IDs. It finds everything in your Encompass instance that
references or depends on those fields and tells you **where to find each one**. Where Encompass
allows it, it can also **replace** the reference with a new field ID or **remove** it.

You can use it three ways, all running the same engine:

| Interface | Use it for |
|---|---|
| `efinder scan / apply` (CLI) | Scripted or repeatable runs, CI, batches of fields |
| `efinder serve` (local web UI) | Admins who want to type field IDs, review results, tick boxes and apply |
| `FieldDependencyFinder` (Python API) | Your own tooling |

Reports come out as console text, HTML (filterable), CSV, Markdown or JSON. The JSON is the
input for `apply`.

---

## What it covers

Encompass exposes some configuration through the public Developer Connect REST APIs, and some
only inside the desktop admin tools. The tool uses REST wherever ICE publishes an API. For the
rest, it scans **exported** files offline. The report also lists every area it could **not**
scan, so "no references found" never quietly means "never looked".

| Item | How it is found | Automatic change? |
|---|---|---|
| Custom field calculations (`[FIELD]` in formulas) | REST `GET /encompass/v3/settings/loan/customFields` (v1 fallback) | **Replace**: `PATCH ...customFields?action=update`. Remove is manual, because it would break the formula |
| AUDIT custom fields that track the field | same | Replace |
| The field's own definition / description / format | REST `v3/schemas/loan/standardFields`, `v3/settings/loan/customFields?ids=` | Info only |
| **Reporting Database** (field is in the RDB / usable in pipeline, reports, dashboards) | REST `GET /encompass/v3/loanPipeline/canonicalFields?fieldIds=` | Manual (Settings > Reporting Database) |
| **Encompass web input forms** (Form Builder) | REST Encompass web search `POST /encwsearch/v1/search?key=form_fields` | Manual (no form-editing API) |
| Enhanced condition templates / types / sets | REST `v3/settings/loan/conditions/*` | Templates and types: replace (+ safe remove) via `PATCH ?action=Update` |
| eFolder documents, document groups, stacking templates | REST `v3/settings/efolder/documents`, `eFolder/documentGroup`, `eFolder/documentStackingTemplates` | Manual |
| Print form groups / custom print form list | REST `v3/settings/loan/printFormGroups`, `customPrintForms` | Manual |
| Loan templates: loan programs, closing costs, template sets, settlement service providers | REST folder crawl `v3/settings/templates/{type}/folders` + `items`. Matched by field ID **and** loan contract path | Manual |
| Milestones, personas, roles, HMDA profiles, disclosure tracking, URLA policy, itemization fees | REST `v3/settings/...` | Manual |
| Investor / funding / transcript templates, TPO custom fields, business contact custom fields | REST `v3/settings/...` | Manual |
| Task settings and task templates | REST `workflow/v1/...` | Task templates: replace |
| Webhook subscriptions (filter attributes) | REST `webhook/v1/subscriptions` (contract-path aware) | Replace (PUT) |
| Global custom data objects (CDOs used by plugins and web tools; base64 content is decoded) | REST `v1/company/customObjects` | Replace (PUT, re-encoded) |
| Loan Opportunity letter and email templates | REST `loanOpportunity/v1/settings/*Templates` | Replace (PUT) |
| API user's own pipeline views | REST `v3/users/me/views/pipelineViews` (undocumented GET, probed) | Replace (PATCH) |
| How many loans hold a value (`--loan-usage`) | REST pipeline cursor, `Fields.X isNotEmpty`, `x-total-count` | Info only |
| **Business rules** (field data entry, triggers, persona field access, automated conditions, milestone completion, input form list, print form rules ...) | **Offline export scan**. No public REST API | Replace in an exported copy, then re-import |
| **Dynamic Data Management** (field rules, fee rules, data tables) | **Offline export scan** | same |
| **Desktop custom input forms** + form codebase | **Offline export scan** (`.empkg`, `.emfrm`, `.cs`, `.vb`, `.dll`) | same; compiled DLLs are manual |
| **Reports, dashboards, alerts** | **Offline export scan** | same |
| Custom print forms / custom letters (field mappings in Word or PDF) | **Offline export scan** (`.docx` is opened, `.pdf`/`.doc` string-scanned) | `.docx` replace; PDF/DOC manual |
| Plugins / SDK apps | **Offline scan** of source or DLLs | manual for binaries |

> Endpoint paths were checked against ICE's published Developer Connect API reference.
> `efinder probe` shows which ones your instance and API user can actually reach. Access
> depends on API user persona rights, product licensing and version.

---

## Install

Python 3.8+.

```bash
cd encompass-field-finder
pip install .            # or: pip install -e ".[dev,yaml]"
cp .env.example .env     # then fill in your Developer Connect credentials
```

`.env` / environment variables:

```
ENCOMPASS_API_BASE=https://api.elliemae.com      # UAT: https://concept.api.elliemae.com
ENCOMPASS_INSTANCE_ID=BE11223344
ENCOMPASS_USERNAME=api_admin                     # without @encompass:BE...
ENCOMPASS_PASSWORD=...
ENCOMPASS_CLIENT_ID=...
ENCOMPASS_CLIENT_SECRET=...
# or ENCOMPASS_TOKEN=<bearer token>
```

Use an **administrator / super administrator** API user. ICE only returns custom field
calculation text to admins, and the settings APIs need settings persona rights. The coverage
section of the report warns you when calculations were hidden.

---

## Use it

### 1. Check what your instance exposes

```bash
efinder probe
```

### 2. Scan

```bash
# one or more fields (comma separated or repeated)
efinder scan -f CX.OLD.FIELD -f 4000,1172 --format html -o report.html

# a list of fields from a file, plus exported desktop settings
efinder scan --fields-file fields.txt --export ./exports --format html -o report.html

# exports only, no API
efinder scan --offline --export ./exports -f CX.OLD.FIELD

# multi-instance fields with a regex
efinder scan -f "re:FL01\d\d"
```

Each run saves full results to `efinder-results.json` (`--save` to change it). Useful flags:

* `--loan-usage`: count loans that hold a value in the field
* `--no-templates`: skip the template crawl (the slowest part on big instances)
* `--only custom_fields,web_input_forms` / `--skip templates`: choose sources
* `--min-confidence low|medium|high`: default `medium` (see *Matching* below)
* `--catalog my_catalog.yaml`: add or override endpoints (see *Extending*)

### 3. Replace or remove (dry run by default)

```bash
# preview
efinder apply --replace CX.OLD.FIELD=CX.NEW.FIELD
# only some references (ref ids are in every report)
efinder apply --replace CX.OLD.FIELD=CX.NEW.FIELD --refs 3e7e37556635,a1b2c3d4e5f6
# really do it
efinder apply --replace CX.OLD.FIELD=CX.NEW.FIELD --yes
# remove references where that is safe
efinder apply --remove -f CX.OLD.FIELD --yes
```

How changes are kept safe:

* **Dry run by default.** Nothing changes without `--yes`, and an interactive confirmation
  follows (`--no-confirm` skips it in scripts).
* **Re-read before write.** Each API object is fetched fresh, edited only at the recorded JSON
  path, and written back with that object type's documented update API.
* **Backups.** The original JSON of every object changed through the API is saved to
  `efinder-backups/` before the update.
* **Exports are never edited in place.** Modified copies, including members rewritten inside
  `.empkg`/`.zip`/`.docx`, go to `efinder-modified-exports/` for you to re-import.
* **Conservative removal.** A reference is only removed automatically when it is a list entry
  (`["CX.OLD", ...]`), an entry object in a list (`[{"fieldId": "CX.OLD", ...}]`) or an object
  key. A field used inside an expression is never auto-removed, because that would break the
  formula or rule. Those are reported as manual, with the location to edit.
* **Replace keeps the syntax.** `[CX.OLD]` → `[CX.NEW]`, `Fields["CX.OLD"]` → `Fields["CX.NEW"]`.
  A numeric ID like `4000` is only replaced where it is clearly a field reference, never where
  it is a plain number.
* Every reference has a `change_support` value: `auto`, `replace_only`, `manual` (with
  instructions) or `none` (informational).

> Custom field IDs cannot be renamed in Encompass. To migrate `CX.OLD` to `CX.NEW`: create
> `CX.NEW`, run `apply --replace CX.OLD=CX.NEW`, fix the manual items, migrate the loan data,
> re-scan to confirm zero references, then retire `CX.OLD`.

### 4. Web UI

```bash
efinder serve            # http://127.0.0.1:8765
```

Type field IDs and export paths, scan, filter the results, tick references, then preview and
apply. Results download as HTML, CSV, JSON or Markdown. The UI binds to localhost, and every
form post carries a per-process token.

### 5. Python API

```python
from encompass_field_finder import FieldDependencyFinder, EncompassConfig

finder = FieldDependencyFinder(config=EncompassConfig.from_env(), export_paths=["./exports"])
result = finder.scan(["CX.OLD.FIELD", "4000"])

for ref in result.references:
    print(ref.field_id, ref.category, ref.item_name, ref.location, ref.change_support.value)

for gap in result.coverage:
    if gap.status != "ok":
        print("not scanned:", gap.category, gap.note)

outcomes = finder.apply(result, "replace", mapping={"CX.OLD.FIELD": "CX.NEW.FIELD"}, dry_run=True)
```

See `examples/python_api.py`.

---

## Getting the offline exports

Put everything you can export into one folder and pass it with `--export`. Any mix of folders,
zips, nested zips, XML/JSON/text, Office files and binaries works. Files are categorized
(business rule, DDM, input form, report, dashboard, alert, codebase ...) from their path and
name, so descriptive folder names help (`BusinessRules/FieldTriggers/...`, `DDM/...`,
`Reports/...`).

Typical sources:

* **Desktop custom input forms:** Input Form Builder, export the forms (Encompass package /
  `.empkg`). Add the codebase project source or DLLs too.
* **Business rules and DDM:** the export function in the Encompass settings screens, any
  settings-migration or packager tool you use, or an SDK dump utility (below).
* **Reports and dashboards:** export the report and dashboard templates.
* **Custom print forms / letters:** the Word/PDF source files.
* **On-prem / hosted database access:** SQL or CSV exports of the settings tables also work.

The exact export menus vary by Encompass version and the admin tools licensed to you.

### SDK dump (optional)

The public Encompass SDK does not cover business rules, DDM, reports or dashboards. Admin tools
that do use internal, unsupported assemblies. If you already have an SDK-based utility, have it
write each object as a JSON or XML file into a folder named after its type (for example
`BusinessRules/FieldDataEntry/<rule>.json`) and scan that folder. The REST and offline engines
need nothing else.

---

## Matching

Each hit gets a kind and a confidence:

| Kind | Example | Confidence |
|---|---|---|
| `exact_value` / `key` | `"fieldId": "CX.OLD"` (whole value), `{"CX.OLD": ...}` | high |
| `calc_expression` | `[CX.OLD]`, `[#4000]` | high |
| `code_accessor` | `Fields["4000"]`, `Fields.4000`, `GetField("4000")`, `FieldID="4000"` | high |
| `contract_path` | template JSON `applications[0].borrower.firstName` for field `4000` | high |
| `search_index` | returned by the Encompass web form search for the field | high |
| `xml_text` / `quoted` | `<FieldID>CX.OLD</FieldID>`, `'CX.OLD'` | medium (low for IDs of 3 digits or fewer) |
| `bare_token` | `... uses CX.OLD for ...` | low (hidden by default) |

IDs never match longer IDs: `CX.A` does not match `CX.AB` or `CX.A.B`, and `1172` does not
match `11720`. Use `--min-confidence low` to see bare-word hits (noisy for numeric IDs).

---

## Extending

**More endpoints, no code.** Write a catalog file (`examples/custom_catalog.yaml`) with entries
like the built-ins in `encompass_field_finder/catalog.py`: list path, optional detail path,
name/ID keys, UI location, and an optional `update` spec that enables automatic changes.
An entry with an existing `key` overrides the built-in one. `"enabled": false` disables it.

**New kinds of source.** Subclass `encompass_field_finder.sources.Source` and implement `scan()`
(and optionally `apply_batch()`). Then pass it in with
`FieldDependencyFinder(extra_sources=[MySource()])`.

---

## Limitations

* ICE has no public REST API for business rules, DDM, desktop input forms, reports, dashboards
  or alerts. Those are covered **only** through exports, and the report says so whenever no
  export covered them.
* Web input forms are found through the Encompass web search index. Editing them is manual,
  because there is no form-editing API.
* Template, eFolder, milestone and persona settings are read-only through the published APIs,
  so changes there are manual (the report gives the location).
* Pipeline views are per user. The API can only see the API user's own views.
* The Reporting Database check shows whether a field is exposed to the pipeline/RDB. RDB
  changes happen in Settings and need an RDB update for existing loans.
* The tool never changes loan data.

## Development

```bash
pip install -e ".[dev]"
pytest
```

The tests run against an in-memory fake of the API and generated export files, so they need no
credentials.

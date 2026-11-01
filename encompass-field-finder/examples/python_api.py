"""Example: use the finder from your own Python code."""

from encompass_field_finder import EncompassConfig, FieldDependencyFinder
from encompass_field_finder.report import to_html

finder = FieldDependencyFinder(
    config=EncompassConfig.from_env(),      # ENCOMPASS_* environment variables
    export_paths=["./exports"],             # optional offline exports
    include_loan_usage=True,
    progress=print,
)

result = finder.scan(["CX.OLD.FIELD"])

# 1) what references the field, and where to find it
for ref in result.references:
    print(f"{ref.category:30} {ref.item_name:40} {ref.location}")

# 2) what could NOT be checked
for c in result.coverage:
    if c.status in ("manual_only", "forbidden", "error"):
        print(f"[{c.status}] {c.category}: {c.note}")

with open("report.html", "w", encoding="utf-8") as fh:
    fh.write(to_html(result))

# 3) preview a migration to a new field (dry run), then apply only the automatic ones
preview = finder.apply(result, "replace", mapping={"CX.OLD.FIELD": "CX.NEW.FIELD"}, dry_run=True)
for o in preview:
    print(o.status, o.message)

# finder.apply(result, "replace", mapping={"CX.OLD.FIELD": "CX.NEW.FIELD"}, dry_run=False)

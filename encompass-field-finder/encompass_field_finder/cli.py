"""Command line interface: ``efinder scan | apply | probe | catalog | serve``."""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import sys
from typing import Dict, List, Optional

from . import __version__
from .catalog import load_catalog
from .client import ApiError, EncompassClient, EncompassConfig
from .finder import FieldDependencyFinder
from .models import ScanResult
from .report import outcomes_to_text, render
from .sources.base import extract_list


# ---------------------------------------------------------------------- helpers
def load_env_file(path: str) -> None:
    """Minimal .env loader (KEY=VALUE lines); existing environment variables win."""
    if not path or not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def parse_field_ids(values: Optional[List[str]], files: Optional[List[str]]) -> List[str]:
    out: List[str] = []
    for v in values or []:
        out += [x.strip() for x in v.replace(";", ",").split(",") if x.strip()]
    for f in files or []:
        with open(f, "r", encoding="utf-8-sig") as fh:
            for line in fh:
                line = line.split("#", 1)[0]
                out += [x.strip() for x in line.replace(";", ",").split(",") if x.strip()]
    return out


def build_config(args: argparse.Namespace) -> Optional[EncompassConfig]:
    if getattr(args, "offline", False):
        return None
    load_env_file(getattr(args, "env_file", None) or ".env")
    cfg = EncompassConfig.from_env()
    for attr, field in (("api_base", "base_url"), ("instance", "instance_id"), ("username", "username"),
                        ("client_id", "client_id"), ("token", "token")):
        val = getattr(args, attr, None)
        if val:
            setattr(cfg, field, val)
    if not cfg.token and cfg.instance_id and cfg.username and cfg.client_id:
        if not cfg.password and sys.stdin.isatty():
            cfg.password = getpass.getpass("Encompass password: ")
        if not cfg.client_secret and sys.stdin.isatty():
            cfg.client_secret = getpass.getpass("API client secret: ")
    return cfg if cfg.is_configured() else None


def make_finder(args: argparse.Namespace, cfg: Optional[EncompassConfig]) -> FieldDependencyFinder:
    return FieldDependencyFinder(
        config=cfg,
        export_paths=args.export or [],
        catalog_path=args.catalog,
        include_undocumented=args.include_undocumented,
        include_loan_usage=getattr(args, "loan_usage", False),
        include_templates=not getattr(args, "no_templates", False),
        only_sources=_split(getattr(args, "only", None)),
        skip_sources=_split(getattr(args, "skip", None)),
        min_confidence=getattr(args, "min_confidence", "medium"),
        workers=getattr(args, "workers", 4),
        progress=(lambda m: print(m, file=sys.stderr)) if not getattr(args, "quiet", False) else None,
    )


def _split(v: Optional[str]) -> Optional[List[str]]:
    return [x.strip() for x in v.split(",") if x.strip()] if v else None


def _write(text: str, path: Optional[str]) -> None:
    if path and path != "-":
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"wrote {path}", file=sys.stderr)
    else:
        sys.stdout.write(text)


# ---------------------------------------------------------------------- commands
def cmd_scan(args: argparse.Namespace) -> int:
    ids = parse_field_ids(args.field, args.fields_file)
    if not ids:
        print("error: give at least one --field or --fields-file", file=sys.stderr)
        return 2
    cfg = build_config(args)
    if cfg is None and not args.export:
        print("error: no API credentials (see --help / .env) and no --export paths to scan", file=sys.stderr)
        return 2
    finder = make_finder(args, cfg)
    result = finder.scan(ids)
    save = args.save or "efinder-results.json"
    with open(save, "w", encoding="utf-8") as fh:
        fh.write(result.to_json())
    print(f"saved full results to {save} (use with `efinder apply --results {save}`)", file=sys.stderr)
    _write(render(result, args.format), args.output)
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    result = ScanResult.load(args.results)
    mapping: Dict[str, str] = {}
    for pair in args.replace or []:
        if "=" not in pair:
            print(f"error: --replace expects OLD=NEW, got {pair!r}", file=sys.stderr)
            return 2
        old, new = pair.split("=", 1)
        mapping[old.strip()] = new.strip()
    if not mapping and not args.remove:
        print("error: give --replace OLD=NEW (repeatable) or --remove", file=sys.stderr)
        return 2
    if mapping and args.remove:
        print("error: use either --replace or --remove, not both", file=sys.stderr)
        return 2
    action = "replace" if mapping else "remove"
    refs = result.references
    if args.remove and args.field:
        wanted = {f.lower() for f in parse_field_ids(args.field, None)}
        refs = [r for r in refs if r.field_id.lower() in wanted]
    cfg = build_config(args)
    finder = make_finder(args, cfg)
    dry = not args.yes
    if not dry and sys.stdin.isatty() and not args.no_confirm:
        n = len(finder.plan(refs, action, mapping, ref_ids=_split(args.refs), include_low=args.include_low))
        ans = input(f"About to {action} {n} reference(s) in Encompass/exports. Type 'yes' to continue: ")
        if ans.strip().lower() != "yes":
            print("aborted")
            return 1
    outcomes = finder.apply(refs, action, mapping=mapping, ref_ids=_split(args.refs), dry_run=dry,
                            backup_dir=args.backup_dir, output_dir=args.output_dir,
                            include_low=args.include_low)
    _write(outcomes_to_text(outcomes), args.output)
    if dry:
        print("\nDRY RUN - nothing was changed. Re-run with --yes to apply.", file=sys.stderr)
    return 0 if all(o.status != "failed" for o in outcomes) else 1


def cmd_probe(args: argparse.Namespace) -> int:
    cfg = build_config(args)
    if cfg is None:
        print("error: API credentials required for probe", file=sys.stderr)
        return 2
    client = EncompassClient(cfg)
    checks = [
        ("custom fields v3", "/encompass/v3/settings/loan/customFields", None),
        ("standard field schema v3", "/encompass/v3/schemas/loan/standardFields", {"ids": "4000"}),
        ("pipeline canonical fields v3", "/encompass/v3/loanPipeline/canonicalFields", {"fieldIds": "4000"}),
    ]
    for e in load_catalog(args.catalog, include_undocumented=True):
        checks.append((e["key"] + ("" if e.get("documented", True) else " (undocumented)"),
                       e["list"]["path"], e["list"].get("params")))
    print(f"{'endpoint':55} {'status':14} items")
    for label, path, params in checks:
        try:
            body = client.get(path, params)
            print(f"{label:55} {'OK':14} {len(extract_list(body))}")
        except ApiError as err:
            print(f"{label:55} {'HTTP ' + str(err.status):14} {err.body[:80]!r}")
        except Exception as err:  # noqa: BLE001
            print(f"{label:55} {'ERROR':14} {err}")
    try:
        client.post("/encwsearch/v1/search", {"from": 0, "size": 1, "query": {"bool": {"must": [
            {"term": {"fieldId": "4000"}}]}}}, {"key": "form_fields"})
        print(f"{'encompass web form search':55} {'OK':14}")
    except ApiError as err:
        print(f"{'encompass web form search':55} {'HTTP ' + str(err.status):14}")
    return 0


def cmd_catalog(args: argparse.Namespace) -> int:
    print(json.dumps(load_catalog(args.catalog, args.include_undocumented), indent=2))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .app import main as app_main

    argv = ["--port", str(args.port)] + (["--no-browser"] if args.no_browser else [])
    return app_main(argv)


# ---------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="efinder",
        description="Find everything in Encompass that references given field ids, and optionally "
                    "replace/remove those references.",
    )
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    def conn(sp: argparse.ArgumentParser) -> None:
        g = sp.add_argument_group("connection (or ENCOMPASS_* env vars / .env)")
        g.add_argument("--env-file", default=".env", help="file with ENCOMPASS_* settings (default .env)")
        g.add_argument("--api-base", help="default https://api.elliemae.com")
        g.add_argument("--instance", help="instance id, e.g. BE11223344")
        g.add_argument("--username", help="API user (without @encompass:instance)")
        g.add_argument("--client-id")
        g.add_argument("--token", help="pre-issued bearer token")
        g.add_argument("--offline", action="store_true", help="do not call the API; scan --export only")

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--export", action="append", metavar="PATH",
                        help="exported Encompass files/folders/zips to scan (repeatable)")
        sp.add_argument("--catalog", help="extra/override endpoint catalog (JSON or YAML)")
        sp.add_argument("--include-undocumented", action="store_true",
                        help="also probe undocumented endpoints listed in the catalog")
        sp.add_argument("--only", help="comma list of source keys to run (e.g. custom_fields,web_input_forms)")
        sp.add_argument("--skip", help="comma list of source keys to skip")
        sp.add_argument("--min-confidence", default="medium", choices=["low", "medium", "high"])
        sp.add_argument("--workers", type=int, default=4)
        sp.add_argument("-q", "--quiet", action="store_true")

    s = sub.add_parser("scan", help="find references")
    s.add_argument("-f", "--field", action="append", help="field id(s), comma separated; repeatable")
    s.add_argument("--fields-file", action="append", help="file with field ids (one per line or comma separated)")
    s.add_argument("--format", default="text", choices=["text", "json", "csv", "md", "html"])
    s.add_argument("-o", "--output", help="write the rendered report here (default stdout)")
    s.add_argument("--save", help="where to save full JSON results (default efinder-results.json)")
    s.add_argument("--loan-usage", action="store_true", help="also count loans holding a value (pipeline)")
    s.add_argument("--no-templates", action="store_true", help="skip the (slow) template folder crawl")
    conn(s), common(s)
    s.set_defaults(func=cmd_scan)

    a = sub.add_parser("apply", help="replace or remove references found by a previous scan")
    a.add_argument("--results", default="efinder-results.json")
    a.add_argument("--replace", action="append", metavar="OLD=NEW", help="field id mapping; repeatable")
    a.add_argument("--remove", action="store_true", help="remove references (only where safe)")
    a.add_argument("-f", "--field", action="append", help="with --remove: only these field ids")
    a.add_argument("--refs", help="comma list of ref ids to touch (default: all changeable)")
    a.add_argument("--include-low", action="store_true", help="also change low-confidence hits")
    a.add_argument("--yes", action="store_true", help="really apply (default is a dry run)")
    a.add_argument("--no-confirm", action="store_true", help="skip the interactive confirmation")
    a.add_argument("--backup-dir", default="efinder-backups")
    a.add_argument("--output-dir", default="efinder-modified-exports",
                   help="where modified copies of export files are written")
    a.add_argument("-o", "--output")
    conn(a), common(a)
    a.set_defaults(func=cmd_apply)

    pr = sub.add_parser("probe", help="check which endpoints your instance/API user can reach")
    pr.add_argument("--catalog")
    conn(pr)
    pr.set_defaults(func=cmd_probe)

    c = sub.add_parser("catalog", help="print the effective endpoint catalog")
    c.add_argument("--catalog")
    c.add_argument("--include-undocumented", action="store_true")
    c.set_defaults(func=cmd_catalog)

    w = sub.add_parser("serve", help="start the desktop-style app (opens in your browser, runs locally)")
    w.add_argument("--port", type=int, default=8765)
    w.add_argument("--no-browser", action="store_true")
    w.set_defaults(func=cmd_serve)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

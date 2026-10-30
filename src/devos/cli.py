"""CLI presentation and composition; domain behavior lives in services."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

from devos import __version__
from devos.core import ENTITY_TYPES, RELATIONS, DevosError
from devos.services import IngestService, VaultService
from devos.storage import SQLiteStore


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="devos", description="A local-first developer knowledge vault")
    root.add_argument("--version", action="version", version=f"devos {__version__}")
    root.add_argument("--vault", default=os.environ.get("DEVOS_VAULT", str(Path.home() / ".devos" / "vault.sqlite3")),
                      help="SQLite vault path (or DEVOS_VAULT)")
    root.add_argument("--json", action="store_true", help="Machine-readable output")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Create a vault with no external services")
    serve = commands.add_parser("serve", help="Run the local web interface and API")
    serve.add_argument("--host", choices=("127.0.0.1", "localhost", "::1"), default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    add = commands.add_parser("add", help="Create a manual entity")
    add.add_argument("type", choices=ENTITY_TYPES)
    add.add_argument("name")
    add.add_argument("--description")
    add.add_argument("--url")
    add.add_argument("--body")
    show = commands.add_parser("show", help="Inspect an entity and its links")
    show.add_argument("id")
    show.add_argument("--provenance", action="store_true")
    edit = commands.add_parser("edit", help="Set manual overrides without erasing source evidence")
    edit.add_argument("id")
    for name in ("name", "description", "body"):
        edit.add_argument(f"--{name}")
    edit.add_argument("--url", dest="canonical_url")
    for name in ("extract", "reextract"):
        cmd = commands.add_parser(name, help="Ingest inputs" if name == "extract" else "Reprocess retained snapshots offline")
        cmd.add_argument("inputs", nargs="+", help="GitHub owner/repo, arxiv:ID, hf:owner/name, or provider URL; - reads stdin")
        cmd.add_argument("--adapter")
        cmd.add_argument("--offline", action="store_true")
        if name == "reextract":
            cmd.add_argument("--snapshot", help="Historical metadata snapshot ID (one input only)")
    search = commands.add_parser("search", help="Search names, descriptions, README text and notes")
    search.add_argument("query")
    search.add_argument("--type", choices=ENTITY_TYPES)
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--offset", type=int, default=0)
    impact = commands.add_parser("impact", help="Find saved items downstream of a dependency or model")
    impact.add_argument("id")
    impact.add_argument("--depth", type=int, default=3)
    impact.add_argument("--limit", type=int, default=100)
    link = commands.add_parser("link", help="Create an evidenced, typed directed relation")
    link.add_argument("subject")
    link.add_argument("relation", choices=RELATIONS)
    link.add_argument("object")
    link.add_argument("--qualifiers", default="{}", help="JSON object, e.g. ecosystem/version/scope")
    export = commands.add_parser("export", help="Export portable versioned JSON")
    export.add_argument("--output", type=Path)
    export.add_argument("--include-raw", action="store_true")
    jobs = commands.add_parser("jobs", help="Inspect and resume durable jobs")
    job_commands = jobs.add_subparsers(dest="job_command", required=True)
    job_commands.add_parser("list")
    for name in ("show", "resume", "cancel"):
        cmd = job_commands.add_parser(name)
        cmd.add_argument("id")
    commands.add_parser("reviews", help="List identity conflicts requiring review")
    backup = commands.add_parser("backup", help="Create a consistent SQLite backup")
    backup.add_argument("destination", type=Path)
    return root


def _emit(value: Any, machine: bool) -> None:
    if machine or isinstance(value, (dict, list)):
        if machine:
            print(json.dumps(value, ensure_ascii=False, sort_keys=True))
        elif isinstance(value, dict) and "values" in value:
            fields = value["values"]
            print(f"{value['id']}  [{value['type_key']}] {fields.get('name', '')}")
            for key in ("description", "canonical_url", "body"):
                if fields.get(key):
                    print(f"{key}: {fields[key]}")
            print(f"relations: {len(value.get('relations', []))}")
            if "claims" in value:
                print(json.dumps(value["claims"], ensure_ascii=False, indent=2))
        elif isinstance(value, list) and all(isinstance(item, dict) and "name" in item for item in value):
            for row in value:
                print(f"{row['id']}  [{row['type_key']}] {row['name']}\n  {row['description']}")
        else:
            print(json.dumps(value, ensure_ascii=False, indent=2))
    else:
        print(value)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    store = None
    try:
        if args.command == "serve":
            try:
                import uvicorn
                from devos.web import create_app
            except ImportError:
                raise DevosError("web_unavailable", "Install the web extra: pip install '.[web]'.") from None
            if not 1 <= args.port <= 65535:
                raise DevosError("invalid_input", "Port must be from 1 to 65535.")
            print(f"Devos: http://{args.host}:{args.port}")
            uvicorn.run(create_app(args.vault), host=args.host, port=args.port)
            return 0
        store = SQLiteStore(args.vault, create=args.command == "init")
        vault = VaultService(store)
        exit_code = 0
        if args.command == "init":
            result: Any = {"vault": str(store.path), "schema_version": 2, "status": "ready"}
        elif args.command == "add":
            result = vault.add(args.type, args.name, description=args.description, url=args.url, body=args.body)
        elif args.command == "show":
            result = vault.show(args.id, provenance=args.provenance)
        elif args.command == "edit":
            result = vault.edit(args.id, {key: getattr(args, key) for key in ("name", "description", "body", "canonical_url")
                                         if getattr(args, key) is not None})
        elif args.command in {"extract", "reextract"}:
            inputs: list[str] = []
            for value in args.inputs:
                if value == "-":
                    inputs.extend(line.strip() for line in sys.stdin if line.strip())
                else:
                    inputs.append(value)
            snapshots = {}
            if getattr(args, "snapshot", None):
                if len(inputs) != 1:
                    raise DevosError("invalid_input", "Select one input when replaying a historical snapshot.")
                snapshots = vault.snapshot_selection(args.snapshot)
            result = IngestService(store).ingest(inputs, offline=args.offline, replay=args.command == "reextract",
                                                 adapter=args.adapter, snapshots=snapshots)
            exit_code = 0 if result["state"] == "complete" else 2
        elif args.command == "search":
            result = vault.search(args.query, entity_type=args.type, limit=args.limit, offset=args.offset)
        elif args.command == "impact":
            result = vault.impact(args.id, depth=args.depth, limit=args.limit)
        elif args.command == "link":
            result = vault.link(args.subject, args.relation, args.object, json.loads(args.qualifiers))
        elif args.command == "export":
            result = vault.export(include_raw=args.include_raw)
            if args.output:
                if args.output.resolve() == store.path:
                    raise DevosError("invalid_input", "Export destination must differ from the vault.")
                with args.output.open("x", encoding="utf-8") as output:
                    json.dump(result, output, ensure_ascii=False, indent=2)
                    output.write("\n")
                result = {"output": str(args.output.resolve()), "raw_included": args.include_raw}
            else:
                _emit(result, True)
                return 0
        elif args.command == "jobs":
            if args.job_command == "list":
                result = vault.jobs()
            elif args.job_command == "show":
                result = vault.job(args.id)
            elif args.job_command == "cancel":
                result = vault.cancel_job(args.id)
            else:
                result = IngestService(store).resume(args.id)
                exit_code = 0 if result["state"] == "complete" else 2
        elif args.command == "reviews":
            result = vault.reviews()
        elif args.command == "backup":
            store.backup(args.destination)
            result = {"backup": str(args.destination.resolve())}
        else:
            raise DevosError("invalid_input", "Unknown command.")
        _emit(result, args.json)
        return exit_code
    except DevosError as exc:
        error = {"error": {"code": exc.code, "message": str(exc)}}
        print(json.dumps(error) if args.json else f"{exc.code}: {exc}", file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error, ValueError) as exc:
        if isinstance(exc, FileExistsError):
            message = "Destination already exists; choose a new path."
        elif isinstance(exc, json.JSONDecodeError):
            message = "Invalid JSON input."
        else:
            message = "The operation could not complete; check paths, permissions, and vault availability."
        print(json.dumps({"error": {"code": "operation_failed", "message": message}}) if args.json else message, file=sys.stderr)
        return 1
    finally:
        if store:
            store.close()

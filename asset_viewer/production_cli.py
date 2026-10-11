"""`asset-viewer prod ...`: the agent side of production objects.

Agents may declare assets and views, attach revisions and evidence, report
stage progress and cull obvious junk. Selecting revisions, locking a design,
accepting a stage and cleaning up files are owner decisions made in the
browser; they are intentionally absent here.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import production


def register(sub: Any) -> None:
    prod = sub.add_parser("prod", help="Production assets: views, revisions, design locks, stages (agent side)")
    cmds = prod.add_subparsers(dest="prod_command", required=True)

    cmds.add_parser("projects", help="List projects")

    show = cmds.add_parser("show", help="Show one asset, or a project overview with --project")
    show.add_argument("asset", nargs="?")
    show.add_argument("--project")
    show.add_argument("--json", action="store_true")

    lock = cmds.add_parser("lock", help="Read the verified design lock an implementation may use (read-only)")
    lock.add_argument("asset")
    lock.add_argument("--output", help="Write the JSON to a file instead of stdout")

    create = cmds.add_parser("create", help="Declare a production asset")
    create.add_argument("--project", required=True)
    create.add_argument("--name", required=True)
    create.add_argument("--key", help="Stable lowercase key, e.g. music-hall")
    create.add_argument("--scene", default="")
    create.add_argument("--kind", default="prop", choices=list(production.KINDS))
    create.add_argument("--brief", default="")
    create.add_argument("--view", action="append", default=None, metavar="KEY[:LABEL][?]",
                        help="Design view; repeat. Append ? for optional, e.g. --view front:正面 --view detail?")
    create.add_argument("--stage", action="append", default=None, help="Pipeline stage; repeat. Defaults follow --kind")
    create.add_argument("--collection", help="Registered collection holding the asset folder")
    create.add_argument("--prefix", default="", help="Folder inside the collection, e.g. music-hall")

    view = cmds.add_parser("view-add", help="Declare another design view")
    view.add_argument("asset")
    view.add_argument("view")
    view.add_argument("--label")
    view.add_argument("--optional", action="store_true")

    discover = cmds.add_parser("discover", help="Attach new files that follow the asset folder convention")
    discover.add_argument("asset")

    attach = cmds.add_parser("attach", help="Attach a catalog file as the next revision of a view")
    attach.add_argument("asset")
    attach.add_argument("--view", required=True)
    attach.add_argument("--collection", required=True)
    attach.add_argument("--rel", required=True)
    attach.add_argument("--note", default="")

    evidence = cmds.add_parser("evidence", help="Attach stage evidence: a catalog file or a source reference")
    evidence.add_argument("asset")
    evidence.add_argument("--stage", required=True)
    evidence.add_argument("--collection")
    evidence.add_argument("--rel")
    evidence.add_argument("--source", default="", help="Source reference when there is no file, e.g. a .blend path")
    evidence.add_argument("--caption", default="")
    evidence.add_argument("--kind", choices=["image", "video", "timelapse", "source"])

    stage = cmds.add_parser("stage", help="Report stage progress (todo, wip, ready); acceptance is the owner's")
    stage.add_argument("asset")
    stage.add_argument("stage")
    stage.add_argument("--status", choices=sorted(production.AGENT_STAGE_STATUSES))
    stage.add_argument("--summary")
    stage.add_argument("--next", dest="next_step")

    cull = cmds.add_parser("cull", help="Hide an obviously unusable candidate revision with a reason")
    cull.add_argument("revision_id")
    cull.add_argument("--reason", required=True)

    cleanup = cmds.add_parser("cleanup-plan", help="List revisions that could be cleaned up (dry run only)")
    cleanup.add_argument("asset", nargs="?")


def _view_spec(text: str) -> dict[str, Any]:
    required = not text.endswith("?")
    text = text.rstrip("?")
    key, _, label = text.partition(":")
    return {"key": key, "label": label or None, "required": required}


def _emit(payload: Any) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))


def _describe(detail: dict[str, Any]) -> None:
    asset = detail["asset"]
    print(f"{asset['name']} ({asset['key']}) · {asset['kind_label']} · {asset['project']} / {asset['scene'] or '-'}")
    print(f"Next: {detail['next']['text']}{' (owner)' if detail['next'].get('owner') else ''}")
    gates = " · ".join(f"{g['label']}={g['state']}" for g in detail["gates"].values() if g["state"] != "na")
    print(f"Gates: {gates}")
    lock = detail["lock"]
    print(f"Design lock: {lock['name'] if lock else 'none'}")
    for view in detail["views"]:
        revs = " ".join(
            f"{r['label']}{'*' if r['selected'] else ''}{'(culled)' if r['state'] == 'culled' else ''}" for r in view["revisions"]
        ) or "-"
        print(f"  view {view['view']:<14} {view['status']:<8} {revs}")
    for stage in detail["stages"]:
        print(f"  stage {stage['stage']:<8} {stage['status']:<8} evidence={len(stage['evidence'])} {stage['next_step'] or ''}")


def _scan(collection: str | None) -> None:
    if collection:
        from .app import scan_collection

        scan_collection(collection, force=True)


def run(args: argparse.Namespace) -> None:
    command = args.prod_command
    if command in {"show", "discover"} and getattr(args, "asset", None):
        _scan(production.source_collection(args.asset))
    elif command in {"attach", "evidence"}:
        _scan(args.collection)
    if command == "projects":
        for row in production.projects():
            print(f"{row['name']}\tassets={row['assets']}\tcollections={row['collections']}")
    elif command == "show":
        if args.project:
            _emit(production.project_overview(args.project))
            return
        if not args.asset:
            raise ValueError("give an asset key or --project")
        production.discover(args.asset)
        detail = production.asset_detail(args.asset)
        _emit(detail) if args.json else _describe(detail)
    elif command == "lock":
        payload = json.dumps(production.design_lock(args.asset), indent=2, ensure_ascii=False, sort_keys=True) + "\n"
        if args.output:
            Path(args.output).expanduser().write_text(payload, encoding="utf-8")
            print(f"Wrote {args.output}")
        else:
            sys.stdout.write(payload)
    elif command == "create":
        detail = production.create_asset(
            args.project, args.name, key=args.key, scene=args.scene, kind=args.kind, brief=args.brief,
            views=[_view_spec(v) for v in args.view] if args.view is not None else None, stages=args.stage,
            source_collection=args.collection, source_prefix=args.prefix, actor="agent",
        )
        print(f"Created {detail['asset']['name']} ({detail['asset']['key']})")
    elif command == "view-add":
        production.add_view(args.asset, args.view, args.label, required=not args.optional, actor="agent")
        print(f"Added view {args.view} to {args.asset}")
    elif command == "discover":
        result = production.discover(args.asset, actor="agent")
        print(f"{args.asset}: {len(result['added'])} new file(s)")
        for item in result["added"]:
            print("  " + (f"{item['view']} v{item['number']}" + (f" (culled: {item['culled']})" if item.get("culled") else "") if "view" in item else f"{item['stage']} {item['rel']}"))
    elif command == "attach":
        result = production.attach_revision(args.asset, args.view, args.collection, rel=args.rel, note=args.note, origin="agent", actor="agent")
        print(f"{args.asset} {result['view']} v{result['number']}" + (f" (culled: {result['culled']})" if result["culled"] else ""))
    elif command == "evidence":
        result = production.attach_evidence(
            args.asset, args.stage, collection=args.collection, rel=args.rel, source=args.source,
            caption=args.caption, kind=args.kind, origin="agent", actor="agent",
        )
        print(f"{args.asset} {result['stage']}: {result['kind']} evidence added")
    elif command == "stage":
        production.set_stage(args.asset, args.stage, status=args.status, summary=args.summary, next_step=args.next_step, actor="agent")
        print(f"{args.asset} {args.stage} updated")
    elif command == "cull":
        production.cull_revision(args.revision_id, args.reason, actor="agent")
        print(f"Culled {args.revision_id}")
    elif command == "cleanup-plan":
        _emit(production.cleanup_plan(args.asset))

"""`asset-viewer prod ...`: the agent side of production assets.

Model: asset → slots (views such as front/side, or variants such as piano /
round key layout that ship side by side) → revisions v1, v2 … per slot →
previews (more screenshots, angles, states, clips of one revision). A design
lock picks one revision per required slot and snapshots all its files.

Agents may declare assets and slots, attach revisions and previews, fix
misclassified ones, attach evidence, report stage progress and cull junk.
Selecting, locking, accepting and cleaning up are owner decisions in the
browser and are absent here. Output is compact on purpose: one line per fact,
JSON without indentation unless --pretty.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import production


def register(sub: Any) -> None:
    prod = sub.add_parser("prod", help="Production assets: slots, revisions, previews, design locks, stages (agent side)")
    cmds = prod.add_subparsers(dest="prod_command", required=True)

    cmds.add_parser("projects", help="List projects")

    show = cmds.add_parser("show", help="One asset (compact), or a project with --project")
    show.add_argument("asset", nargs="?")
    show.add_argument("--project")
    show.add_argument("--json", action="store_true", help="Compact machine view (no media URLs or history)")

    lock = cmds.add_parser("lock", help="The verified design lock to build from: every required slot with all its files")
    lock.add_argument("asset")
    lock.add_argument("--output", help="Write the JSON to a file instead of stdout")
    lock.add_argument("--pretty", action="store_true")

    create = cmds.add_parser("create", help="Declare a production asset")
    create.add_argument("--project", required=True)
    create.add_argument("--name", required=True)
    create.add_argument("--key", help="Stable lowercase key, e.g. perform-keys-ui")
    create.add_argument("--scene", default="")
    create.add_argument("--kind", default="prop", choices=list(production.KINDS))
    create.add_argument("--brief", default="")
    create.add_argument("--view", action="append", default=None, metavar="KEY[:LABEL][?]",
                        help="Design view slot (angle, screen); repeat. Append ? for optional")
    create.add_argument("--variant", action="append", default=[], metavar="KEY[:LABEL][?]",
                        help="Variant slot: a configuration that exists side by side (layout, skin); repeat")
    create.add_argument("--stage", action="append", default=None, help="Pipeline stage; repeat. Defaults follow --kind")
    create.add_argument("--collection", help="Registered collection holding the asset folder")
    create.add_argument("--prefix", default="", help="Folder inside the collection, e.g. music-hall")

    for name in ("slot-add", "view-add"):
        slot = cmds.add_parser(name, help="Declare another slot (view or --variant)")
        slot.add_argument("asset")
        slot.add_argument("view")
        slot.add_argument("--label")
        slot.add_argument("--variant", action="store_true")
        slot.add_argument("--optional", action="store_true")

    kind = cmds.add_parser("slot-kind", help="Mark an existing slot as a view or a variant")
    kind.add_argument("asset")
    kind.add_argument("view")
    kind.add_argument("kind", choices=sorted(production.SLOT_KINDS))

    discover = cmds.add_parser("discover", help="Attach files that follow the folder convention; report 待归类")
    discover.add_argument("asset")

    unsorted = cmds.add_parser("unsorted", help="List files whose slot or version is not a known fact")
    unsorted.add_argument("asset")

    attach = cmds.add_parser("attach", help="Attach a catalog file as a new revision, or as a preview of one")
    attach.add_argument("asset")
    attach.add_argument("--slot", "--view", dest="view", required=True)
    attach.add_argument("--collection", required=True)
    attach.add_argument("--rel", required=True)
    attach.add_argument("--revision", type=int, help="Explicit revision number for a new revision")
    attach.add_argument("--preview-of", type=int, metavar="N", help="Add as another preview of vN instead")
    attach.add_argument("--label", default="", help="Preview label (state, angle …)")
    attach.add_argument("--note", default="")

    move = cmds.add_parser("move", help="Fix a misclassified revision: other slot/asset, or turn it into a preview")
    move.add_argument("revision_id")
    move.add_argument("--slot", "--view", dest="view", required=True)
    move.add_argument("--asset")
    move.add_argument("--preview-of", type=int, metavar="N")
    move.add_argument("--revision", type=int)

    detach = cmds.add_parser("detach", help="Unlink a misfiled revision; the file returns to 待归类 untouched")
    detach.add_argument("revision_id")

    evidence = cmds.add_parser("evidence", help="Attach stage evidence: a catalog file or a source reference")
    evidence.add_argument("asset")
    evidence.add_argument("--stage", required=True)
    evidence.add_argument("--collection")
    evidence.add_argument("--rel")
    evidence.add_argument("--source", default="", help="Source reference when there is no file, e.g. a .blend path")
    evidence.add_argument("--caption", default="")
    evidence.add_argument("--kind", choices=["image", "video", "audio", "timelapse", "source"])

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


def _slot_spec(text: str, kind: str) -> dict[str, Any]:
    required = not text.endswith("?")
    key, _, label = text.rstrip("?").partition(":")
    return {"key": key, "label": label or None, "required": required, "kind": kind}


def _json(payload: Any, pretty: bool = False) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2 if pretty else None, separators=None if pretty else (",", ":"))


def _describe(view: dict[str, Any]) -> None:
    asset = view["asset"]
    print(f"{asset['name']} ({asset['key']}) · {asset['kind']} · next: {view['next']} · lock: {view['lock'] or '-'}"
          + (f" · 待归类 {view['unsorted']}" if view["unsorted"] else ""))
    for slot in view["slots"]:
        revs = " ".join(
            f"v{r['v']}{'*' if r['selected'] else ''}{'L' if r['locked'] else ''}{'(culled)' if r['state'] == 'culled' else ''}"
            + (f"+{len(r['previews'])}p" if r["previews"] else "")
            for r in slot["revisions"]
        ) or "-"
        print(f"  {slot['kind']:<7} {slot['slot']:<16} {slot['status']:<8} {revs}")
    print("  stages " + " ".join(f"{s['stage']}={s['status']}" for s in view["stages"]))


def _scan(collection: str | None, force: bool = False) -> None:
    if collection:
        from .app import scan_collection

        scan_collection(collection, force=force)  # cached unless files were just written


def run(args: argparse.Namespace) -> None:
    command = args.prod_command
    if command in {"show", "discover", "unsorted"} and getattr(args, "asset", None):
        _scan(production.source_collection(args.asset), force=command == "discover")
    elif command == "attach":
        _scan(args.collection, force=True)
    if command == "projects":
        for row in production.projects():
            print(f"{row['name']}\tassets={row['assets']}\tcollections={row['collections']}")
    elif command == "show":
        if args.project:
            for key in production.asset_keys(args.project):
                detail = production.asset_detail(key)
                print(f"{key}\t{detail['asset']['name']}\tlock={detail['lock']['name'] if detail['lock'] else '-'}\tnext={detail['next']['text']}")
            return
        if not args.asset:
            raise ValueError("give an asset key or --project")
        production.discover(args.asset, actor="agent")
        view = production.agent_view(args.asset)
        print(_json(view)) if args.json else _describe(view)
    elif command == "lock":
        payload = _json(production.design_lock(args.asset), args.pretty) + "\n"
        if args.output:
            Path(args.output).expanduser().write_text(payload, encoding="utf-8")
            print(f"Wrote {args.output}")
        else:
            sys.stdout.write(payload)
    elif command == "create":
        slots = [_slot_spec(v, "view") for v in args.view] if args.view is not None else ([] if args.variant else None)
        if slots is not None:
            slots += [_slot_spec(v, "variant") for v in args.variant]
        detail = production.create_asset(
            args.project, args.name, key=args.key, scene=args.scene, kind=args.kind, brief=args.brief,
            views=slots, stages=args.stage, source_collection=args.collection, source_prefix=args.prefix, actor="agent",
        )
        print(f"Created {detail['asset']['key']}")
    elif command in {"slot-add", "view-add"}:
        production.add_view(args.asset, args.view, args.label, required=not args.optional,
                            kind="variant" if args.variant else "view", actor="agent")
        print(f"Added {'variant' if args.variant else 'view'} {args.view}")
    elif command == "slot-kind":
        production.update_view(args.asset, args.view, kind=args.kind, actor="agent")
        print(f"{args.view} is a {args.kind}")
    elif command == "discover":
        result = production.discover(args.asset, actor="agent")
        for item in result["added"]:
            if "stage" in item:
                print(f"evidence {item['stage']} {item['rel']}")
            elif "preview_id" in item:
                print(f"preview {item['view']} v{item['number']}")
            else:
                print(f"revision {item['view']} v{item['number']}" + (f" (culled: {item['culled']})" if item.get("culled") else ""))
        print(f"{len(result['added'])} attached · 待归类 {result['unsorted']}")
    elif command == "unsorted":
        for item in production.unsorted_files(args.asset):
            print(f"{item['rel']}\t{item['reason']}")
    elif command == "attach":
        if args.preview_of is not None:
            result = production.attach_preview(args.asset, args.view, args.preview_of, args.collection, rel=args.rel, label=args.label, actor="agent")
            print(f"preview of {result['view']} v{result['number']}")
        else:
            result = production.attach_revision(args.asset, args.view, args.collection, rel=args.rel, note=args.note,
                                                number=args.revision, origin="agent", actor="agent")
            print(f"revision {result['view']} v{result['number']}" + (f" (culled: {result['culled']})" if result["culled"] else ""))
    elif command == "move":
        result = production.move_revision(args.revision_id, view=args.view, asset=args.asset, preview_of=args.preview_of,
                                          number=args.revision, actor="agent")
        print(_json(result["to"]))
    elif command == "detach":
        print(f"detached {production.detach_revision(args.revision_id, actor='agent')['rel']}")
    elif command == "evidence":
        if args.collection:
            _scan(args.collection, force=True)
        result = production.attach_evidence(
            args.asset, args.stage, collection=args.collection, rel=args.rel, source=args.source,
            caption=args.caption, kind=args.kind, origin="agent", actor="agent",
        )
        print(f"evidence {result['stage']} {result['kind']}")
    elif command == "stage":
        production.set_stage(args.asset, args.stage, status=args.status, summary=args.summary, next_step=args.next_step, actor="agent")
        print(f"{args.asset} {args.stage} updated")
    elif command == "cull":
        production.cull_revision(args.revision_id, args.reason, actor="agent")
        print(f"culled {args.revision_id}")
    elif command == "cleanup-plan":
        for item in production.cleanup_plan(args.asset)["items"]:
            print(f"{item['asset']}\t{item['view']} v{item['number']}\t{item['reason']}\t{item['rel']}")

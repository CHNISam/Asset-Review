from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import time
from pathlib import Path

from . import __version__
from .app import LOOPBACK_HOSTS, capability_document, scan_collection, serve as development_serve
from .wsgi import serve as production_serve
from .report import render_review_report
from .mcp_server import run_mcp
from .activity import activity_feed
from .handoff import approved_handoff, copy_approved_set
from .demo_assets import DEMO_COLLECTION_LABEL, DEMO_FAMILY, DEMO_PROJECT, DEMO_REVIEWS, create_demo
from .metadata_adapters import import_collection_metadata
from .project_config import find_project_config, load_project_config
from .project_sync import sync_project
from .webhook import run_webhook, validate_webhook_url, webhook_status
from .storage import (
    add_collection,
    asset_metadata,
    set_asset_metadata,
    annotations_for_asset,
    create_annotation,
    update_annotation,
    delete_annotation,
    catalog_records,
    catalog_state,
    collections,
    complete_collection_review,
    create_family,
    add_family_members,
    remove_family_members,
    set_family_preferred,
    rename_family,
    delete_family,
    families_for_collection,
    data_dir,
    database_path,
    pending_summary,
    preview_cache_status,
    prune_preview_cache,
    remove_collection,
    reopen_collection_review,
    review_events_since,
    review_history,
    review_manifest,
    root_for,
    undo_last_review,
    set_review,
)


def _configure_utf8_stdio() -> None:
    """Keep CLI output readable when Windows inherits a legacy code page."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass


def scan_registered(collection: str | None = None) -> list[tuple[str, int, bool]]:
    rows = collections()
    if collection:
        rows = [row for row in rows if row["slug"] == collection]
        if not rows:
            raise ValueError(f"collection not found: {collection}")
    result = []
    for row in rows:
        images, meta = scan_collection(row["slug"], force=True)
        result.append((row["slug"], len(images), bool(meta["truncated"])))
    return result


def print_manifest(manifest: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(manifest, indent=2, sort_keys=True))
        return
    counts = manifest["counts"]
    print(
        f"total={counts['total']} approved={counts['approved']} maybe={counts['maybe']} "
        f"rejected={counts['rejected']} unreviewed={counts['unreviewed']} new={counts['new']}"
    )
    for item in manifest["items"]:
        status = item["status"] or "unreviewed"
        comment = item["comment"].replace("\t", " ").replace("\n", " ")
        print(f"{item['collection']}\t{status}\t{item['rel']}\t{comment}")


def resolve_asset(collection: str, asset: str) -> dict:
    records = catalog_records(collection, present_only=False)
    match = next((row for row in records if row["asset_id"] == asset or row["rel"] == asset), None)
    if not match:
        raise ValueError(f"asset not found: {asset}")
    return match


def doctor(host: str, trusted_hosts: list[str]) -> int:
    findings: list[tuple[str, str]] = []
    root = data_dir()
    mode = stat.S_IMODE(root.stat().st_mode)
    findings.append(("PASS" if mode & 0o077 == 0 else "WARN", f"data directory permissions {oct(mode)}: {root}"))
    db = database_path()
    if db.exists():
        db_mode = stat.S_IMODE(db.stat().st_mode)
        findings.append(("PASS" if db_mode & 0o077 == 0 else "WARN", f"SQLite permissions {oct(db_mode)}: {db}"))
    rows = collections()
    findings.append(("PASS" if rows else "INFO", f"registered collections: {len(rows)}"))
    for row in rows:
        path = Path(row["path"])
        available = bool(row.get("available"))
        readable = available and os.access(path, os.R_OK | os.X_OK)
        findings.append(("PASS" if readable else "FAIL", f"{row['slug']}: {'readable' if readable else 'unavailable/not readable'} — {path}"))
        state = catalog_state(row["slug"])
        if state["generation"]:
            if state["truncated"]:
                findings.append(("WARN", f"{row['slug']}: latest catalog scan incomplete ({state['reason'] or 'unknown'})"))
            else:
                findings.append(("PASS", f"{row['slug']}: catalog generation {state['generation']} last scanned {state['last_scan_at']}"))
    normalized = {item.lower().rstrip('.') for item in trusted_hosts}
    if host not in LOOPBACK_HOSTS and not normalized:
        findings.append(("FAIL", "non-loopback bind has no --trusted-host"))
    elif host not in LOOPBACK_HOSTS:
        findings.append(("WARN", "non-loopback bind: keep Asset Viewer behind TLS and an authenticated/private access boundary"))
    else:
        findings.append(("PASS", "loopback bind is the safest default"))
    findings.append(("PASS" if os.environ.get("ASSET_VIEWER_PASSWORD") else "INFO", "built-in Basic auth " + ("enabled" if os.environ.get("ASSET_VIEWER_PASSWORD") else "disabled (acceptable for localhost/private proxy use)")))
    try:
        from importlib.metadata import version
        waitress_version = version("waitress")
        findings.append(("PASS", f"production HTTP server: Waitress {waitress_version}"))
    except Exception as exc:
        findings.append(("FAIL", f"Waitress production server unavailable: {exc}"))
    findings.append(("INFO", f"scan limits: files={os.environ.get('ASSET_VIEWER_MAX_SCAN_FILES', '50000')} seconds={os.environ.get('ASSET_VIEWER_MAX_SCAN_SECONDS', '10')}"))
    findings.append(("INFO", f"preview limits: bytes={os.environ.get('ASSET_VIEWER_MAX_THUMBNAIL_BYTES', str(250 * 1024 * 1024))} pixels={os.environ.get('ASSET_VIEWER_MAX_IMAGE_PIXELS', '50000000')} workers={os.environ.get('ASSET_VIEWER_THUMBNAIL_WORKERS', '2')}"))
    cache = preview_cache_status()
    findings.append(("INFO", f"preview cache: files={cache['files']} bytes={cache['bytes']} path={cache['path']}"))
    try:
        from importlib.metadata import version
        findings.append(("PASS", f"filesystem watcher: watchdog {version('watchdog')}"))
    except Exception as exc:
        findings.append(("FAIL", f"filesystem watcher unavailable: {exc}"))
    project_config = find_project_config(Path.cwd())
    if project_config:
        try:
            project = load_project_config(project_config)
            missing = [item["relative_path"] for item in project["collections"] if not item["available"]]
            level = "WARN" if missing else "PASS"
            suffix = f"; missing: {', '.join(missing)}" if missing else ""
            findings.append((level, f"project integration config: {project_config} ({len(project['collections'])} collections){suffix}"))
        except ValueError as exc:
            findings.append(("FAIL", f"project integration config invalid: {exc}"))
    else:
        findings.append(("INFO", "project integration config: no .asset-viewer.toml found from current directory"))
    findings.append(("PASS", "metadata adapters available: sidecar, isolated PNG text"))
    webhook_url = os.environ.get("ASSET_VIEWER_WEBHOOK_URL", "").strip()
    if webhook_url:
        try:
            parsed = validate_webhook_url(webhook_url)
            findings.append(("PASS", f"webhook adapter configured: {parsed.split('://', 1)[0]}://{parsed.split('://', 1)[1].split('/', 1)[0]}"))
        except ValueError as exc:
            findings.append(("FAIL", f"webhook adapter configuration invalid: {exc}"))
    else:
        findings.append(("INFO", "webhook adapter disabled; set ASSET_VIEWER_WEBHOOK_URL or pass --url when needed"))
    webhook_state = webhook_status()
    failed_endpoints = [item for item in webhook_state["endpoints"] if item["last_error"]]
    if failed_endpoints:
        findings.append(("WARN", f"webhook delivery state has {len(failed_endpoints)} endpoint(s) with a recorded error; run asset-viewer webhook-status"))
    elif webhook_state["endpoints"]:
        findings.append(("PASS", f"webhook delivery state: {len(webhook_state['endpoints'])} endpoint cursor(s), no recorded errors"))
    failures = 0
    for level, text in findings:
        print(f"[{level}] {text}")
        failures += level == "FAIL"
    return 1 if failures else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="asset-viewer", description="Local-first visual review gallery for image folders.")
    parser.add_argument("--version", action="version", version=f"asset-viewer {__version__}")
    sub = parser.add_subparsers(dest="command")

    add = sub.add_parser("add", help="Register an image folder")
    add.add_argument("path")
    add.add_argument("label", nargs="?")
    add.add_argument("--group", help="Optional navigation group; does not move the source folder")

    remove = sub.add_parser("remove", help="Remove a registered folder")
    remove.add_argument("key", help="Collection slug or absolute path")

    sub.add_parser("list", help="List registered folders")

    capabilities = sub.add_parser("capabilities", help="Describe the agent/API protocol and enabled capabilities")
    capabilities.add_argument("--json", action="store_true")

    scan = sub.add_parser("scan", help="Discover assets and refresh review metadata")
    scan.add_argument("--collection")

    reviews = sub.add_parser("reviews", help="Read review state for agents or humans")
    reviews.add_argument("--collection")
    reviews.add_argument("--status", choices=["approved", "maybe", "rejected", "unreviewed", "new"])
    reviews.add_argument("--json", action="store_true")
    reviews.add_argument("--no-scan", action="store_true", help="Do not scan folders before reading the manifest")
    reviews.add_argument("--present-only", action="store_true", help="Exclude tombstoned/missing assets")

    export = sub.add_parser("export-manifest", help="Export machine-readable review state")
    export.add_argument("--collection")
    export.add_argument("--status", choices=["approved", "maybe", "rejected", "unreviewed", "new"])
    export.add_argument("--output", default="-", help="Output file or - for stdout")
    export.add_argument("--no-scan", action="store_true")
    export.add_argument("--present-only", action="store_true", help="Exclude tombstoned/missing assets")

    report = sub.add_parser("report", help="Export a human-readable review report")
    report.add_argument("--collection")
    report.add_argument("--status", choices=["approved", "maybe", "rejected", "unreviewed", "new"])
    report.add_argument("--format", choices=["html", "markdown"], default="html")
    report.add_argument("--output", default="-", help="Output file or - for stdout")
    report.add_argument("--title", default="Asset Viewer Review Report")
    report.add_argument("--no-scan", action="store_true")
    report.add_argument("--present-only", action="store_true", help="Exclude tombstoned/missing assets")
    report.add_argument("--no-images", action="store_true", help="Do not embed thumbnails in HTML reports")

    project_config = sub.add_parser("project-config", help="Inspect the nearest project-owned .asset-viewer.toml")
    project_config.add_argument("path", nargs="?", default=".")
    project_config.add_argument("--json", action="store_true")

    project_sync = sub.add_parser("project-sync", help="Register project-declared review folders and import metadata")
    project_sync.add_argument("path", nargs="?", default=".")
    project_sync.add_argument("--dry-run", action="store_true", help="Validate and show the plan without changing Asset Viewer state")
    project_sync.add_argument("--json", action="store_true")

    metadata_import = sub.add_parser("metadata-import", help="Import sidecar/PNG generation metadata for one collection")
    metadata_import.add_argument("collection")
    metadata_import.add_argument("--adapter", action="append", choices=["sidecar", "png_text"], dest="adapters")
    metadata_import.add_argument("--json", action="store_true")

    metadata = sub.add_parser("metadata", help="Read or update optional provenance/generation metadata")
    metadata.add_argument("collection")
    metadata.add_argument("asset", help="Stable asset ID or relative path")
    metadata.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="Set a provenance field; repeatable")
    metadata.add_argument("--extra-json", help="Merge arbitrary extra metadata from a JSON object")
    metadata.add_argument("--json", action="store_true")

    activity = sub.add_parser("activity", help="Read human-readable collection activity")
    activity.add_argument("--collection")
    activity.add_argument("--after", type=int, default=0, dest="after_id")
    activity.add_argument("--limit", type=int, default=200)
    activity.add_argument("--json", action="store_true")

    handoff = sub.add_parser("handoff", help="Export the exact approved asset set")
    handoff.add_argument("collection")
    handoff.add_argument("--output", default="-", help="Manifest output file or - for stdout")
    handoff.add_argument("--copy-to", help="Optionally copy approved files to this separate destination")
    handoff.add_argument("--overwrite", action="store_true", help="Allow replacement inside --copy-to destination")

    pending = sub.add_parser("pending", help="Report collections still waiting for human review")
    pending.add_argument("--collection")
    pending.add_argument("--json", action="store_true")
    pending.add_argument("--no-scan", action="store_true")

    events = sub.add_parser("events", help="Read ordered review events for agents/automation")
    events.add_argument("--collection")
    events.add_argument("--after", type=int, default=0, dest="after_id")
    events.add_argument("--limit", type=int, default=100)
    events.add_argument("--json", action="store_true")

    webhook = sub.add_parser("webhook", help="Deliver review events to an outbound webhook with a durable cursor")
    webhook.add_argument("--url", default=os.environ.get("ASSET_VIEWER_WEBHOOK_URL"))
    webhook.add_argument("--collection")
    webhook.add_argument("--secret", default=os.environ.get("ASSET_VIEWER_WEBHOOK_SECRET", ""))
    webhook.add_argument("--once", action="store_true", help="Deliver one available batch and exit")
    webhook.add_argument("--replay", action="store_true", help="Replay existing history on first use instead of starting at the latest event")
    webhook.add_argument("--after", type=int, dest="after_id", help="Explicit event cursor; overrides saved state")
    webhook.add_argument("--interval", type=float, default=2.0)
    webhook.add_argument("--batch-size", type=int, default=100)
    webhook.add_argument("--timeout", type=float, default=10.0)
    webhook.add_argument("--json", action="store_true")

    webhook_status_cmd = sub.add_parser("webhook-status", help="Show sanitized webhook cursor/error state")
    webhook_status_cmd.add_argument("--json", action="store_true")

    wait = sub.add_parser("wait-for-review", help="Wait until review is explicitly complete")
    wait.add_argument("--collection")
    wait.add_argument("--timeout", type=float, default=300.0, help="Maximum seconds to wait")
    wait.add_argument("--interval", type=float, default=2.0, help="Seconds between review-state checks")
    wait.add_argument("--scan-interval", type=float, default=30.0, help="Seconds between filesystem rescans")
    wait.add_argument("--json", action="store_true")
    wait.add_argument("--no-scan", action="store_true")

    complete = sub.add_parser("complete", help="Mark a collection review complete")
    complete.add_argument("collection")

    reopen = sub.add_parser("reopen", help="Reopen a completed collection review")
    reopen.add_argument("collection")

    history = sub.add_parser("history", help="Show review history for one asset")
    history.add_argument("collection")
    history.add_argument("rel")
    history.add_argument("--json", action="store_true")
    history.add_argument("--limit", type=int, default=50)

    undo = sub.add_parser("undo", help="Undo the most recent review/comment change for one asset")
    undo.add_argument("collection")
    undo.add_argument("rel")

    url = sub.add_parser("collection-url", help="Print a stable browser URL for a collection")
    url.add_argument("collection")
    url.add_argument("--base-url", default="http://127.0.0.1:8160")

    asset_url = sub.add_parser("asset-url", help="Print a stable browser deep link for one asset")
    asset_url.add_argument("collection")
    asset_url.add_argument("asset", help="Stable asset ID or relative path")
    asset_url.add_argument("--base-url", default="http://127.0.0.1:8160")

    families = sub.add_parser("families", help="List variant/version families")
    families.add_argument("collection")
    families.add_argument("--json", action="store_true")
    families.add_argument("--present-only", action="store_true")

    family_create = sub.add_parser("family-create", help="Create a variant/version family")
    family_create.add_argument("collection")
    family_create.add_argument("name")
    family_create.add_argument("assets", nargs="+", help="Stable asset IDs or relative paths")
    family_create.add_argument("--preferred", help="Preferred stable asset ID or relative path")
    family_create.add_argument("--json", action="store_true")

    family_add = sub.add_parser("family-add", help="Add assets to an existing family")
    family_add.add_argument("collection")
    family_add.add_argument("family_id")
    family_add.add_argument("assets", nargs="+", help="Stable asset IDs or relative paths")
    family_add.add_argument("--json", action="store_true")

    family_remove = sub.add_parser("family-remove", help="Remove assets from a family without moving files")
    family_remove.add_argument("collection")
    family_remove.add_argument("family_id")
    family_remove.add_argument("assets", nargs="+", help="Stable asset IDs or relative paths")
    family_remove.add_argument("--json", action="store_true")

    family_prefer = sub.add_parser("family-prefer", help="Set the preferred member of a family")
    family_prefer.add_argument("collection")
    family_prefer.add_argument("family_id")
    family_prefer.add_argument("asset", nargs="?", help="Stable asset ID or relative path; omit to clear")
    family_prefer.add_argument("--json", action="store_true")

    family_rename = sub.add_parser("family-rename", help="Rename a family")
    family_rename.add_argument("collection")
    family_rename.add_argument("family_id")
    family_rename.add_argument("name")
    family_rename.add_argument("--json", action="store_true")

    family_delete = sub.add_parser("family-delete", help="Delete family metadata without deleting assets")
    family_delete.add_argument("collection")
    family_delete.add_argument("family_id")

    annotations = sub.add_parser("annotations", help="List spatial annotations for one asset")
    annotations.add_argument("collection")
    annotations.add_argument("asset", help="Stable asset ID or relative path")
    annotations.add_argument("--json", action="store_true")
    annotations.add_argument("--active-only", action="store_true", help="Exclude stale annotations")

    annotate = sub.add_parser("annotate", help="Create a point or region annotation")
    annotate.add_argument("collection")
    annotate.add_argument("asset", help="Stable asset ID or relative path")
    annotate.add_argument("kind", choices=["point", "region"])
    annotate.add_argument("x", type=float, help="Normalized left position 0..1")
    annotate.add_argument("y", type=float, help="Normalized top position 0..1")
    annotate.add_argument("text", nargs="?", default="")
    annotate.add_argument("--width", type=float, default=0.0)
    annotate.add_argument("--height", type=float, default=0.0)
    annotate.add_argument("--json", action="store_true")

    annotation_update = sub.add_parser("annotation-update", help="Update or resolve a spatial annotation")
    annotation_update.add_argument("collection")
    annotation_update.add_argument("annotation_id")
    annotation_update.add_argument("--text")
    state_group = annotation_update.add_mutually_exclusive_group()
    state_group.add_argument("--resolve", action="store_true")
    state_group.add_argument("--reopen", action="store_true")
    annotation_update.add_argument("--json", action="store_true")

    annotation_delete = sub.add_parser("annotation-delete", help="Delete a spatial annotation")
    annotation_delete.add_argument("collection")
    annotation_delete.add_argument("annotation_id")

    cache = sub.add_parser("cache", help="Inspect or prune generated preview cache")
    cache.add_argument("action", choices=["status", "prune"], nargs="?", default="status")
    cache.add_argument("--max-mb", type=int, default=int(os.environ.get("ASSET_VIEWER_CACHE_MAX_MB", "2048")))
    cache.add_argument("--max-age-days", type=int, default=int(os.environ.get("ASSET_VIEWER_CACHE_MAX_AGE_DAYS", "30")))
    cache.add_argument("--json", action="store_true")

    watch = sub.add_parser("watch", help="Watch registered folders and reconcile changes immediately")
    watch.add_argument("--debounce", type=float, default=0.75, help="Seconds to coalesce filesystem events")
    watch.add_argument("--reconcile-interval", type=float, default=60.0, help="Periodic full reconciliation fallback")

    mcp = sub.add_parser("mcp", help="Run the optional MCP adapter for agents")
    mcp.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    mcp.add_argument("--host", default="127.0.0.1")
    mcp.add_argument("--port", type=int, default=8161)

    run = sub.add_parser("serve", help="Run the web viewer")
    run.add_argument("--host", default="127.0.0.1")
    run.add_argument("--port", type=int, default=8160)
    run.add_argument("--trusted-host", action="append", default=[], help="Allowed Host header (repeatable). Required for non-loopback binds and reverse proxies.")
    run.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default="INFO")
    run.add_argument("--http-threads", type=int, default=6, help="Waitress HTTP worker threads (default: 6)")
    run.add_argument("--watch", action=argparse.BooleanOptionalAction, default=True, help="Watch registered folders for near-real-time updates")
    run.add_argument("--watch-debounce", type=float, default=0.75, help="Seconds to coalesce filesystem events")
    run.add_argument("--watch-reconcile-interval", type=float, default=60.0, help="Periodic full reconciliation fallback")
    run.add_argument("--development-server", action="store_true", help="Use the legacy stdlib development server instead of Waitress")
    run.add_argument(
        "--allow-unauthenticated-remote", action="store_true",
        help="Allow a non-loopback listener without built-in Basic auth. Use only behind a trusted authenticated/private boundary.",
    )

    check = sub.add_parser("doctor", help="Check deployment safety and local state")
    check.add_argument("--host", default="127.0.0.1")
    check.add_argument("--trusted-host", action="append", default=[])

    demo = sub.add_parser("demo", help="Create and register synthetic demo assets")
    demo.add_argument("--path", default="./asset-viewer-demo")
    return parser


def main() -> None:
    _configure_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "add":
            row = add_collection(args.path, args.label, args.group)
            suffix = f" [{row['group']}]" if row.get("group") else ""
            print(f"Added {row['label']} ({row['slug']}){suffix}: {row['path']}")
        elif args.command == "remove":
            row = remove_collection(args.key)
            print(f"Removed {row['label']} ({row['slug']})")
        elif args.command == "list":
            rows = collections()
            if not rows:
                print("No folders registered.")
            for row in rows:
                print(f"{row['slug']}\t{row.get('group', '')}\t{row['label']}\t{row['path']}")
        elif args.command == "capabilities":
            payload = capability_document()
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                enabled = [name for name, value in payload["features"].items() if value]
                print(f"Asset Viewer {payload['app_version']} protocol v{payload['protocol_version']}")
                print("Features: " + ", ".join(enabled))
        elif args.command == "scan":
            for slug, count, truncated in scan_registered(args.collection):
                print(f"{slug}\t{count} images\t{'TRUNCATED' if truncated else 'complete'}")
        elif args.command == "reviews":
            if not args.no_scan:
                scan_registered(args.collection)
            print_manifest(review_manifest(args.collection, args.status, include_missing=not args.present_only), args.json)
        elif args.command == "export-manifest":
            if not args.no_scan:
                scan_registered(args.collection)
            payload = json.dumps(
                review_manifest(args.collection, args.status, include_missing=not args.present_only),
                indent=2, sort_keys=True
            ) + "\n"
            if args.output == "-":
                sys.stdout.write(payload)
            else:
                Path(args.output).expanduser().write_text(payload, encoding="utf-8")
                print(f"Wrote {args.output}")
        elif args.command == "report":
            if not args.no_scan:
                scan_registered(args.collection)
            manifest = review_manifest(args.collection, args.status, include_missing=not args.present_only)
            payload = render_review_report(
                manifest, format=args.format, title=args.title, embed_images=not args.no_images
            )
            if args.output == "-":
                sys.stdout.write(payload)
            else:
                Path(args.output).expanduser().write_text(payload, encoding="utf-8")
                print(f"Wrote {args.output}")
        elif args.command == "project-config":
            payload = load_project_config(args.path)
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(f"Project: {payload['project']} ({payload['config_path']})")
                for item in payload["collections"]:
                    marker = "ready" if item["available"] else "missing"
                    print(f"{marker}\t{item['group']}\t{item['label']}\t{item['relative_path']}\t{','.join(item['adapters'])}")
        elif args.command == "project-sync":
            payload = sync_project(args.path, dry_run=args.dry_run)
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                for item in payload["collections"]:
                    if args.dry_run:
                        action = "would register" if item["available"] else "missing"
                    else:
                        action = "registered" if item["registered"] else "missing"
                    imported = (item.get("metadata") or {}).get("imported", 0)
                    print(f"{action}\t{item['label']}\t{item['relative_path']}\tmetadata={imported}")
        elif args.command == "metadata-import":
            adapters = args.adapters or ["sidecar", "png_text"]
            scan_registered(args.collection)
            payload = import_collection_metadata(args.collection, adapters)
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(f"{args.collection}: imported={payload['imported']} skipped={payload['skipped']} errors={len(payload['errors'])}")
                for error in payload["errors"]:
                    print(f"ERROR\t{error['rel']}\t{error['error']}")
        elif args.command == "metadata":
            patch = {}
            for assignment in args.set:
                if "=" not in assignment:
                    raise ValueError("--set values must use KEY=VALUE")
                key, value = assignment.split("=", 1)
                patch[key.strip()] = value
            if args.extra_json:
                extra = json.loads(args.extra_json)
                if not isinstance(extra, dict):
                    raise ValueError("--extra-json must decode to an object")
                patch["extra"] = extra
            payload = set_asset_metadata(args.collection, args.asset, patch) if patch else asset_metadata(args.collection, args.asset)
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                visible = {key: value for key, value in payload.items() if key not in {"asset_id", "collection", "created_at", "updated_at"} and value not in {"", None, {}}}
                print(f"{payload['asset_id']} ({payload['collection']})")
                for key, value in visible.items():
                    print(f"{key}: {json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else value}")
        elif args.command == "activity":
            payload = activity_feed(args.collection, args.after_id, args.limit)
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                for event in payload["events"]:
                    print(f"{event['id']}\t{event['created_at']}\t{event['summary']}")
        elif args.command == "handoff":
            payload = copy_approved_set(args.collection, args.copy_to, overwrite=args.overwrite) if args.copy_to else approved_handoff(args.collection)
            rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
            if args.output == "-":
                sys.stdout.write(rendered)
            else:
                Path(args.output).expanduser().write_text(rendered, encoding="utf-8")
                print(f"Wrote {args.output}")
        elif args.command == "pending":
            if not args.no_scan:
                scan_registered(args.collection)
            payload = pending_summary(args.collection)
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                for state in payload["collections"]:
                    marker = "PENDING" if state["pending"] else "COMPLETE"
                    print(f"{marker}\t{state['collection']}\tunreviewed={state['unreviewed']} new={state['new']} completed_at={state['completed_at'] or '-'}")
            raise SystemExit(2 if payload["pending"] else 0)
        elif args.command == "events":
            payload = review_events_since(args.collection, args.after_id, args.limit)
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                for event in payload["events"]:
                    target = event["rel"] or "(collection)"
                    print(f"{event['id']}\t{event['created_at']}\t{event['collection']}\t{target}\t{event['action']}")
        elif args.command == "webhook":
            if not args.url:
                raise ValueError("--url or ASSET_VIEWER_WEBHOOK_URL is required")
            result = run_webhook(
                args.url, collection=args.collection, secret=args.secret, once=args.once, replay=args.replay,
                after_id=args.after_id, interval=args.interval, batch_size=args.batch_size, timeout=args.timeout,
            )
            if args.json:
                print(json.dumps(result, indent=2, sort_keys=True))
            else:
                print(f"webhook delivered={result['delivered']} last_event_id={result['last_event_id']} initialized={result['initialized']}")
        elif args.command == "webhook-status":
            payload = webhook_status()
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            elif not payload["endpoints"]:
                print("No webhook cursor state yet.")
            else:
                for endpoint in payload["endpoints"]:
                    suffix = f" ERROR={endpoint['last_error']}" if endpoint["last_error"] else ""
                    print(f"{endpoint['endpoint_id']}\t{endpoint['host']}\t{endpoint['collection']}\tlast_event_id={endpoint['last_event_id']}{suffix}")
        elif args.command == "wait-for-review":
            timeout = max(0.0, args.timeout)
            interval = max(0.1, args.interval)
            scan_interval = max(interval, args.scan_interval)
            started = time.monotonic()
            last_scan = -scan_interval
            while True:
                elapsed = time.monotonic() - started
                if not args.no_scan and elapsed - last_scan >= scan_interval:
                    scan_registered(args.collection)
                    last_scan = elapsed
                payload = pending_summary(args.collection)
                if not payload["pending"]:
                    if args.json:
                        print(json.dumps(payload, indent=2, sort_keys=True))
                    else:
                        print("Review complete")
                    raise SystemExit(0)
                if elapsed >= timeout:
                    if args.json:
                        print(json.dumps(payload, indent=2, sort_keys=True))
                    else:
                        print("Review still pending")
                    raise SystemExit(2)
                time.sleep(min(interval, max(0.0, timeout - elapsed)))
        elif args.command == "complete":
            if not root_for(args.collection):
                raise ValueError(f"collection not found: {args.collection}")
            scan_registered(args.collection)
            state = complete_collection_review(args.collection)
            print(f"Completed {args.collection} at {state['completed_at']}")
        elif args.command == "reopen":
            if not root_for(args.collection):
                raise ValueError(f"collection not found: {args.collection}")
            state = reopen_collection_review(args.collection)
            print(f"Reopened {args.collection}")
        elif args.command == "history":
            if not root_for(args.collection):
                raise ValueError(f"collection not found: {args.collection}")
            events = review_history(args.collection, args.rel, args.limit)
            if args.json:
                print(json.dumps({"collection": args.collection, "rel": args.rel, "events": events}, indent=2, sort_keys=True))
            else:
                for event in events:
                    undone = " UNDONE" if event["undone_at"] else ""
                    print(f"{event['id']}\t{event['created_at']}\t{event['action']}\t{event['old_status'] or 'unreviewed'} -> {event['new_status'] or 'unreviewed'}{undone}")
        elif args.command == "undo":
            if not root_for(args.collection):
                raise ValueError(f"collection not found: {args.collection}")
            result = undo_last_review(args.collection, args.rel)
            if not result:
                raise ValueError("no review change to undo")
            print(f"Restored {args.rel} to {result['status'] or 'unreviewed'}")
        elif args.command == "collection-url":
            if not root_for(args.collection):
                raise ValueError(f"collection not found: {args.collection}")
            print(args.base_url.rstrip('/') + "/c/" + args.collection)
        elif args.command == "asset-url":
            if not root_for(args.collection):
                raise ValueError(f"collection not found: {args.collection}")
            asset = resolve_asset(args.collection, args.asset)
            base = args.base_url.rstrip('/')
            print(base + "/c/" + args.collection + "?asset=" + asset["asset_id"])
        elif args.command == "families":
            payload = {"version": 1, "collection": args.collection, "families": families_for_collection(args.collection, include_missing=not args.present_only)}
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                for family in payload["families"]:
                    preferred = family["preferred_asset_id"] or "-"
                    print(f"{family['family_id']}\t{family['name']}\tmembers={len(family['members'])}\tpreferred={preferred}")
        elif args.command == "family-create":
            assets = [resolve_asset(args.collection, asset) for asset in args.assets]
            preferred = resolve_asset(args.collection, args.preferred)["asset_id"] if args.preferred else None
            family = create_family(args.collection, args.name, [asset["asset_id"] for asset in assets], preferred)
            if args.json:
                print(json.dumps(family, indent=2, sort_keys=True))
            else:
                print(f"Created family {family['name']} ({family['family_id']}) with {len(family['members'])} members")
        elif args.command == "family-add":
            assets = [resolve_asset(args.collection, asset) for asset in args.assets]
            family = add_family_members(args.collection, args.family_id, [asset["asset_id"] for asset in assets])
            if args.json:
                print(json.dumps(family, indent=2, sort_keys=True))
            else:
                print(f"Updated family {family['name']}: {len(family['members'])} members")
        elif args.command == "family-remove":
            assets = [resolve_asset(args.collection, asset) for asset in args.assets]
            family = remove_family_members(args.collection, args.family_id, [asset["asset_id"] for asset in assets])
            if args.json:
                print(json.dumps(family, indent=2, sort_keys=True))
            else:
                print("Family deleted (last member removed)" if family is None else f"Updated family {family['name']}: {len(family['members'])} members")
        elif args.command == "family-prefer":
            asset_id = resolve_asset(args.collection, args.asset)["asset_id"] if args.asset else None
            family = set_family_preferred(args.collection, args.family_id, asset_id)
            if args.json:
                print(json.dumps(family, indent=2, sort_keys=True))
            else:
                print(f"Preferred member for {family['name']}: {family['preferred_asset_id'] or '-'}")
        elif args.command == "family-rename":
            family = rename_family(args.collection, args.family_id, args.name)
            if args.json:
                print(json.dumps(family, indent=2, sort_keys=True))
            else:
                print(f"Renamed family to {family['name']}")
        elif args.command == "family-delete":
            family = delete_family(args.collection, args.family_id)
            print(f"Deleted family metadata {family['name']} ({family['family_id']}); source files were untouched")
        elif args.command == "annotations":
            asset = resolve_asset(args.collection, args.asset)
            rows = annotations_for_asset(args.collection, asset["asset_id"], include_stale=not args.active_only)
            if args.json:
                print(json.dumps({"version": 1, "collection": args.collection, "asset_id": asset["asset_id"], "annotations": rows}, indent=2, sort_keys=True))
            else:
                for annotation in rows:
                    marker = "stale" if annotation["stale"] else ("resolved" if annotation["resolved"] else "open")
                    print(f"{annotation['annotation_id']}\t{annotation['kind']}\t{marker}\t{annotation['text']}")
        elif args.command == "annotate":
            asset = resolve_asset(args.collection, args.asset)
            annotation = create_annotation(
                args.collection, asset["asset_id"], args.kind, args.x, args.y,
                w=args.width, h=args.height, text=args.text,
            )
            if args.json:
                print(json.dumps(annotation, indent=2, sort_keys=True))
            else:
                print(f"Created {annotation['kind']} annotation {annotation['annotation_id']}")
        elif args.command == "annotation-update":
            resolved = True if args.resolve else False if args.reopen else None
            annotation = update_annotation(args.collection, args.annotation_id, text=args.text, resolved=resolved)
            if args.json:
                print(json.dumps(annotation, indent=2, sort_keys=True))
            else:
                print(f"Updated annotation {annotation['annotation_id']}")
        elif args.command == "annotation-delete":
            annotation = delete_annotation(args.collection, args.annotation_id)
            print(f"Deleted annotation {annotation['annotation_id']}")
        elif args.command == "cache":
            if args.action == "prune":
                payload = prune_preview_cache(max(0, args.max_mb) * 1024 * 1024, max(0, args.max_age_days) * 86400)
            else:
                payload = preview_cache_status()
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(f"files={payload['files']} bytes={payload['bytes']} path={payload['path']}")
                if 'removed_files' in payload:
                    print(f"removed_files={payload['removed_files']} removed_bytes={payload['removed_bytes']}")
        elif args.command == "watch":
            from .watcher import start_collection_watcher
            watcher = start_collection_watcher(
                scan_collection, debounce_seconds=args.debounce, reconcile_interval=args.reconcile_interval
            )
            print("Watching registered Asset Viewer collections. Press Ctrl+C to stop.")
            try:
                while True:
                    time.sleep(3600)
            except KeyboardInterrupt:
                pass
            finally:
                watcher.stop()
        elif args.command == "mcp":
            run_mcp(args.transport, host=args.host, port=args.port)
        elif args.command == "serve":
            if args.development_server:
                development_serve(
                    args.host, args.port, args.trusted_host, log_level=args.log_level,
                    allow_unauthenticated_remote=args.allow_unauthenticated_remote,
                    watch=args.watch, watch_debounce=args.watch_debounce,
                    watch_reconcile_interval=args.watch_reconcile_interval,
                )
            else:
                production_serve(
                    args.host, args.port, args.trusted_host, log_level=args.log_level,
                    allow_unauthenticated_remote=args.allow_unauthenticated_remote, threads=args.http_threads,
                    watch=args.watch, watch_debounce=args.watch_debounce,
                    watch_reconcile_interval=args.watch_reconcile_interval,
                )
        elif args.command == "doctor":
            raise SystemExit(doctor(args.host, args.trusted_host))
        elif args.command == "demo":
            path = Path(args.path).expanduser().resolve()
            create_demo(path)
            row = add_collection(str(path), DEMO_COLLECTION_LABEL)
            scan_collection(row["slug"], force=True)
            manifest = review_manifest(row["slug"])
            current = {item["rel"]: item.get("status") for item in manifest["items"]}
            for rel, (status, comment) in DEMO_REVIEWS.items():
                if not current.get(rel):
                    set_review(row["slug"], rel, status, comment)
            records = {item["rel"]: item for item in catalog_records(row["slug"], present_only=True)}
            for rel, record in records.items():
                set_asset_metadata(row["slug"], record["asset_id"], {
                    "source_project": DEMO_PROJECT,
                    "tool": "asset-viewer-demo",
                    "model": "synthetic-demo",
                    "run_id": "summer-launch",
                })
            family_name, members, preferred = DEMO_FAMILY
            if not any(family["name"] == family_name for family in families_for_collection(row["slug"])):
                member_ids = [records[rel]["asset_id"] for rel in members]
                create_family(row["slug"], family_name, member_ids, records[preferred]["asset_id"])
            print(f"Created and registered demo collection: {row['path']}")
            print("Seeded a realistic campaign review with approvals, feedback, and A/B hero variants.")
            print("Run: asset-viewer serve")
        else:
            parser.print_help()
    except (ValueError, OSError, RuntimeError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()

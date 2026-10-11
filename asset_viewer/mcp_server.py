from __future__ import annotations

import json
from typing import Any

from .app import capability_document, scan_collection
from .activity import activity_feed
from .handoff import approved_handoff
from .project_config import load_project_config
from .project_sync import sync_project
from . import production
from .storage import (
    annotations_for_asset,
    asset_metadata,
    set_asset_metadata,
    catalog_records,
    catalog_state,
    collection_review_state,
    collections as registered_collections,
    families_for_collection,
    pending_summary,
    review_events_since,
    review_manifest,
)


def _scan(scope: str | None) -> None:
    rows = registered_collections()
    if scope:
        rows = [row for row in rows if row["slug"] == scope]
        if not rows:
            raise ValueError(f"collection not found: {scope}")
    for row in rows:
        scan_collection(row["slug"], force=True)


def list_collections() -> dict[str, Any]:
    items = []
    for row in registered_collections():
        slug = row["slug"]
        items.append({
            "slug": slug,
            "label": row["label"],
            "group": row.get("group", ""),
            "available": bool(row.get("available")),
            "catalog": catalog_state(slug),
            "review": collection_review_state(slug),
        })
    return {"version": 1, "collections": items}


def get_reviews(
    collection: str | None = None,
    status: str | None = None,
    present_only: bool = True,
    refresh: bool = False,
) -> dict[str, Any]:
    """Return durable human review state without changing review decisions."""
    if refresh:
        _scan(collection)
    return review_manifest(collection, status, include_missing=not present_only)


def get_pending(collection: str | None = None, refresh: bool = False) -> dict[str, Any]:
    """Return whether human review is explicitly complete."""
    if refresh:
        _scan(collection)
    return pending_summary(collection)


def get_events(collection: str | None = None, after_id: int = 0, limit: int = 100) -> dict[str, Any]:
    """Read ordered review and filesystem events after a durable cursor."""
    return review_events_since(collection, max(0, int(after_id)), max(1, min(int(limit), 1000)))


def get_families(collection: str, present_only: bool = True) -> dict[str, Any]:
    """Return variant/version families and preferred members."""
    return {
        "version": 1,
        "collection": collection,
        "families": families_for_collection(collection, include_missing=not present_only),
    }


def get_annotations(collection: str, asset: str, include_stale: bool = False) -> dict[str, Any]:
    """Return point/region feedback for a stable asset id or relative path."""
    records = catalog_records(collection, present_only=False)
    record = next((row for row in records if row["asset_id"] == asset or row["rel"] == asset), None)
    if not record:
        raise ValueError(f"asset not found: {asset}")
    return {
        "version": 1,
        "collection": collection,
        "asset_id": record["asset_id"],
        "annotations": annotations_for_asset(collection, record["asset_id"], include_stale=include_stale),
    }


def get_collection_url(collection: str, base_url: str = "http://127.0.0.1:8160") -> str:
    """Return the stable browser handoff URL for a collection."""
    if not any(row["slug"] == collection for row in registered_collections()):
        raise ValueError(f"collection not found: {collection}")
    return base_url.rstrip("/") + "/c/" + collection


def get_asset_url(collection: str, asset: str, base_url: str = "http://127.0.0.1:8160") -> str:
    """Return a browser deep link using the durable asset UUID."""
    record = next(
        (row for row in catalog_records(collection, present_only=False) if row["asset_id"] == asset or row["rel"] == asset),
        None,
    )
    if not record:
        raise ValueError(f"asset not found: {asset}")
    return base_url.rstrip("/") + f"/c/{collection}?asset={record['asset_id']}"


def get_activity(collection: str | None = None, after_id: int = 0, limit: int = 200) -> dict[str, Any]:
    """Return human-readable activity backed by the durable event cursor."""
    return activity_feed(collection, after_id, limit)


def get_provenance(collection: str, asset: str) -> dict[str, Any]:
    """Return optional generation/source metadata for one stable asset."""
    return asset_metadata(collection, asset)


def set_provenance(
    collection: str, asset: str, source_project: str = "", tool: str = "", agent: str = "",
    model: str = "", prompt: str = "", seed: str = "", run_id: str = "", git_commit: str = "",
    parent_asset_id: str = "", extra_json: str = "{}",
) -> dict[str, Any]:
    """Attach optional provenance metadata without changing the source image."""
    extra = json.loads(extra_json or "{}")
    if not isinstance(extra, dict):
        raise ValueError("extra_json must decode to an object")
    return set_asset_metadata(collection, asset, {
        "source_project": source_project, "tool": tool, "agent": agent, "model": model, "prompt": prompt,
        "seed": seed, "run_id": run_id, "git_commit": git_commit, "parent_asset_id": parent_asset_id, "extra": extra,
    })


def get_approved_handoff(collection: str) -> dict[str, Any]:
    """Return the exact human-approved handoff manifest without copying or moving originals."""
    return approved_handoff(collection)


def inspect_project_config(path: str = ".") -> dict[str, Any]:
    """Inspect the nearest project-owned .asset-viewer.toml without changing viewer state."""
    return load_project_config(path)


def sync_project_config(path: str = ".", dry_run: bool = True) -> dict[str, Any]:
    """Register project-declared review folders and import metadata; dry-run is the safe default."""
    return sync_project(path, dry_run=dry_run)


def refresh_collections(collection: str | None = None) -> dict[str, Any]:
    """Run the normal bounded filesystem reconciliation and return catalog state."""
    _scan(collection)
    rows = registered_collections()
    if collection:
        rows = [row for row in rows if row["slug"] == collection]
    return {"version": 1, "collections": [catalog_state(row["slug"]) for row in rows]}


def get_production_asset(asset: str) -> dict[str, Any]:
    """Read one production asset: views, revisions, design lock, stages, next step."""
    production.discover(asset, actor="agent")
    return production.asset_detail(asset)


def get_design_lock(asset: str) -> dict[str, Any]:
    """The verified design lock an implementation may build from. Never build from unlocked candidates."""
    return production.design_lock(asset)


def attach_design_revision(asset: str, view: str, collection: str, rel: str, note: str = "") -> dict[str, Any]:
    """Attach a catalog file as the next revision of one design view (v1, v2 ...)."""
    _scan(collection)
    return production.attach_revision(asset, view, collection, rel=rel, note=note, origin="agent", actor="agent")


def attach_stage_evidence(
    asset: str, stage: str, collection: str | None = None, rel: str | None = None, source: str = "", caption: str = "",
) -> dict[str, Any]:
    """Attach evidence (screenshot, video, timelapse, or a source reference) to a production stage."""
    if collection:
        _scan(collection)
    return production.attach_evidence(asset, stage, collection=collection, rel=rel, source=source, caption=caption, origin="agent", actor="agent")


def report_stage_progress(asset: str, stage: str, status: str | None = None, summary: str | None = None, next_step: str | None = None) -> dict[str, Any]:
    """Report stage progress as todo, wip or ready. Acceptance is reserved for the owner."""
    return production.set_stage(asset, stage, status=status, summary=summary, next_step=next_step, actor="agent")


def cull_revision(revision_id: str, reason: str) -> dict[str, Any]:
    """Hide an obviously unusable candidate revision, with a reason. Selected or locked revisions are refused."""
    return production.cull_revision(revision_id, reason, actor="agent")


def create_mcp_server():
    try:
        from mcp.server import MCPServer
    except ImportError as exc:  # pragma: no cover - exercised by CLI integration
        raise RuntimeError('MCP support requires: pip install "local-asset-viewer[mcp]"') from exc

    server = MCPServer("Asset Viewer")
    server.tool()(list_collections)
    server.tool()(get_reviews)
    server.tool()(get_pending)
    server.tool()(get_events)
    server.tool()(get_families)
    server.tool()(get_annotations)
    server.tool()(get_collection_url)
    server.tool()(get_asset_url)
    server.tool()(get_activity)
    server.tool()(get_provenance)
    server.tool()(set_provenance)
    server.tool()(get_approved_handoff)
    server.tool()(inspect_project_config)
    server.tool()(sync_project_config)
    server.tool()(refresh_collections)
    server.tool()(get_production_asset)
    server.tool()(get_design_lock)
    server.tool()(attach_design_revision)
    server.tool()(attach_stage_evidence)
    server.tool()(report_stage_progress)
    server.tool()(cull_revision)

    @server.resource("asset-viewer://capabilities")
    def capabilities_resource() -> str:
        return json.dumps(capability_document(), indent=2, sort_keys=True)

    @server.resource("asset-viewer://collections")
    def collections_resource() -> str:
        return json.dumps(list_collections(), indent=2, sort_keys=True)

    @server.resource("asset-viewer://pending")
    def pending_resource() -> str:
        return json.dumps(get_pending(), indent=2, sort_keys=True)

    return server


def run_mcp(
    transport: str = "stdio",
    *,
    host: str = "127.0.0.1",
    port: int = 8161,
) -> None:
    server = create_mcp_server()
    if transport == "stdio":
        server.run("stdio")
        return
    if transport != "streamable-http":
        raise ValueError("MCP transport must be stdio or streamable-http")
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Asset Viewer MCP HTTP currently permits loopback binds only; use a trusted local/private bridge")
    server.run("streamable-http", host=host, port=port, stateless_http=True, json_response=True)


def main() -> None:
    run_mcp()


if __name__ == "__main__":
    main()

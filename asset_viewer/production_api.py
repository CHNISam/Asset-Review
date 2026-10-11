"""HTTP routes for production objects, shared by the built-in and WSGI servers.

Browser requests are the owner: they arrive with the session CSRF token and
may select revisions, lock designs, accept stages and clean up. Agents use the
CLI/MCP instead, which only exposes progress, evidence and read-only locks.
"""

from __future__ import annotations

import urllib.parse
from typing import Any

from . import production
from .storage import collections

PREFIX = "/api/production/"


def _first(query: dict[str, list[str]], name: str, default: str = "") -> str:
    return (query.get(name) or [default])[0]


def _refresh(asset_key: str) -> None:
    """Pick up files dropped into the asset folder since the last look."""
    from .app import scan_collection

    detail_source = production.source_collection(asset_key)
    if detail_source and any(row["slug"] == detail_source and row.get("available") for row in collections()):
        scan_collection(detail_source)
        production.discover(asset_key)


def meta() -> dict[str, Any]:
    return {
        "kinds": [{"kind": key, "label": value[0], "stages": value[1], "views": value[2]} for key, value in production.KINDS.items()],
        "stages": [{"stage": key, "label": value[0], "hint": value[1]} for key, value in production.STAGES.items()],
        "views": [{"view": key, "label": label} for key, label in production.VIEW_LABELS.items()],
        "collections": [{"slug": row["slug"], "label": row["label"], "group": row.get("group", "")} for row in collections()],
    }


def handle_get(path: str, query_string: str) -> tuple[int, Any] | None:
    if not path.startswith(PREFIX):
        return None
    query = urllib.parse.parse_qs(query_string)
    route = path[len(PREFIX):]
    try:
        if route == "meta":
            return 200, meta()
        if route == "projects":
            return 200, {"version": 1, "projects": production.projects()}
        if route == "overview":
            project = _first(query, "project")
            if not project:
                return 400, {"error": "project is required"}
            for key in production.asset_keys(project):
                _refresh(key)
            return 200, production.project_overview(project)
        if route == "asset":
            key = _first(query, "key")
            if not key:
                return 400, {"error": "key is required"}
            _refresh(key)
            return 200, production.asset_detail(key)
        if route == "lock":
            key = _first(query, "key")
            if not key:
                return 400, {"error": "key is required"}
            return 200, production.design_lock(key)
        if route == "cleanup":
            return 200, production.cleanup_plan(_first(query, "asset") or None)
        if route == "file":
            return 200, {"links": production.file_links(_first(query, "file_id")), "targets": production.assign_targets()}
    except ValueError as exc:
        return 404 if "not found" in str(exc) else 400, {"error": str(exc)}
    return 404, {"error": "not found"}


def handle_post(path: str, payload: dict[str, Any]) -> tuple[int, Any] | None:
    if not path.startswith(PREFIX):
        return None
    route = path[len(PREFIX):]
    key = str(payload.get("key") or "")
    action = str(payload.get("action") or "")
    try:
        if route == "asset":
            if action == "create":
                views = payload.get("views")
                if views is not None and not isinstance(views, list):
                    raise ValueError("views must be a list")
                detail = production.create_asset(
                    str(payload.get("project") or ""), str(payload.get("name") or ""),
                    scene=str(payload.get("scene") or ""), kind=str(payload.get("kind") or "prop"),
                    key=str(payload["asset_key"]) if payload.get("asset_key") else None,
                    brief=str(payload.get("brief") or ""), views=views, stages=payload.get("stages"),
                    source_collection=payload.get("source_collection") or None,
                    source_prefix=str(payload.get("source_prefix") or ""), actor="owner",
                )
                return 201, detail
            if action == "update":
                fields = {name: payload[name] for name in ("name", "scene", "kind", "brief", "stages", "source_collection", "source_prefix") if name in payload}
                return 200, production.update_asset(key, actor="owner", **fields)
        elif route == "view":
            view = str(payload.get("view") or "")
            if action == "add":
                return 200, production.add_view(key, view, payload.get("label"), required=bool(payload.get("required", True)), actor="owner")
            if action == "update":
                required = payload.get("required")
                return 200, production.update_view(key, view, label=payload.get("label"), required=None if required is None else bool(required), actor="owner")
            if action == "remove":
                return 200, production.remove_view(key, view, actor="owner")
        elif route == "select":
            return 200, production.select_revision(key, str(payload.get("view") or ""), str(payload.get("revision_id") or ""), actor="owner")
        elif route == "lock":
            return 200, production.lock_design(key, str(payload.get("note") or ""), actor="owner")
        elif route == "stage":
            return 200, production.set_stage(
                key, str(payload.get("stage") or ""), status=payload.get("status") or None,
                next_step=payload.get("next_step"), actor="owner",
            )
        elif route == "revision":
            revision_id = str(payload.get("revision_id") or "")
            if action == "restore":
                return 200, production.restore_revision(revision_id, actor="owner")
            if action == "cull":
                production.cull_revision(revision_id, str(payload.get("reason") or "不采用"), actor="owner")
                return 200, production.asset_detail(key)
            if action == "attach":
                production.attach_revision(
                    key, str(payload.get("view") or ""), str(payload.get("collection") or ""),
                    rel=payload.get("rel"), file_id=payload.get("file_id"), origin="linked", actor="owner",
                )
                return 200, production.asset_detail(key)
        elif route == "evidence":
            if action == "attach":
                production.attach_evidence(
                    key, str(payload.get("stage") or ""), collection=payload.get("collection") or None,
                    rel=payload.get("rel"), file_id=payload.get("file_id"), source=str(payload.get("source") or ""),
                    caption=str(payload.get("caption") or ""), actor="owner",
                )
                return 200, production.asset_detail(key)
            if action == "remove":
                return 200, production.remove_evidence(str(payload.get("evidence_id") or ""), actor="owner")
        elif route == "cleanup":
            ids = payload.get("revision_ids")
            if not isinstance(ids, list) or len(ids) > 500:
                raise ValueError("revision_ids must be a list of at most 500 IDs")
            return 200, production.cleanup_execute(ids, actor="owner")
        else:
            return 404, {"error": "not found"}
        raise ValueError("unsupported action")
    except (ValueError, TypeError) as exc:
        return 400, {"ok": False, "error": str(exc)}


def handle_upload(path: str, query_string: str, filename: str, data: bytes) -> tuple[int, Any] | None:
    if path != PREFIX + "upload":
        return None
    query = urllib.parse.parse_qs(query_string)
    key = _first(query, "key")
    try:
        stored = production.store_upload(
            key, filename=urllib.parse.unquote(filename or ""), data=data,
            view=_first(query, "view") or None, stage=_first(query, "stage") or None,
        )
        from .app import scan_collection

        scan_collection(stored["collection"], force=True)
        view = _first(query, "view")
        stage = _first(query, "stage")
        if view:
            production.attach_revision(key, view, stored["collection"], rel=stored["rel"], origin="uploaded", actor="owner")
        else:
            production.attach_evidence(key, stage, collection=stored["collection"], rel=stored["rel"],
                                       caption=_first(query, "caption"), origin="uploaded", actor="owner")
        return 201, production.asset_detail(key)
    except (ValueError, OSError) as exc:
        return 400, {"ok": False, "error": str(exc)}

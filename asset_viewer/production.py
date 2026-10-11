"""Production objects: the asset you are making, not the folder it came from.

An asset (a concert hall, a sword, a HUD) declares the design views it needs.
Each view keeps its own revisions (v1, v2, v3 ...), each pointing at a file
that stays in its registered folder. The owner selects one revision per view
and confirms the set as a design lock (D1, D2 ...): the exact bytes a coding
agent may build from. Later stages (model, UV, texture, rig, engine) collect
evidence and move through progress set by agents and acceptance set by the
owner, so visual approval, technical readiness and runtime acceptance stay
separate gates.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
import urllib.parse
from pathlib import Path
from typing import Any, Iterable

from .storage import (
    ASSET_EXTS,
    VIDEO_EXTS,
    _connection,
    collections,
    file_sha256,
    root_for,
    safe_file,
    slugify,
    utc_now,
)

TRASH_DIR = ".asset-review-trash"

STAGES: dict[str, tuple[str, str]] = {
    "design": ("设计", "各个视图的设计稿，选定后确认为整套定稿"),
    "model": ("建模", "把设计变成三维形体"),
    "uv": ("UV", "展开模型表面，方便上贴图"),
    "texture": ("材质", "颜色、质感与贴图"),
    "rig": ("绑定动画", "让模型能动起来"),
    "engine": ("实机", "在游戏引擎里的真实效果"),
}
STAGE_ORDER = list(STAGES)
TECH_STAGES = ("model", "uv", "texture", "rig")
KINDS: dict[str, tuple[str, list[str], list[str]]] = {
    # kind: (label, default stages, suggested views)
    "environment": ("场景建筑", ["design", "model", "uv", "texture", "engine"], ["front", "side", "top", "detail"]),
    "prop": ("道具", ["design", "model", "uv", "texture", "engine"], ["front", "side", "back"]),
    "character": ("角色", ["design", "model", "uv", "texture", "rig", "engine"], ["front", "side", "back"]),
    "ui": ("界面", ["design", "engine"], ["main"]),
    "vfx": ("特效", ["design", "engine"], ["main"]),
    "2d": ("2D 美术", ["design", "engine"], ["main"]),
}
VIEW_LABELS = {
    "front": "正面", "side": "侧面", "back": "背面", "top": "俯视", "bottom": "仰视",
    "three-quarter": "斜侧", "detail": "局部细节", "main": "主图", "plan": "平面", "section": "剖面",
}
VIEW_ALIASES = {label: key for key, label in VIEW_LABELS.items()} | {
    "正立面": "front", "立面": "front", "侧立面": "side", "顶视": "top", "俯视图": "top", "细节": "detail",
    "局部": "detail", "主视图": "main", "34": "three-quarter", "3-4": "three-quarter",
}
STAGE_ALIASES = {
    "design": "design", "设计": "design", "concept": "design", "views": "design",
    "model": "model", "modeling": "model", "mesh": "model", "建模": "model", "模型": "model", "blockout": "model",
    "uv": "uv", "uvs": "uv", "展uv": "uv",
    "texture": "texture", "textures": "texture", "material": "texture", "materials": "texture",
    "lookdev": "texture", "材质": "texture", "贴图": "texture",
    "rig": "rig", "rigging": "rig", "anim": "rig", "animation": "rig", "绑定": "rig", "动画": "rig",
    "engine": "engine", "runtime": "engine", "godot": "engine", "unity": "engine", "unreal": "engine",
    "ingame": "engine", "实机": "engine", "引擎": "engine",
}
AGENT_STAGE_STATUSES = {"todo", "wip", "ready"}
OWNER_STAGE_STATUSES = {"accepted", "wip"}
_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_VERSION_RE = re.compile(r"(?:^|[\W_])v(\d{1,3})(?=[\W_]|$)", re.IGNORECASE)
_DRAFT_RE = re.compile(r"(?:^|[\W_])(tmp|temp|draft|scratch|wip|test)(?=[\W_]|$)|草稿|临时", re.IGNORECASE)
MIN_DESIGN_SIDE = 256
MAX_UPLOAD_BYTES = 200 * 1024 * 1024


# --------------------------------------------------------------------------- helpers

def _event(conn: Any, asset_key: str, action: str, actor: str, details: dict[str, Any] | None = None, now: str | None = None) -> None:
    conn.execute(
        "INSERT INTO prod_events(asset_key, action, actor, details, created_at) VALUES(?, ?, ?, ?, ?)",
        (asset_key, action, actor, json.dumps(details or {}, ensure_ascii=False, sort_keys=True), now or utc_now()),
    )


def _media(collection: str, rel: str) -> dict[str, Any]:
    quoted = urllib.parse.quote(rel, safe="/")
    return {
        "media_type": "video" if Path(rel).suffix.lower() in VIDEO_EXTS else "image",
        "thumb": f"/asset/thumb/{collection}/{quoted}",
        "preview": f"/asset/preview/{collection}/{quoted}",
        "file": f"/asset/file/{collection}/{quoted}",
    }


def _clean_text(value: Any, field: str, maximum: int = 4000) -> str:
    text = str(value or "").strip()
    if len(text) > maximum:
        raise ValueError(f"{field} must be {maximum} characters or fewer")
    return text


def _view_key(value: str) -> str:
    raw = str(value or "").strip()
    if raw in VIEW_ALIASES:
        return VIEW_ALIASES[raw]
    lowered = raw.lower().replace("_", "-").replace(" ", "-")
    if lowered in VIEW_ALIASES:
        return VIEW_ALIASES[lowered]
    if _KEY_RE.match(lowered):
        return lowered
    slug = slugify(raw)
    if slug != "collection" and _KEY_RE.match(slug):
        return slug
    if raw and len(raw) <= 60 and not any(char in raw for char in "/\\"):
        # Names like 门厅细节 get a stable key; the name stays the label.
        return "view-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]  # nosec B324 - identifier, not security
    raise ValueError(f"invalid view key: {value!r}")


def _stage_list(kind: str, stages: Iterable[str] | None) -> list[str]:
    chosen = list(stages) if stages is not None else KINDS.get(kind, KINDS["prop"])[1]
    normalized = []
    for stage in chosen:
        key = STAGE_ALIASES.get(str(stage).strip().lower(), str(stage).strip().lower())
        if key not in STAGES:
            raise ValueError(f"unknown stage: {stage}")
        normalized.append(key)
    normalized = [stage for stage in STAGE_ORDER if stage in set(normalized) | {"design"}]
    return normalized


def _asset_row(conn: Any, asset_key: str) -> Any:
    row = conn.execute("SELECT * FROM prod_assets WHERE asset_key=?", (asset_key,)).fetchone()
    if not row:
        raise ValueError(f"asset not found: {asset_key}")
    return row


def _catalog_row(conn: Any, collection: str, rel: str | None = None, file_id: str | None = None) -> Any:
    if file_id:
        row = conn.execute("SELECT * FROM assets WHERE asset_id=? AND collection=?", (file_id, collection)).fetchone()
    else:
        row = conn.execute("SELECT * FROM assets WHERE collection=? AND rel=?", (collection, rel or "")).fetchone()
    if not row or not row["present"]:
        raise ValueError("file is not in the catalog; scan the collection first")
    return row


def _parse_version(rel: str) -> int | None:
    match = _VERSION_RE.search(Path(rel).stem)
    return int(match.group(1)) if match else None


def _triage(rel: str, width: int, height: int, sha256: str, siblings: dict[str, int]) -> str:
    """Return a reason when a new revision is obviously not worth an owner's time."""
    if _DRAFT_RE.search(Path(rel).stem):
        return "临时草稿"
    if width and height and min(width, height) < MIN_DESIGN_SIDE:
        return f"分辨率过低（{width}×{height}）"
    if sha256 in siblings:
        return f"与 v{siblings[sha256]} 完全相同"
    return ""


# --------------------------------------------------------------------------- assets

def projects() -> list[dict[str, Any]]:
    counts: dict[str, dict[str, Any]] = {}
    with _connection() as conn:
        for row in conn.execute("SELECT project, COUNT(*) AS n FROM prod_assets GROUP BY project"):
            counts.setdefault(row["project"], {"name": row["project"], "assets": 0, "collections": 0})["assets"] = int(row["n"])
    for row in collections():
        name = (row.get("group") or "").split("/")[0].strip() or "未分组"
        counts.setdefault(name, {"name": name, "assets": 0, "collections": 0})["collections"] += 1
    return sorted(counts.values(), key=lambda item: (-item["assets"], item["name"].lower()))


def create_asset(
    project: str,
    name: str,
    *,
    scene: str = "",
    kind: str = "prop",
    key: str | None = None,
    brief: str = "",
    views: Iterable[dict[str, Any] | str] | None = None,
    stages: Iterable[str] | None = None,
    source_collection: str | None = None,
    source_prefix: str = "",
    actor: str = "agent",
) -> dict[str, Any]:
    project = _clean_text(project, "project", 200)
    name = _clean_text(name, "name", 200)
    if not project or not name:
        raise ValueError("project and name are required")
    if kind not in KINDS:
        raise ValueError(f"kind must be one of: {', '.join(KINDS)}")
    if source_collection and not any(row["slug"] == source_collection for row in collections()):
        raise ValueError(f"collection not found: {source_collection}")
    prefix = str(source_prefix or "").strip().strip("/")
    if prefix and any(part in {"", ".", ".."} for part in prefix.split("/")):
        raise ValueError("source prefix must be a relative folder")
    stage_list = _stage_list(kind, stages)
    view_rows = []
    for index, view in enumerate(views if views is not None else KINDS[kind][2]):
        spec = {"key": view} if isinstance(view, str) else dict(view)
        view_key = _view_key(spec.get("key") or spec.get("label") or "")
        label = _clean_text(spec.get("label") or VIEW_LABELS.get(view_key, view_key), "view label", 60)
        view_rows.append((view_key, label, 1 if spec.get("required", True) else 0, index))
    if len({row[0] for row in view_rows}) != len(view_rows):
        raise ValueError("view keys must be unique")
    now = utc_now()
    with _connection() as conn:
        if key:
            if not _KEY_RE.match(key):
                raise ValueError("asset key must use lowercase letters, digits and dashes")
            asset_key = key
        else:
            base = slugify(name)
            asset_key = base if base != "collection" else f"asset-{uuid.uuid4().hex[:6]}"
            n = 2
            while conn.execute("SELECT 1 FROM prod_assets WHERE asset_key=?", (asset_key,)).fetchone():
                asset_key = f"{base}-{n}"
                n += 1
        if conn.execute("SELECT 1 FROM prod_assets WHERE asset_key=?", (asset_key,)).fetchone():
            raise ValueError(f"asset already exists: {asset_key}")
        sort = conn.execute("SELECT COALESCE(MAX(sort), 0) + 1 AS n FROM prod_assets WHERE project=?", (project,)).fetchone()["n"]
        conn.execute(
            """INSERT INTO prod_assets(asset_key, project, scene, name, kind, brief, stages, source_collection,
                   source_prefix, sort, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (asset_key, project, _clean_text(scene, "scene", 200), name, kind, _clean_text(brief, "brief"),
             json.dumps(stage_list), source_collection, prefix, sort, now, now),
        )
        conn.executemany(
            "INSERT INTO prod_views(asset_key, view_key, label, required, sort) VALUES(?, ?, ?, ?, ?)",
            [(asset_key, *row) for row in view_rows],
        )
        _event(conn, asset_key, "asset_created", actor, {"name": name, "views": [row[0] for row in view_rows]}, now)
    return asset_detail(asset_key)


_UPDATE_ASSET_FIELD = {
    "name": "UPDATE prod_assets SET name=?, updated_at=? WHERE asset_key=?",
    "scene": "UPDATE prod_assets SET scene=?, updated_at=? WHERE asset_key=?",
    "kind": "UPDATE prod_assets SET kind=?, updated_at=? WHERE asset_key=?",
    "brief": "UPDATE prod_assets SET brief=?, updated_at=? WHERE asset_key=?",
    "stages": "UPDATE prod_assets SET stages=?, updated_at=? WHERE asset_key=?",
    "source_collection": "UPDATE prod_assets SET source_collection=?, updated_at=? WHERE asset_key=?",
    "source_prefix": "UPDATE prod_assets SET source_prefix=?, updated_at=? WHERE asset_key=?",
    "project": "UPDATE prod_assets SET project=?, updated_at=? WHERE asset_key=?",
}


def update_asset(asset_key: str, *, actor: str = "agent", **fields: Any) -> dict[str, Any]:
    allowed = {"name", "scene", "kind", "brief", "stages", "source_collection", "source_prefix", "project"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError("unsupported asset field(s): " + ", ".join(sorted(unknown)))
    now = utc_now()
    with _connection() as conn:
        row = _asset_row(conn, asset_key)
        values: dict[str, Any] = {}
        for field, value in fields.items():
            if value is None:
                continue
            if field == "kind":
                if value not in KINDS:
                    raise ValueError(f"kind must be one of: {', '.join(KINDS)}")
                values[field] = value
            elif field == "stages":
                values[field] = json.dumps(_stage_list(fields.get("kind") or row["kind"], value))
            elif field == "source_collection":
                if value and not any(item["slug"] == value for item in collections()):
                    raise ValueError(f"collection not found: {value}")
                values[field] = value or None
            elif field == "source_prefix":
                values[field] = str(value).strip().strip("/")
            else:
                values[field] = _clean_text(value, field, 4000 if field == "brief" else 200)
                if field in {"name", "project"} and not values[field]:
                    raise ValueError(f"{field} must not be empty")
        for field, value in values.items():
            conn.execute(_UPDATE_ASSET_FIELD[field], (value, now, asset_key))
        if values:
            _event(conn, asset_key, "asset_updated", actor, {"fields": sorted(values)}, now)
    return asset_detail(asset_key)


def add_view(asset_key: str, view_key: str, label: str | None = None, *, required: bool = True, actor: str = "agent") -> dict[str, Any]:
    key = _view_key(view_key)
    clean = _clean_text(label or VIEW_LABELS.get(key, key), "view label", 60)
    now = utc_now()
    with _connection() as conn:
        _asset_row(conn, asset_key)
        if conn.execute("SELECT 1 FROM prod_views WHERE asset_key=? AND view_key=?", (asset_key, key)).fetchone():
            raise ValueError(f"view already exists: {key}")
        sort = conn.execute("SELECT COALESCE(MAX(sort), -1) + 1 AS n FROM prod_views WHERE asset_key=?", (asset_key,)).fetchone()["n"]
        conn.execute(
            "INSERT INTO prod_views(asset_key, view_key, label, required, sort) VALUES(?, ?, ?, ?, ?)",
            (asset_key, key, clean, int(bool(required)), sort),
        )
        _event(conn, asset_key, "view_added", actor, {"view": key, "label": clean, "required": bool(required)}, now)
    return asset_detail(asset_key)


def update_view(asset_key: str, view_key: str, *, label: str | None = None, required: bool | None = None, actor: str = "agent") -> dict[str, Any]:
    now = utc_now()
    with _connection() as conn:
        row = conn.execute("SELECT * FROM prod_views WHERE asset_key=? AND view_key=?", (asset_key, view_key)).fetchone()
        if not row:
            raise ValueError(f"view not found: {view_key}")
        new_label = _clean_text(label, "view label", 60) if label is not None else row["label"]
        new_required = int(bool(required)) if required is not None else row["required"]
        conn.execute("UPDATE prod_views SET label=?, required=? WHERE asset_key=? AND view_key=?", (new_label or row["label"], new_required, asset_key, view_key))
        _event(conn, asset_key, "view_updated", actor, {"view": view_key, "label": new_label, "required": bool(new_required)}, now)
    return asset_detail(asset_key)


def remove_view(asset_key: str, view_key: str, *, actor: str = "owner") -> dict[str, Any]:
    with _connection() as conn:
        used = conn.execute("SELECT COUNT(*) AS n FROM prod_revisions WHERE asset_key=? AND view_key=?", (asset_key, view_key)).fetchone()["n"]
        if used:
            raise ValueError("this view already has revisions; mark it optional instead of removing it")
        deleted = conn.execute("DELETE FROM prod_views WHERE asset_key=? AND view_key=?", (asset_key, view_key)).rowcount
        if not deleted:
            raise ValueError(f"view not found: {view_key}")
        _event(conn, asset_key, "view_removed", actor, {"view": view_key})
    return asset_detail(asset_key)


# --------------------------------------------------------------------------- revisions

def _insert_revision(conn: Any, asset_key: str, view_key: str, file_row: Any, *, note: str, origin: str, now: str, actor: str) -> dict[str, Any]:
    if conn.execute("SELECT 1 FROM prod_revisions WHERE file_id=? AND state<>'archived'", (file_row["asset_id"],)).fetchone():
        raise ValueError("this file is already a revision")
    if conn.execute("SELECT 1 FROM prod_evidence WHERE file_id=?", (file_row["asset_id"],)).fetchone():
        raise ValueError("this file is already stage evidence")
    source = safe_file(file_row["collection"], file_row["rel"])
    if not source:
        raise ValueError("file is unavailable")
    sha256 = file_sha256(source)
    siblings = {
        row["sha256"]: int(row["number"])
        for row in conn.execute("SELECT sha256, number FROM prod_revisions WHERE asset_key=? AND view_key=?", (asset_key, view_key))
    }
    used = {int(n) for n in siblings.values()} | {
        int(row["number"]) for row in conn.execute("SELECT number FROM prod_revisions WHERE asset_key=? AND view_key=?", (asset_key, view_key))
    }
    wanted = _parse_version(file_row["rel"])
    number = wanted if wanted and wanted not in used else (max(used) + 1 if used else 1)
    reason = _triage(file_row["rel"], int(file_row["width"] or 0), int(file_row["height"] or 0), sha256, siblings)
    revision_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO prod_revisions(revision_id, asset_key, view_key, number, collection, file_id, rel, sha256, note,
               origin, state, state_reason, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (revision_id, asset_key, view_key, number, file_row["collection"], file_row["asset_id"], file_row["rel"], sha256,
         note, origin, "culled" if reason else "candidate", reason, now, now),
    )
    _event(conn, asset_key, "revision_added", actor, {"view": view_key, "number": number, "rel": file_row["rel"], "culled": reason}, now)
    return {"revision_id": revision_id, "view": view_key, "number": number, "culled": reason}


def attach_revision(
    asset_key: str, view_key: str, collection: str, *, rel: str | None = None, file_id: str | None = None,
    note: str = "", origin: str = "linked", actor: str = "agent",
) -> dict[str, Any]:
    now = utc_now()
    with _connection() as conn:
        _asset_row(conn, asset_key)
        if not conn.execute("SELECT 1 FROM prod_views WHERE asset_key=? AND view_key=?", (asset_key, view_key)).fetchone():
            raise ValueError(f"view not found: {view_key}")
        file_row = _catalog_row(conn, collection, rel, file_id)
        result = _insert_revision(conn, asset_key, view_key, file_row, note=_clean_text(note, "note"), origin=origin, now=now, actor=actor)
        conn.execute("UPDATE prod_assets SET updated_at=? WHERE asset_key=?", (now, asset_key))
    return result


def select_revision(asset_key: str, view_key: str, revision_id: str, *, actor: str = "owner") -> dict[str, Any]:
    now = utc_now()
    with _connection() as conn:
        view = conn.execute("SELECT * FROM prod_views WHERE asset_key=? AND view_key=?", (asset_key, view_key)).fetchone()
        if not view:
            raise ValueError(f"view not found: {view_key}")
        revision = conn.execute(
            "SELECT * FROM prod_revisions WHERE revision_id=? AND asset_key=? AND view_key=?", (revision_id, asset_key, view_key)
        ).fetchone()
        if not revision or revision["state"] == "archived":
            raise ValueError("revision not available")
        if revision["state"] == "culled":
            conn.execute("UPDATE prod_revisions SET state='candidate', state_reason='', updated_at=? WHERE revision_id=?", (now, revision_id))
        conn.execute(
            "UPDATE prod_views SET selected_revision_id=?, selected_at=? WHERE asset_key=? AND view_key=?",
            (revision_id, now, asset_key, view_key),
        )
        _event(conn, asset_key, "revision_selected", actor, {"view": view_key, "number": revision["number"], "previous": view["selected_revision_id"]}, now)
    return asset_detail(asset_key)


def cull_revision(revision_id: str, reason: str, *, actor: str = "agent") -> dict[str, Any]:
    reason = _clean_text(reason, "reason", 200) or "已筛除"
    now = utc_now()
    with _connection() as conn:
        revision = conn.execute("SELECT * FROM prod_revisions WHERE revision_id=?", (revision_id,)).fetchone()
        if not revision or revision["state"] == "archived":
            raise ValueError("revision not found")
        selected = conn.execute(
            "SELECT 1 FROM prod_views WHERE selected_revision_id=?", (revision_id,)
        ).fetchone()
        if selected or _locked_revision_ids(conn, revision["asset_key"], all_locks=True) & {revision_id}:
            raise ValueError("selected or locked revisions are owner decisions and cannot be culled")
        conn.execute("UPDATE prod_revisions SET state='culled', state_reason=?, updated_at=? WHERE revision_id=?", (reason, now, revision_id))
        _event(conn, revision["asset_key"], "revision_culled", actor, {"view": revision["view_key"], "number": revision["number"], "reason": reason}, now)
    return {"revision_id": revision_id, "state": "culled", "reason": reason}


def restore_revision(revision_id: str, *, actor: str = "owner") -> dict[str, Any]:
    now = utc_now()
    with _connection() as conn:
        revision = conn.execute("SELECT * FROM prod_revisions WHERE revision_id=?", (revision_id,)).fetchone()
        if not revision:
            raise ValueError("revision not found")
        if revision["state"] == "archived":
            _restore_archived_file(conn, revision)
        conn.execute("UPDATE prod_revisions SET state='candidate', state_reason='', trash_rel=NULL, updated_at=? WHERE revision_id=?", (now, revision_id))
        _event(conn, revision["asset_key"], "revision_restored", actor, {"view": revision["view_key"], "number": revision["number"]}, now)
    return asset_detail(str(revision["asset_key"]))


# --------------------------------------------------------------------------- design lock

def _locked_revision_ids(conn: Any, asset_key: str, *, all_locks: bool = False) -> set[str]:
    query = ("SELECT views FROM prod_locks WHERE asset_key=?" if all_locks
             else "SELECT views FROM prod_locks WHERE asset_key=? AND superseded_at IS NULL")
    ids: set[str] = set()
    for row in conn.execute(query, (asset_key,)):
        ids.update(str(view["revision_id"]) for view in json.loads(row["views"]))
    return ids


def lock_design(asset_key: str, note: str = "", *, actor: str = "owner") -> dict[str, Any]:
    now = utc_now()
    with _connection() as conn:
        _asset_row(conn, asset_key)
        views = conn.execute("SELECT * FROM prod_views WHERE asset_key=? ORDER BY sort, view_key", (asset_key,)).fetchall()
        if not views:
            raise ValueError("declare at least one design view first")
        locked = []
        for view in views:
            if not view["selected_revision_id"]:
                if view["required"]:
                    raise ValueError(f"select a revision for {view['label']} first")
                continue
            revision = conn.execute("SELECT * FROM prod_revisions WHERE revision_id=?", (view["selected_revision_id"],)).fetchone()
            source = safe_file(revision["collection"], revision["rel"]) if revision else None
            if not revision or not source:
                raise ValueError(f"the selected {view['label']} file is unavailable")
            if file_sha256(source) != revision["sha256"]:
                raise ValueError(f"the selected {view['label']} file changed since it was added; add it again as a new revision")
            locked.append({
                "view": view["view_key"], "label": view["label"], "required": bool(view["required"]),
                "revision_id": revision["revision_id"], "number": int(revision["number"]),
                "collection": revision["collection"], "file_id": revision["file_id"], "rel": revision["rel"], "sha256": revision["sha256"],
            })
        current = conn.execute("SELECT * FROM prod_locks WHERE asset_key=? AND superseded_at IS NULL", (asset_key,)).fetchone()
        if current and {v["revision_id"] for v in json.loads(current["views"])} == {v["revision_id"] for v in locked}:
            raise ValueError("the design is already locked with these revisions")
        number = conn.execute("SELECT COALESCE(MAX(number), 0) + 1 AS n FROM prod_locks WHERE asset_key=?", (asset_key,)).fetchone()["n"]
        conn.execute("UPDATE prod_locks SET superseded_at=? WHERE asset_key=? AND superseded_at IS NULL", (now, asset_key))
        conn.execute(
            "INSERT INTO prod_locks(lock_id, asset_key, number, note, views, created_at) VALUES(?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), asset_key, number, _clean_text(note, "note"), json.dumps(locked, ensure_ascii=False), now),
        )
        _event(conn, asset_key, "design_locked", actor, {"number": number, "views": {v["view"]: v["number"] for v in locked}}, now)
        conn.execute("UPDATE prod_assets SET updated_at=? WHERE asset_key=?", (now, asset_key))
    return asset_detail(asset_key)


def design_lock(asset_key: str) -> dict[str, Any]:
    """The exact, verified design a coding agent may build from."""
    with _connection() as conn:
        asset = _asset_row(conn, asset_key)
        lock = conn.execute("SELECT * FROM prod_locks WHERE asset_key=? AND superseded_at IS NULL", (asset_key,)).fetchone()
        if not lock:
            return {"version": 1, "asset": _asset_public(asset), "locked": False, "lock": None, "views": [],
                    "message": "no design lock yet; do not treat candidate revisions as implementation authority"}
        annotations: dict[str, list[dict[str, Any]]] = {}
        comments: dict[str, str] = {}
        views = json.loads(lock["views"])
        for view in views:
            catalog = conn.execute("SELECT comment FROM assets WHERE asset_id=?", (view["file_id"],)).fetchone()
            comments[view["file_id"]] = str(catalog["comment"]) if catalog else ""
            annotations[view["file_id"]] = [
                {"kind": row["kind"], "x": row["x"], "y": row["y"], "w": row["w"], "h": row["h"], "text": row["text"], "resolved": bool(row["resolved"])}
                for row in conn.execute("SELECT * FROM annotations WHERE asset_id=? AND stale=0 ORDER BY created_at", (view["file_id"],))
            ]
        stages = _stage_rows(conn, asset)
    problems = []
    payload_views = []
    for view in views:
        source = safe_file(view["collection"], view["rel"])
        if not source:
            problems.append(f"{view['view']}: file missing")
        elif file_sha256(source) != view["sha256"]:
            problems.append(f"{view['view']}: bytes changed since lock")
        payload_views.append({
            "view": view["view"], "label": view["label"], "required": view["required"],
            "revision": f"v{view['number']}", "revision_id": view["revision_id"],
            "collection": view["collection"], "rel": view["rel"], "path": str(source) if source else None,
            "sha256": view["sha256"], "notes": comments.get(view["file_id"], ""), "annotations": annotations.get(view["file_id"], []),
        })
    return {
        "version": 1,
        "generated_at": utc_now(),
        "asset": _asset_public(asset),
        "locked": True,
        "verified": not problems,
        "problems": problems,
        "lock": {"name": f"D{lock['number']}", "number": int(lock["number"]), "created_at": lock["created_at"], "note": lock["note"]},
        "views": payload_views,
        "stages": [{"stage": s["stage"], "status": s["status"], "summary": s["summary"], "next_step": s["next_step"]} for s in stages],
    }


# --------------------------------------------------------------------------- stages and evidence

def _stage_rows(conn: Any, asset: Any) -> list[dict[str, Any]]:
    stored = {row["stage"]: dict(row) for row in conn.execute("SELECT * FROM prod_stages WHERE asset_key=?", (asset["asset_key"],))}
    rows = []
    for stage in json.loads(asset["stages"]):
        row = stored.get(stage) or {"stage": stage, "status": "todo", "summary": "", "next_step": "", "updated_by": "", "accepted_at": None, "updated_at": None}
        rows.append(row)
    return rows


def set_stage(
    asset_key: str, stage: str, *, status: str | None = None, summary: str | None = None,
    next_step: str | None = None, actor: str = "agent",
) -> dict[str, Any]:
    stage = STAGE_ALIASES.get(str(stage).lower(), str(stage).lower())
    if stage == "design":
        raise ValueError("the design stage follows revisions and design locks; it cannot be set directly")
    now = utc_now()
    with _connection() as conn:
        asset = _asset_row(conn, asset_key)
        if stage not in json.loads(asset["stages"]):
            raise ValueError(f"stage not in this asset's pipeline: {stage}")
        current = conn.execute("SELECT * FROM prod_stages WHERE asset_key=? AND stage=?", (asset_key, stage)).fetchone()
        old_status = current["status"] if current else "todo"
        new_status = status or old_status
        if status:
            allowed = OWNER_STAGE_STATUSES if actor == "owner" else AGENT_STAGE_STATUSES
            if status not in allowed:
                raise ValueError(f"{actor} may set stage status to: {', '.join(sorted(allowed))}")
            if actor != "owner" and old_status == "accepted" and status != "accepted":
                raise ValueError("an accepted stage is an owner decision; agents cannot reopen it")
            if status == "accepted":
                evidence = conn.execute("SELECT COUNT(*) AS n FROM prod_evidence WHERE asset_key=? AND stage=?", (asset_key, stage)).fetchone()["n"]
                if not evidence:
                    raise ValueError("attach at least one piece of evidence before accepting a stage")
        accepted_at = now if new_status == "accepted" and old_status != "accepted" else (current["accepted_at"] if current and new_status == "accepted" else None)
        conn.execute(
            """INSERT INTO prod_stages(asset_key, stage, status, summary, next_step, updated_by, accepted_at, updated_at)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(asset_key, stage) DO UPDATE SET status=excluded.status, summary=excluded.summary,
                   next_step=excluded.next_step, updated_by=excluded.updated_by, accepted_at=excluded.accepted_at,
                   updated_at=excluded.updated_at""",
            (asset_key, stage, new_status,
             _clean_text(summary, "summary") if summary is not None else (current["summary"] if current else ""),
             _clean_text(next_step, "next step", 500) if next_step is not None else (current["next_step"] if current else ""),
             actor, accepted_at, now),
        )
        _event(conn, asset_key, "stage_updated", actor, {"stage": stage, "old": old_status, "new": new_status,
                                                          **({"summary": summary} if summary else {}), **({"next_step": next_step} if next_step else {})}, now)
        conn.execute("UPDATE prod_assets SET updated_at=? WHERE asset_key=?", (now, asset_key))
    return asset_detail(asset_key)


def attach_evidence(
    asset_key: str, stage: str, *, collection: str | None = None, rel: str | None = None, file_id: str | None = None,
    source: str = "", caption: str = "", kind: str | None = None, origin: str = "linked", actor: str = "agent",
) -> dict[str, Any]:
    stage = STAGE_ALIASES.get(str(stage).lower(), str(stage).lower())
    now = utc_now()
    with _connection() as conn:
        asset = _asset_row(conn, asset_key)
        if stage not in json.loads(asset["stages"]):
            raise ValueError(f"stage not in this asset's pipeline: {stage}")
        file_row = None
        sha256 = None
        if collection and (rel or file_id):
            file_row = _catalog_row(conn, collection, rel, file_id)
            if conn.execute("SELECT 1 FROM prod_evidence WHERE asset_key=? AND file_id=?", (asset_key, file_row["asset_id"])).fetchone():
                raise ValueError("this file is already evidence for this asset")
            if conn.execute("SELECT 1 FROM prod_revisions WHERE file_id=? AND state<>'archived'", (file_row["asset_id"],)).fetchone():
                raise ValueError("this file is already a design revision")
            path = safe_file(collection, file_row["rel"])
            sha256 = file_sha256(path) if path else None
        elif not str(source or "").strip():
            raise ValueError("evidence needs a catalog file or a source reference")
        name = (file_row["rel"] if file_row else "").lower()
        evidence_kind = kind or (
            "timelapse" if file_row and ("timelapse" in name or "延时" in name)
            else "video" if file_row and Path(name).suffix in VIDEO_EXTS
            else "image" if file_row else "source"
        )
        if evidence_kind not in {"image", "video", "timelapse", "source"}:
            raise ValueError("evidence kind must be image, video, timelapse or source")
        evidence_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO prod_evidence(evidence_id, asset_key, stage, kind, collection, file_id, rel, sha256, source, caption, origin, created_at)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (evidence_id, asset_key, stage, evidence_kind, collection if file_row else None,
             file_row["asset_id"] if file_row else None, file_row["rel"] if file_row else None, sha256,
             _clean_text(source, "source", 2000), _clean_text(caption, "caption", 500), origin, now),
        )
        if stage != "design":
            current = conn.execute("SELECT status FROM prod_stages WHERE asset_key=? AND stage=?", (asset_key, stage)).fetchone()
            if not current:
                conn.execute(
                    "INSERT INTO prod_stages(asset_key, stage, status, updated_by, updated_at) VALUES(?, ?, 'wip', ?, ?)",
                    (asset_key, stage, actor, now),
                )
        _event(conn, asset_key, "evidence_added", actor, {"stage": stage, "kind": evidence_kind, "rel": file_row["rel"] if file_row else None, "caption": caption}, now)
        conn.execute("UPDATE prod_assets SET updated_at=? WHERE asset_key=?", (now, asset_key))
    return {"evidence_id": evidence_id, "stage": stage, "kind": evidence_kind}


def remove_evidence(evidence_id: str, *, actor: str = "owner") -> dict[str, Any]:
    with _connection() as conn:
        row = conn.execute("SELECT * FROM prod_evidence WHERE evidence_id=?", (evidence_id,)).fetchone()
        if not row:
            raise ValueError("evidence not found")
        stage = conn.execute("SELECT status FROM prod_stages WHERE asset_key=? AND stage=?", (row["asset_key"], row["stage"])).fetchone()
        if stage and stage["status"] == "accepted":
            remaining = conn.execute("SELECT COUNT(*) AS n FROM prod_evidence WHERE asset_key=? AND stage=?", (row["asset_key"], row["stage"])).fetchone()["n"]
            if remaining <= 1:
                raise ValueError("an accepted stage keeps at least one piece of evidence")
        conn.execute("DELETE FROM prod_evidence WHERE evidence_id=?", (evidence_id,))
        _event(conn, row["asset_key"], "evidence_removed", actor, {"stage": row["stage"], "rel": row["rel"], "source": row["source"]})
    return asset_detail(str(row["asset_key"]))


# --------------------------------------------------------------------------- discovery and upload

def _source_root(asset: Any) -> tuple[str, Path, str]:
    collection = asset["source_collection"]
    if not collection:
        raise ValueError("this asset has no source folder; set source_collection first")
    root = root_for(collection)
    if not root:
        raise ValueError("the asset's source folder is unavailable")
    return collection, root, str(asset["source_prefix"] or "")


def classify(rel_in_source: str, known_views: dict[str, str]) -> tuple[str, str] | None:
    """Map a path inside an asset folder to ('view', key) or ('stage', key)."""
    parts = [part for part in rel_in_source.split("/") if part]
    if len(parts) < 2:
        return None
    head = STAGE_ALIASES.get(parts[0].lower(), STAGE_ALIASES.get(parts[0]))
    if head == "design":
        if len(parts) >= 3:
            try:
                return ("view", _view_key(parts[1]))
            except ValueError:
                return None
        stem = Path(parts[1]).stem
        token = re.split(r"[_\-\s.]v\d|[_\s.]", stem, maxsplit=1)[0]
        for candidate in (token, token.lower()):
            if candidate in known_views or candidate in VIEW_ALIASES:
                return ("view", known_views.get(candidate) or VIEW_ALIASES[candidate])
        return None
    if head in STAGES:
        return ("stage", head)
    return None


def discover(asset_key: str, *, actor: str = "system") -> dict[str, Any]:
    """Attach new files that follow the asset folder convention.

    <prefix>/design/<view>/<file>  or  <prefix>/design/<view>_v3.png   → revisions
    <prefix>/<model|uv|texture|rig|engine>/<file>                     → stage evidence
    """
    added: list[dict[str, Any]] = []
    now = utc_now()
    with _connection() as conn:
        asset = _asset_row(conn, asset_key)
        if not asset["source_collection"] or not root_for(asset["source_collection"]):
            return {"asset": asset_key, "added": []}
        collection, prefix = asset["source_collection"], str(asset["source_prefix"] or "")
        stages = set(json.loads(asset["stages"]))
        views = {row["view_key"]: row["view_key"] for row in conn.execute("SELECT view_key, label FROM prod_views WHERE asset_key=?", (asset_key,))}
        views |= {row["label"]: row["view_key"] for row in conn.execute("SELECT view_key, label FROM prod_views WHERE asset_key=?", (asset_key,))}
        linked = {row["file_id"] for row in conn.execute("SELECT file_id FROM prod_revisions WHERE collection=?", (collection,))}
        linked |= {row["file_id"] for row in conn.execute("SELECT file_id FROM prod_evidence WHERE collection=?", (collection,))}
        rows = conn.execute(
            "SELECT * FROM assets WHERE collection=? AND present=1 ORDER BY COALESCE(mtime_ns, 0), rel", (collection,)
        ).fetchall()
        candidates = []
        for row in rows:
            if row["asset_id"] in linked:
                continue
            rel = str(row["rel"])
            if prefix:
                if not rel.startswith(prefix + "/"):
                    continue
                inner = rel[len(prefix) + 1:]
            else:
                inner = rel
            target = classify(inner, views)
            if target:
                candidates.append((target, row))
        # Explicit vN first so numbering follows the artist's names, then by time.
        candidates.sort(key=lambda item: (_parse_version(item[1]["rel"]) or 10_000, item[1]["mtime_ns"] or 0))
        for (kind, key), row in candidates:
            if kind == "view":
                if key not in views.values():
                    sort = conn.execute("SELECT COALESCE(MAX(sort), -1) + 1 AS n FROM prod_views WHERE asset_key=?", (asset_key,)).fetchone()["n"]
                    inner_parts = (str(row["rel"])[len(prefix) + 1:] if prefix else str(row["rel"])).split("/")
                    label = VIEW_LABELS.get(key) or (inner_parts[1] if len(inner_parts) >= 3 else key)
                    conn.execute(
                        "INSERT INTO prod_views(asset_key, view_key, label, required, sort) VALUES(?, ?, ?, 0, ?)",
                        (asset_key, key, label[:60], sort),
                    )
                    views[key] = key
                    _event(conn, asset_key, "view_added", actor, {"view": key, "required": False, "discovered": True}, now)
                added.append(_insert_revision(conn, asset_key, key, row, note="", origin="discovered", now=now, actor=actor))
            elif key in stages:
                source = safe_file(collection, row["rel"])
                name = str(row["rel"]).lower()
                evidence_kind = "timelapse" if ("timelapse" in name or "延时" in name) else ("video" if Path(name).suffix in VIDEO_EXTS else "image")
                conn.execute(
                    """INSERT INTO prod_evidence(evidence_id, asset_key, stage, kind, collection, file_id, rel, sha256, source, caption, origin, created_at)
                       VALUES(?, ?, ?, ?, ?, ?, ?, ?, '', '', 'discovered', ?)""",
                    (str(uuid.uuid4()), asset_key, key, evidence_kind, collection, row["asset_id"], row["rel"],
                     file_sha256(source) if source else None, now),
                )
                if not conn.execute("SELECT 1 FROM prod_stages WHERE asset_key=? AND stage=?", (asset_key, key)).fetchone() and key != "design":
                    conn.execute("INSERT INTO prod_stages(asset_key, stage, status, updated_by, updated_at) VALUES(?, ?, 'wip', ?, ?)", (asset_key, key, actor, now))
                _event(conn, asset_key, "evidence_added", actor, {"stage": key, "kind": evidence_kind, "rel": row["rel"], "discovered": True}, now)
                added.append({"stage": key, "rel": row["rel"]})
        if added:
            conn.execute("UPDATE prod_assets SET updated_at=? WHERE asset_key=?", (now, asset_key))
    return {"asset": asset_key, "added": added}


def store_upload(asset_key: str, *, filename: str, data: bytes, view: str | None = None, stage: str | None = None) -> dict[str, Any]:
    """Write an uploaded file into the asset's own folder, never over an existing file."""
    if not data:
        raise ValueError("empty upload")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("upload too large")
    suffix = Path(filename or "").suffix.lower()
    if suffix not in ASSET_EXTS or suffix == ".svg":
        raise ValueError("unsupported file type")
    with _connection() as conn:
        asset = _asset_row(conn, asset_key)
        collection, root, prefix = _source_root(asset)
        if view:
            if not conn.execute("SELECT 1 FROM prod_views WHERE asset_key=? AND view_key=?", (asset_key, view)).fetchone():
                raise ValueError(f"view not found: {view}")
            numbers = [int(row["number"]) for row in conn.execute("SELECT number FROM prod_revisions WHERE asset_key=? AND view_key=?", (asset_key, view))]
            folder = Path(prefix, "design", view) if prefix else Path("design", view)
            name = f"{view}_v{(max(numbers) + 1) if numbers else 1}{suffix}"
        elif stage:
            stage = STAGE_ALIASES.get(stage.lower(), stage.lower())
            if stage not in json.loads(asset["stages"]) or stage == "design":
                raise ValueError(f"stage not in this asset's pipeline: {stage}")
            folder = Path(prefix, stage) if prefix else Path(stage)
            stem = re.sub(r"[^\w\-. ]+", "_", Path(filename).stem).strip(" .") or stage
            name = f"{stem[:80]}{suffix}"
        else:
            raise ValueError("upload needs a view or a stage")
    target_dir = (root / folder).resolve()
    if root != target_dir and root not in target_dir.parents:
        raise ValueError("upload escaped the asset folder")
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / name
    counter = 2
    while target.exists():
        target = target_dir / f"{Path(name).stem}-{counter}{suffix}"
        counter += 1
    with open(target, "xb") as handle:
        handle.write(data)
    return {"collection": collection, "rel": target.relative_to(root).as_posix()}


# --------------------------------------------------------------------------- cleanup

def cleanup_plan(asset_key: str | None = None) -> dict[str, Any]:
    """Revisions that can leave the folder safely: culled, or not chosen once a later design was locked."""
    items = []
    with _connection() as conn:
        if asset_key:
            assets = conn.execute("SELECT * FROM prod_assets WHERE asset_key=?", (asset_key,)).fetchall()
        else:
            assets = conn.execute("SELECT * FROM prod_assets").fetchall()
        evidence_files = {row["file_id"] for row in conn.execute("SELECT file_id FROM prod_evidence WHERE file_id IS NOT NULL")}
        for asset in assets:
            key = asset["asset_key"]
            protected = _locked_revision_ids(conn, key, all_locks=True)
            protected |= {row["selected_revision_id"] for row in conn.execute("SELECT selected_revision_id FROM prod_views WHERE asset_key=? AND selected_revision_id IS NOT NULL", (key,))}
            lock = conn.execute("SELECT created_at FROM prod_locks WHERE asset_key=? AND superseded_at IS NULL", (key,)).fetchone()
            for revision in conn.execute(
                """SELECT r.*, a.status AS review_status, a.present AS present FROM prod_revisions r
                   LEFT JOIN assets a ON a.asset_id=r.file_id WHERE r.asset_key=? AND r.state<>'archived'
                   ORDER BY r.view_key, r.number""",
                (key,),
            ):
                if revision["revision_id"] in protected or revision["file_id"] in evidence_files:
                    continue
                if not revision["present"] or revision["review_status"] == "approved":
                    continue
                if revision["state"] == "culled":
                    reason = f"已筛除：{revision['state_reason'] or '未说明'}"
                elif lock and revision["created_at"] <= lock["created_at"]:
                    reason = "定稿未采用"
                else:
                    continue
                items.append({
                    "revision_id": revision["revision_id"], "asset": key, "asset_name": asset["name"],
                    "view": revision["view_key"], "number": int(revision["number"]), "collection": revision["collection"],
                    "rel": revision["rel"], "reason": reason, **_media(revision["collection"], revision["rel"]),
                })
    return {"version": 1, "count": len(items), "items": items, "destination": f"<collection>/{TRASH_DIR}/<time>/"}


def cleanup_execute(revision_ids: Iterable[str], *, actor: str = "owner") -> dict[str, Any]:
    wanted = list(dict.fromkeys(str(value) for value in revision_ids if value))
    if not wanted:
        raise ValueError("choose at least one revision")
    plan = {item["revision_id"]: item for item in cleanup_plan()["items"]}
    unsafe = [value for value in wanted if value not in plan]
    if unsafe:
        raise ValueError("some revisions are no longer safe to clean up; refresh and try again")
    now = utc_now()
    stamp = now[:19].replace(":", "").replace("-", "")
    moved = []
    with _connection() as conn:
        for revision_id in wanted:
            item = plan[revision_id]
            root = root_for(item["collection"])
            source = safe_file(item["collection"], item["rel"])
            if not root or not source:
                continue
            trash_rel = Path(TRASH_DIR, stamp, item["rel"]).as_posix()
            target = (root / trash_rel).resolve()
            if root not in target.parents or target.exists():
                raise ValueError(f"cannot park {item['rel']} safely")
            target.parent.mkdir(parents=True, exist_ok=True)
            source.rename(target)
            conn.execute("UPDATE prod_revisions SET state='archived', trash_rel=?, updated_at=? WHERE revision_id=?", (trash_rel, now, revision_id))
            conn.execute("UPDATE assets SET present=0, missing_at=? WHERE asset_id=(SELECT file_id FROM prod_revisions WHERE revision_id=?)", (now, revision_id))
            _event(conn, item["asset"], "revision_archived", actor, {"view": item["view"], "number": item["number"], "rel": item["rel"], "trash_rel": trash_rel}, now)
            moved.append({"revision_id": revision_id, "rel": item["rel"], "trash_rel": trash_rel})
    return {"ok": True, "moved": moved}


def _restore_archived_file(conn: Any, revision: Any) -> None:
    root = root_for(revision["collection"])
    if not root or not revision["trash_rel"]:
        raise ValueError("archived file location is unknown")
    source = (root / revision["trash_rel"]).resolve()
    target = (root / revision["rel"]).resolve()
    if root not in source.parents or root not in target.parents:
        raise ValueError("archived file path escaped the collection")
    if not source.is_file():
        raise ValueError("archived file no longer exists")
    if target.exists():
        raise ValueError("a different file now occupies the original path")
    target.parent.mkdir(parents=True, exist_ok=True)
    source.rename(target)
    conn.execute("UPDATE assets SET present=1, missing_at=NULL WHERE asset_id=?", (revision["file_id"],))


# --------------------------------------------------------------------------- read models

def _asset_public(asset: Any) -> dict[str, Any]:
    return {
        "key": asset["asset_key"], "name": asset["name"], "project": asset["project"], "scene": asset["scene"],
        "kind": asset["kind"], "kind_label": KINDS.get(asset["kind"], (asset["kind"],))[0], "brief": asset["brief"],
        "stages": json.loads(asset["stages"]), "source_collection": asset["source_collection"],
        "source_prefix": asset["source_prefix"], "created_at": asset["created_at"], "updated_at": asset["updated_at"],
    }


def _load(conn: Any, asset_key: str) -> dict[str, Any]:
    asset = _asset_row(conn, asset_key)
    views = [dict(row) for row in conn.execute("SELECT * FROM prod_views WHERE asset_key=? ORDER BY sort, view_key", (asset_key,))]
    revisions = [dict(row) for row in conn.execute(
        """SELECT r.*, a.rel AS current_rel, a.present AS present, a.width AS width, a.height AS height,
                  a.comment AS comment, a.content_changed_at AS content_changed_at,
                  (SELECT COUNT(*) FROM annotations an WHERE an.asset_id=r.file_id AND an.stale=0 AND an.resolved=0) AS annotation_count
           FROM prod_revisions r LEFT JOIN assets a ON a.asset_id=r.file_id
           WHERE r.asset_key=? ORDER BY r.view_key, r.number""",
        (asset_key,),
    )]
    locks = [dict(row) | {"views": json.loads(row["views"])} for row in conn.execute(
        "SELECT * FROM prod_locks WHERE asset_key=? ORDER BY number", (asset_key,)
    )]
    evidence = [dict(row) for row in conn.execute(
        """SELECT e.*, a.rel AS current_rel, a.present AS present, a.width AS width, a.height AS height
           FROM prod_evidence e LEFT JOIN assets a ON a.asset_id=e.file_id
           WHERE e.asset_key=? ORDER BY e.created_at, e.evidence_id""",
        (asset_key,),
    )]
    events = [dict(row) | {"details": json.loads(row["details"] or "{}")} for row in conn.execute(
        "SELECT * FROM prod_events WHERE asset_key=? ORDER BY id DESC LIMIT 120", (asset_key,)
    )]
    return {"asset": asset, "views": views, "revisions": revisions, "locks": locks, "evidence": evidence,
            "stages": _stage_rows(conn, asset), "events": events}


def _revision_payload(row: dict[str, Any], selected: set[str], locked: set[str]) -> dict[str, Any]:
    rel = row.get("current_rel") or row["rel"]
    present = bool(row.get("present")) and row["state"] != "archived"
    changed = bool(row.get("content_changed_at") and row["content_changed_at"] > row["created_at"])
    return {
        "revision_id": row["revision_id"], "view": row["view_key"], "number": int(row["number"]), "label": f"v{row['number']}",
        "state": row["state"], "reason": row["state_reason"], "note": row["note"], "origin": row["origin"],
        "created_at": row["created_at"], "collection": row["collection"], "file_id": row["file_id"], "rel": rel,
        "present": present, "changed": changed, "width": row.get("width") or 0, "height": row.get("height") or 0,
        "sha256": row["sha256"], "comment": row.get("comment") or "", "annotation_count": int(row.get("annotation_count") or 0),
        "selected": row["revision_id"] in selected, "locked": row["revision_id"] in locked,
        "trash_rel": row.get("trash_rel"), **(_media(row["collection"], rel) if present else {}),
    }


def _evidence_payload(row: dict[str, Any]) -> dict[str, Any]:
    rel = row.get("current_rel") or row.get("rel")
    present = bool(row.get("present")) if row.get("file_id") else False
    return {
        "evidence_id": row["evidence_id"], "stage": row["stage"], "kind": row["kind"], "caption": row["caption"],
        "source": row["source"], "origin": row["origin"], "created_at": row["created_at"], "collection": row.get("collection"),
        "rel": rel, "present": present, "width": row.get("width") or 0, "height": row.get("height") or 0,
        **(_media(row["collection"], rel) if present and rel else {}),
    }


def _compose(data: dict[str, Any]) -> dict[str, Any]:
    asset = data["asset"]
    current_lock = next((lock for lock in reversed(data["locks"]) if not lock["superseded_at"]), None)
    locked_ids = {view["revision_id"] for view in current_lock["views"]} if current_lock else set()
    selected_ids = {view["selected_revision_id"] for view in data["views"] if view["selected_revision_id"]}
    revisions = [_revision_payload(row, selected_ids, locked_ids) for row in data["revisions"]]
    by_view: dict[str, list[dict[str, Any]]] = {}
    for revision in revisions:
        by_view.setdefault(revision["view"], []).append(revision)

    decisions: list[dict[str, Any]] = []
    views = []
    for view in data["views"]:
        items = by_view.get(view["view_key"], [])
        active = [item for item in items if item["state"] == "candidate"]
        selected = next((item for item in items if item["selected"]), None)
        newer = [item for item in active if view["selected_at"] and item["created_at"] > view["selected_at"] and not item["selected"]]
        if selected and selected["locked"]:
            status = "locked"
        elif selected:
            status = "selected"
        elif active:
            status = "choose"
        else:
            status = "missing"
        if status == "choose":
            decisions.append({"type": "select", "view": view["view_key"], "text": f"选定「{view['label']}」版本", "detail": f"{len(active)} 个候选"})
        elif newer:
            status = "updated"
            decisions.append({"type": "select", "view": view["view_key"], "text": f"「{view['label']}」有新版本 {newer[-1]['label']}", "detail": f"当前选用 {selected['label']}" if selected else ""})
        views.append({
            "view": view["view_key"], "label": view["label"], "required": bool(view["required"]), "status": status,
            "selected_revision_id": view["selected_revision_id"], "selected_at": view["selected_at"],
            "revisions": [item for item in items if item["state"] != "archived"],
            "culled": sum(1 for item in items if item["state"] == "culled"),
            "archived": sum(1 for item in items if item["state"] == "archived"),
            "current": selected or (active[-1] if active else None),
        })

    required = [view for view in views if view["required"]]
    all_selected = bool(required) and all(view["selected_revision_id"] for view in required)
    lock_matches = bool(current_lock) and locked_ids == selected_ids
    if all_selected and not lock_matches:
        decisions.append({"type": "lock", "text": "确认整套设计" if not current_lock else f"更新定稿为 D{len(data['locks']) + 1}",
                          "detail": " · ".join(f"{v['label']} {v['current']['label']}" for v in views if v["current"] and v["selected_revision_id"])})

    evidence = [_evidence_payload(row) for row in data["evidence"]]
    stages = []
    for row in data["stages"]:
        key = row["stage"]
        label, hint = STAGES[key]
        items = [item for item in evidence if item["stage"] == key]
        if key == "design":
            status = "accepted" if current_lock and lock_matches else ("accepted" if current_lock else ("wip" if revisions else "todo"))
            summary = f"定稿 D{current_lock['number']} · {len(current_lock['views'])} 个视图" if current_lock else ""
            stages.append({"stage": key, "label": label, "hint": hint, "status": status, "summary": summary, "next_step": "",
                           "accepted_at": current_lock["created_at"] if current_lock else None, "updated_at": None, "updated_by": "", "evidence": items})
            continue
        if row["status"] == "ready":
            decisions.append({"type": "accept", "stage": key, "text": "实机验收" if key == "engine" else f"验收「{label}」", "detail": row["summary"] or ""})
        stages.append({"stage": key, "label": label, "hint": hint, "status": row["status"], "summary": row["summary"],
                       "next_step": row["next_step"], "accepted_at": row["accepted_at"], "updated_at": row["updated_at"],
                       "updated_by": row["updated_by"], "evidence": items})

    stage_map = {stage["stage"]: stage for stage in stages}
    tech = [stage_map[key] for key in TECH_STAGES if key in stage_map]
    gates = {
        "visual": {"label": "视觉批准", "state": "approved" if current_lock else ("progress" if revisions else "none"),
                   "detail": f"定稿 D{current_lock['number']}" if current_lock else ("待定稿" if revisions else "未开始")},
        "tech": {"label": "技术就绪", "state": "na"} if not tech else {
            "label": "技术就绪",
            "state": "approved" if all(s["status"] == "accepted" for s in tech) else
                     "review" if all(s["status"] in {"ready", "accepted"} for s in tech) else
                     "progress" if any(s["status"] != "todo" for s in tech) else "none",
            "detail": f"{sum(1 for s in tech if s['status'] in {'ready', 'accepted'})}/{len(tech)} 项就绪",
        },
        "runtime": {"label": "实机验收", "state": "na"} if "engine" not in stage_map else {
            "label": "实机验收",
            "state": {"accepted": "approved", "ready": "review", "wip": "progress"}.get(stage_map["engine"]["status"], "none"),
            "detail": {"accepted": "已验收", "ready": "待验收", "wip": "进行中"}.get(stage_map["engine"]["status"], "未开始"),
        },
    }

    # One sentence: what happens next, and whether it is the owner's move.
    next_step: dict[str, Any]
    missing = [view for view in required if view["status"] == "missing"]
    if decisions:
        next_step = {"text": decisions[0]["text"], "owner": True, "decision": decisions[0]}
    elif missing:
        next_step = {"text": f"等待「{missing[0]['label']}」设计稿", "owner": False, "stage": "design"}
    elif not current_lock:
        next_step = {"text": "补充设计视图" if not views else "等待设计稿", "owner": False, "stage": "design"}
    else:
        pending = next((stage for stage in stages if stage["stage"] != "design" and stage["status"] != "accepted"), None)
        if pending is None:
            next_step = {"text": "全部完成", "owner": False, "done": True}
        elif pending["next_step"]:
            next_step = {"text": pending["next_step"], "owner": False, "stage": pending["stage"]}
        elif pending["status"] == "wip":
            next_step = {"text": f"{pending['label']}进行中", "owner": False, "stage": pending["stage"]}
        else:
            next_step = {"text": f"开始{pending['label']}", "owner": False, "stage": pending["stage"]}

    # The current result: the most advanced stage that has something to show.
    cover = None
    for stage in reversed(stages):
        if stage["stage"] == "design":
            continue
        shown = [item for item in stage["evidence"] if item.get("thumb")]
        if shown:
            cover = {"stage": stage["stage"], "label": stage["label"], "caption": shown[-1]["caption"], **{k: shown[-1][k] for k in ("thumb", "preview", "file", "media_type")}}
            break
    if not cover:
        pick_view = (next((v for v in views if v["current"] and v["current"].get("thumb") and v["current"]["locked"]), None)
                     or next((v for v in views if v["current"] and v["current"].get("thumb")), None))
        if pick_view:
            design_pick = pick_view["current"]
            cover = {"stage": "design", "label": "设计", "caption": f"{pick_view['label']} {design_pick['label']}",
                     **{k: design_pick[k] for k in ("thumb", "preview", "file", "media_type")}}

    chapters = []
    for stage in stages:
        if stage["stage"] == "design":
            items = []
            for view in views:
                for revision in view["revisions"]:
                    if revision["state"] == "candidate" and revision.get("thumb"):
                        items.append({"kind": "revision", "view_label": view["label"], **revision})
            items.sort(key=lambda item: item["created_at"])
            chapter_locks = [{"name": f"D{lock['number']}", "created_at": lock["created_at"], "note": lock["note"], "current": not lock["superseded_at"]} for lock in data["locks"]]
            chapters.append({"stage": "design", "label": stage["label"], "hint": stage["hint"], "status": stage["status"],
                             "items": items, "locks": chapter_locks, "missing": not items,
                             "started_at": items[0]["created_at"] if items else None, "accepted_at": stage["accepted_at"]})
        else:
            chapters.append({"stage": stage["stage"], "label": stage["label"], "hint": stage["hint"], "status": stage["status"],
                             "summary": stage["summary"], "items": stage["evidence"], "missing": not stage["evidence"],
                             "started_at": stage["evidence"][0]["created_at"] if stage["evidence"] else None, "accepted_at": stage["accepted_at"]})

    done = sum(1 for stage in stages if stage["status"] == "accepted")
    return {
        "version": 1,
        "asset": _asset_public(asset),
        "cover": cover,
        "gates": gates,
        "next": next_step,
        "decisions": decisions,
        "progress": {"done": done, "total": len(stages)},
        "views": views,
        "lock": ({"name": f"D{current_lock['number']}", "number": current_lock["number"], "created_at": current_lock["created_at"],
                  "note": current_lock["note"], "views": current_lock["views"], "current": lock_matches} if current_lock else None),
        "locks": [{"name": f"D{lock['number']}", "created_at": lock["created_at"], "note": lock["note"],
                   "superseded_at": lock["superseded_at"], "views": lock["views"]} for lock in data["locks"]],
        "stages": stages,
        "making_of": chapters,
        "events": data["events"],
    }


def asset_detail(asset_key: str) -> dict[str, Any]:
    with _connection() as conn:
        data = _load(conn, asset_key)
    return _compose(data)


def asset_summary(detail: dict[str, Any]) -> dict[str, Any]:
    return {key: detail[key] for key in ("asset", "cover", "gates", "next", "decisions", "progress")} | {
        "stages": [{"stage": s["stage"], "label": s["label"], "status": s["status"]} for s in detail["stages"]],
        "views": [{"view": v["view"], "label": v["label"], "status": v["status"], "required": v["required"]} for v in detail["views"]],
    }


def file_links(file_id: str) -> list[dict[str, Any]]:
    """Where a catalog file is used: as a view revision or as stage evidence."""
    with _connection() as conn:
        links = [
            {"type": "revision", "asset": row["asset_key"], "asset_name": row["name"], "view": row["view_key"],
             "label": f"{row['label']} v{row['number']}", "state": row["state"]}
            for row in conn.execute(
                """SELECT r.asset_key, r.view_key, r.number, r.state, a.name, v.label FROM prod_revisions r
                   JOIN prod_assets a ON a.asset_key=r.asset_key
                   LEFT JOIN prod_views v ON v.asset_key=r.asset_key AND v.view_key=r.view_key
                   WHERE r.file_id=? AND r.state<>'archived'""",
                (file_id,),
            )
        ]
        links += [
            {"type": "evidence", "asset": row["asset_key"], "asset_name": row["name"], "stage": row["stage"],
             "label": STAGES.get(row["stage"], (row["stage"],))[0]}
            for row in conn.execute(
                "SELECT e.asset_key, e.stage, a.name FROM prod_evidence e JOIN prod_assets a ON a.asset_key=e.asset_key WHERE e.file_id=?",
                (file_id,),
            )
        ]
    return links


def assign_targets() -> list[dict[str, Any]]:
    """Every asset with its views and stages, for 'add this file to ...' menus."""
    with _connection() as conn:
        assets = conn.execute("SELECT * FROM prod_assets ORDER BY project, scene, sort, name").fetchall()
        views = {}
        for row in conn.execute("SELECT asset_key, view_key, label FROM prod_views ORDER BY sort, view_key"):
            views.setdefault(row["asset_key"], []).append({"view": row["view_key"], "label": row["label"]})
    return [{"key": a["asset_key"], "name": a["name"], "project": a["project"], "scene": a["scene"],
             "views": views.get(a["asset_key"], []),
             "stages": [{"stage": s, "label": STAGES[s][0]} for s in json.loads(a["stages"])]} for a in assets]


def source_collection(asset_key: str) -> str | None:
    with _connection() as conn:
        row = conn.execute("SELECT source_collection FROM prod_assets WHERE asset_key=?", (asset_key,)).fetchone()
    return row["source_collection"] if row else None


def asset_keys(project: str | None = None) -> list[str]:
    with _connection() as conn:
        if project:
            rows = conn.execute("SELECT asset_key FROM prod_assets WHERE project=? ORDER BY scene, sort, name", (project,))
        else:
            rows = conn.execute("SELECT asset_key FROM prod_assets ORDER BY project, scene, sort, name")
        return [row["asset_key"] for row in rows]


def project_overview(project: str) -> dict[str, Any]:
    scenes: dict[str, list[dict[str, Any]]] = {}
    decisions = []
    for key in asset_keys(project):
        summary = asset_summary(asset_detail(key))
        scenes.setdefault(summary["asset"]["scene"] or "", []).append(summary)
        for decision in summary["decisions"]:
            decisions.append(decision | {"asset": key, "asset_name": summary["asset"]["name"]})
    groups = [{"scene": scene, "assets": items} for scene, items in sorted(scenes.items(), key=lambda pair: (pair[0] == "", pair[0]))]
    total = sum(len(group["assets"]) for group in groups)
    locked = sum(1 for group in groups for item in group["assets"] if item["gates"]["visual"]["state"] == "approved")
    shipped = sum(1 for group in groups for item in group["assets"] if item["gates"]["runtime"]["state"] == "approved")
    return {"version": 1, "project": project, "scenes": groups, "decisions": decisions,
            "counts": {"assets": total, "locked": locked, "runtime": shipped, "decisions": len(decisions)}}

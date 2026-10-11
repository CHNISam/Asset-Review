from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif", ".bmp", ".svg"}
VIDEO_EXTS = {".mp4", ".webm", ".mov", ".m4v"}
ASSET_EXTS = IMAGE_EXTS | VIDEO_EXTS
VALID_STATUSES = {"", "approved", "maybe", "rejected"}
_WRITE_LOCK = threading.RLock()
_DB_INIT_LOCK = threading.RLock()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def data_dir() -> Path:
    override = os.environ.get("ASSET_VIEWER_HOME")
    root = Path(override).expanduser() if override else Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "asset-viewer"
    root.mkdir(parents=True, exist_ok=True)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    return root


def cache_dir() -> Path:
    override = os.environ.get("ASSET_VIEWER_CACHE")
    root = Path(override).expanduser() if override else Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "asset-viewer"
    root.mkdir(parents=True, exist_ok=True)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    return root


def registry_path() -> Path:
    return data_dir() / "collections.json"


def database_path() -> Path:
    return data_dir() / "asset-viewer.db"


def legacy_reviews_path() -> Path:
    return data_dir() / "reviews.json"


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return default


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    with _WRITE_LOCK:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "wb", closefd=False) as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            os.close(fd)
        os.replace(tmp, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass


def _connect() -> sqlite3.Connection:
    path = database_path()
    with _DB_INIT_LOCK:
        conn = sqlite3.connect(path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS assets (
                collection TEXT NOT NULL,
                rel TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT '' CHECK(status IN ('', 'approved', 'maybe', 'rejected')),
                comment TEXT NOT NULL DEFAULT '',
                first_seen_at TEXT NOT NULL,
                seen_at TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (collection, rel)
            );
            CREATE INDEX IF NOT EXISTS idx_assets_collection_status
                ON assets(collection, status);
            CREATE INDEX IF NOT EXISTS idx_assets_collection_seen
                ON assets(collection, seen_at);
            CREATE TABLE IF NOT EXISTS review_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                collection TEXT NOT NULL,
                rel TEXT NOT NULL,
                action TEXT NOT NULL,
                old_status TEXT NOT NULL DEFAULT '',
                new_status TEXT NOT NULL DEFAULT '',
                old_comment TEXT NOT NULL DEFAULT '',
                new_comment TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                undone_at TEXT,
                details TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_review_events_asset
                ON review_events(collection, rel, id DESC);
            CREATE TABLE IF NOT EXISTS collection_state (
                collection TEXT PRIMARY KEY,
                completed_at TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS annotations (
                annotation_id TEXT PRIMARY KEY,
                asset_id TEXT NOT NULL,
                collection TEXT NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('point', 'region')),
                x REAL NOT NULL CHECK(x >= 0 AND x <= 1),
                y REAL NOT NULL CHECK(y >= 0 AND y <= 1),
                w REAL NOT NULL DEFAULT 0 CHECK(w >= 0 AND w <= 1),
                h REAL NOT NULL DEFAULT 0 CHECK(h >= 0 AND h <= 1),
                text TEXT NOT NULL DEFAULT '',
                resolved INTEGER NOT NULL DEFAULT 0,
                stale INTEGER NOT NULL DEFAULT 0,
                content_sha256 TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_annotations_asset
                ON annotations(asset_id, stale, resolved, created_at);
            CREATE INDEX IF NOT EXISTS idx_annotations_collection
                ON annotations(collection, created_at);
            CREATE TABLE IF NOT EXISTS families (
                family_id TEXT PRIMARY KEY,
                collection TEXT NOT NULL,
                name TEXT NOT NULL,
                preferred_asset_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_families_collection
                ON families(collection, updated_at DESC);
            CREATE TABLE IF NOT EXISTS family_members (
                family_id TEXT NOT NULL,
                asset_id TEXT NOT NULL UNIQUE,
                added_at TEXT NOT NULL,
                PRIMARY KEY (family_id, asset_id)
            );
            CREATE INDEX IF NOT EXISTS idx_family_members_family
                ON family_members(family_id, added_at);
            CREATE TABLE IF NOT EXISTS catalog_state (
                collection TEXT PRIMARY KEY,
                generation INTEGER NOT NULL DEFAULT 0,
                last_scan_at TEXT,
                changed_at TEXT,
                truncated INTEGER NOT NULL DEFAULT 0,
                reason TEXT,
                elapsed_ms INTEGER NOT NULL DEFAULT 0,
                added INTEGER NOT NULL DEFAULT 0,
                changed INTEGER NOT NULL DEFAULT 0,
                removed INTEGER NOT NULL DEFAULT 0,
                renamed INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        _migrate_legacy_reviews(conn)
        _ensure_catalog_schema(conn)
        _ensure_context_schema(conn)
        _ensure_production_schema(conn)
        conn.commit()
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return conn


@contextmanager
def _connection():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _ensure_catalog_schema(conn: sqlite3.Connection) -> None:
    asset_columns = {row["name"] for row in conn.execute("PRAGMA table_info(assets)")}
    additions = {
        "asset_id": "TEXT",
        "device": "INTEGER",
        "inode": "INTEGER",
        "size": "INTEGER",
        "mtime_ns": "INTEGER",
        "width": "INTEGER",
        "height": "INTEGER",
        "preview_error": "TEXT",
        "present": "INTEGER NOT NULL DEFAULT 1",
        "scan_generation": "INTEGER NOT NULL DEFAULT 0",
        "content_changed_at": "TEXT",
        "missing_at": "TEXT",
        "review_sha256": "TEXT",
    }
    for name, declaration in additions.items():
        if name not in asset_columns:
            conn.execute(f"ALTER TABLE assets ADD COLUMN {name} {declaration}")
    for row in conn.execute("SELECT rowid FROM assets WHERE asset_id IS NULL OR asset_id='' ").fetchall():
        conn.execute("UPDATE assets SET asset_id=? WHERE rowid=?", (str(uuid.uuid4()), row["rowid"]))
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_assets_asset_id ON assets(asset_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_assets_collection_present ON assets(collection, present, rel)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_assets_collection_inode ON assets(collection, device, inode)")

    event_columns = {row["name"] for row in conn.execute("PRAGMA table_info(review_events)")}
    if "asset_id" not in event_columns:
        conn.execute("ALTER TABLE review_events ADD COLUMN asset_id TEXT")
    if "details" not in event_columns:
        conn.execute("ALTER TABLE review_events ADD COLUMN details TEXT NOT NULL DEFAULT '{}'")
    conn.execute(
        """
        UPDATE review_events
        SET asset_id=(
            SELECT assets.asset_id FROM assets
            WHERE assets.collection=review_events.collection AND assets.rel=review_events.rel
        )
        WHERE asset_id IS NULL
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_review_events_asset_id ON review_events(asset_id, id DESC)")

    annotation_columns = {row["name"] for row in conn.execute("PRAGMA table_info(annotations)")}
    if "content_sha256" not in annotation_columns:
        conn.execute("ALTER TABLE annotations ADD COLUMN content_sha256 TEXT")

    state_columns = {row["name"] for row in conn.execute("PRAGMA table_info(catalog_state)")}
    state_additions = {
        "changed_at": "TEXT",
        "added": "INTEGER NOT NULL DEFAULT 0",
        "changed": "INTEGER NOT NULL DEFAULT 0",
        "removed": "INTEGER NOT NULL DEFAULT 0",
        "renamed": "INTEGER NOT NULL DEFAULT 0",
    }
    for name, declaration in state_additions.items():
        if name not in state_columns:
            conn.execute(f"ALTER TABLE catalog_state ADD COLUMN {name} {declaration}")


def _ensure_context_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS asset_metadata (
            asset_id TEXT PRIMARY KEY,
            collection TEXT NOT NULL,
            source_project TEXT NOT NULL DEFAULT '',
            tool TEXT NOT NULL DEFAULT '',
            agent TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            prompt TEXT NOT NULL DEFAULT '',
            seed TEXT NOT NULL DEFAULT '',
            run_id TEXT NOT NULL DEFAULT '',
            git_commit TEXT NOT NULL DEFAULT '',
            parent_asset_id TEXT,
            extra_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_asset_metadata_collection ON asset_metadata(collection, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_asset_metadata_model ON asset_metadata(model);
        CREATE INDEX IF NOT EXISTS idx_asset_metadata_run ON asset_metadata(run_id);
        """
    )



def _ensure_production_schema(conn: sqlite3.Connection) -> None:
    """Production objects: assets with design views, revisions, design locks, stages, evidence.

    Files stay in their registered folders; these rows only point at catalog
    assets by stable asset_id and remember the exact bytes (sha256) chosen.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS prod_assets (
            asset_key TEXT PRIMARY KEY,
            project TEXT NOT NULL,
            scene TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'prop',
            brief TEXT NOT NULL DEFAULT '',
            stages TEXT NOT NULL DEFAULT '[]',
            source_collection TEXT,
            source_prefix TEXT NOT NULL DEFAULT '',
            sort INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_prod_assets_project ON prod_assets(project, scene, sort);
        CREATE TABLE IF NOT EXISTS prod_views (
            asset_key TEXT NOT NULL,
            view_key TEXT NOT NULL,
            label TEXT NOT NULL,
            required INTEGER NOT NULL DEFAULT 1,
            sort INTEGER NOT NULL DEFAULT 0,
            selected_revision_id TEXT,
            selected_at TEXT,
            PRIMARY KEY (asset_key, view_key)
        );
        CREATE TABLE IF NOT EXISTS prod_revisions (
            revision_id TEXT PRIMARY KEY,
            asset_key TEXT NOT NULL,
            view_key TEXT NOT NULL,
            number INTEGER NOT NULL,
            collection TEXT NOT NULL,
            file_id TEXT NOT NULL,
            rel TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            origin TEXT NOT NULL DEFAULT 'linked',
            state TEXT NOT NULL DEFAULT 'candidate' CHECK(state IN ('candidate', 'culled', 'archived')),
            state_reason TEXT NOT NULL DEFAULT '',
            trash_rel TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (asset_key, view_key, number)
        );
        CREATE INDEX IF NOT EXISTS idx_prod_revisions_view ON prod_revisions(asset_key, view_key, number);
        CREATE INDEX IF NOT EXISTS idx_prod_revisions_file ON prod_revisions(file_id);
        CREATE TABLE IF NOT EXISTS prod_locks (
            lock_id TEXT PRIMARY KEY,
            asset_key TEXT NOT NULL,
            number INTEGER NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            views TEXT NOT NULL,
            created_at TEXT NOT NULL,
            superseded_at TEXT,
            UNIQUE (asset_key, number)
        );
        CREATE TABLE IF NOT EXISTS prod_stages (
            asset_key TEXT NOT NULL,
            stage TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'todo' CHECK(status IN ('todo', 'wip', 'ready', 'accepted')),
            summary TEXT NOT NULL DEFAULT '',
            next_step TEXT NOT NULL DEFAULT '',
            updated_by TEXT NOT NULL DEFAULT '',
            accepted_at TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (asset_key, stage)
        );
        CREATE TABLE IF NOT EXISTS prod_evidence (
            evidence_id TEXT PRIMARY KEY,
            asset_key TEXT NOT NULL,
            stage TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'image',
            collection TEXT,
            file_id TEXT,
            rel TEXT,
            sha256 TEXT,
            source TEXT NOT NULL DEFAULT '',
            caption TEXT NOT NULL DEFAULT '',
            origin TEXT NOT NULL DEFAULT 'linked',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_prod_evidence_asset ON prod_evidence(asset_key, stage, created_at);
        CREATE TABLE IF NOT EXISTS prod_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_key TEXT NOT NULL,
            action TEXT NOT NULL,
            actor TEXT NOT NULL DEFAULT 'system',
            details TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_prod_events_asset ON prod_events(asset_key, id);
        """
    )


def _migrate_legacy_reviews(conn: sqlite3.Connection) -> None:
    done = conn.execute("SELECT value FROM meta WHERE key='legacy_reviews_migrated'").fetchone()
    if done:
        return
    legacy = legacy_reviews_path()
    payload = _load_json(legacy, {})
    now = utc_now()
    if isinstance(payload, dict):
        for collection, rows in payload.items():
            if not isinstance(rows, dict):
                continue
            for rel, status in rows.items():
                if status not in VALID_STATUSES or not status:
                    continue
                conn.execute(
                    """
                    INSERT INTO assets(collection, rel, status, comment, first_seen_at, seen_at, updated_at)
                    VALUES(?, ?, ?, '', ?, ?, ?)
                    ON CONFLICT(collection, rel) DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at
                    """,
                    (str(collection), str(rel), str(status), now, now, now),
                )
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('legacy_reviews_migrated', ?)", (now,))
    if legacy.exists():
        migrated = legacy.with_name("reviews.json.migrated")
        try:
            legacy.replace(migrated)
        except OSError:
            pass


def slugify(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", value.strip()).strip("-").lower() or "collection"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def collections() -> list[dict[str, Any]]:
    """Return configured collections, including temporarily unavailable paths."""
    rows = _load_json(registry_path(), [])
    valid: list[dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        try:
            path = Path(row["path"]).expanduser().resolve()
            valid.append({
                "slug": str(row["slug"]),
                "label": str(row["label"]),
                "path": str(path),
                "group": str(row.get("group") or "").strip(),
                "available": path.is_dir(),
            })
        except (KeyError, TypeError, OSError):
            continue
    return valid


def add_collection(path: str, label: str | None = None, group: str | None = None) -> dict[str, Any]:
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_dir():
        raise ValueError(f"not a directory: {resolved}")
    label = (label or resolved.name.replace("-", " ").replace("_", " ").title()).strip()
    if not label:
        raise ValueError("collection label must not be empty")
    rows = collections()
    existing = next((r for r in rows if Path(r["path"]) == resolved), None)
    used = {r["slug"] for r in rows if not existing or r["slug"] != existing["slug"]}
    slug = existing["slug"] if existing else slugify(label)
    base = slug
    n = 2
    while slug in used:
        slug = f"{base}-{n}"
        n += 1
    clean_group = str(group if group is not None else (existing or {}).get("group", "")).strip()
    row = {"slug": slug, "label": label, "path": str(resolved), "group": clean_group, "available": True}
    rows = [r for r in rows if Path(r["path"]) != resolved]
    rows.append(row)
    rows.sort(key=lambda r: (r.get("group", "").lower(), r["label"].lower()))
    _atomic_json(registry_path(), [{"slug": r["slug"], "label": r["label"], "path": r["path"], **({"group": r.get("group", "")} if r.get("group") else {})} for r in rows])
    return row


def remove_collection(key: str) -> dict[str, Any]:
    rows = collections()
    try:
        resolved_key = str(Path(key).expanduser().resolve())
    except OSError:
        resolved_key = key
    match = next((r for r in rows if r["slug"] == key or r["path"] == resolved_key), None)
    if not match:
        raise ValueError(f"collection not found: {key}")
    kept = [r for r in rows if r != match]
    _atomic_json(registry_path(), [{"slug": r["slug"], "label": r["label"], "path": r["path"], **({"group": r.get("group", "")} if r.get("group") else {})} for r in kept])
    return match


def root_for(slug: str) -> Path | None:
    row = next((r for r in collections() if r["slug"] == slug), None)
    if not row or not row.get("available"):
        return None
    return Path(row["path"])


def asset_rel(collection: str, asset_id: str) -> str | None:
    if not asset_id:
        return None
    with _connection() as conn:
        row = conn.execute(
            "SELECT rel FROM assets WHERE collection=? AND asset_id=? AND present=1",
            (collection, asset_id),
        ).fetchone()
    return str(row["rel"]) if row else None


_METADATA_FIELDS = ("source_project", "tool", "agent", "model", "prompt", "seed", "run_id", "git_commit", "parent_asset_id")


def _resolve_asset_row(conn: sqlite3.Connection, collection: str, asset: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT asset_id, collection, rel, present FROM assets WHERE collection=? AND (asset_id=? OR rel=?) LIMIT 1",
        (collection, asset, asset),
    ).fetchone()
    if not row:
        raise ValueError(f"asset not found: {asset}")
    return row


def _metadata_payload(row: sqlite3.Row | None, asset_id: str, collection: str) -> dict[str, Any]:
    if not row:
        return {"asset_id": asset_id, "collection": collection, **{key: "" for key in _METADATA_FIELDS}, "extra": {}, "created_at": None, "updated_at": None}
    payload = {"asset_id": asset_id, "collection": collection}
    for key in _METADATA_FIELDS:
        payload[key] = row[key] or ""
    try:
        payload["extra"] = json.loads(row["extra_json"] or "{}")
    except json.JSONDecodeError:
        payload["extra"] = {}
    payload["created_at"] = row["created_at"]
    payload["updated_at"] = row["updated_at"]
    return payload


def asset_metadata(collection: str, asset: str) -> dict[str, Any]:
    with _connection() as conn:
        target = _resolve_asset_row(conn, collection, asset)
        row = conn.execute("SELECT * FROM asset_metadata WHERE asset_id=?", (target["asset_id"],)).fetchone()
        return _metadata_payload(row, str(target["asset_id"]), collection)


def set_asset_metadata(collection: str, asset: str, metadata: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        raise ValueError("metadata must be an object")
    unknown = set(metadata) - set(_METADATA_FIELDS) - {"extra"}
    if unknown:
        raise ValueError("unsupported metadata field(s): " + ", ".join(sorted(unknown)))
    now = utc_now()
    with _connection() as conn:
        target = _resolve_asset_row(conn, collection, asset)
        asset_id = str(target["asset_id"])
        old = conn.execute("SELECT * FROM asset_metadata WHERE asset_id=?", (asset_id,)).fetchone()
        old_payload = _metadata_payload(old, asset_id, collection)
        clean: dict[str, str] = {}
        for key in _METADATA_FIELDS:
            value = metadata[key] if key in metadata else old_payload.get(key, "")
            if value is None:
                value = ""
            if isinstance(value, (dict, list)):
                raise ValueError(f"{key} must be a scalar value")
            text = str(value).strip()
            limit = 20000 if key == "prompt" else 1000
            if len(text) > limit:
                raise ValueError(f"{key} is too long")
            clean[key] = text
        extra = metadata["extra"] if "extra" in metadata else old_payload.get("extra", {})
        if extra is None:
            extra = {}
        if not isinstance(extra, dict):
            raise ValueError("extra metadata must be an object")
        encoded_extra = json.dumps(extra, separators=(",", ":"), sort_keys=True)
        if len(encoded_extra) > 50000:
            raise ValueError("extra metadata is too large")
        parent = clean["parent_asset_id"]
        if parent:
            parent_row = conn.execute("SELECT asset_id FROM assets WHERE asset_id=?", (parent,)).fetchone()
            if not parent_row:
                raise ValueError("parent_asset_id does not reference a known asset")
        created = old["created_at"] if old else now
        conn.execute(
            """INSERT INTO asset_metadata(
                asset_id, collection, source_project, tool, agent, model, prompt, seed, run_id, git_commit, parent_asset_id, extra_json, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(asset_id) DO UPDATE SET
                collection=excluded.collection, source_project=excluded.source_project, tool=excluded.tool, agent=excluded.agent,
                model=excluded.model, prompt=excluded.prompt, seed=excluded.seed, run_id=excluded.run_id, git_commit=excluded.git_commit,
                parent_asset_id=excluded.parent_asset_id, extra_json=excluded.extra_json, updated_at=excluded.updated_at
            """,
            (asset_id, collection, clean["source_project"], clean["tool"], clean["agent"], clean["model"], clean["prompt"],
             clean["seed"], clean["run_id"], clean["git_commit"], parent or None, encoded_extra, created, now),
        )
        changed = [key for key in _METADATA_FIELDS if str(old_payload.get(key) or "") != clean[key]]
        if old_payload.get("extra", {}) != extra:
            changed.append("extra")
        if changed:
            _record_system_event(conn, collection, str(target["rel"]), asset_id, "metadata_updated", now, {"fields": sorted(changed)})
        row = conn.execute("SELECT * FROM asset_metadata WHERE asset_id=?", (asset_id,)).fetchone()
        return _metadata_payload(row, asset_id, collection)

def metadata_map(collection: str | None = None) -> dict[str, dict[str, Any]]:
    query = "SELECT * FROM asset_metadata"
    args: tuple[Any, ...] = ()
    if collection:
        query += " WHERE collection=?"
        args = (collection,)
    with _connection() as conn:
        rows = conn.execute(query, args).fetchall()
    return {str(row["asset_id"]): _metadata_payload(row, str(row["asset_id"]), str(row["collection"])) for row in rows}



def safe_file(slug: str, relative: str) -> Path | None:
    root = root_for(slug)
    if not root:
        return None
    try:
        # SECURITY: reject absolute/traversal components before joining, then
        # resolve and enforce containment. Symlinks that escape the root fail
        # the post-resolution parent check.
        relative_path = Path(relative)
        if relative_path.is_absolute() or any(part in {"", ".", ".."} for part in relative_path.parts):
            return None
        if relative_path.suffix.lower() not in ASSET_EXTS:
            return None
        path = (root / relative_path).resolve()
        if path == root or root not in path.parents or not path.is_file():
            return None
        return path
    except (OSError, ValueError):
        return None


def ensure_assets(collection: str, relatives: Iterable[str]) -> None:
    unique = sorted({str(rel) for rel in relatives if rel})
    if not unique:
        return
    now = utc_now()
    with _connection() as conn:
        for rel in unique:
            conn.execute(
                """
                INSERT INTO assets(
                    collection, rel, status, comment, first_seen_at, seen_at, updated_at, asset_id, present
                ) VALUES(?, ?, '', '', ?, NULL, ?, ?, 1)
                ON CONFLICT(collection, rel) DO UPDATE SET present=1
                """,
                (collection, rel, now, now, str(uuid.uuid4())),
            )


def catalog_state(collection: str) -> dict[str, Any]:
    with _connection() as conn:
        row = conn.execute(
            """
            SELECT generation, last_scan_at, changed_at, truncated, reason, elapsed_ms,
                   added, changed, removed, renamed
            FROM catalog_state WHERE collection=?
            """,
            (collection,),
        ).fetchone()
    if not row:
        return {
            "collection": collection, "generation": 0, "last_scan_at": None, "changed_at": None,
            "truncated": False, "reason": None, "elapsed_ms": 0,
            "added": 0, "changed": 0, "removed": 0, "renamed": 0,
        }
    return {
        "collection": collection,
        "generation": int(row["generation"]),
        "last_scan_at": row["last_scan_at"],
        "changed_at": row["changed_at"],
        "truncated": bool(row["truncated"]),
        "reason": row["reason"],
        "elapsed_ms": int(row["elapsed_ms"] or 0),
        "added": int(row["added"] or 0),
        "changed": int(row["changed"] or 0),
        "removed": int(row["removed"] or 0),
        "renamed": int(row["renamed"] or 0),
    }

def catalog_records(collection: str, present_only: bool = True) -> list[dict[str, Any]]:
    query = """
        SELECT asset_id, collection, rel, status, comment, first_seen_at, seen_at, updated_at,
               device, inode, size, mtime_ns, width, height, preview_error, present, scan_generation,
               content_changed_at, missing_at, review_sha256,
               (SELECT fm.family_id FROM family_members fm WHERE fm.asset_id=assets.asset_id LIMIT 1) AS family_id,
               (SELECT f.name FROM family_members fm JOIN families f ON f.family_id=fm.family_id
                WHERE fm.asset_id=assets.asset_id LIMIT 1) AS family_name,
               (SELECT CASE WHEN f.preferred_asset_id=assets.asset_id THEN 1 ELSE 0 END
                FROM family_members fm JOIN families f ON f.family_id=fm.family_id
                WHERE fm.asset_id=assets.asset_id LIMIT 1) AS family_preferred,
               (SELECT COUNT(*) FROM annotations an
                WHERE an.asset_id=assets.asset_id AND an.stale=0 AND an.resolved=0) AS annotation_count,
               (SELECT an.content_sha256 FROM annotations an
                WHERE an.asset_id=assets.asset_id AND an.stale=0 AND an.content_sha256 IS NOT NULL
                ORDER BY an.created_at LIMIT 1) AS annotation_sha256
        FROM assets WHERE collection=?
    """
    args: list[Any] = [collection]
    if present_only:
        query += " AND present=1"
    query += " ORDER BY COALESCE(mtime_ns, 0) DESC, rel COLLATE NOCASE"
    with _connection() as conn:
        records = [dict(row) for row in conn.execute(query, tuple(args))]
    metadata = metadata_map(collection)
    for record in records:
        record["provenance"] = metadata.get(str(record["asset_id"]), _metadata_payload(None, str(record["asset_id"]), collection))
    return records



def _family_name(value: Any) -> str:
    name = str(value or "").strip()
    if not name:
        raise ValueError("family name is required")
    if len(name) > 200:
        raise ValueError("family name must be 200 characters or fewer")
    return name


def _family_member_rows(conn: sqlite3.Connection, family_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT a.asset_id, a.collection, a.rel, a.status, a.comment, a.present, a.mtime_ns,
               a.width, a.height, fm.added_at
        FROM family_members fm JOIN assets a ON a.asset_id=fm.asset_id
        WHERE fm.family_id=?
        ORDER BY COALESCE(a.mtime_ns, 0) DESC, fm.added_at, a.rel COLLATE NOCASE
        """,
        (family_id,),
    ).fetchall()
    return [dict(row) | {"present": bool(row["present"])} for row in rows]


def _family_payload(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    members = _family_member_rows(conn, str(row["family_id"]))
    latest = members[0]["asset_id"] if members else None
    preferred = row["preferred_asset_id"]
    return {
        "family_id": str(row["family_id"]),
        "collection": str(row["collection"]),
        "name": str(row["name"]),
        "preferred_asset_id": preferred,
        "latest_asset_id": latest,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "members": [member | {"preferred": member["asset_id"] == preferred} for member in members],
    }


def families_for_collection(collection: str, *, include_missing: bool = True) -> list[dict[str, Any]]:
    with _connection() as conn:
        rows = conn.execute(
            "SELECT family_id, collection, name, preferred_asset_id, created_at, updated_at FROM families WHERE collection=? ORDER BY updated_at DESC, name COLLATE NOCASE",
            (collection,),
        ).fetchall()
        payloads = [_family_payload(conn, row) for row in rows]
    if include_missing:
        return payloads
    for family in payloads:
        family["members"] = [member for member in family["members"] if member["present"]]
        family["latest_asset_id"] = family["members"][0]["asset_id"] if family["members"] else None
    return [family for family in payloads if family["members"]]


def family_for_asset(collection: str, asset_id: str) -> dict[str, Any] | None:
    with _connection() as conn:
        row = conn.execute(
            """SELECT f.family_id, f.collection, f.name, f.preferred_asset_id, f.created_at, f.updated_at
               FROM family_members fm JOIN families f ON f.family_id=fm.family_id
               WHERE fm.asset_id=? AND f.collection=?""",
            (asset_id, collection),
        ).fetchone()
        return _family_payload(conn, row) if row else None


def _validated_family_assets(conn: sqlite3.Connection, collection: str, asset_ids: Iterable[str]) -> list[str]:
    ids = list(dict.fromkeys(str(asset_id) for asset_id in asset_ids if asset_id))
    if not ids:
        raise ValueError("at least one asset is required")
    for asset_id in ids:
        row = conn.execute(
            "SELECT collection FROM assets WHERE asset_id=?",
            (asset_id,),
        ).fetchone()
        if not row or str(row["collection"]) != collection:
            raise ValueError("all family assets must belong to the collection")
    return ids


def create_family(collection: str, name: str, asset_ids: Iterable[str], preferred_asset_id: str | None = None) -> dict[str, Any]:
    now = utc_now()
    clean_name = _family_name(name)
    with _connection() as conn:
        ids = _validated_family_assets(conn, collection, asset_ids)
        if len(ids) < 2:
            raise ValueError("a new family requires at least two assets")
        occupied = [
            asset_id for asset_id in ids
            if conn.execute("SELECT 1 FROM family_members WHERE asset_id=?", (asset_id,)).fetchone()
        ]
        if occupied:
            raise ValueError("one or more assets already belong to a family")
        if preferred_asset_id and preferred_asset_id not in ids:
            raise ValueError("preferred asset must be a family member")
        family_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO families(family_id, collection, name, preferred_asset_id, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?)",
            (family_id, collection, clean_name, preferred_asset_id, now, now),
        )
        conn.executemany(
            "INSERT INTO family_members(family_id, asset_id, added_at) VALUES(?, ?, ?)",
            [(family_id, asset_id, now) for asset_id in ids],
        )
        _record_system_event(conn, collection, "", preferred_asset_id, "family_created", now, {"family_id": family_id, "name": clean_name, "asset_ids": ids})
        row = conn.execute("SELECT * FROM families WHERE family_id=?", (family_id,)).fetchone()
        return _family_payload(conn, row)


def add_family_members(collection: str, family_id: str, asset_ids: Iterable[str]) -> dict[str, Any]:
    now = utc_now()
    with _connection() as conn:
        family = conn.execute("SELECT * FROM families WHERE family_id=? AND collection=?", (family_id, collection)).fetchone()
        if not family:
            raise ValueError("family not found")
        ids = _validated_family_assets(conn, collection, asset_ids)
        occupied = []
        for asset_id in ids:
            row = conn.execute(
                "SELECT asset_id, family_id FROM family_members WHERE asset_id=?",
                (asset_id,),
            ).fetchone()
            if row:
                occupied.append(row)
        conflicts = [row for row in occupied if row["family_id"] != family_id]
        if conflicts:
            raise ValueError("one or more assets already belong to another family")
        existing = {str(row["asset_id"]) for row in occupied}
        conn.executemany(
            "INSERT INTO family_members(family_id, asset_id, added_at) VALUES(?, ?, ?)",
            [(family_id, asset_id, now) for asset_id in ids if asset_id not in existing],
        )
        conn.execute("UPDATE families SET updated_at=? WHERE family_id=?", (now, family_id))
        added = [asset_id for asset_id in ids if asset_id not in existing]
        if added:
            _record_system_event(conn, collection, "", None, "family_members_added", now, {"family_id": family_id, "asset_ids": added})
        row = conn.execute("SELECT * FROM families WHERE family_id=?", (family_id,)).fetchone()
        return _family_payload(conn, row)


def remove_family_members(collection: str, family_id: str, asset_ids: Iterable[str]) -> dict[str, Any] | None:
    now = utc_now()
    ids = list(dict.fromkeys(str(asset_id) for asset_id in asset_ids if asset_id))
    if not ids:
        raise ValueError("at least one asset is required")
    with _connection() as conn:
        family = conn.execute("SELECT * FROM families WHERE family_id=? AND collection=?", (family_id, collection)).fetchone()
        if not family:
            raise ValueError("family not found")
        removed = []
        for asset_id in ids:
            member = conn.execute(
                "SELECT 1 FROM family_members WHERE family_id=? AND asset_id=?",
                (family_id, asset_id),
            ).fetchone()
            if member:
                removed.append(asset_id)
        if removed:
            conn.executemany(
                "DELETE FROM family_members WHERE family_id=? AND asset_id=?",
                [(family_id, asset_id) for asset_id in removed],
            )
            _record_system_event(conn, collection, "", None, "family_members_removed", now, {"family_id": family_id, "asset_ids": removed})
        remaining = conn.execute("SELECT COUNT(*) AS n FROM family_members WHERE family_id=?", (family_id,)).fetchone()["n"]
        if remaining == 0:
            conn.execute("DELETE FROM families WHERE family_id=?", (family_id,))
            _record_system_event(conn, collection, "", None, "family_deleted", now, {"family_id": family_id, "reason": "last_member_removed"})
            return None
        preferred = family["preferred_asset_id"]
        if preferred in removed:
            preferred = None
        conn.execute("UPDATE families SET preferred_asset_id=?, updated_at=? WHERE family_id=?", (preferred, now, family_id))
        row = conn.execute("SELECT * FROM families WHERE family_id=?", (family_id,)).fetchone()
        return _family_payload(conn, row)


def set_family_preferred(collection: str, family_id: str, asset_id: str | None) -> dict[str, Any]:
    now = utc_now()
    with _connection() as conn:
        family = conn.execute("SELECT * FROM families WHERE family_id=? AND collection=?", (family_id, collection)).fetchone()
        if not family:
            raise ValueError("family not found")
        if asset_id:
            member = conn.execute("SELECT 1 FROM family_members WHERE family_id=? AND asset_id=?", (family_id, asset_id)).fetchone()
            if not member:
                raise ValueError("preferred asset must be a family member")
        old = family["preferred_asset_id"]
        conn.execute("UPDATE families SET preferred_asset_id=?, updated_at=? WHERE family_id=?", (asset_id, now, family_id))
        _record_system_event(conn, collection, "", asset_id, "family_preferred_changed", now, {"family_id": family_id, "old_asset_id": old, "new_asset_id": asset_id})
        row = conn.execute("SELECT * FROM families WHERE family_id=?", (family_id,)).fetchone()
        return _family_payload(conn, row)


def rename_family(collection: str, family_id: str, name: str) -> dict[str, Any]:
    now = utc_now()
    clean_name = _family_name(name)
    with _connection() as conn:
        family = conn.execute("SELECT * FROM families WHERE family_id=? AND collection=?", (family_id, collection)).fetchone()
        if not family:
            raise ValueError("family not found")
        conn.execute("UPDATE families SET name=?, updated_at=? WHERE family_id=?", (clean_name, now, family_id))
        _record_system_event(conn, collection, "", None, "family_renamed", now, {"family_id": family_id, "old_name": family["name"], "new_name": clean_name})
        row = conn.execute("SELECT * FROM families WHERE family_id=?", (family_id,)).fetchone()
        return _family_payload(conn, row)


def delete_family(collection: str, family_id: str) -> dict[str, Any]:
    now = utc_now()
    with _connection() as conn:
        family = conn.execute("SELECT * FROM families WHERE family_id=? AND collection=?", (family_id, collection)).fetchone()
        if not family:
            raise ValueError("family not found")
        payload = _family_payload(conn, family)
        conn.execute("DELETE FROM family_members WHERE family_id=?", (family_id,))
        conn.execute("DELETE FROM families WHERE family_id=?", (family_id,))
        _record_system_event(conn, collection, "", None, "family_deleted", now, {"family_id": family_id, "name": family["name"]})
        return payload

def _record_system_event(
    conn: sqlite3.Connection, collection: str, rel: str, asset_id: str | None,
    action: str, now: str, details: dict[str, Any] | None = None,
    *, old_status: str = "", new_status: str = "", old_comment: str = "", new_comment: str = "",
) -> None:
    conn.execute(
        """INSERT INTO review_events(
            collection, rel, action, old_status, new_status, old_comment, new_comment, created_at, asset_id, details
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (collection, rel, action, old_status, new_status, old_comment, new_comment, now, asset_id,
         json.dumps(details or {}, separators=(",", ":"), sort_keys=True)),
    )


def reconcile_catalog(
    collection: str,
    discoveries: Iterable[dict[str, Any]],
    *,
    truncated: bool,
    reason: str | None,
    elapsed_ms: int,
) -> dict[str, Any]:
    """Reconcile a filesystem scan into the durable catalog.

    Review state follows stable asset IDs across same-filesystem renames. A
    content replacement at an existing path invalidates the old decision, and
    a truncated scan never marks unseen rows missing.
    """
    now = utc_now()
    rows = list(discoveries)
    discovered_rels = {str(item["rel"]) for item in rows}
    added = changed = removed = renamed = restored = 0
    with _connection() as conn:
        previous_state = conn.execute(
            "SELECT generation, changed_at FROM catalog_state WHERE collection=?", (collection,)
        ).fetchone()
        generation = int(previous_state["generation"] if previous_state else 0) + 1
        existing = conn.execute("SELECT * FROM assets WHERE collection=?", (collection,)).fetchall()
        by_rel = {row["rel"]: row for row in existing}
        by_identity: dict[tuple[int, int], list[sqlite3.Row]] = {}
        for row in existing:
            if row["device"] and row["inode"]:
                by_identity.setdefault((int(row["device"]), int(row["inode"])), []).append(row)
        used_ids: set[str] = set()

        for item in rows:
            rel = str(item["rel"])
            device = int(item.get("device") or 0)
            inode = int(item.get("inode") or 0)
            size = int(item.get("size") or 0)
            mtime_ns = int(item.get("mtime_ns") or 0)
            target = by_rel.get(rel)
            renamed_from: str | None = None
            if target is None and device and inode:
                candidates = [
                    row for row in by_identity.get((device, inode), [])
                    if row["asset_id"] not in used_ids and row["rel"] not in discovered_rels
                ]
                if len(candidates) == 1:
                    target = candidates[0]
                    renamed_from = str(target["rel"])
                    conn.execute("UPDATE assets SET rel=? WHERE asset_id=?", (rel, target["asset_id"]))
                    conn.execute(
                        "UPDATE review_events SET rel=?, asset_id=COALESCE(asset_id, ?) WHERE collection=? AND rel=?",
                        (rel, target["asset_id"], collection, renamed_from),
                    )
                    _record_system_event(conn, collection, rel, str(target["asset_id"]), "asset_renamed", now, {"old_rel": renamed_from, "new_rel": rel})
                    renamed += 1

            if target is None:
                asset_id = str(uuid.uuid4())
                conn.execute(
                    """
                    INSERT INTO assets(
                        collection, rel, status, comment, first_seen_at, seen_at, updated_at, asset_id,
                        device, inode, size, mtime_ns, width, height, preview_error, present, scan_generation,
                        content_changed_at, missing_at
                    ) VALUES(?, ?, '', '', ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, NULL)
                    """,
                    (collection, rel, now, now, asset_id, device or None, inode or None, size, mtime_ns,
                     item.get("width"), item.get("height"), item.get("preview_error"), generation, now),
                )
                _record_system_event(conn, collection, rel, asset_id, "asset_added", now)
                added += 1
                used_ids.add(asset_id)
                continue

            asset_id = str(target["asset_id"])
            used_ids.add(asset_id)
            was_present = bool(target["present"])
            metadata_known = target["size"] is not None and target["mtime_ns"] is not None
            content_changed = (
                metadata_known and (int(target["size"]) != size or int(target["mtime_ns"]) != mtime_ns)
            ) or bool(item.get("review_hash_mismatch")) or bool(item.get("annotation_hash_mismatch"))
            if not was_present:
                _record_system_event(conn, collection, rel, asset_id, "asset_restored", now)
                restored += 1
            if content_changed:
                old_status = str(target["status"] or "")
                old_comment = str(target["comment"] or "")
                conn.execute(
                    """
                    UPDATE assets SET status='', comment='', seen_at=NULL, updated_at=?, content_changed_at=?, review_sha256=NULL
                    WHERE asset_id=?
                    """,
                    (now, now, asset_id),
                )
                conn.execute(
                    "UPDATE annotations SET stale=1, updated_at=? WHERE asset_id=? AND stale=0",
                    (now, asset_id),
                )
                _record_system_event(
                    conn, collection, rel, asset_id, "content_changed", now,
                    {"old_size": target["size"], "new_size": size, "old_mtime_ns": target["mtime_ns"], "new_mtime_ns": mtime_ns,
                     "review_hash_mismatch": bool(item.get("review_hash_mismatch")),
                     "annotation_hash_mismatch": bool(item.get("annotation_hash_mismatch"))},
                    old_status=old_status, new_status="", old_comment=old_comment, new_comment="",
                )
                changed += 1
            conn.execute(
                """
                UPDATE assets SET device=?, inode=?, size=?, mtime_ns=?, width=?, height=?, preview_error=?,
                                  present=1, scan_generation=?, missing_at=NULL
                WHERE asset_id=?
                """,
                (device or None, inode or None, size, mtime_ns, item.get("width"), item.get("height"),
                 item.get("preview_error"), generation, asset_id),
            )

        if not truncated:
            missing_rows = conn.execute(
                "SELECT asset_id, rel FROM assets WHERE collection=? AND present=1 AND scan_generation<>?",
                (collection, generation),
            ).fetchall()
            removed = len(missing_rows)
            if missing_rows:
                conn.executemany(
                    "UPDATE assets SET present=0, missing_at=? WHERE asset_id=?",
                    [(now, row["asset_id"]) for row in missing_rows],
                )
                for row in missing_rows:
                    _record_system_event(conn, collection, row["rel"], row["asset_id"], "asset_missing", now)

        meaningful_change = bool(added or changed or removed or restored)
        changed_at = now if meaningful_change else (previous_state["changed_at"] if previous_state else None)
        conn.execute(
            """
            INSERT INTO catalog_state(
                collection, generation, last_scan_at, changed_at, truncated, reason, elapsed_ms,
                added, changed, removed, renamed
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(collection) DO UPDATE SET
                generation=excluded.generation, last_scan_at=excluded.last_scan_at, changed_at=excluded.changed_at,
                truncated=excluded.truncated, reason=excluded.reason, elapsed_ms=excluded.elapsed_ms,
                added=excluded.added, changed=excluded.changed, removed=excluded.removed, renamed=excluded.renamed
            """,
            (collection, generation, now, changed_at, int(truncated), reason, int(elapsed_ms),
             added + restored, changed, removed, renamed),
        )
    return catalog_state(collection)

def _annotation_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "annotation_id": str(row["annotation_id"]),
        "asset_id": str(row["asset_id"]),
        "collection": str(row["collection"]),
        "kind": str(row["kind"]),
        "x": float(row["x"]),
        "y": float(row["y"]),
        "w": float(row["w"]),
        "h": float(row["h"]),
        "text": str(row["text"] or ""),
        "resolved": bool(row["resolved"]),
        "stale": bool(row["stale"]),
        "content_sha256": row["content_sha256"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def annotations_for_asset(collection: str, asset_id: str, *, include_stale: bool = True) -> list[dict[str, Any]]:
    with _connection() as conn:
        if include_stale:
            rows = conn.execute(
                """
                SELECT annotation_id, asset_id, collection, kind, x, y, w, h, text, resolved, stale, content_sha256, created_at, updated_at
                FROM annotations WHERE collection=? AND asset_id=? ORDER BY created_at, annotation_id
                """,
                (collection, asset_id),
            )
        else:
            rows = conn.execute(
                """
                SELECT annotation_id, asset_id, collection, kind, x, y, w, h, text, resolved, stale, content_sha256, created_at, updated_at
                FROM annotations WHERE collection=? AND asset_id=? AND stale=0 ORDER BY created_at, annotation_id
                """,
                (collection, asset_id),
            )
        return [_annotation_row(row) for row in rows]


def annotations_for_collection(collection: str, *, include_stale: bool = True) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    with _connection() as conn:
        if include_stale:
            rows = conn.execute(
                """
                SELECT annotation_id, asset_id, collection, kind, x, y, w, h, text, resolved, stale, content_sha256, created_at, updated_at
                FROM annotations WHERE collection=? ORDER BY created_at, annotation_id
                """,
                (collection,),
            )
        else:
            rows = conn.execute(
                """
                SELECT annotation_id, asset_id, collection, kind, x, y, w, h, text, resolved, stale, content_sha256, created_at, updated_at
                FROM annotations WHERE collection=? AND stale=0 ORDER BY created_at, annotation_id
                """,
                (collection,),
            )
        for row in rows:
            item = _annotation_row(row)
            out.setdefault(item["asset_id"], []).append(item)
    return out


def _normalized_coordinate(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not 0 <= number <= 1:
        raise ValueError(f"{name} must be between 0 and 1")
    return round(number, 6)


def create_annotation(
    collection: str, asset_id: str, kind: str, x: Any, y: Any,
    *, w: Any = 0, h: Any = 0, text: str = "",
) -> dict[str, Any]:
    if kind not in {"point", "region"}:
        raise ValueError("annotation kind must be point or region")
    x_n = _normalized_coordinate(x, "x")
    y_n = _normalized_coordinate(y, "y")
    w_n = _normalized_coordinate(w, "w") if kind == "region" else 0.0
    h_n = _normalized_coordinate(h, "h") if kind == "region" else 0.0
    if kind == "region" and (w_n <= 0 or h_n <= 0):
        raise ValueError("region annotations require positive width and height")
    if x_n + w_n > 1.000001 or y_n + h_n > 1.000001:
        raise ValueError("annotation geometry extends outside the image")
    text = str(text)[:4000]
    now = utc_now()
    annotation_id = str(uuid.uuid4())
    with _connection() as conn:
        asset = conn.execute(
            "SELECT rel, present FROM assets WHERE collection=? AND asset_id=?",
            (collection, asset_id),
        ).fetchone()
    if not asset or not asset["present"]:
        raise ValueError("asset not found")
    source = safe_file(collection, str(asset["rel"]))
    if not source:
        raise ValueError("asset unavailable")
    content_sha256 = file_sha256(source)
    with _connection() as conn:
        conn.execute(
            """
            INSERT INTO annotations(annotation_id, asset_id, collection, kind, x, y, w, h, text, resolved, stale, content_sha256, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?)
            """,
            (annotation_id, asset_id, collection, kind, x_n, y_n, w_n, h_n, text, content_sha256, now, now),
        )
        details = {
            "annotation_id": annotation_id, "kind": kind, "x": x_n, "y": y_n,
            "w": w_n, "h": h_n, "text": text, "resolved": False,
        }
        _record_system_event(conn, collection, asset["rel"], asset_id, "annotation_created", now, details)
        row = conn.execute(
            "SELECT annotation_id, asset_id, collection, kind, x, y, w, h, text, resolved, stale, content_sha256, created_at, updated_at FROM annotations WHERE annotation_id=?",
            (annotation_id,),
        ).fetchone()
    return _annotation_row(row)


def update_annotation(
    collection: str, annotation_id: str, *, text: str | None = None, resolved: bool | None = None,
) -> dict[str, Any]:
    now = utc_now()
    with _connection() as conn:
        current = conn.execute(
            """SELECT an.*, a.rel FROM annotations an
            JOIN assets a ON a.asset_id=an.asset_id
            WHERE an.collection=? AND an.annotation_id=?""",
            (collection, annotation_id),
        ).fetchone()
        if not current:
            raise ValueError("annotation not found")
        new_text = str(current["text"] or "") if text is None else str(text)[:4000]
        new_resolved = bool(current["resolved"]) if resolved is None else bool(resolved)
        conn.execute(
            "UPDATE annotations SET text=?, resolved=?, updated_at=? WHERE annotation_id=?",
            (new_text, int(new_resolved), now, annotation_id),
        )
        details = {
            "annotation_id": annotation_id, "text": new_text, "resolved": new_resolved,
            "previous_resolved": bool(current["resolved"]),
        }
        _record_system_event(
            conn, collection, current["rel"], current["asset_id"], "annotation_updated", now, details
        )
        row = conn.execute(
            "SELECT annotation_id, asset_id, collection, kind, x, y, w, h, text, resolved, stale, content_sha256, created_at, updated_at FROM annotations WHERE annotation_id=?",
            (annotation_id,),
        ).fetchone()
    return _annotation_row(row)


def delete_annotation(collection: str, annotation_id: str) -> dict[str, Any]:
    now = utc_now()
    with _connection() as conn:
        current = conn.execute(
            """SELECT an.*, a.rel FROM annotations an
            JOIN assets a ON a.asset_id=an.asset_id
            WHERE an.collection=? AND an.annotation_id=?""",
            (collection, annotation_id),
        ).fetchone()
        if not current:
            raise ValueError("annotation not found")
        item = _annotation_row(current)
        conn.execute("DELETE FROM annotations WHERE annotation_id=?", (annotation_id,))
        _record_system_event(
            conn, collection, current["rel"], current["asset_id"], "annotation_deleted", now,
            {"annotation_id": annotation_id, "kind": current["kind"], "text": current["text"]},
        )
    return item


def review_records(collection: str | None = None) -> dict[str, dict[str, dict[str, Any]]]:
    query = "SELECT asset_id, collection, rel, status, comment, first_seen_at, seen_at, updated_at, present, size, mtime_ns, width, height, content_changed_at, missing_at, review_sha256 FROM assets"
    args: tuple[Any, ...] = ()
    if collection is not None:
        query += " WHERE collection=?"
        args = (collection,)
    query += " ORDER BY collection, rel"
    out: dict[str, dict[str, dict[str, Any]]] = {}
    with _connection() as conn:
        for row in conn.execute(query, args):
            out.setdefault(row["collection"], {})[row["rel"]] = {
                "asset_id": row["asset_id"],
                "status": row["status"],
                "comment": row["comment"],
                "first_seen_at": row["first_seen_at"],
                "seen_at": row["seen_at"],
                "updated_at": row["updated_at"],
                "present": bool(row["present"]),
                "size": row["size"],
                "mtime_ns": row["mtime_ns"],
                "width": row["width"],
                "height": row["height"],
                "review_sha256": row["review_sha256"],
                "content_changed_at": row["content_changed_at"],
                "missing_at": row["missing_at"],
            }
    return out


def reviews() -> dict[str, dict[str, str]]:
    """Backward-compatible status-only view of review state."""
    records = review_records()
    return {
        collection: {rel: data["status"] for rel, data in rows.items() if data["status"] and data.get("present", True)}
        for collection, rows in records.items()
    }


def _record_review_event(
    conn: sqlite3.Connection,
    collection: str,
    relative: str,
    action: str,
    old_status: str,
    new_status: str,
    old_comment: str,
    new_comment: str,
    now: str,
    force: bool = False,
) -> None:
    if not force and old_status == new_status and old_comment == new_comment:
        return
    asset = conn.execute(
        "SELECT asset_id FROM assets WHERE collection=? AND rel=?", (collection, relative)
    ).fetchone()
    asset_id = asset["asset_id"] if asset else None
    conn.execute(
        """
        INSERT INTO review_events(
            collection, rel, action, old_status, new_status, old_comment, new_comment, created_at, asset_id
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (collection, relative, action, old_status, new_status, old_comment, new_comment, now, asset_id),
    )


def review_history(collection: str, relative: str, limit: int = 50) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 500))
    with _connection() as conn:
        asset = conn.execute(
            "SELECT asset_id FROM assets WHERE collection=? AND rel=?", (collection, relative)
        ).fetchone()
        if asset:
            rows = conn.execute(
                """
                SELECT id, action, old_status, new_status, old_comment, new_comment, created_at, undone_at, asset_id, details
                FROM review_events
                WHERE (asset_id=? OR (asset_id IS NULL AND collection=? AND rel=?))
                  AND action IN ('review', 'batch', 'comment', 'content_changed')
                ORDER BY id DESC LIMIT ?
                """,
                (asset["asset_id"], collection, relative, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, action, old_status, new_status, old_comment, new_comment, created_at, undone_at, asset_id, details
                FROM review_events WHERE collection=? AND rel=?
                  AND action IN ('review', 'batch', 'comment', 'content_changed')
                ORDER BY id DESC LIMIT ?
                """,
                (collection, relative, limit),
            ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        try:
            item["details"] = json.loads(item.get("details") or "{}")
        except (TypeError, json.JSONDecodeError):
            item["details"] = {}
        out.append(item)
    return out


def undo_last_review(collection: str, relative: str) -> dict[str, Any] | None:
    now = utc_now()
    with _connection() as conn:
        asset = conn.execute(
            "SELECT asset_id FROM assets WHERE collection=? AND rel=?", (collection, relative)
        ).fetchone()
        if not asset:
            return None
        event = conn.execute(
            """
            SELECT id, old_status, old_comment, new_status, new_comment
            FROM review_events
            WHERE (asset_id=? OR (asset_id IS NULL AND collection=? AND rel=?))
              AND action IN ('review', 'batch', 'comment') AND undone_at IS NULL
              AND id > COALESCE((SELECT MAX(id) FROM review_events WHERE asset_id=? AND action='content_changed'), 0)
            ORDER BY id DESC LIMIT 1
            """,
            (asset["asset_id"], collection, relative, asset["asset_id"]),
        ).fetchone()
    if not event:
        return None
    source = safe_file(collection, relative)
    review_sha256 = file_sha256(source) if event["old_status"] and source else None
    with _connection() as conn:
        conn.execute(
            "UPDATE assets SET status=?, comment=?, review_sha256=?, updated_at=? WHERE asset_id=?",
            (event["old_status"], event["old_comment"], review_sha256, now, asset["asset_id"]),
        )
        conn.execute("UPDATE review_events SET undone_at=? WHERE id=?", (now, event["id"]))
    return {
        "event_id": event["id"],
        "asset_id": asset["asset_id"],
        "status": event["old_status"],
        "comment": event["old_comment"],
        "undone_at": now,
    }


def set_review(collection: str, relative: str, status: str, comment: str | None = None, mark_seen: bool = True) -> None:
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid review status: {status}")
    source = safe_file(collection, relative)
    review_sha256 = file_sha256(source) if status and source else None
    ensure_assets(collection, [relative])
    now = utc_now()
    with _connection() as conn:
        current = conn.execute(
            "SELECT status, comment FROM assets WHERE collection=? AND rel=?", (collection, relative)
        ).fetchone()
        if not current:
            raise ValueError("asset review state not found")
        old_status, old_comment = current["status"], current["comment"]
        new_comment = old_comment if comment is None else str(comment)[:10000]
        conn.execute(
            """
            UPDATE assets
            SET status=?, comment=?, review_sha256=?, seen_at=CASE WHEN ? THEN COALESCE(seen_at, ?) ELSE seen_at END, updated_at=?
            WHERE collection=? AND rel=?
            """,
            (status, new_comment, review_sha256, int(mark_seen), now, now, collection, relative),
        )
        _record_review_event(conn, collection, relative, "review", old_status, status, old_comment, new_comment, now)


def set_reviews_batch(collection: str, relatives: Iterable[str], status: str) -> int:
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid review status: {status}")
    unique = sorted({str(rel) for rel in relatives if rel})
    if not unique:
        return 0
    review_hashes = {}
    if status:
        for rel in unique:
            source = safe_file(collection, rel)
            review_hashes[rel] = file_sha256(source) if source else None
    ensure_assets(collection, unique)
    now = utc_now()
    changed = 0
    with _connection() as conn:
        for rel in unique:
            current = conn.execute(
                "SELECT status, comment FROM assets WHERE collection=? AND rel=?", (collection, rel)
            ).fetchone()
            if not current:
                continue
            conn.execute(
                "UPDATE assets SET status=?, review_sha256=?, seen_at=COALESCE(seen_at, ?), updated_at=? WHERE collection=? AND rel=?",
                (status, review_hashes.get(rel) if status else None, now, now, collection, rel),
            )
            _record_review_event(
                conn, collection, rel, "batch", current["status"], status, current["comment"], current["comment"], now
            )
            changed += 1
    return changed


def set_comment(collection: str, relative: str, comment: str) -> None:
    ensure_assets(collection, [relative])
    now = utc_now()
    new_comment = str(comment)[:10000]
    with _connection() as conn:
        current = conn.execute(
            "SELECT status, comment FROM assets WHERE collection=? AND rel=?", (collection, relative)
        ).fetchone()
        if not current:
            raise ValueError("asset review state not found")
        conn.execute(
            "UPDATE assets SET comment=?, seen_at=COALESCE(seen_at, ?), updated_at=? WHERE collection=? AND rel=?",
            (new_comment, now, now, collection, relative),
        )
        _record_review_event(
            conn, collection, relative, "comment", current["status"], current["status"], current["comment"], new_comment, now
        )


def mark_seen(collection: str, relatives: Iterable[str]) -> int:
    unique = sorted({str(rel) for rel in relatives if rel})
    if not unique:
        return 0
    ensure_assets(collection, unique)
    now = utc_now()
    with _connection() as conn:
        conn.executemany(
            "UPDATE assets SET seen_at=COALESCE(seen_at, ?), updated_at=CASE WHEN seen_at IS NULL THEN ? ELSE updated_at END WHERE collection=? AND rel=?",
            [(now, now, collection, rel) for rel in unique],
        )
    return len(unique)


def review_events_since(collection: str | None = None, after_id: int = 0, limit: int = 100) -> dict[str, Any]:
    limit = max(1, min(int(limit), 500))
    after_id = max(0, int(after_id))
    rows: list[dict[str, Any]] = []
    with _connection() as conn:
        if collection:
            cursor = conn.execute(
                """
                SELECT id, collection, rel, asset_id, action, old_status, new_status,
                       old_comment, new_comment, created_at, undone_at, details
                FROM review_events
                WHERE id>? AND collection=?
                ORDER BY id ASC LIMIT ?
                """,
                (after_id, collection, limit),
            )
        else:
            cursor = conn.execute(
                """
                SELECT id, collection, rel, asset_id, action, old_status, new_status,
                       old_comment, new_comment, created_at, undone_at, details
                FROM review_events
                WHERE id>?
                ORDER BY id ASC LIMIT ?
                """,
                (after_id, limit),
            )
        for row in cursor:
            item = dict(row)
            try:
                item["details"] = json.loads(item.get("details") or "{}")
            except (TypeError, json.JSONDecodeError):
                item["details"] = {}
            rows.append(item)
        newest = conn.execute("SELECT COALESCE(MAX(id), 0) AS value FROM review_events").fetchone()["value"]
    return {
        "version": 1,
        "generated_at": utc_now(),
        "after_id": after_id,
        "last_event_id": int(rows[-1]["id"] if rows else after_id),
        "newest_event_id": int(newest or 0),
        "events": rows,
    }

def recent_review_events(collection: str | None = None, limit: int = 200) -> dict[str, Any]:
    """Return the newest durable events as an ascending chronological slice."""
    limit = max(1, min(int(limit), 500))
    with _connection() as conn:
        if collection:
            cursor = conn.execute(
                """
                SELECT id, collection, rel, asset_id, action, old_status, new_status,
                       old_comment, new_comment, created_at, undone_at, details
                FROM review_events WHERE collection=?
                ORDER BY id DESC LIMIT ?
                """,
                (collection, limit),
            )
        else:
            cursor = conn.execute(
                """
                SELECT id, collection, rel, asset_id, action, old_status, new_status,
                       old_comment, new_comment, created_at, undone_at, details
                FROM review_events ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            )
        rows = []
        for row in cursor:
            item = dict(row)
            try:
                item["details"] = json.loads(item.get("details") or "{}")
            except (TypeError, json.JSONDecodeError):
                item["details"] = {}
            rows.append(item)
        newest = conn.execute("SELECT COALESCE(MAX(id), 0) AS value FROM review_events").fetchone()["value"]
    rows.reverse()
    return {
        "version": 1,
        "generated_at": utc_now(),
        "after_id": 0,
        "last_event_id": int(rows[-1]["id"] if rows else 0),
        "newest_event_id": int(newest or 0),
        "events": rows,
    }


def _record_collection_event(conn: sqlite3.Connection, collection: str, action: str, now: str) -> None:
    conn.execute(
        """
        INSERT INTO review_events(
            collection, rel, action, old_status, new_status, old_comment, new_comment, created_at, asset_id
        ) VALUES(?, '', ?, '', '', '', '', ?, NULL)
        """,
        (collection, action, now),
    )


def verify_review_fingerprints(collection: str, *, bind_missing: bool = False) -> int:
    """Invalidate decisions whose current bytes no longer match the reviewed bytes."""
    now = utc_now()
    with _connection() as conn:
        rows = conn.execute(
            "SELECT asset_id, rel, status, comment, review_sha256 FROM assets WHERE collection=? AND present=1 AND status<>''",
            (collection,),
        ).fetchall()
    mismatches: list[tuple[sqlite3.Row, str]] = []
    bindings: list[tuple[str, str]] = []
    for row in rows:
        source = safe_file(collection, row["rel"])
        if not source:
            raise ValueError(f"reviewed asset unavailable; rescan required: {row['rel']}")
        current = file_sha256(source)
        expected = row["review_sha256"]
        if not expected:
            if bind_missing:
                bindings.append((current, row["asset_id"]))
            continue
        if not hmac.compare_digest(str(expected), current):
            mismatches.append((row, current))
    if not bindings and not mismatches:
        return 0
    with _connection() as conn:
        for digest, asset_id in bindings:
            conn.execute("UPDATE assets SET review_sha256=? WHERE asset_id=?", (digest, asset_id))
        for row, current in mismatches:
            conn.execute(
                "UPDATE assets SET status='', comment='', review_sha256=NULL, seen_at=NULL, updated_at=?, content_changed_at=? WHERE asset_id=?",
                (now, now, row["asset_id"]),
            )
            _record_system_event(
                conn, collection, row["rel"], row["asset_id"], "content_changed", now,
                {"reason": "review_sha256_mismatch", "observed_sha256": current},
                old_status=row["status"], new_status="", old_comment=row["comment"], new_comment="",
            )
    return len(mismatches)


def collection_review_state(collection: str) -> dict[str, Any]:
    with _connection() as conn:
        state = conn.execute(
            "SELECT completed_at, updated_at FROM collection_state WHERE collection=?", (collection,)
        ).fetchone()
        counts = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN status='' THEN 1 ELSE 0 END) AS unreviewed,
                   SUM(CASE WHEN seen_at IS NULL THEN 1 ELSE 0 END) AS new_count,
                   MAX(COALESCE(content_changed_at, first_seen_at)) AS newest_content
            FROM assets WHERE collection=? AND present=1
            """,
            (collection,),
        ).fetchone()
        scan = conn.execute(
            "SELECT last_scan_at, changed_at, truncated, reason FROM catalog_state WHERE collection=?",
            (collection,),
        ).fetchone()
    completed_at = state["completed_at"] if state else None
    total = int(counts["total"] or 0)
    unreviewed = int(counts["unreviewed"] or 0)
    new_count = int(counts["new_count"] or 0)
    newest_content = counts["newest_content"]
    changed_at = scan["changed_at"] if scan else None
    scan_incomplete = bool(scan and scan["truncated"])
    stale = bool(completed_at and (
        (newest_content and newest_content > completed_at) or (changed_at and changed_at > completed_at)
    ))
    complete = bool(completed_at and total > 0 and unreviewed == 0 and not stale and not scan_incomplete)
    return {
        "collection": collection,
        "completed_at": completed_at,
        "updated_at": state["updated_at"] if state else None,
        "total": total,
        "unreviewed": unreviewed,
        "new": new_count,
        "empty": total == 0,
        "stale": stale,
        "scan_incomplete": scan_incomplete,
        "scan_reason": scan["reason"] if scan else None,
        "last_scan_at": scan["last_scan_at"] if scan else None,
        "complete": complete,
        "pending": not complete,
    }

def complete_collection_review(collection: str) -> dict[str, Any]:
    state = collection_review_state(collection)
    if state["scan_incomplete"]:
        raise ValueError(f"latest collection scan is incomplete ({state['scan_reason'] or 'unknown reason'})")
    if state["total"] == 0:
        raise ValueError("collection has no present assets to review")
    if state["unreviewed"]:
        raise ValueError(f"collection has {state['unreviewed']} unreviewed asset(s)")
    verify_review_fingerprints(collection, bind_missing=True)
    state = collection_review_state(collection)
    if state["unreviewed"]:
        raise ValueError(f"collection changed after review; {state['unreviewed']} asset(s) require re-review")
    now = utc_now()
    with _connection() as conn:
        conn.execute(
            """
            INSERT INTO collection_state(collection, completed_at, updated_at) VALUES(?, ?, ?)
            ON CONFLICT(collection) DO UPDATE SET completed_at=excluded.completed_at, updated_at=excluded.updated_at
            """,
            (collection, now, now),
        )
        _record_collection_event(conn, collection, "collection_complete", now)
    return collection_review_state(collection)


def reopen_collection_review(collection: str) -> dict[str, Any]:
    now = utc_now()
    with _connection() as conn:
        conn.execute(
            """
            INSERT INTO collection_state(collection, completed_at, updated_at) VALUES(?, NULL, ?)
            ON CONFLICT(collection) DO UPDATE SET completed_at=NULL, updated_at=excluded.updated_at
            """,
            (collection, now),
        )
        _record_collection_event(conn, collection, "collection_reopen", now)
    return collection_review_state(collection)


def pending_summary(collection: str | None = None) -> dict[str, Any]:
    rows = collections()
    if collection:
        rows = [row for row in rows if row["slug"] == collection]
        if not rows:
            raise ValueError(f"collection not found: {collection}")
    states = [collection_review_state(row["slug"]) | {"label": row["label"]} for row in rows]
    return {
        "version": 1,
        "generated_at": utc_now(),
        "pending": any(state["pending"] for state in states),
        "collections": states,
    }


def review_manifest(collection: str | None = None, status: str | None = None, include_missing: bool = True) -> dict[str, Any]:
    if status is not None and status not in VALID_STATUSES | {"unreviewed", "new"}:
        raise ValueError(f"invalid manifest status: {status}")
    labels = {row["slug"]: row["label"] for row in collections()}
    query = """SELECT asset_id, collection, rel, status, comment, first_seen_at, seen_at, updated_at, present, size, mtime_ns, width, height, content_changed_at, missing_at, review_sha256,
        (SELECT fm.family_id FROM family_members fm WHERE fm.asset_id=assets.asset_id LIMIT 1) AS family_id,
        (SELECT f.name FROM family_members fm JOIN families f ON f.family_id=fm.family_id WHERE fm.asset_id=assets.asset_id LIMIT 1) AS family_name,
        (SELECT CASE WHEN f.preferred_asset_id=assets.asset_id THEN 1 ELSE 0 END FROM family_members fm JOIN families f ON f.family_id=fm.family_id WHERE fm.asset_id=assets.asset_id LIMIT 1) AS family_preferred
        FROM assets"""
    clauses: list[str] = []
    args: list[Any] = []
    if not include_missing:
        clauses.append("present=1")
    if collection:
        clauses.append("collection=?")
        args.append(collection)
    if status == "unreviewed":
        clauses.append("status=''")
    elif status == "new":
        clauses.append("seen_at IS NULL")
        clauses.append("present=1")
    elif status is not None:
        clauses.append("status=?")
        args.append(status)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY collection, rel"
    items = []
    annotations_by_asset: dict[str, list[dict[str, Any]]] = {}
    metadata_by_asset = metadata_map(collection)
    with _connection() as conn:
        annotation_query = "SELECT annotation_id, asset_id, collection, kind, x, y, w, h, text, resolved, stale, content_sha256, created_at, updated_at FROM annotations"
        annotation_args: tuple[Any, ...] = ()
        if collection:
            annotation_query += " WHERE collection=?"
            annotation_args = (collection,)
        annotation_query += " ORDER BY created_at, annotation_id"
        for annotation_row in conn.execute(annotation_query, annotation_args):
            annotation = _annotation_row(annotation_row)
            annotations_by_asset.setdefault(annotation["asset_id"], []).append(annotation)
        for row in conn.execute(query, tuple(args)):
            items.append({
                "asset_id": row["asset_id"],
                "collection": row["collection"],
                "collection_label": labels.get(row["collection"], row["collection"]),
                "rel": row["rel"],
                "status": row["status"],
                "comment": row["comment"],
                "first_seen_at": row["first_seen_at"],
                "seen_at": row["seen_at"],
                "updated_at": row["updated_at"],
                "present": bool(row["present"]),
                "size": row["size"],
                "mtime_ns": row["mtime_ns"],
                "width": row["width"],
                "height": row["height"],
                "review_sha256": row["review_sha256"],
                "provenance": metadata_by_asset.get(str(row["asset_id"]), _metadata_payload(None, str(row["asset_id"]), str(row["collection"]))),
                "family": ({"family_id": row["family_id"], "name": row["family_name"], "preferred": bool(row["family_preferred"])} if row["family_id"] else None),
                "annotations": annotations_by_asset.get(str(row["asset_id"]), []),
                "content_changed_at": row["content_changed_at"],
                "missing_at": row["missing_at"],
            })
    counts = {"total": len(items), "present": 0, "missing": 0, "approved": 0, "maybe": 0, "rejected": 0, "unreviewed": 0, "new": 0}
    for item in items:
        counts["present" if item["present"] else "missing"] += 1
        counts[item["status"] or "unreviewed"] += 1
        if item["present"] and item["seen_at"] is None:
            counts["new"] += 1
    family_rows = []
    family_collections = [collection] if collection else sorted(labels)
    for family_collection in family_collections:
        family_rows.extend(families_for_collection(family_collection, include_missing=include_missing))
    return {"version": 1, "generated_at": utc_now(), "counts": counts, "families": family_rows, "items": items}


def _render_cache_path(source: Path, purpose: str) -> Path:
    stat = source.stat()
    key = hashlib.sha256(f"{purpose}:{source}:{stat.st_mtime_ns}:{stat.st_size}".encode()).hexdigest()
    return cache_dir() / f"{purpose}-{key}.jpg"


def thumb_path(source: Path) -> Path:
    return _render_cache_path(source, "thumb")


def preview_path(source: Path) -> Path:
    return _render_cache_path(source, "preview")

def preview_cache_status() -> dict[str, Any]:
    root = cache_dir()
    entries = []
    for path in root.glob("*.jpg"):
        try:
            stat = path.stat()
            entries.append((path, int(stat.st_size), float(stat.st_mtime)))
        except OSError:
            continue
    return {
        "path": str(root),
        "files": len(entries),
        "bytes": sum(size for _, size, _ in entries),
        "oldest_mtime": min((mtime for _, _, mtime in entries), default=None),
        "newest_mtime": max((mtime for _, _, mtime in entries), default=None),
    }


def prune_preview_cache(max_bytes: int, max_age_seconds: int) -> dict[str, Any]:
    """Prune cached previews deterministically by age, then oldest-first size pressure."""
    root = cache_dir()
    now = datetime.now(timezone.utc).timestamp()
    entries: list[tuple[Path, int, float]] = []
    for path in root.glob("*.jpg"):
        try:
            stat = path.stat()
            entries.append((path, int(stat.st_size), float(stat.st_mtime)))
        except OSError:
            continue
    removed_files = 0
    removed_bytes = 0
    kept: list[tuple[Path, int, float]] = []
    for path, size, mtime in entries:
        expired = max_age_seconds > 0 and now - mtime > max_age_seconds
        if expired:
            try:
                path.unlink()
                removed_files += 1
                removed_bytes += size
            except OSError:
                kept.append((path, size, mtime))
        else:
            kept.append((path, size, mtime))
    total = sum(size for _, size, _ in kept)
    if max_bytes > 0 and total > max_bytes:
        for path, size, mtime in sorted(kept, key=lambda item: (item[2], item[0].name)):
            if total <= max_bytes:
                break
            try:
                path.unlink()
                removed_files += 1
                removed_bytes += size
                total -= size
            except OSError:
                continue
    status = preview_cache_status()
    return status | {"removed_files": removed_files, "removed_bytes": removed_bytes}
